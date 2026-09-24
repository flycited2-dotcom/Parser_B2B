"""Extract email from public VK group pages."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

from utils.net_safety import UnsafeURLError
from utils.safe_http import fetch_public_text

_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}")
_VK_SKIP = {"vk.com", "vkontakte.ru", "noreply"}
_VK_HOSTS = {"vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru", "m.vk.ru"}


async def extract_email_from_vk_async(vk_url: str, timeout: int = 20) -> str | None:
    """Fetch an exact VK host through the public-only HTTP client."""
    hostname = (urlsplit(vk_url or "").hostname or "").casefold().rstrip(".")
    if hostname not in _VK_HOSTS:
        return None
    try:
        html = await fetch_public_text(
            vk_url,
            timeout_seconds=timeout,
            max_response_bytes=2 * 1024 * 1024,
            allowed_hosts=_VK_HOSTS,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )
    except (UnsafeURLError, asyncio.TimeoutError):
        return None
    if not html:
        return None

    # Strip script/style tags to avoid matching obfuscated JS strings.
    html_clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    emails = _EMAIL_RE.findall(html_clean)
    for email in emails:
        domain = email.split("@")[-1].lower()
        if not any(skip in domain for skip in _VK_SKIP):
            return email
    return None


def extract_email_from_vk(vk_url: str, timeout: int = 20) -> str | None:
    """
    Fetch a public VK group/user page and extract the first email from the text.
    Returns email string or None. Never raises.
    """
    # Backward-compatible synchronous entry point.  Async parser code should
    # call ``extract_email_from_vk_async`` to avoid blocking its event loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(extract_email_from_vk_async(vk_url, timeout))
        except Exception:
            return None
    else:
        # Never nest asyncio.run() in a live loop; retain the old never-raises
        # contract and let async callers use the explicit coroutine.
        return None
