"""VK API — поиск групп коммерческих компаний Крыма по B2B-сегментам.

ENV: VK_TOKEN (user access_token со scope groups). Без токена — пропуск.

Логика:
1. groups.search по матрице city_id × категория — собираем уникальные group_id.
2. groups.getById пачками по 500 с fields=contacts,site,description,...
3. email: из contacts (приоритет фирменному — домен совпадает с site), затем из описания.
4. phone: из contacts. website: site (очищенный). social: vk.com/<screen_name>.
5. save_item — persistent dedup сам отсеет повторы между прогонами.

Широкая выдача сохраняется в raw result с confidence/quality flags. При сборке
master отдельные VK-кандидаты без сегментного сигнала уходят в
master_quarantine.csv; если такой VK-контакт надёжно сопоставился с
OSM/Яндекс/Crawler-объектом, он может обогатить сильную сущность.
"""
import json
import os
import re
import time
import urllib.parse
from datetime import datetime
from urllib.parse import urlparse

from utils.http_retry import http_request

from utils.storage import save_item, normalize_phone
from config.hosts import is_non_company_url
from config.segments import DEFAULT_SEGMENT, normalize_segment, vk_queries
from utils.geo_city import normalize_city_name

API = "https://api.vk.com/method"
V = "5.131"
RPS_PAUSE = 0.34  # VK лимит ~3 запроса/сек

# Проверенные city_id (database.getCities, country_id=1)
VK_CITIES = {
    818:     "Ялта",
    627:     "Симферополь",
    185:     "Севастополь",
    799:     "Евпатория",
    1483:    "Феодосия",
    478:     "Керчь",
    2510:    "Алушта",
    7188:    "Судак",
    4331:    "Саки",
    475:     "Бахчисарай",
    5490687: "Коктебель",
    5490709: "Гурзуф",
    5490654: "Партенит",
    5490729: "Симеиз",
    5490701: "Алупка",
}

QUERY_SEGMENT: dict[str, str] = dict(vk_queries())
QUERIES: list[str] = list(QUERY_SEGMENT)

# Сообщества-«шум»: барахолки, новости, паблики — не компании.
NOISE_PRIMARY_RE = re.compile(
    r"\b(?:подслушано|барахолк\w*|объявлени\w*|вакансии|работа в|новости|"
    r"сплетни|знакомств\w*|выпускник\w*|волонт\w*|благотворительн\w*|"
    r"приют\w*|клуб любителей|фан-?клуб\w*|типичн\w*|мемы|отдам даром|помогите)\b",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
EMAIL_BLOCKLIST = ("noreply", "no-reply", "example.", "@vk.com", "@vkontakte")

PREFERRED_PREFIXES = ("reservation", "reservations", "booking", "reserve", "book",
                      "reception", "info", "sales", "office", "manager", "order",
                      "zakaz")

# VK API error codes, означающие что VK_TOKEN недействителен.
_TOKEN_DEAD_CODES = (5, 15, 27, 28)
_token_alert_sent = False  # один алерт за прогон

def _maybe_alert_token_dead(error: dict) -> None:
    """Шлёт TG-алерт один раз за прогон, если ошибка VK означает invalid token."""
    global _token_alert_sent
    if os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    if _token_alert_sent:
        return
    code = error.get("error_code")
    if code not in _TOKEN_DEAD_CODES:
        return
    _token_alert_sent = True
    msg = error.get("error_msg", "?")
    try:
        from utils.telegram_notify import send_message
        tok = os.environ.get("TG_BOT_TOKEN", "")
        chat = os.environ.get("TG_CHAT_ID", "")
        if tok and chat:
            send_message(tok, chat, (
                f"❌ <b>VK_TOKEN протух</b> (code={code}): <code>{msg[:120]}</code>\n"
                "Получи новый по инструкции из README, обнови VK_TOKEN в .env"
            ))
    except Exception as e:
        print(f"  [VK] не смог отправить алерт о токене: {e}")


def _call(method: str, token: str, **params) -> dict:
    params["access_token"] = token
    params["v"] = V
    url = f"{API}/{method}?{urllib.parse.urlencode(params)}"
    try:
        body = http_request(url, timeout=25)
        resp = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:
        return {"error": {"error_msg": str(e)}}
    if isinstance(resp, dict) and "error" in resp:
        _maybe_alert_token_dead(resp["error"])
        code = resp["error"].get("error_code")
        if code in _TOKEN_DEAD_CODES:
            raise RuntimeError(f"VK_TOKEN недействителен (code={code})")
    return resp


def _group_relevance(group: dict, matched_queries: list[str]) -> tuple[float, list[str]]:
    """Score broad VK candidates; master later quarantines weak standalone rows."""
    name = str(group.get("name") or "")
    activity = str(group.get("activity") or "")
    description = str(group.get("description") or "")
    has_primary = (
        normalize_segment(name) != DEFAULT_SEGMENT
        or normalize_segment(activity) != DEFAULT_SEGMENT
    )
    corpus = " ".join((name, activity, description, str(group.get("status") or ""))).casefold()
    query_hits = [q for q in matched_queries if q.casefold() in corpus]

    score = 0.35
    if has_primary:
        score += 0.35
    if normalize_segment(description) != DEFAULT_SEGMENT:
        score += 0.10
    score += min(0.15, 0.05 * len(query_hits))

    flags: list[str] = []
    if not has_primary:
        flags.append("vk_no_primary_segment_signal")
        score = min(score, 0.60)
    if NOISE_PRIMARY_RE.search(f"{name} {activity}"):
        flags.append("vk_noise_primary")
        score = min(score, 0.25)
    site = _clean_site(str(group.get("site") or ""))
    if not site or is_non_company_url(site):
        flags.append("vk_no_own_website")
    score = max(0.0, min(1.0, score))
    if score < 0.5:
        flags.append("manual_review")
    return score, flags


def _pick_address(group: dict) -> str:
    addresses = group.get("addresses") or []
    if isinstance(addresses, dict):
        addresses = addresses.get("items") or addresses.get("addresses") or []
    for address in addresses:
        if not isinstance(address, dict):
            continue
        parts = []
        city = address.get("city")
        if isinstance(city, dict):
            city = city.get("title") or city.get("name")
        if city:
            parts.append(str(city))
        line = address.get("address") or address.get("title") or ""
        if line:
            parts.append(str(line))
        if parts:
            return ", ".join(dict.fromkeys(parts))
    return ""


def _category_for(group: dict, matched_queries: list[str]) -> str:
    for text in (group.get("activity"), group.get("name")):
        segment = normalize_segment(text)
        if segment != DEFAULT_SEGMENT:
            return segment
    for query in matched_queries:
        segment = QUERY_SEGMENT.get(query, DEFAULT_SEGMENT)
        if segment != DEFAULT_SEGMENT:
            return segment
    return DEFAULT_SEGMENT


def _site_domain(site: str) -> str:
    try:
        h = urlparse(site if "://" in site else "http://" + site).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _clean_site(site: str) -> str:
    if not site:
        return ""
    try:
        site = site.strip().split()[0]
        if not site.startswith("http"):
            site = "https://" + site
        sp = urlparse(site)
        if not sp.netloc:
            return ""
        return f"{sp.scheme}://{sp.netloc}{sp.path}".rstrip("/")
    except Exception:
        # мусор в поле site (IPv6-подобное, скобки и т.п.) — не сайт
        return ""


def _pick_email(contacts: list, description: str, site_domain: str) -> str:
    """Лучший email: фирменный (домен сайта) > правильный префикс > любой."""
    cands = []
    for c in contacts or []:
        e = (c.get("email") or "").strip()
        if e and "@" in e:
            cands.append(e)
    cands += EMAIL_RE.findall(description or "")

    best, best_score = "", -1
    for e in cands:
        low = e.lower()
        if any(b in low for b in EMAIL_BLOCKLIST):
            continue
        score = 0
        local, _, domain = low.partition("@")
        if site_domain and (domain == site_domain or domain.endswith("." + site_domain)):
            score += 100
        for i, pref in enumerate(PREFERRED_PREFIXES):
            if local == pref or local.startswith(pref + "."):
                score += 50 - i
                break
        if score > best_score:
            best, best_score = e, score
    return best if best_score >= 0 else ""


def _all_emails(contacts: list, description: str) -> list[str]:
    values = [str(c.get("email") or "").strip() for c in contacts or []]
    values.extend(EMAIL_RE.findall(description or ""))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        low = value.casefold()
        if not value or "@" not in value or any(block in low for block in EMAIL_BLOCKLIST):
            continue
        if low not in seen:
            seen.add(low)
            result.append(value)
    return result


def _pick_phone(contacts: list) -> str:
    for c in contacts or []:
        p = (c.get("phone") or "").strip()
        if p:
            return normalize_phone(p)
    return ""


def _all_phones(contacts: list) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for contact in contacts or []:
        value = normalize_phone(str(contact.get("phone") or "").strip())
        identity = re.sub(r"\D", "", value)
        if value and identity and identity not in seen:
            seen.add(identity)
            result.append(value)
    return result


async def run(context):
    """context не используется (HTTP-only)."""
    token = os.getenv("VK_TOKEN", "").strip()
    if not token:
        print("\n=== VK: VK_TOKEN не задан, пропуск ===")
        return

    print("\n=== VK Groups ===")

    # 1. Сбор group_id по матрице город × категория
    try:
        max_cities = int(os.getenv("MAX_CITIES") or os.getenv("VK_MAX_CITIES") or "0")
        max_queries = int(os.getenv("MAX_QUERIES_PER_SOURCE") or os.getenv("VK_MAX_QUERIES") or "0")
        max_items = int(os.getenv("MAX_ITEMS_PER_SOURCE") or os.getenv("VK_MAX_GROUPS") or "0")
    except ValueError:
        max_cities = max_queries = max_items = 0

    city_items = list(VK_CITIES.items())[:max_cities or None]
    queries = QUERIES[:max_queries or None]
    found: dict[int, dict] = {}  # group_id -> city + matched queries
    for city_id, city_name in city_items:
        for q in queries:
            r = _call("groups.search", token, q=q, city_id=city_id,
                      count=200, sort=0)
            time.sleep(RPS_PAUSE)
            if "error" in r:
                msg = r["error"].get("error_msg", "?")
                if "Too many" in msg:
                    time.sleep(1.0)
                continue
            for it in r.get("response", {}).get("items", []):
                gid = it.get("id")
                if not gid:
                    continue
                meta = found.setdefault(gid, {"city": city_name, "queries": set()})
                meta["queries"].add(q)
                if max_items and len(found) >= max_items:
                    break
            if max_items and len(found) >= max_items:
                break
        print(f"  [VK] {city_name}: накоплено {len(found)} групп")
        if max_items and len(found) >= max_items:
            break

    if not found:
        print("  [VK] ничего не найдено")
        return

    print(f"  [VK] всего уникальных групп: {len(found)}")

    # 2. Детали пачками по 500
    added = 0
    gids = list(found.keys())
    for start in range(0, len(gids), 500):
        chunk = gids[start:start + 500]
        r = _call("groups.getById", token,
                  group_ids=",".join(map(str, chunk)),
                  fields="contacts,site,description,addresses,activity,city,screen_name")
        time.sleep(RPS_PAUSE)
        if "error" in r:
            print(f"  [VK] getById err: {r['error'].get('error_msg', '?')[:80]}")
            time.sleep(1.0)
            continue
        resp = r.get("response")
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        for g in groups or []:
            name = (g.get("name") or "").strip()
            if not name:
                continue
            gid = g.get("id")
            site = _clean_site(g.get("site") or "")
            sdom = _site_domain(site)
            contacts = g.get("contacts") or []
            desc = g.get("description") or ""
            email = _pick_email(contacts, desc, sdom)
            phone = _pick_phone(contacts)
            city_obj = g.get("city") or {}
            meta = found.get(gid) or {"city": "Крым", "queries": set()}
            matched_queries = sorted(meta.get("queries") or [])
            city = (city_obj.get("title") if isinstance(city_obj, dict) else "") \
                or meta.get("city", "Крым")
            city = normalize_city_name(city)
            screen = g.get("screen_name") or f"club{gid}"
            social = f"https://vk.com/{screen}"
            activity = g.get("activity") or ""
            confidence, quality_flags = _group_relevance(g, matched_queries)
            if confidence <= 0.05:
                continue

            if save_item({
                "city": city,
                "name": name,
                "address": _pick_address(g),
                "phone": phone,
                "email": email,
                "website": site,
                "social": social,
                "all_phones": " | ".join(_all_phones(contacts)),
                "all_emails": " | ".join(_all_emails(contacts, desc)),
                "all_websites": site,
                "all_socials": social,
                "category": _category_for(g, matched_queries),
                "raw_category": activity,
                "matched_query": " | ".join(matched_queries),
                "source": "VK",
                "source_id": str(gid),
                "source_url": social,
                "confidence": f"{confidence:.2f}",
                "quality_flags": "|".join(quality_flags),
                "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }):
                added += 1

    print(f"\n[VK] добавлено: {added}")
    return added
