"""Find the official website of a HoReCa venue by name and city via DuckDuckGo."""
from __future__ import annotations

import re
import html as html_lib
import urllib.parse
import urllib.request

from utils.net_safety import UnsafeURLError, validate_public_url

_BLACKLIST = {
    "tripadvisor.com", "tripadvisor.ru", "yandex.ru", "google.com",
    "2gis.ru", "2gis.com", "avito.ru",
    "vk.com", "instagram.com", "ok.ru", "otzovik.com", "zoon.ru", "flamp.ru",
    "restoclub.ru", "eda.yandex.ru", "delivery-club.ru", "wikipedia.org",
    "restoran.ru", "tomesto.ru", "afisha.ru", "kudago.com",
    # сам DDG — если попали на anomaly-modal, первая ссылка ведёт на duckduckgo.com.
    # без этого фильтра email_finder уходит туда и записывает error+...@duckduckgo.com.
    "duckduckgo.com", "duckduckgo.org", "html.duckduckgo.com",
}

_RESULT_URL_RE = re.compile(r'<a[^>]+class="[^"]*result__url[^"]*"[^>]*>([^<]+)</a>', re.I)
_HREF_RE = re.compile(r'href="(https?://[^"]+)"', re.I)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect before urllib follows it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def _safe_candidate(url: str) -> str | None:
    try:
        return validate_public_url(html_lib.unescape(url))
    except UnsafeURLError:
        return None


def find_website(name: str, city: str, timeout: int = 15) -> str | None:
    """
    Search DuckDuckGo HTML for the official website of the given venue.
    Returns a URL string or None. Uses only stdlib (no aiohttp needed).
    """
    query = urllib.parse.quote(f"{name} {city} официальный сайт")
    url = f"https://html.duckduckgo.com/html/?q={query}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; HorecaSiteFinder/1.0)"},
    )
    try:
        validate_public_url(url)
        with _SAFE_OPENER.open(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    # Extract result URLs from DDG HTML response
    for match in _RESULT_URL_RE.finditer(html):
        href = match.group(1).strip()
        if not href.startswith("http"):
            href = "https://" + href
        href = _safe_candidate(href)
        if not href:
            continue
        domain = _extract_domain(href)
        if domain and not any(bl in domain for bl in _BLACKLIST):
            return href

    # Fallback: look for any https link not in blacklist
    for match in _HREF_RE.finditer(html):
        href = match.group(1)
        href = _safe_candidate(href)
        if not href:
            continue
        domain = _extract_domain(href)
        if domain and not any(bl in domain for bl in _BLACKLIST):
            return href

    return None


def _extract_domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""
