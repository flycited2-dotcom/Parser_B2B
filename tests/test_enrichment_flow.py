"""Поток run_enrichment: статика → браузер только для остатка, email-first, бюджет, шлюз."""
import asyncio
import csv
from types import SimpleNamespace

import pytest

from config.hosts import host_of
from parsers import email_finder, static_contacts
from parsers.static_contacts import StaticResult
from utils.storage import FIELDS

ENV_KEYS = (
    "ENRICH_MAX_SITES", "ENRICH_STATIC", "ENRICH_EMAIL_ONLY", "SITE_FINDER", "ENRICH_MAX_PATHS",
    "ENRICH_STATIC_PARALLEL", "ENRICH_PARALLEL", "ENRICH_CACHE", "ENRICH_RECHECK_DAYS",
)
CACHE_PATH = "output/enrich_cache.json"
EMPTY_BROWSER = ("", "", "", "", "", "", "")


def row(name, website, **extra):
    data = {field: "" for field in FIELDS}
    data.update(name=name, website=website, city="Ялта", client_type="stroitelstvo", **extra)
    return data


def static_ok(website, emails=(), phones=(), kind="ok"):
    return StaticResult(website, host_of(website), kind, list(emails), list(phones), 1)


class FakeContext:
    async def new_page(self):
        return object()


class FakeBrowser:
    async def close(self):
        return None


class FakePlaywright:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    state = SimpleNamespace(
        browser_calls=[], static_calls=[], launches=0, vk={},
        browser_results={}, static_results={}, active=0, peak=0, delay=0,
    )
    monkeypatch.setattr(email_finder, "OUTPUT_FILE", str(tmp_path / "out.csv"))
    monkeypatch.setattr(email_finder, "async_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(email_finder.random, "uniform", lambda a, b: 0)

    async def fake_context(playwright, headless=True):
        state.launches += 1
        return FakeBrowser(), FakeContext()

    async def fake_enrich(page, website, include_all=False, **kwargs):
        state.browser_calls.append({"website": website, **kwargs})
        state.active += 1
        state.peak = max(state.peak, state.active)
        if state.delay:
            await asyncio.sleep(state.delay)
        state.active -= 1
        return state.browser_results.get(website, EMPTY_BROWSER)

    async def fake_static(websites, **kwargs):
        state.static_calls.append(list(websites))
        return {host_of(w): state.static_results[host_of(w)] for w in websites if host_of(w) in state.static_results}

    async def fake_vk(url):
        return state.vk.get(url)

    monkeypatch.setattr(email_finder, "create_browser_context", fake_context)
    monkeypatch.setattr(email_finder, "enrich_from_website", fake_enrich)
    monkeypatch.setattr(email_finder, "extract_email_from_vk_async", fake_vk)
    monkeypatch.setattr(static_contacts, "static_prepass", fake_static)

    def run(rows):
        source = tmp_path / "in.csv"
        with source.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";", quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
        asyncio.run(email_finder.run_enrichment(str(source)))
        with (tmp_path / "out.csv").open(newline="", encoding="utf-8-sig") as handle:
            return {item["name"]: item for item in csv.DictReader(handle, delimiter=";")}

    state.run = run
    return state


def test_static_first_email_first_flow(harness):
    harness.static_results = {
        "a.ru": static_ok("https://a.ru", ["info@a.ru", "sales@a.ru"], ["+7 (978) 111-22-33"]),
        "b.ru": static_ok("https://b.ru"),
        "c.ru": static_ok("https://c.ru", kind="dead"),
    }
    harness.browser_results = {"https://b.ru": ("info@b.ru", "", "", "", "info@b.ru", "", "")}

    out = harness.run([
        row("R1", "https://a.ru"),
        row("R2", "https://b.ru"),
        row("R3", "https://c.ru"),
        row("R4", "https://d.ru", email="has@d.ru", all_emails="has@d.ru"),
        row("R5", ""),
    ])

    assert out["R1"]["email"] == "info@a.ru"
    assert out["R1"]["all_emails"] == "info@a.ru | sales@a.ru"
    # CSV-граница нейтрализует ведущий «+» (защита от formula injection)
    assert out["R1"]["phone"].lstrip("'").startswith("+7")
    assert out["R2"]["email"] == "info@b.ru"
    assert out["R3"]["email"] == "" and out["R4"]["email"] == "has@d.ru" and out["R5"]["email"] == ""
    assert harness.static_calls == [["https://a.ru", "https://b.ru", "https://c.ru"]]
    assert [call["website"] for call in harness.browser_calls] == ["https://b.ru"]
    assert harness.browser_calls[0]["need"] == frozenset({"email"})
    assert harness.browser_calls[0]["max_paths"] == 12


def test_browser_is_not_launched_when_static_found_everything(harness):
    harness.static_results = {"a.ru": static_ok("https://a.ru", ["info@a.ru"])}
    out = harness.run([row("R1", "https://a.ru")])
    assert out["R1"]["email"] == "info@a.ru"
    assert harness.launches == 0 and harness.browser_calls == []


def test_static_can_be_disabled_and_dead_filter_then_does_not_apply(harness, monkeypatch):
    monkeypatch.setenv("ENRICH_STATIC", "0")
    harness.run([row("R1", "https://a.ru"), row("R2", "https://b.ru"), row("R3", "https://d.ru", email="x@d.ru")])
    assert harness.static_calls == []
    assert [call["website"] for call in harness.browser_calls] == ["https://a.ru", "https://b.ru"]


def test_legacy_mode_visits_rows_missing_any_field_after_rows_without_email(harness, monkeypatch):
    monkeypatch.setenv("ENRICH_STATIC", "0")
    monkeypatch.setenv("ENRICH_EMAIL_ONLY", "0")
    harness.run([
        row("R1", "https://d.ru", email="x@d.ru"),
        row("R2", "https://b.ru"),
    ])
    assert [call["website"] for call in harness.browser_calls] == ["https://b.ru", "https://d.ru"]
    assert all(call["need"] is None for call in harness.browser_calls)


def test_site_budget_counts_browser_visits_only(harness, monkeypatch):
    monkeypatch.setenv("ENRICH_MAX_SITES", "1")
    harness.static_results = {
        "a.ru": static_ok("https://a.ru", ["info@a.ru"]),
        "b.ru": static_ok("https://b.ru"),
        "c.ru": static_ok("https://c.ru"),
    }
    out = harness.run([row("R1", "https://a.ru"), row("R2", "https://b.ru"), row("R3", "https://c.ru")])
    assert out["R1"]["email"] == "info@a.ru"
    assert [call["website"] for call in harness.browser_calls] == ["https://b.ru"]


def test_browser_results_pass_the_email_quality_gate(harness):
    harness.browser_results = {
        "https://x.ru": ("rating@mail.ru", "", "", "", "rating@mail.ru | %20info@x.ru | support@beget.com", "", ""),
    }
    out = harness.run([row("R1", "https://x.ru")])
    assert out["R1"]["email"] == "info@x.ru"
    assert out["R1"]["all_emails"] == "info@x.ru"


def test_vk_fallback_still_fills_missing_email(harness):
    harness.vk = {"https://vk.com/okna": "shop@okna.ru"}
    out = harness.run([row("R1", "https://x.ru", social="https://vk.com/okna")])
    assert out["R1"]["email"] == "shop@okna.ru"


def test_output_keeps_every_input_row_and_all_columns(harness):
    out = harness.run([row("R1", ""), row("R2", "https://x.ru")])
    assert set(out) == {"R1", "R2"}
    assert list(out["R1"]) == FIELDS


def test_contacts_complete_rules():
    done = email_finder._contacts_complete
    assert done(None, "a@b.ru", "", "") is False  # прежнее поведение: нужны email, телефон и адрес
    assert done(None, "a@b.ru", "+7", "addr") is True
    assert done(frozenset({"email"}), "a@b.ru", "", "") is True
    assert done(frozenset({"email"}), "", "+7", "addr") is False


def test_pages_to_visit_order_cap_and_dedup():
    pages = email_finder._pages_to_visit(
        "https://s.ru", ["https://s.ru/kontakty"], ["/a", "/b", "/c"], ["https://s.ru/map"], 2)
    assert pages == ["https://s.ru/kontakty", "https://s.ru/a", "https://s.ru/b", "https://s.ru/map"]
    assert email_finder._pages_to_visit("https://s.ru", [], ["/a", "/b"], [], None) == [
        "https://s.ru/a", "https://s.ru/b"]
    assert email_finder._pages_to_visit("https://s.ru", ["https://s.ru/a"], ["/a"], [], None) == ["https://s.ru/a"]


def test_settings_defaults_and_overrides(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    defaults = email_finder.EnrichSettings.from_env()
    assert (defaults.max_sites, defaults.static, defaults.email_only, defaults.max_paths) == (400, True, True, 12)
    monkeypatch.setenv("ENRICH_MAX_SITES", "0")
    monkeypatch.setenv("ENRICH_STATIC", "0")
    monkeypatch.setenv("ENRICH_EMAIL_ONLY", "no")
    monkeypatch.setenv("ENRICH_MAX_PATHS", "45")
    monkeypatch.setenv("ENRICH_STATIC_PARALLEL", "bad")
    custom = email_finder.EnrichSettings.from_env()
    assert (custom.max_sites, custom.static, custom.email_only, custom.max_paths) == (0, False, False, 45)
    assert custom.static_parallel == 16  # некорректное значение → по умолчанию


# --- параллельный браузерный этап ------------------------------------------------

def test_parallel_workers_share_one_browser_and_stay_bounded(harness, monkeypatch):
    monkeypatch.setenv("ENRICH_STATIC", "0")
    monkeypatch.setenv("ENRICH_PARALLEL", "3")
    harness.delay = 0.02
    harness.run([row(f"R{i}", f"https://s{i}.ru") for i in range(7)])
    assert harness.launches == 1
    assert harness.peak == 3
    assert len(harness.browser_calls) == 7


def test_same_host_rows_are_visited_once_and_share_the_result(harness, monkeypatch):
    monkeypatch.setenv("ENRICH_STATIC", "0")
    harness.browser_results = {"https://chain.ru/a": ("info@chain.ru", "", "", "", "info@chain.ru", "", "")}
    out = harness.run([row("Filial1", "https://chain.ru/a"), row("Filial2", "https://www.chain.ru/b")])
    assert len(harness.browser_calls) == 1
    assert out["Filial1"]["email"] == out["Filial2"]["email"] == "info@chain.ru"


def test_budget_with_parallel_workers_is_exact(harness, monkeypatch):
    monkeypatch.setenv("ENRICH_STATIC", "0")
    monkeypatch.setenv("ENRICH_PARALLEL", "3")
    monkeypatch.setenv("ENRICH_MAX_SITES", "2")
    harness.delay = 0.01
    harness.run([row(f"R{i}", f"https://s{i}.ru") for i in range(6)])
    assert len(harness.browser_calls) == 2


# --- память между прогонами ------------------------------------------------------

def seed_cache(tmp_path, entries):
    import json
    path = tmp_path / CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def stamp(days_ago=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def test_cache_hit_applies_contacts_without_network_and_stale_entries_are_rechecked(harness, tmp_path):
    seed_cache(tmp_path, {
        "a.ru": {"checked_at": stamp(3), "status": "found", "via": "browser",
                 "contacts": {"email": "info@a.ru", "all_emails": "info@a.ru", "phone": "+7 (978) 111-22-33"}},
        "b.ru": {"checked_at": stamp(3), "status": "none", "via": "browser", "contacts": {}},
        "c.ru": {"checked_at": stamp(30), "status": "none", "via": "browser", "contacts": {}},
    })
    harness.static_results = {"c.ru": static_ok("https://c.ru"), "d.ru": static_ok("https://d.ru")}

    out = harness.run([row("Ra", "https://a.ru"), row("Rb", "https://b.ru"),
                       row("Rc", "https://c.ru"), row("Rd", "https://d.ru")])

    assert out["Ra"]["email"] == "info@a.ru" and out["Ra"]["all_emails"] == "info@a.ru"
    assert out["Rb"]["email"] == ""
    assert harness.static_calls == [["https://c.ru", "https://d.ru"]]
    assert [call["website"] for call in harness.browser_calls] == ["https://c.ru", "https://d.ru"]


def test_cache_is_updated_after_the_run(harness, tmp_path):
    import json
    harness.static_results = {
        "a.ru": static_ok("https://a.ru", ["info@a.ru", "sales@a.ru"], ["+7 (978) 111-22-33"]),
        "b.ru": static_ok("https://b.ru"),
        "c.ru": static_ok("https://c.ru", kind="dead"),
        "d.ru": static_ok("https://d.ru"),
        "e.ru": static_ok("https://e.ru"),
    }
    harness.browser_results = {
        "https://b.ru": ("info@b.ru", "", "", "", "info@b.ru", "", ""),
        "https://d.ru": ("", "+7 (978) 000-00-00", "", "", "", "+7 (978) 000-00-00", ""),  # загрузился, email нет
    }  # e.ru: браузер вернул совсем пусто (сайт не загрузился)

    harness.run([row("Ra", "https://a.ru"), row("Rb", "https://b.ru"), row("Rc", "https://c.ru"),
                 row("Rd", "https://d.ru"), row("Re", "https://e.ru")])

    saved = json.loads((tmp_path / CACHE_PATH).read_text(encoding="utf-8"))
    assert {host: (entry["status"], entry["via"]) for host, entry in saved.items()} == {
        "a.ru": ("found", "static"), "b.ru": ("found", "browser"),
        "c.ru": ("dead", "static"), "d.ru": ("none", "browser"), "e.ru": ("dead", "browser"),
    }
    assert saved["a.ru"]["contacts"]["email"] == "info@a.ru"
    assert saved["a.ru"]["contacts"]["all_emails"] == "info@a.ru | sales@a.ru"
    assert saved["b.ru"]["contacts"]["email"] == "info@b.ru"


def test_cache_can_be_disabled(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("ENRICH_CACHE", "0")
    harness.static_results = {"a.ru": static_ok("https://a.ru", ["info@a.ru"])}
    harness.run([row("Ra", "https://a.ru")])
    assert not (tmp_path / CACHE_PATH).exists()


# --- соцсети и площадки в поле «сайт» ----------------------------------------------

def test_social_and_platform_websites_are_never_scraped_or_shared(harness):
    harness.browser_results = {"https://instagram.com/a": ("owner@gmail.com", "", "", "", "owner@gmail.com", "", "")}
    out = harness.run([
        row("A", "https://instagram.com/a"),
        row("B", "https://instagram.com/b"),
        row("C", "https://vk.com/c"),
        row("D", "https://booking.com/hotel/d"),
        row("E", "https://wa.me/79780000000"),
    ])
    assert harness.static_calls == [] and harness.browser_calls == []
    assert all(item["email"] == "" for item in out.values())


def test_company_sites_are_still_processed_next_to_social_links(harness):
    harness.static_results = {"okna.ru": static_ok("https://okna.ru", ["info@okna.ru"])}
    out = harness.run([row("A", "https://vk.com/okna"), row("B", "https://okna.ru")])
    assert harness.static_calls == [["https://okna.ru"]]
    assert out["B"]["email"] == "info@okna.ru" and out["A"]["email"] == ""


def test_vk_fallback_covers_rows_whose_site_is_a_social_link_or_dead(harness):
    harness.static_results = {"gone.ru": static_ok("https://gone.ru", kind="dead")}
    harness.vk = {"https://vk.com/a": "shop-a@mail.ru", "https://vk.com/b": "shop-b@mail.ru"}
    out = harness.run([
        row("A", "https://instagram.com/a", social="https://vk.com/a"),
        row("B", "https://gone.ru", social="https://vk.com/b"),
    ])
    assert out["A"]["email"] == "shop-a@mail.ru"
    assert out["B"]["email"] == "shop-b@mail.ru"
    assert harness.browser_calls == []
