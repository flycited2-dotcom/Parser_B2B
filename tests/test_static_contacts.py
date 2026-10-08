import asyncio

from parsers import static_contacts as sc
from utils.net_safety import UnsafeURLError


def cf_encode(email: str, key: int = 0x2A) -> str:
    return f"{key:02x}" + "".join(f"{ord(char) ^ key:02x}" for char in email)


def fake_fetch(pages, calls=None):
    """pages: url → html | (status, html) | Exception. Неизвестный URL — отказ соединения."""
    async def fetch(url, **kwargs):
        if calls is not None:
            calls.append(url)
        value = pages.get(url, OSError("connection refused"))
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, tuple) else (200, value)
    return fetch


def test_decode_cfemail_roundtrip():
    assert sc.decode_cfemail(cf_encode("tender@tk-kit.com")) == "tender@tk-kit.com"
    assert sc.decode_cfemail("zz") == ""


def test_extract_reads_cloudflare_entity_and_percent_encoded_addresses():
    html = (
        f'<a href="/cdn-cgi/l/email-protection#{cf_encode("tender@tk-kit.com")}">[email&#160;protected]</a>'
        '<span>info&#64;site.ru</span>'
        '<a href="mailto:sales%40site.ru">write</a>'
    )
    emails, _phones = sc.extract_contacts(html, "site.ru")
    assert set(emails) == {"info@site.ru", "sales@site.ru", "tender@tk-kit.com"}
    assert emails[-1] == "tender@tk-kit.com"  # адрес на домене сайта выше чужого


def test_extract_drops_junk_and_reads_phones():
    html = "<p>rating@mail.ru support@beget.com info@okna.ru +7 978 111-22-33</p>"
    emails, phones = sc.extract_contacts(html, "okna.ru")
    assert emails == ["info@okna.ru"]
    assert len(phones) == 1


def test_ranking_prefers_branch_label_then_document_order():
    html = "<p>abakan@top-academy.ru evpatoria@top-academy.ru kerch@top-academy.ru</p>"
    emails, _ = sc.extract_contacts(html, "evpatoria.top-academy.ru")
    assert emails[0] == "evpatoria@top-academy.ru"
    emails, _ = sc.extract_contacts("<p>zeta@site.ru alpha@site.ru</p>", "site.ru")
    assert emails == ["zeta@site.ru", "alpha@site.ru"]


def test_contact_links_same_origin_with_hints_only():
    html = (
        '<a href="/kontakty">Контакты</a><a href="https://other.ru/contacts">x</a>'
        '<a href="mailto:x@y.ru">m</a><a href="/about">О компании</a><a href="/catalog">Каталог</a>'
        '<a href="/kontakty">Контакты</a>'
    )
    assert sc.contact_links(html, "https://site.ru/") == ["https://site.ru/kontakty", "https://site.ru/about"]
    assert sc.contact_links(html, "https://site.ru/", limit=1) == ["https://site.ru/kontakty"]


def run(coro):
    return asyncio.run(coro)


def test_follows_discovered_contact_page_and_stops_after_email():
    calls = []
    pages = {
        "https://site.ru/": '<a href="/kontakty">Контакты</a>',
        "https://site.ru/kontakty": "<p>info@site.ru</p>",
        "https://site.ru/about": "<p>other@site.ru</p>",
    }
    result = run(sc.fetch_static_contacts("https://site.ru/", fetch=fake_fetch(pages, calls)))
    assert result.kind == "ok" and result.emails == ["info@site.ru"] and result.pages == 2
    assert calls == ["https://site.ru/", "https://site.ru/kontakty"]


def test_home_page_with_email_needs_a_single_request():
    calls = []
    result = run(sc.fetch_static_contacts(
        "https://site.ru/", fetch=fake_fetch({"https://site.ru/": "<p>info@site.ru</p>"}, calls)))
    assert result.emails == ["info@site.ru"] and calls == ["https://site.ru/"]


def test_guesses_standard_paths_when_home_has_no_links():
    calls = []
    pages = {
        "https://site.ru/": "<p>нет ссылок</p>",
        "https://site.ru/contacts": (404, ""),
        "https://site.ru/kontakty": "<p>info@site.ru</p>",
    }
    result = run(sc.fetch_static_contacts("https://site.ru/", fetch=fake_fetch(pages, calls)))
    assert result.emails == ["info@site.ru"]
    assert calls == ["https://site.ru/", "https://site.ru/contacts", "https://site.ru/kontakty"]


def test_deep_entry_url_falls_back_to_site_root():
    pages = {"https://site.ru/shop/": (404, ""), "https://site.ru/": "<p>info@site.ru</p>"}
    result = run(sc.fetch_static_contacts("https://site.ru/shop/", fetch=fake_fetch(pages)))
    assert result.kind == "ok" and result.emails == ["info@site.ru"]


def test_http_fallback_when_https_is_broken():
    pages = {"https://site.ru/": OSError("ssl"), "http://site.ru/": "<p>info@site.ru</p>"}
    result = run(sc.fetch_static_contacts("https://site.ru/", fetch=fake_fetch(pages)))
    assert result.kind == "ok" and result.emails == ["info@site.ru"]


def test_result_kinds_for_browser_fallback_decisions():
    blocked = run(sc.fetch_static_contacts(
        "https://guarded.ru/", fetch=fake_fetch({"https://guarded.ru/": (403, ""), "http://guarded.ru/": (403, "")})))
    dns = UnsafeURLError("DNS resolution failed for gone.ru: getaddrinfo failed")
    dead = run(sc.fetch_static_contacts("https://gone.ru/", fetch=fake_fetch({})))
    dns_dead = run(sc.fetch_static_contacts(
        "https://gone.ru/", fetch=fake_fetch({"https://gone.ru/": dns, "http://gone.ru/": dns})))
    js_only = run(sc.fetch_static_contacts(
        "https://spa.ru/", fetch=fake_fetch({"https://spa.ru/": "<div id=app></div>"})))
    not_html = run(sc.fetch_static_contacts(
        "https://pdf.ru/", fetch=fake_fetch({"https://pdf.ru/": (200, ""), "http://pdf.ru/": (200, "")})))
    assert (blocked.kind, dead.kind, dns_dead.kind, js_only.kind, not_html.kind) == (
        "blocked", "dead", "dead", "ok", "unknown")
    assert js_only.emails == [] and not js_only.found


def test_prepass_dedupes_hosts_and_bounds_parallelism():
    active = 0
    peak = 0

    async def fetch(url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return 200, f"<p>info@{url.split('/')[2]}</p>"

    sites = [f"https://s{i}.ru/" for i in range(5)] + ["https://www.s0.ru/page", "https://s1.ru/"]
    results = run(sc.static_prepass(sites, parallel=2, fetch=fetch))
    assert sorted(results) == [f"s{i}.ru" for i in range(5)]
    assert results["s3.ru"].emails == ["info@s3.ru"]
    assert peak <= 2


def test_prepass_ignores_social_and_platform_urls():
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return 200, "<p>info@site.ru</p>"

    sites = ["https://instagram.com/a", "https://vk.com/b", "https://booking.com/h/c", "https://site.ru/"]
    results = run(sc.static_prepass(sites, fetch=fetch))
    assert sorted(results) == ["site.ru"]
    assert calls == ["https://site.ru/"]


def test_only_stable_failures_count_as_dead_for_the_browser_decision():
    import ssl

    def kind_for(error_or_status):
        pages = {"https://x.ru/": error_or_status, "http://x.ru/": error_or_status}
        return run(sc.fetch_static_contacts("https://x.ru/", fetch=fake_fetch(pages))).kind

    assert kind_for(TimeoutError("slow")) == "unknown"  # браузер может загрузить
    assert kind_for(ssl.SSLError("incomplete chain")) == "unknown"
    assert kind_for((503, "")) == "unknown"
    assert kind_for(OSError("connection refused")) == "dead"
    assert kind_for((404, "")) == "dead"
    assert kind_for(UnsafeURLError("DNS resolution failed for x.ru")) == "dead"
