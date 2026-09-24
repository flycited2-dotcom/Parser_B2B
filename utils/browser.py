import time
from urllib.parse import urlsplit

from utils.net_safety import validate_public_url, validate_public_url_async


def _make_public_network_guard(cache_ttl: float = 30.0):
    """Build a Playwright route handler that blocks non-public HTTP targets."""
    host_cache: dict[str, float] = {}

    async def guard_public_network(route, request) -> None:
        url = request.url
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme not in {"http", "https"}:
                await route.continue_()
                return
            # This always checks literal IPs/userinfo/scheme synchronously.
            validate_public_url(url, resolve_dns=False)
            hostname = (parts.hostname or "").lower().rstrip(".")
            now = time.monotonic()
            must_resolve = request.is_navigation_request() or host_cache.get(hostname, 0) <= now
            if must_resolve:
                await validate_public_url_async(url)
                host_cache[hostname] = now + cache_ttl
        except Exception as exc:
            if request.is_navigation_request():
                print(f"[browser] blocked unsafe navigation: {url!r} ({exc})")
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    return guard_public_network


async def create_browser_context(playwright, headless=False):
    browser = await playwright.chromium.launch(
        headless=headless,
        # Playwright otherwise disables the Chromium sandbox by default.
        # Production runs use a dedicated non-root system user.
        chromium_sandbox=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-features=VizDisplayCompositor",
        ]
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="ru-RU",
        timezone_id="Europe/Simferopol",
        # Service workers can issue requests outside Playwright routing.
        service_workers="block",
    )

    # Resolve each navigation afresh and cache subresource host checks briefly.
    # Redirects produce new requests and therefore pass through the same guard.
    await context.route("**/*", _make_public_network_guard())
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return browser, context
