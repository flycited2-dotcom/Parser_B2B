"""Статический добор контактов: HTML по HTTP без браузера.

Быстрый путь перед Chromium: главная страница и контактные страницы, найденные по
ссылкам самого сайта (а не угадыванием путей). На пробе 122 сайтов это дало email
на 69 сайтах за минуту, тогда как браузер тратит ~80 секунд на один сайт.
В браузер уходят только сайты, где статика ничего не нашла (JS-вёрстка, бот-защита).

Все запросы идут через safe_http (защита от SSRF, лимит размера, проверка редиректов);
найденные адреса проходят шлюз качества utils.email_quality.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

from config.hosts import host_of, is_non_company_url
from parsers.email_finder import _decode_obfuscated_email, _email_score, pick_emails, pick_phones
from utils.email_quality import sanitize_email
from utils.safe_http import fetch_public_response
from utils.web_signals import DEAD_KINDS, attempt_fetch

# Мёртвым для решения «пробовать ли браузер» считаем только устойчивые сбои: DNS, отказ
# соединения, 404/410. Таймаут, TLS-ошибка и 5xx — «неизвестно»: браузер может загрузить сайт.
STATIC_DEAD_KINDS = frozenset({"unreachable", "error"})
MAX_PAGES = 5  # главная + до 4 контактных страниц
MAX_EMAILS = 8
GUESSED_PATHS = ("contacts", "kontakty")
CONTACT_HINTS = (
    "contact", "kontakt", "контакт", "o-nas", "o_nas", "about", "о нас", "о компании",
    "связ", "реквизит", "как нас найти", "где мы", "обратн", "feedback",
)
_A_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_CF_ATTR_RE = re.compile(r"data-cfemail=[\"']([0-9a-fA-F]{6,})[\"']")
_CF_HREF_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]{6,})")


@dataclass
class StaticResult:
    website: str
    host: str
    kind: str = "unknown"  # ok | blocked | dead | unknown — основание для решения о браузере
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    pages: int = 0

    @property
    def found(self) -> bool:
        return bool(self.emails)


def decode_cfemail(hexstr: str) -> str:
    """Расшифровка Cloudflare email-protection (XOR с ключом в первом байте)."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr) - 1, 2))
    except ValueError:
        return ""


def normalize_html(raw: str) -> str:
    """HTML → текст для regex: HTML-сущности, %40 и расшифрованные Cloudflare-адреса."""
    extra = [decode_cfemail(m) for m in _CF_ATTR_RE.findall(raw)]
    extra += [decode_cfemail(m) for m in _CF_HREF_RE.findall(raw)]
    text = htmllib.unescape(raw)
    if re.search(r"%40", text, re.IGNORECASE):
        text = unquote(text)
    return text + "\n" + "\n".join(value for value in extra if value)


def extract_contacts(raw: str, site_host: str = "") -> tuple[list[str], list[str]]:
    """(email, телефоны): очищенные шлюзом, ранжированные (фирменный домен, метка
    поддомена-филиала, порядок в документе вместо алфавита)."""
    text = normalize_html(raw)
    candidates = pick_emails(text, site_host)
    obfuscated = _decode_obfuscated_email(text)
    if obfuscated:
        candidates.append(obfuscated)
    positions: dict[str, int] = {}
    for candidate in candidates:
        value, _note = sanitize_email(candidate, site_host)
        if not value:
            continue
        found_at = text.find(candidate)
        positions[value] = min(positions.get(value, len(text)), found_at if found_at >= 0 else len(text))
    labels = site_host.split(".")
    branch = labels[0] if len(labels) > 2 and len(labels[0]) >= 4 else ""

    def key(email: str) -> tuple[int, int]:
        bonus = 60 if branch and branch in email.partition("@")[0] else 0
        return -(_email_score(email, site_host) + bonus), positions[email]

    return sorted(positions, key=key)[:MAX_EMAILS], pick_phones(text)


def contact_links(raw: str, base_url: str, limit: int = 4) -> list[str]:
    """Ссылки того же сайта, похожие на контактные (по адресу или тексту ссылки)."""
    origin = urlparse(base_url).netloc.lower().removeprefix("www.")
    found: list[str] = []
    for href, label in _A_RE.findall(raw):
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        target = urljoin(base_url, href.strip()).split("#")[0]
        parts = urlparse(target)
        if parts.scheme not in {"http", "https"} or parts.netloc.lower().removeprefix("www.") != origin:
            continue
        haystack = f"{parts.path.lower()} {_TAG_RE.sub(' ', label).lower()}"
        if any(hint in haystack for hint in CONTACT_HINTS) and target not in found:
            found.append(target)
        if len(found) >= limit:
            break
    return found


async def fetch_static_contacts(
    website: str, *, fetch=fetch_public_response, max_pages: int = MAX_PAGES,
) -> StaticResult:
    raw = (website or "").strip()
    url = raw if "://" in raw else "https://" + raw
    parts = urlparse(url)
    result = StaticResult(website=raw, host=host_of(url))
    kinds: list[str] = []
    html = base = ""
    for entry in dict.fromkeys([url, f"{parts.scheme}://{parts.netloc}/"]):
        kind, body = await attempt_fetch(fetch, entry)
        kinds.append(kind)
        if kind == "ok":
            html, base = body, entry
            break
        if kind == "blocked":
            break  # бот-защита: повторные запросы не помогут
    if not html and parts.scheme == "https" and kinds and all(kind in DEAD_KINDS for kind in kinds):
        entry = f"http://{parts.netloc}/"
        kind, body = await attempt_fetch(fetch, entry)
        kinds.append(kind)
        if kind == "ok":
            html, base = body, entry
    if not html:
        result.kind = (
            "blocked" if "blocked" in kinds
            else "dead" if all(kind in STATIC_DEAD_KINDS for kind in kinds)
            else "unknown"
        )
        return result

    result.kind = "ok"
    result.pages = 1
    emails, phones = extract_contacts(html, result.host)
    if not emails and max_pages > 1:
        origin = f"{urlparse(base).scheme}://{urlparse(base).netloc}/"
        links = contact_links(html, base) or [urljoin(origin, path) for path in GUESSED_PATHS]
        for link in links[: max_pages - 1]:
            kind, body = await attempt_fetch(fetch, link)
            if kind != "ok":
                continue
            result.pages += 1
            more_emails, more_phones = extract_contacts(body, result.host)
            phones = phones or more_phones
            if more_emails:
                emails = more_emails
                break
    result.emails, result.phones = emails, phones[:3]
    return result


async def static_prepass(websites, *, parallel: int = 16, fetch=fetch_public_response) -> dict[str, StaticResult]:
    """Параллельный статический проход: по одному обходу на домен, результат по домену."""
    unique: dict[str, str] = {}
    for website in websites:
        host = host_of(website)
        # Соцсеть/площадка в поле «сайт» — не сайт компании: у разных компаний общий домен,
        # результат одной страницы нельзя раздавать остальным.
        if host and host not in unique and not is_non_company_url(website):
            unique[host] = website
    semaphore = asyncio.Semaphore(max(1, parallel))
    results: dict[str, StaticResult] = {}

    async def one(host: str, website: str) -> None:
        async with semaphore:
            try:
                results[host] = await fetch_static_contacts(website, fetch=fetch)
            except Exception:
                results[host] = StaticResult(website=website, host=host, kind="unknown")

    await asyncio.gather(*(one(host, website) for host, website in unique.items()))
    return results
