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
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

from parsers.site_finder import find_website
from parsers.vk_email import extract_email_from_vk_async
from utils.browser import create_browser_context
from utils.csv_safety import neutralize_csv_formula
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


async def enrich_from_website(page, website: str, include_all: bool = False) -> tuple[str, ...]:
    """Return preferred contacts and, optionally, flattened alternatives."""
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

        if not (email and phone and address):
            # Build pages-to-visit list: standard paths + sitemap contact URLs
            parsed = urlparse(website)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            pages_to_visit: list[str] = [urljoin(base_url + "/", p.lstrip("/")) for p in CONTACT_PATHS]
            sitemap_urls = await _get_sitemap_contact_urls(base_url)
            pages_to_visit.extend(u for u in sitemap_urls if u not in pages_to_visit)

            for page_url in pages_to_visit:
                if email and phone and address:
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

    targets = [r for r in rows if r.get("website") and (
        not r.get("email") or not r.get("phone") or not r.get("address") or not r.get("social", ""))]

    # Защита от «вечного» прогона (известная боль hotels_sbor_baza: 12K сайтов
    # × ~36/час = 14 суток, systemd убивал по таймауту на ~10%).
    # ENRICH_MAX_SITES — сколько сайтов максимум посещаем за прогон (0 = без лимита).
    # SITE_FINDER=1 — искать сайт через DuckDuckGo для записей без website
    # (по умолчанию выключено: тысячи DDG-запросов подряд = rate-limit/бан).
    try:
        max_sites = int(os.getenv("ENRICH_MAX_SITES", "400") or 0)
    except ValueError:
        max_sites = 400
    site_finder_on = os.getenv("SITE_FINDER", "0").lower() in ("1", "true", "yes")

    print(f"\n=== Email Finder: {len(targets)}/{len(rows)} объектов на обогащение "
          f"(лимит за прогон: {max_sites or 'нет'}, site_finder: "
          f"{'вкл' if site_finder_on else 'выкл'}) ===")

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

    processed_since_flush = 0
    sites_visited = 0
    FLUSH_EVERY = 25  # каждые ~25 сайтов сохраняем прогресс на диск

    try:
        async with async_playwright() as p:
            browser, context = await create_browser_context(p, headless=True)
            page = await context.new_page()

            for i, row in enumerate(rows):
                if max_sites and sites_visited >= max_sites:
                    print(f"\n[email_finder] достигнут лимит ENRICH_MAX_SITES={max_sites}, "
                          f"остальные записи — в следующем прогоне")
                    break
                if site_finder_on and not (row.get("website") or "").strip():
                    found = find_website(row.get("name", ""), row.get("city", ""))
                    if found:
                        row["website"] = found
                        print(f"[site_finder] {row.get('name')} → {found}")
                if not row.get("website"):
                    continue
                need_email = not row.get("email")
                need_phone = not row.get("phone")
                need_addr = not row.get("address")
                need_social = not row.get("social", "")
                if not (need_email or need_phone or need_addr or need_social):
                    continue

                sites_visited += 1
                print(f"  [{i + 1}/{len(rows)}] {row.get('name','?')} → {row['website']}")
                try:
                    (
                        email, phone, address, social,
                        all_emails, all_phones, all_socials,
                    ) = await enrich_from_website(page, row["website"], include_all=True)
                except Exception as e:
                    print(f"    ошибка: {e}")
                    email = phone = address = social = ""
                    all_emails = all_phones = all_socials = ""

                if need_email and email:
                    row["email"] = email
                    print(f"    email: {email}")
                if need_phone and phone:
                    row["phone"] = phone
                    print(f"    phone: {phone}")
                if need_addr and address:
                    row["address"] = address
                    print(f"    address: {address}")
                if need_social and social:
                    row["social"] = social
                    print(f"    social: {social}")
                if all_emails:
                    row["all_emails"] = _merge_flat_contacts(
                        row.get("all_emails") or row.get("email", ""), all_emails
                    )
                if all_phones:
                    row["all_phones"] = _merge_flat_contacts(
                        row.get("all_phones") or row.get("phone", ""),
                        all_phones,
                        phones=True,
                    )
                if all_socials:
                    row["all_socials"] = _merge_flat_contacts(
                        row.get("all_socials") or row.get("social", ""), all_socials
                    )
                row["all_websites"] = _merge_flat_contacts(
                    row.get("all_websites") or row.get("website", ""),
                    row.get("website", ""),
                )

                # VK fallback: if no email found yet and record has a VK social link
                if not row.get("email") and "vk.com" in (row.get("social") or ""):
                    try:
                        vk_email = await extract_email_from_vk_async(row["social"])
                        if vk_email:
                            row["email"] = vk_email
                            print(f"[vk_email] {row.get('name')} → {vk_email}")
                    except Exception:
                        pass

                processed_since_flush += 1
                if processed_since_flush >= FLUSH_EVERY:
                    _flush_csv()
                    processed_since_flush = 0

                await asyncio.sleep(random.uniform(1.2, 2.5))

            await browser.close()
    finally:
        # Гарантированный финальный flush — даже если поймали Exception или TERM.
        try:
            _flush_csv()
        except Exception as e:
            print(f"[email_finder] финальный flush упал: {e}")

    print(f"\n✅ Обогащённый файл: {OUTPUT_FILE}")
    return OUTPUT_FILE
