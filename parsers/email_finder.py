"""Email/phone/address enrichment by visiting each website.

Логика:
1. На главной — ищем mailto:/tel:/visible-text email и phone.
2. Читаем JSON-LD (script[type='application/ld+json']) — там часто структурированные email/telephone.
3. Дополнительно обходим типовые контактные страницы (/contacts, /kontakty, /booking ...).
4. Из найденного текста пробуем выудить адрес (эвристика на «г./пгт./ул./ш./пр.»).
5. Ранжируем email: фирменный (домен совпадает с website) > info@/sales@/booking@ > остальные.
6. Подбираем social-ссылки (vk.com, t.me, instagram, ok.ru) на случай отсутствия website.
"""
import asyncio
import csv
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

from config.hosts import host_of
from parsers.site_finder import find_website
from parsers.vk_email import extract_email_from_vk_async
from utils.browser import create_browser_context
from utils.csv_safety import neutralize_csv_formula
from utils.email_quality import sanitize_row_emails
from utils.safe_http import fetch_public_text
from utils.storage import CSV_DELIMITER, FIELDS, normalize_phone

OUTPUT_FILE = f"output/result_enriched_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")

# Адрес: префикс города (`г.`/`город` etc) + ИМЯ_СОБСТВЕННОЕ (заглавная) +
# тип улицы + название, всё в одной строке. Точки обязательны для сокращений,
# чтобы `с` (в «расположены») не съедало правило. Заглавная буква имени — без
# IGNORECASE, иначе false positive типа «расположены на улице».
ADDRESS_RE = re.compile(
    r"\b"
    r"(?i:(?:г\.|город|пгт\.|посёлок|поселок|с\.|село|д\.|деревня))"
    r"\s+"
    r"[А-ЯЁ][А-Яа-яЁё\-]{2,}"
    r"[^\n\r]{0,40}?"
    r"(?i:(?:ул\.|улица|пр-т|проспект|пр\.|пер\.|переулок|ш\.|шоссе|пл\.|площадь|наб\.|набережная|просп\.|бульвар|б-р))"
    r"[^\n\r]{3,100}"
)

# JSON-LD: schema.org PostalAddress. Захватываем весь массив с парами.
JSON_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

EMAIL_BLOCKLIST = (
    "example.", "@domain", "@test.", "noreply", "no-reply", "do-not-reply",
    "@sentry", "@wixpress", "@2x.png", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif",
    "@react", "@vue", "@babel", "@types", "@material", "@material-ui",
    "@yandex-team", "@google-analytics", "@cloudflare", "@gravatar",
    "webmaster@", "postmaster@", "abuse@", "hostmaster@",
    "admin@yandex", "admin@google", "support@google", "support@apple",
    "your@email", "name@email", "test@",
)

# Префиксы фирменных email заведений — чем выше в списке, тем выше приоритет.
PREFERRED_EMAIL_PREFIXES = (
    "reservation", "reservations", "booking", "book",
    "reception", "info", "order", "zakaz",
    "sales", "manager", "office", "contact",
    "rsv", "delivery",
)

CONTACT_PATHS = [
    "/contacts", "/contact", "/contact-us", "/contact_us",
    "/kontakty", "/kontakt", "/contacts.html", "/contact.html",
    "/o-nas", "/o_nas", "/about", "/about-us", "/about_us",
    "/o-kompanii", "/o_kompanii",
    "/page/contact", "/page/contacts", "/feedback", "/info",
    "/obratnaya-svyaz", "/obratnaya_svyaz",
    "/svyazatsya", "/связаться", "/контакты", "/о-нас",
    "/index.php?route=information/contact",
    "/info/contacts", "/cms/contacts",
    # Booking/order-specific
    "/booking", "/reservation", "/reservations", "/book",
    "/reserve", "/bronirovat", "/zabronirovat",
    "/cooperation", "/partners", "/agents", "/menu", "/delivery",
    "/rezervirovanie", "/бронирование", "/dostavka",
    # Pricing pages (often contain contact info)
    "/price", "/prices", "/tseny",
]

SOCIAL_HOSTS = (
    "vk.com", "vk.ru", "t.me", "telegram.me", "telegram.org",
    "instagram.com", "ok.ru", "facebook.com", "wa.me", "whatsapp.com",
)

_SITEMAP_CONTACT_KW = {"contact", "about", "kontakt", "kontakty", "feedback", "obratnaya"}


def _csv_safe_row(row: dict) -> dict:
    """Neutralize spreadsheet formulas at the enrichment CSV boundary."""
    return {
        field: neutralize_csv_formula(str(row.get(field) or ""))
        for field in FIELDS
    }


def _merge_flat_contacts(existing: str, incoming: str, *, phones: bool = False) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for raw in (existing, incoming):
        for value in re.split(r"\s+\|\s+", str(raw or "")):
            clean = value.strip()
            identity = re.sub(r"\D", "", clean) if phones else clean.casefold().rstrip("/")
            if clean and identity and identity not in seen:
                seen.add(identity)
                values.append(clean)
    return " | ".join(values)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class EnrichSettings:
    """Настройки добора контактов (все из окружения; значения по умолчанию — email-first).

    ENRICH_MAX_SITES      лимит обходов БРАУЗЕРОМ за прогон (0 = без лимита); статика не лимитируется
    ENRICH_STATIC         1 — сначала быстрый HTTP-проход без браузера (parsers.static_contacts)
    ENRICH_EMAIL_ONLY     1 — обрабатывать только строки без email; 0 — как раньше: любая нехватка поля
    ENRICH_STATIC_PARALLEL параллельность статического прохода
    ENRICH_MAX_PATHS      сколько типовых контактных путей пробовать в браузере (раньше — все 45)
    SITE_FINDER           1 — искать сайт через DuckDuckGo для записей без website
    """

    max_sites: int = 400
    site_finder: bool = False
    static: bool = True
    email_only: bool = True
    static_parallel: int = 16
    max_paths: int = 12

    @classmethod
    def from_env(cls) -> "EnrichSettings":
        return cls(
            max_sites=max(0, _env_int("ENRICH_MAX_SITES", 400)),
            site_finder=_env_flag("SITE_FINDER", False),
            static=_env_flag("ENRICH_STATIC", True),
            email_only=_env_flag("ENRICH_EMAIL_ONLY", True),
            static_parallel=max(1, _env_int("ENRICH_STATIC_PARALLEL", 16)),
            max_paths=max(1, _env_int("ENRICH_MAX_PATHS", 12)),
        )


def _row_needs(row: dict) -> dict[str, bool]:
    return {
        "email": not row.get("email"),
        "phone": not row.get("phone"),
        "address": not row.get("address"),
        "social": not row.get("social", ""),
    }


def _wants_enrichment(row: dict, settings: EnrichSettings) -> bool:
    needs = _row_needs(row)
    return needs["email"] if settings.email_only else any(needs.values())


def _visit_order(rows: list[dict], settings: EnrichSettings) -> list[int]:
    """Индексы строк к обработке: сначала без email, затем остальные; без сайта — в конце."""
    with_site = [
        i for i, row in enumerate(rows)
        if (row.get("website") or "").strip() and _wants_enrichment(row, settings)
    ]
    without_site = [
        i for i, row in enumerate(rows)
        if settings.site_finder and not (row.get("website") or "").strip() and _wants_enrichment(row, settings)
    ]
    return sorted(with_site, key=lambda i: bool(rows[i].get("email"))) + without_site


def _needs_browser(row: dict, static_result, settings: EnrichSettings) -> bool:
    """Нужен ли браузер после статического прохода. Мёртвый сайт браузером не пробуем."""
    if static_result is not None and static_result.kind == "dead":
        return False
    return _wants_enrichment(row, settings)


def _apply_static(row: dict, result) -> bool:
    """Перенести найденное статическим проходом в строку. True, если получили email."""
    if result is None:
        return False
    got_email = False
    if result.emails:
        if not row.get("email"):
            row["email"] = result.emails[0]
            got_email = True
        row["all_emails"] = _merge_flat_contacts(
            row.get("all_emails") or row.get("email", ""), " | ".join(result.emails)
        )
    if result.phones:
        if not row.get("phone"):
            row["phone"] = result.phones[0]
        row["all_phones"] = _merge_flat_contacts(
            row.get("all_phones") or row.get("phone", ""), " | ".join(result.phones), phones=True
        )
    return got_email


def _apply_browser_result(row: dict, result: tuple[str, ...], needs: dict[str, bool]) -> None:
    """Перенести найденное браузером в строку; адреса проходят шлюз качества."""
    email, phone, address, social, all_emails, all_phones, all_socials = result
    email, all_emails = sanitize_row_emails(email, all_emails, host_of(row.get("website", "")))
    if needs["email"] and email:
        row["email"] = email
        print(f"    email: {email}")
    if needs["phone"] and phone:
        row["phone"] = phone
        print(f"    phone: {phone}")
    if needs["address"] and address:
        row["address"] = address
        print(f"    address: {address}")
    if needs["social"] and social:
        row["social"] = social
        print(f"    social: {social}")
    if all_emails:
        row["all_emails"] = _merge_flat_contacts(row.get("all_emails") or row.get("email", ""), all_emails)
    if all_phones:
        row["all_phones"] = _merge_flat_contacts(
            row.get("all_phones") or row.get("phone", ""), all_phones, phones=True
        )
    if all_socials:
        row["all_socials"] = _merge_flat_contacts(row.get("all_socials") or row.get("social", ""), all_socials)
    row["all_websites"] = _merge_flat_contacts(
        row.get("all_websites") or row.get("website", ""), row.get("website", "")
    )


def _contacts_complete(need, email: str, phone: str, address: str) -> bool:
    """Достаточно ли найдено, чтобы прекратить обход. need=None — прежнее поведение (все три)."""
    if need is None:
        return bool(email and phone and address)
    found = {"email": bool(email), "phone": bool(phone), "address": bool(address)}
    return all(found.get(field, True) for field in need)


def _pages_to_visit(base_url: str, discovered: list[str], guessed_paths, sitemap_urls: list[str],
                    max_paths: int | None) -> list[str]:
    """Порядок обхода: ссылки самого сайта → типовые пути (не больше max_paths) → sitemap."""
    paths = list(guessed_paths)[:max_paths] if max_paths else list(guessed_paths)
    guessed = [urljoin(base_url + "/", path.lstrip("/")) for path in paths]
    return list(dict.fromkeys([*discovered, *guessed, *sitemap_urls]))


async def _get_sitemap_contact_urls(base_url: str, limit: int = 5) -> list[str]:
    """Extract up to `limit` contact-looking URLs from sitemap.xml."""
    try:
        url = urljoin(base_url.rstrip("/") + "/", "sitemap.xml")
        text = await fetch_public_text(
            url,
            timeout_seconds=10,
            max_response_bytes=2 * 1024 * 1024,
            headers={"User-Agent": "Mozilla/5.0"},
            allowed_content_types=("xml", "text/plain", "text/html"),
        )
        if not text:
            return []
        root = ET.fromstring(text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]
        return [u for u in urls if any(kw in u.lower() for kw in _SITEMAP_CONTACT_KW)][:limit]
    except Exception:
        return []


def _decode_obfuscated_email(text: str) -> str:
    """info[at]hotel[dot]ru → info@hotel.ru."""
    if not text:
        return ""
    candidates = re.findall(
        r"[a-zA-Z0-9._%+\-]+\s*[\[\(]\s*(?:at|собака|@)\s*[\]\)]\s*[a-zA-Z0-9.\-]+\s*[\[\(]\s*(?:dot|точка|\.)\s*[\]\)]\s*[a-zA-Z]{2,}",
        text, re.IGNORECASE,
    )
    for c in candidates:
        decoded = re.sub(r"\s*[\[\(]\s*(?:at|собака|@)\s*[\]\)]\s*", "@", c, flags=re.IGNORECASE)
        decoded = re.sub(r"\s*[\[\(]\s*(?:dot|точка|\.)\s*[\]\)]\s*", ".", decoded, flags=re.IGNORECASE)
        if "@" in decoded and "." in decoded.split("@")[-1]:
            return decoded
    return ""


def _site_domain(website: str) -> str:
    if not website:
        return ""
    try:
        host = urlparse(website).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _email_score(email: str, site_domain: str) -> int:
    """Чем больше — тем приоритетнее. -1 = в blocklist (отбрасывается)."""
    low = email.lower()
    if any(b in low for b in EMAIL_BLOCKLIST):
        return -1
    if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")):
        return -1
    score = 0
    try:
        local, _, domain = low.partition("@")
    except Exception:
        return 0
    # +100 — фирменный (домен совпадает с сайтом)
    if site_domain and (domain == site_domain or domain.endswith("." + site_domain)
                        or site_domain.endswith("." + domain)):
        score += 100
    # +N — «правильный» префикс (info@/booking@/...)
    for i, pref in enumerate(PREFERRED_EMAIL_PREFIXES):
        if local == pref or local.startswith(pref + "."):
            score += 50 - i  # info=50, reservation=49, ...
            break
    return score


def pick_emails(text: str, site_domain: str = "") -> list[str]:
    """Return every plausible email, ranked with the preferred one first."""
    if not text:
        return []
    candidates = set()
    for e in EMAIL_RE.findall(text):
        candidates.add(e)
    ranked: list[tuple[int, str]] = []
    for e in candidates:
        score = _email_score(e, site_domain)
        if score >= 0:
            ranked.append((score, e))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].casefold()))
    return [email for _score, email in ranked]


def pick_email(text: str, site_domain: str = "") -> str:
    """Выбрать лучший email из текста с учётом домена сайта (фирменность)."""
    values = pick_emails(text, site_domain)
    return values[0] if values else ""


def pick_phones(text: str) -> list[str]:
    if not text:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for match in PHONE_RE.findall(text):
        value = normalize_phone(match)
        identity = re.sub(r"\D", "", value)
        if identity and identity not in seen:
            seen.add(identity)
            result.append(value)
    return result


def pick_phone(text: str) -> str:
    values = pick_phones(text)
    return values[0] if values else ""


def _walk_json_for_address(obj) -> str:
    """Рекурсивный обход JSON-LD: ищет PostalAddress, возвращает 'street, locality'."""
    if isinstance(obj, dict):
        t = obj.get("@type") or obj.get("type")
        types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
        if any((isinstance(x, str) and "PostalAddress" in x) for x in types):
            street = (obj.get("streetAddress") or "").strip()
            locality = (obj.get("addressLocality") or obj.get("addressRegion") or "").strip()
            parts = [p for p in (locality, street) if p]
            if parts:
                return ", ".join(parts)
        # address может быть строкой или вложенным PostalAddress
        addr = obj.get("address")
        if isinstance(addr, str) and len(addr) > 8:
            return addr.strip()
        for v in obj.values():
            found = _walk_json_for_address(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _walk_json_for_address(item)
            if found:
                return found
    return ""


def pick_address_from_html(html: str) -> str:
    """Адрес из JSON-LD schema.org/PostalAddress (приоритет надёжности)."""
    if not html:
        return ""
    for m in JSON_LD_RE.finditer(html):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        found = _walk_json_for_address(data)
        if found:
            return re.sub(r"\s{2,}", " ", found).strip(" ,.;")[:200]
    return ""


def pick_address(text: str) -> str:
    if not text:
        return ""
    m = ADDRESS_RE.search(text)
    if not m:
        return ""
    addr = re.sub(r"\s{2,}", " ", m.group(0)).strip(" ,.;")
    return addr[:200]


async def _harvest_dom(page, site_domain: str = "") -> tuple[str, str]:
    """mailto: / tel: ссылки в DOM (приоритет). Если mailto несколько — выбираем лучший."""
    email = ""
    phone = ""
    try:
        ems = await page.query_selector_all("a[href^='mailto:']")
        candidates = []
        for em in ems:
            href = await em.get_attribute("href") or ""
            cand = href.replace("mailto:", "").split("?")[0].strip()
            if cand and "@" in cand:
                candidates.append(cand)
        if candidates:
            best = ""
            best_score = -1
            for c in candidates:
                s = _email_score(c, site_domain)
                if s > best_score:
                    best = c
                    best_score = s
            email = best if best_score >= 0 else ""
    except Exception:
        pass
    try:
        tl = await page.query_selector("a[href^='tel:']")
        if tl:
            href = await tl.get_attribute("href") or ""
            cand = href.replace("tel:", "").strip()
            if cand:
                phone = normalize_phone(cand)
    except Exception:
        pass
    return email, phone


async def _scroll_to_bottom(page, n: int = 6):
    """Многие сайты подгружают footer (контакты) только после скролла."""
    for _ in range(n):
        try:
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        except Exception:
            pass
        await page.wait_for_timeout(400)


async def _extract_from_jsonld(page) -> tuple[str, str]:
    """Из <script type='application/ld+json'> вытаскиваем email и telephone."""
    email = phone = ""
    try:
        scripts = await page.query_selector_all("script[type='application/ld+json']")
        for s in scripts:
            txt = await s.inner_text()
            if not txt or "{" not in txt:
                continue
            try:
                data = json.loads(txt)
            except Exception:
                continue
            stack = [data] if not isinstance(data, list) else list(data)
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if not email:
                        e = node.get("email")
                        if isinstance(e, str) and "@" in e:
                            email = e.strip()
                    if not phone:
                        t = node.get("telephone")
                        if isinstance(t, str) and t.strip():
                            phone = normalize_phone(t.strip())
                    for v in node.values():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(node, list):
                    stack.extend(node)
                if email and phone:
                    return email, phone
    except Exception:
        pass
    return email, phone


async def _extract_socials(page) -> list[str]:
    """Collect public social profile links while skipping share actions."""
    result: list[str] = []
    seen: set[str] = set()
    try:
        anchors = await page.query_selector_all("a[href^='http']")
        for a in anchors:
            href = await a.get_attribute("href") or ""
            host = urlparse(href).netloc.lower()
            if any(s in host for s in SOCIAL_HOSTS):
                # отбрасываем share/sharer ссылки
                low = href.lower()
                if any(b in low for b in ("share", "sharer", "send_to", "post=")):
                    continue
                clean = href.split("?")[0]
                identity = clean.casefold().rstrip("/")
                if identity not in seen:
                    seen.add(identity)
                    result.append(clean)
    except Exception:
        pass
    return result


async def _extract_social(page) -> str:
    """Подбор первой социальной ссылки на профиль организации."""
    values = await _extract_socials(page)
    return values[0] if values else ""


async def _harvest_all_contacts(page, site_domain: str) -> tuple[list[str], list[str], list[str]]:
    """Collect alternate contacts from one rendered page for audit/review."""
    emails: list[str] = []
    phones: list[str] = []
    try:
        page_html = await page.content()
        emails = pick_emails(page_html, site_domain)
        obfuscated = _decode_obfuscated_email(page_html)
        if obfuscated and obfuscated.casefold() not in {value.casefold() for value in emails}:
            emails.append(obfuscated)
        phones = pick_phones(page_html)
    except Exception:
        pass
    return emails, phones, await _extract_socials(page)


async def _harvest_page(page, site_domain: str = "") -> tuple[str, str, str, str]:
    """email/phone/address/social из текущей страницы (DOM + JSON-LD + HTML + visible)."""
    email = phone = address = social = ""

    # 1) DOM — mailto/tel (highest precision)
    e, p = await _harvest_dom(page, site_domain)
    email = email or e
    phone = phone or p

    # 2) JSON-LD — структурированные данные (часто чистые)
    if not email or not phone:
        e, p = await _extract_from_jsonld(page)
        if not email:
            email = e
        if not phone:
            phone = p

    # 3) Полный HTML — regex с ранжированием
    try:
        html = await page.content()
        if not email:
            email = pick_email(html, site_domain) or _decode_obfuscated_email(html)
        if not phone:
            phone = pick_phone(html)
    except Exception:
        pass

    # 4) Visible text — для address и обфусцированных email
    try:
        visible = await page.evaluate("document.body && document.body.innerText || ''")
        if not address:
            try:
                page_html = await page.content()
                address = pick_address_from_html(page_html)
            except Exception:
                pass
        if not address:
            address = pick_address(visible)
        if not email:
            email = _decode_obfuscated_email(visible)
    except Exception:
        pass

    # 5) Social — отдельной попыткой
    social = await _extract_social(page)

    return email, phone, address, social


async def enrich_from_website(
    page, website: str, include_all: bool = False, need=None, max_paths: int | None = None,
) -> tuple[str, ...]:
    """Return preferred contacts and, optionally, flattened alternatives.

    need      — какие поля нужны ({'email'}, …): обход прекращается, когда они найдены;
                None — прежнее поведение (ждать email, телефон и адрес разом).
    max_paths — сколько типовых контактных путей пробовать (None — все).
    """
    email = phone = address = social = ""
    all_emails: list[str] = []
    all_phones: list[str] = []
    all_socials: list[str] = []
    site_domain = _site_domain(website)

    def add_values(target: list[str], values: list[str], *, phone_values: bool = False) -> None:
        identities = {
            (re.sub(r"\D", "", value) if phone_values else value.casefold().rstrip("/"))
            for value in target
        }
        for value in values:
            clean = (value or "").strip()
            identity = re.sub(r"\D", "", clean) if phone_values else clean.casefold().rstrip("/")
            if clean and identity and identity not in identities:
                identities.add(identity)
                target.append(clean)

    def result() -> tuple[str, ...]:
        preferred = (email, phone, address, social)
        if not include_all:
            return preferred
        return preferred + (
            " | ".join(all_emails),
            " | ".join(all_phones),
            " | ".join(all_socials),
        )

    try:
        try:
            await page.goto(website, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            print(f"    goto fail: {e}")
            return result()
        await page.wait_for_timeout(1500)
        await _scroll_to_bottom(page, n=4)
        await page.wait_for_timeout(800)

        e, p, a, s = await _harvest_page(page, site_domain)
        email = email or e
        phone = phone or p
        address = address or a
        social = social or s
        extra_emails, extra_phones, extra_socials = await _harvest_all_contacts(page, site_domain)
        add_values(all_emails, [email] + extra_emails)
        add_values(all_phones, [phone] + extra_phones, phone_values=True)
        add_values(all_socials, [social] + extra_socials)

        if not _contacts_complete(need, email, phone, address):
            # Порядок: ссылки самого сайта → типовые пути → контактные URL из sitemap
            parsed = urlparse(website)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            try:
                from parsers import static_contacts

                discovered = static_contacts.contact_links(await page.content(), page.url or website)
            except Exception:
                discovered = []
            sitemap_urls = await _get_sitemap_contact_urls(base_url)
            pages_to_visit = _pages_to_visit(base_url, discovered, CONTACT_PATHS, sitemap_urls, max_paths)

            for page_url in pages_to_visit:
                if _contacts_complete(need, email, phone, address):
                    break
                try:
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=10000)
                    await page.wait_for_timeout(1000)
                    await _scroll_to_bottom(page, n=2)
                    e, p, a, s = await _harvest_page(page, site_domain)
                    email = email or e
                    phone = phone or p
                    address = address or a
                    social = social or s
                    extra_emails, extra_phones, extra_socials = await _harvest_all_contacts(page, site_domain)
                    add_values(all_emails, [e] + extra_emails)
                    add_values(all_phones, [p] + extra_phones, phone_values=True)
                    add_values(all_socials, [s] + extra_socials)
                except Exception:
                    continue

    except Exception:
        pass

    add_values(all_emails, [email])
    add_values(all_phones, [phone], phone_values=True)
    add_values(all_socials, [social])
    return result()


async def _browser_phase(rows: list[dict], indices: list[int], settings: EnrichSettings, flush) -> int:
    """Обход сайтов браузером (остаток после статики). Возвращает число посещённых сайтов."""
    visited = 0
    since_flush = 0
    by_host: dict[str, tuple[str, ...]] = {}
    empty = ("", "", "", "", "", "", "")
    need = frozenset({"email"}) if settings.email_only else None
    async with async_playwright() as p:
        browser, context = await create_browser_context(p, headless=True)
        page = await context.new_page()
        for i in indices:
            row = rows[i]
            if settings.max_sites and visited >= settings.max_sites:
                print(f"\n[email_finder] достигнут лимит ENRICH_MAX_SITES={settings.max_sites}, "
                      f"остальные записи — в следующем прогоне")
                break
            if settings.site_finder and not (row.get("website") or "").strip():
                found = find_website(row.get("name", ""), row.get("city", ""))
                if found:
                    row["website"] = found
                    print(f"[site_finder] {row.get('name')} → {found}")
            if not (row.get("website") or "").strip():
                continue
            needs = _row_needs(row)
            if not (needs["email"] if settings.email_only else any(needs.values())):
                continue
            host = host_of(row["website"])
            result = by_host.get(host)
            fresh = result is None
            if fresh:
                visited += 1
                print(f"  [{i + 1}/{len(rows)}] {row.get('name','?')} → {row['website']}")
                try:
                    result = await enrich_from_website(
                        page, row["website"], include_all=True, need=need, max_paths=settings.max_paths
                    )
                except Exception as e:
                    print(f"    ошибка: {e}")
                    result = empty
                by_host[host] = result
            _apply_browser_result(row, result, needs)

            # VK fallback: email не найден, а в записи есть страница VK
            if not row.get("email") and "vk.com" in (row.get("social") or ""):
                try:
                    vk_email = await extract_email_from_vk_async(row["social"])
                    vk_email, _all = sanitize_row_emails(vk_email, "", host)
                    if vk_email:
                        row["email"] = vk_email
                        print(f"[vk_email] {row.get('name')} → {vk_email}")
                except Exception:
                    pass

            since_flush += 1
            if since_flush >= 25:  # каждые ~25 сайтов сохраняем прогресс на диск
                flush()
                since_flush = 0
            if fresh:
                await asyncio.sleep(random.uniform(1.2, 2.5))
        await browser.close()
    return visited


async def run_enrichment(input_csv: str):
    if not os.path.exists(input_csv):
        print(f"Файл не найден: {input_csv}")
        return None

    # читаем CSV — пробуем оба разделителя
    rows = []
    for delim in (";", ","):
        try:
            with open(input_csv, encoding="utf-8-sig") as f:
                sample = f.read(2048)
                f.seek(0)
                if delim in sample:
                    reader = csv.DictReader(f, delimiter=delim)
                    rows = list(reader)
                    if rows and "name" in rows[0]:
                        break
        except Exception:
            continue

    if not rows:
        print(f"Не удалось прочитать CSV: {input_csv}")
        return None

    from parsers import static_contacts

    settings = EnrichSettings.from_env()
    order = _visit_order(rows, settings)
    print(
        f"\n=== Email Finder: к обработке {len(order)}/{len(rows)} объектов "
        f"(статика: {'вкл' if settings.static else 'выкл'}, только без email: "
        f"{'да' if settings.email_only else 'нет'}, лимит браузера за прогон: "
        f"{settings.max_sites or 'нет'}, site_finder: {'вкл' if settings.site_finder else 'выкл'}) ==="
    )

    # Заранее подбираем client_type для строк без него (CSV из старой схемы).
    try:
        from utils.categories import normalize as _norm_cat
        for r in rows:
            if not r.get("client_type"):
                r["client_type"] = _norm_cat(r.get("category", ""))
    except Exception:
        pass

    os.makedirs("output", exist_ok=True)

    def _flush_csv() -> None:
        """Полный rewrite файла — атомарно через tmp + rename, чтобы при kill -9
        не остался полуписаный CSV. Вызывается каждые N обработанных строк
        и в финальном finally."""
        tmp = OUTPUT_FILE + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f, fieldnames=FIELDS,
                delimiter=CSV_DELIMITER, quoting=csv.QUOTE_ALL,
                extrasaction="ignore", restval="",
            )
            writer.writeheader()
            writer.writerows(_csv_safe_row(row) for row in rows)
        os.replace(tmp, OUTPUT_FILE)

    # Сразу делаем стартовый flush — даже если процесс умрёт на первом сайте,
    # файл существует и пригоден к чтению.
    _flush_csv()

    try:
        static_results: dict = {}
        if settings.static:
            sites = [rows[i]["website"] for i in order if (rows[i].get("website") or "").strip()]
            if sites:
                started = time.monotonic()
                static_results = await static_contacts.static_prepass(
                    sites, parallel=settings.static_parallel
                )
                applied = sum(
                    _apply_static(rows[i], static_results.get(host_of(rows[i].get("website", ""))))
                    for i in order
                )
                dead = sum(1 for result in static_results.values() if result.kind == "dead")
                print(f"[static] сайтов {len(static_results)}, email найден у {applied} строк, "
                      f"недоступны {dead}, {time.monotonic() - started:.0f} с")
                _flush_csv()

        browser_order = [
            i for i in order
            if _needs_browser(rows[i], static_results.get(host_of(rows[i].get("website", ""))), settings)
        ]
        print(f"[browser] к обходу браузером: {len(browser_order)}")
        if browser_order:
            await _browser_phase(rows, browser_order, settings, _flush_csv)
    finally:
        # Гарантированный финальный flush — даже если поймали Exception или TERM.
        try:
            _flush_csv()
        except Exception as e:
            print(f"[email_finder] финальный flush упал: {e}")

    print(f"\n✅ Обогащённый файл: {OUTPUT_FILE}")
    return OUTPUT_FILE
