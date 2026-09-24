"""Web crawler: рекурсивно ходит по сайтам уже собранных объектов, обнаруживает
новые объекты (соседние заведения, сетевые филиалы) и контакты.

Стратегия:
1. Источник seed-доменов — текущий CSV (поле website всех записей).
2. На каждом домене:
   а) пробуем GET /sitemap.xml — оттуда быстро получаем 10-1000 страниц
   б) GET / — извлекаем все <a href>, в т.ч. /о-нас, /контакты, /филиалы, /партнеры
   в) обходим до MAX_PAGES_PER_DOMAIN страниц внутри домена
3. Из каждой посещённой страницы:
   - извлекаем phone/email/address (как email_finder)
   - извлекаем <title>/<h1> как потенциальное имя нового объекта (если на странице
     есть HoReCa-триггеры: «ресторан», «кафе», «меню», «доставка», «бронирование стола»)
4. Ссылки на сторонние домены проверяем по эвристике «сайт заведения общепита» —
   если домен не агрегатор и в title есть триггер → добавляем как seed-кандидат
   (но всё равно не больше MAX_TOTAL_PAGES).

Все save_item проходят через persistent dedup — повторов между прогонами не будет.
"""
import asyncio
import csv
import os
import re
import socket
from datetime import datetime
from urllib.parse import urlparse, urljoin

import aiohttp

from utils.storage import save_item
from utils.aggregators import is_aggregator
from utils.net_safety import (
    UnsafeURLError,
    resolve_public_host,
    validate_public_url,
)
from utils.quality import can_seed_crawler
from parsers.email_finder import (
    pick_address,
    pick_address_from_html,
    pick_emails,
    pick_phones,
)


def _positive_env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


MAX_PAGES_PER_DOMAIN = _positive_env_int("CRAWLER_MAX_PAGES_PER_DOMAIN", 15)
_common_item_limit = _positive_env_int("MAX_ITEMS_PER_SOURCE", 0, minimum=0)
_default_total_pages = (
    min(4000, max(25, _common_item_limit * 10))
    if _common_item_limit
    else 4000
)
MAX_TOTAL_PAGES = _positive_env_int(
    "CRAWLER_MAX_TOTAL_PAGES", _default_total_pages
)
PARALLEL = _positive_env_int("CRAWLER_PARALLEL", 15)
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

HORECA_TRIGGERS = (
    "ресторан", "кафе", "бар", "паб", "клуб", "кофейня",
    "столовая", "фудкорт", "пиццерия", "кондитерская", "фастфуд",
    "меню", "доставка еды", "бронирование стола", "столик",
    "restaurant", "cafe", "bar", "pub", "club", "coffee", "pizza",
)

# Интересные пути — приоритет в очереди обхода
INTERESTING_PATHS_RE = re.compile(
    r"/(contacts?|about|partner|filial|location|menu|delivery|objects?|"
    r"меню|доставка|контакт|о-нас|о_нас|о-компании|объект|филиал)",
    re.IGNORECASE,
)

A_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def _origin(url: str) -> str:
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


class _PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """aiohttp resolver that pins connections to validated public IPs."""

    async def resolve(self, host: str, port: int = 0,
                      family: int = socket.AF_UNSPEC):
        answers = await asyncio.to_thread(resolve_public_host, host, port, family)
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": answer_family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
            for answer_family, address in answers
        ]

    async def close(self) -> None:
        return None


def _strip(s: str) -> str:
    s = TAG_RE.sub(" ", s or "")
    return WS_RE.sub(" ", s).strip()


def _has_horeca_trigger(text: str) -> bool:
    low = (text or "").lower()
    return any(t in low for t in HORECA_TRIGGERS)


def _load_seeds_from_csv(path: str) -> list[str]:
    if not path or not os.path.exists(path):
        return []
    origins: set[str] = set()
    for delim in (";", ","):
        try:
            with open(path, encoding="utf-8-sig") as f:
                sample = f.read(2048)
                f.seek(0)
                if delim not in sample:
                    continue
                for r in csv.DictReader(f, delimiter=delim):
                    if not can_seed_crawler(r):
                        continue
                    w = (r.get("website") or "").strip()
                    if not w:
                        continue
                    o = _origin(w)
                    if not o or is_aggregator(o):
                        continue
                    try:
                        # DNS is enforced by _PublicOnlyResolver at connect time.
                        validate_public_url(o, resolve_dns=False)
                    except UnsafeURLError:
                        continue
                    origins.add(o)
                if origins:
                    break
        except Exception:
            continue
    return sorted(origins)


def _latest_csv() -> str:
    """Возвращает источник seed-доменов для Crawler.

    Приоритет master_all.csv: внутри одного прогона result_*.csv в начале
    ещё пустой (Crawler работает не последним), а master_all накопил тысячи
    доменов за все прошлые прогоны — это и есть лучший seed.
    """
    import glob
    master = "output/master_all.csv"
    if os.path.exists(master) and os.path.getsize(master) > 1024:
        return master
    files = sorted(glob.glob("output/result_2*.csv"), key=os.path.getmtime)
    return files[-1] if files else ""


def _name_from_html(html: str) -> str:
    for re_ in (H1_RE, TITLE_RE):
        m = re_.search(html)
        if m:
            t = _strip(m.group(1))
            if 3 <= len(t) <= 200:
                return t
    return ""


def _extract_links(html: str, base_origin: str) -> set[str]:
    links: set[str] = set()
    for m in A_HREF_RE.finditer(html):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base_origin + "/", href)
        links.add(full.split("#")[0])
    return links


async def _fetch(session: aiohttp.ClientSession, url: str) -> str:
    current = url
    for _redirect_count in range(MAX_REDIRECTS + 1):
        try:
            # Literal/private targets are rejected here; hostnames are resolved
            # and pinned by the connector's _PublicOnlyResolver.
            validate_public_url(current, resolve_dns=False)
            async with session.get(
                current,
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
                headers={"User-Agent": UA},
            ) as r:
                if r.status in {301, 302, 303, 307, 308}:
                    location = r.headers.get("Location", "")
                    if not location:
                        return ""
                    current = urljoin(current, location)
                    continue
                if r.status >= 400:
                    return ""
                ctype = r.headers.get("Content-Type", "").lower()
                if "text/html" not in ctype and "xml" not in ctype:
                    return ""
                try:
                    declared_size = int(r.headers.get("Content-Length", "0"))
                except ValueError:
                    declared_size = 0
                if declared_size > MAX_RESPONSE_BYTES:
                    return ""
                body = await r.content.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    return ""
                encoding = r.charset or "utf-8"
                return body.decode(encoding, errors="replace")
        except (UnsafeURLError, aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
            return ""
    return ""


async def _sitemap_urls(session: aiohttp.ClientSession, origin: str) -> list[str]:
    txt = await _fetch(session, origin + "/sitemap.xml")
    if not txt:
        return []
    return re.findall(r"<loc>([^<]+)</loc>", txt, re.IGNORECASE)[:200]


def _detect_city_from_text(text: str) -> str:
    from parsers.osm import CITY_HINTS
    low = text.lower()
    for c in CITY_HINTS:
        if c.lower() in low:
            return c
    return "Крым"


NAV_NAME_BLACKLIST_RE = re.compile(
    r"^(о\s*нас|о\s*компании|контакт|отзыв|фото|галере|новост|"
    r"номера|услуг|карта\s*сайт|404|index|главн|меню|каталог)",
    re.IGNORECASE,
)


def _is_real_object_name(name: str) -> bool:
    """Имя выглядит как реальный объект (а не навигационная страница)?"""
    if not name or len(name) < 4:
        return False
    if NAV_NAME_BLACKLIST_RE.match(name.strip()):
        return False
    # отбрасываем чистые доменные имена типа "1crimea.com"
    if re.match(r"^[a-z0-9\-]+\.[a-z]{2,}$", name.strip().lower()):
        return False
    return True


async def _crawl_domain(session: aiohttp.ClientSession, origin: str,
                       visited_pages: set[str], total_counter: list[int]) -> int:
    """Обходим один домен. 1 домен = 1 объект. Имя — с главной,
    контакты собираем со всех страниц.
    """
    pages_in_domain = 0
    queue: list[str] = []

    # sitemap первым делом
    sitemap = await _sitemap_urls(session, origin)
    if sitemap:
        for u in sitemap:
            if _origin(u) == origin:
                queue.append(u)

    queue.insert(0, origin + "/")
    queue.sort(key=lambda u: 0 if INTERESTING_PATHS_RE.search(u) else 1)

    main_name = ""        # имя с главной (или /о-нас)
    best_phone = ""
    best_email = ""
    best_address = ""
    all_phones: list[str] = []
    all_emails: list[str] = []
    has_horeca_trigger = False

    while queue and pages_in_domain < MAX_PAGES_PER_DOMAIN \
            and total_counter[0] < MAX_TOTAL_PAGES:
        url = queue.pop(0)
        if url in visited_pages:
            continue
        visited_pages.add(url)
        total_counter[0] += 1
        pages_in_domain += 1

        html = await _fetch(session, url)
        if not html:
            continue

        # Триггер «общепита» хоть на одной странице → засчитываем домен
        if not has_horeca_trigger and _has_horeca_trigger(html[:8000]):
            has_horeca_trigger = True

        # Имя — приоритет: главная (первая успешная), затем /о-нас если на главной не нашли
        if not main_name:
            cand = _name_from_html(html)
            if _is_real_object_name(cand):
                main_name = cand

        # Контакты — берём первое непустое
        for email in pick_emails(html):
            if email.casefold() not in {value.casefold() for value in all_emails}:
                all_emails.append(email)
        for phone in pick_phones(html):
            digits = re.sub(r"\D", "", phone)
            if digits and digits not in {re.sub(r"\D", "", value) for value in all_phones}:
                all_phones.append(phone)
        if not best_email and all_emails:
            best_email = all_emails[0]
        if not best_phone and all_phones:
            best_phone = all_phones[0]
        if not best_address:
            best_address = pick_address_from_html(html) or pick_address(_strip(html))

        if pages_in_domain < MAX_PAGES_PER_DOMAIN:
            for link in _extract_links(html, origin):
                if _origin(link) != origin:
                    continue
                if link in visited_pages or link in queue:
                    continue
                if INTERESTING_PATHS_RE.search(link):
                    queue.insert(0, link)
                elif len(queue) < MAX_PAGES_PER_DOMAIN * 2:
                    queue.append(link)

    # Запись только если: есть имя + домен похож на заведение общепита
    if not main_name or not has_horeca_trigger:
        return 0

    city = _detect_city_from_text(best_address or main_name)
    # Категория из имени: триггеры вроде «меню»/«столик» — только гейт домена,
    # категорией могут стать лишь те, что нормализуются в реальный тип.
    from utils.categories import normalize as normalize_category
    cat = "прочее"
    low_name = main_name.lower()
    for trig in HORECA_TRIGGERS:
        if trig in low_name:
            norm = normalize_category(trig)
            if norm != "прочее":
                cat = norm
                break

    if save_item({
        "city": city,
        "name": main_name,
        "address": best_address,
        "phone": best_phone,
        "email": best_email,
        "website": origin,
        "all_phones": " | ".join(all_phones),
        "all_emails": " | ".join(all_emails),
        "all_websites": origin,
        "category": cat,
        "source": "Crawler",
        "source_id": origin.casefold(),
        "source_url": origin,
        "confidence": "0.72" if best_address else "0.58",
        "quality_flags": "" if best_address else "missing_address|manual_review",
        "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }):
        return 1
    return 0


async def run(context):
    """context — Playwright не используется (HTTP-only crawler).
    Сигнатура для совместимости с main.py.
    """
    print("\n=== Crawler ===")
    seeds = _load_seeds_from_csv(_latest_csv())
    if not seeds:
        print("[Crawler] нет seed-доменов (CSV пуст или нет website). Пропуск.")
        return

    print(f"[Crawler] seed-доменов: {len(seeds)}, "
          f"max_pages_per_domain={MAX_PAGES_PER_DOMAIN}, "
          f"max_total_pages={MAX_TOTAL_PAGES}, parallel={PARALLEL}")

    visited_pages: set[str] = set()
    total_counter = [0]  # mutable closure
    added_total = 0

    # TLS verification stays enabled (aiohttp default).  The custom resolver
    # prevents DNS/private-address SSRF and supplies the exact checked IPs.
    connector = aiohttp.TCPConnector(
        limit=PARALLEL,
        resolver=_PublicOnlyResolver(),
    )
    async with aiohttp.ClientSession(connector=connector,
                                     timeout=HTTP_TIMEOUT) as session:
        sem = asyncio.Semaphore(PARALLEL)

        async def _one(origin):
            nonlocal added_total
            async with sem:
                if total_counter[0] >= MAX_TOTAL_PAGES:
                    return
                try:
                    n = await _crawl_domain(session, origin, visited_pages, total_counter)
                    if n:
                        added_total += n
                        print(f"  [Crawler] {origin} → +{n} (всего страниц: {total_counter[0]})")
                except Exception as e:
                    print(f"  [Crawler] {origin} err: {e}")

        await asyncio.gather(*[_one(o) for o in seeds])

    print(f"\n[Crawler] обойдено страниц: {total_counter[0]}, добавлено: {added_total}")
