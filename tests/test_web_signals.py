import asyncio
import csv
import json
from datetime import datetime, timezone

from utils import web_signals as ws
from utils.storage import FIELDS

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
VIEWPORT = '<meta name="viewport" content="width=device-width">'
MODERN = f"<html><head>{VIEWPORT}</head><body>© 2026 Компания</body></html>"
TILDA = (
    f'<html><head>{VIEWPORT}<link href="https://static.tildacdn.com/css/tilda-grid.css">'
    "</head><body>© 2025</body></html>"
)
NO_VIEWPORT = "<html><head><title>Окна</title></head><body>© 2019 Окна</body></html>"
PARKED = "<html><body>Этот домен припаркован в REG.RU</body></html>"
YCLIENTS = (
    f"<html><head>{VIEWPORT}</head><body>"
    '<script src="https://w123.yclients.com/widgetJS"></script>© 2026</body></html>'
)


def _fake_fetch(pages, calls=None):
    """Fake (status, text) fetcher; an unknown URL is unreachable (connection error)."""
    async def fetch(url, **kwargs):
        if calls is not None:
            calls.append(url)
        value = pages.get(url, OSError("connection refused"))
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return value
        return 200, value
    return fetch


def test_modern_site_has_no_signals():
    assert ws.detect_html_signals(MODERN, booking_relevant=False, now_year=2026) == []


def test_builder_and_outdated_and_mobile():
    assert ws.detect_html_signals(TILDA, booking_relevant=False, now_year=2026) == ["site_builder"]
    assert ws.detect_html_signals(NO_VIEWPORT, booking_relevant=False, now_year=2026) == [
        "no_mobile", "outdated",
    ]


def test_booking_signal_only_for_booking_segments():
    assert "no_online_booking" in ws.detect_html_signals(MODERN, booking_relevant=True, now_year=2026)
    assert "no_online_booking" not in ws.detect_html_signals(MODERN, booking_relevant=False, now_year=2026)
    assert "no_online_booking" not in ws.detect_html_signals(YCLIENTS, booking_relevant=True, now_year=2026)


def test_pitch_follows_priority():
    assert ws.pick_pitch(["site_builder", "no_https"]) == "Модернизация сайта"
    assert ws.pick_pitch(["no_website"]) == "Сайт с нуля"
    assert ws.pick_pitch([]) == "Автоматизация/боты/CRM"


def test_http_only_site_is_no_https_not_dead():
    fetch = _fake_fetch({"https://okna.ru/": OSError("ssl"), "http://okna.ru/": MODERN})
    result = asyncio.run(ws.probe_site("okna.ru", booking_relevant=False, fetch=fetch, now=NOW))
    assert result == {"signals": ["no_https"], "checked_at": "2026-09-24T00:00:00+00:00", "status": "ok"}


def test_dead_and_parked_sites():
    dead = asyncio.run(ws.probe_site("https://dead.ru/about", booking_relevant=False, fetch=_fake_fetch({}), now=NOW))
    parked = asyncio.run(ws.probe_site(
        "parked.ru", booking_relevant=False,
        fetch=_fake_fetch({"https://parked.ru/": PARKED}), now=NOW,
    ))
    assert dead["signals"] == ["site_dead"]
    assert parked["signals"] == ["site_dead"]


def test_social_link_in_website_is_no_website():
    for website in ("https://vk.com/okna_crimea", "https://taplink.cc/okna", ""):
        signals, pitch = ws.row_signals({"website": website}, {})
        assert signals == ["no_website"]
        assert pitch == "Сайт с нуля"


def test_unchecked_and_cached_sites():
    assert ws.row_signals({"website": "https://okna.ru"}, {}) == (["not_checked"], "Автоматизация/боты/CRM")
    cache = {"okna.ru": {"signals": ["site_builder", "no_https"], "checked_at": "x"}}
    assert ws.row_signals({"website": "https://www.okna.ru/"}, cache) == (
        ["no_https", "site_builder"], "Модернизация сайта",
    )


def test_refresh_respects_budget_freshness_and_social_links(tmp_path):
    master = tmp_path / "master_all.csv"
    rows = [
        {"name": "Fresh", "website": "https://fresh.ru", "client_type": "torgovlya"},
        {"name": "Stale", "website": "https://stale.ru", "client_type": "torgovlya"},
        {"name": "New1", "website": "https://new1.ru", "client_type": "krasota"},
        {"name": "New2", "website": "https://new2.ru", "client_type": "torgovlya"},
        {"name": "Social", "website": "https://vk.com/x", "client_type": "torgovlya"},
    ]
    with master.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    cache_path = tmp_path / "web_signals.json"
    cache_path.write_text(json.dumps({
        "fresh.ru": {"signals": [], "checked_at": "2026-09-20T00:00:00+00:00"},
        "stale.ru": {"signals": ["no_mobile"], "checked_at": "2026-07-01T00:00:00+00:00"},
    }), encoding="utf-8")
    calls = []

    stats = asyncio.run(ws.refresh_signals(
        str(master), str(cache_path), max_sites=2,
        fetch=_fake_fetch({"https://new1.ru/": MODERN, "https://new2.ru/": MODERN}, calls),
        now=NOW,
    ))

    assert stats == {"sites": 4, "fresh": 1, "checked": 2, "pending": 1, "outage_suspected": False}
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["fresh.ru"]["checked_at"] == "2026-09-20T00:00:00+00:00"
    assert saved["stale.ru"]["signals"] == ["no_mobile"]
    assert saved["new1.ru"]["signals"] == ["no_online_booking"]
    assert saved["new2.ru"]["signals"] == []
    # Never-checked hosts go first; the expired one waits for the next run.
    assert calls == ["https://new1.ru/", "https://new2.ru/"]


def test_bot_wall_is_not_reported_as_dead():
    fetch = _fake_fetch({"https://guarded.ru/": (403, ""), "http://guarded.ru/": (403, "")})
    result = asyncio.run(ws.probe_site("guarded.ru", booking_relevant=False, fetch=fetch, now=NOW))
    assert "site_dead" not in result["signals"]
    assert result["status"] == "blocked"
    assert ws.row_signals({"website": "https://guarded.ru"}, {"guarded.ru": result}) == (
        ["not_checked"], "Автоматизация/боты/CRM",
    )


def test_dns_failure_and_server_errors_are_dead():
    from utils.net_safety import UnsafeURLError

    dns = UnsafeURLError("DNS resolution failed for gone.ru: getaddrinfo failed")
    gone = asyncio.run(ws.probe_site(
        "gone.ru", booking_relevant=False,
        fetch=_fake_fetch({"https://gone.ru/": dns, "http://gone.ru/": dns}), now=NOW,
    ))
    broken = asyncio.run(ws.probe_site(
        "broken.ru", booking_relevant=False,
        fetch=_fake_fetch({"https://broken.ru/": (503, ""), "http://broken.ru/": (503, "")}), now=NOW,
    ))
    assert gone["signals"] == ["site_dead"]
    assert broken["signals"] == ["site_dead"]


def test_non_html_homepage_is_unknown_not_dead():
    fetch = _fake_fetch({"https://pdf.ru/": (200, ""), "http://pdf.ru/": (200, "")})
    result = asyncio.run(ws.probe_site("pdf.ru", booking_relevant=False, fetch=fetch, now=NOW))
    assert "site_dead" not in result["signals"]
    assert result["status"] == "unknown"


def _write_rows(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def test_dead_entries_are_rechecked_after_a_week_but_ok_entries_last_a_month(tmp_path):
    master = tmp_path / "master_all.csv"
    _write_rows(master, [
        {"name": "Dead", "website": "https://dead.ru", "client_type": "torgovlya", "email": "a@dead.ru"},
        {"name": "Ok", "website": "https://ok.ru", "client_type": "torgovlya", "email": "a@ok.ru"},
    ])
    cache_path = tmp_path / "web_signals.json"
    cache_path.write_text(json.dumps({
        "dead.ru": {"signals": ["site_dead"], "checked_at": "2026-09-14T00:00:00+00:00"},
        "ok.ru": {"signals": [], "checked_at": "2026-09-14T00:00:00+00:00"},
    }), encoding="utf-8")
    calls = []
    asyncio.run(ws.refresh_signals(
        str(master), str(cache_path), fetch=_fake_fetch({"https://dead.ru/": MODERN}, calls), now=NOW,
    ))
    assert calls == ["https://dead.ru/"]


def test_mass_unreachable_batch_is_not_persisted_as_dead(tmp_path):
    master = tmp_path / "master_all.csv"
    _write_rows(master, [
        {"name": f"C{i}", "website": f"https://c{i}.ru", "client_type": "torgovlya", "email": f"a@c{i}.ru"}
        for i in range(10)
    ])
    cache_path = tmp_path / "web_signals.json"
    stats = asyncio.run(ws.refresh_signals(
        str(master), str(cache_path), fetch=_fake_fetch({}), now=NOW,
    ))
    assert stats["outage_suspected"] is True
    assert json.loads(cache_path.read_text(encoding="utf-8")) == {}


def test_budget_prefers_outreach_eligible_never_checked_hosts(tmp_path):
    master = tmp_path / "master_all.csv"
    _write_rows(master, [
        {"name": "NoEmail", "website": "https://a.ru", "client_type": "torgovlya"},
        {"name": "Stale", "website": "https://b.ru", "client_type": "torgovlya", "email": "x@b.ru"},
        {"name": "Fresh", "website": "https://c.ru", "client_type": "torgovlya", "email": "x@c.ru"},
        {"name": "Chain", "website": "https://d.ru", "client_type": "torgovlya", "email": "x@d.ru",
         "quality_flags": "excluded_chain"},
    ])
    cache_path = tmp_path / "web_signals.json"
    cache_path.write_text(json.dumps({
        "b.ru": {"signals": [], "checked_at": "2026-07-01T00:00:00+00:00"},
    }), encoding="utf-8")
    calls = []
    asyncio.run(ws.refresh_signals(
        str(master), str(cache_path), max_sites=2,
        fetch=_fake_fetch({"https://b.ru/": MODERN, "https://c.ru/": MODERN}, calls), now=NOW,
    ))
    assert calls == ["https://c.ru/", "https://b.ru/"]
