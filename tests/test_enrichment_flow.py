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
        browser_results={}, static_results={},
    )
    monkeypatch.setattr(email_finder, "OUTPUT_FILE", str(tmp_path / "out.csv"))
    monkeypatch.setattr(email_finder, "async_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(email_finder.random, "uniform", lambda a, b: 0)

    async def fake_context(playwright, headless=True):
        state.launches += 1
        return FakeBrowser(), FakeContext()

    async def fake_enrich(page, website, include_all=False, **kwargs):
        state.browser_calls.append({"website": website, **kwargs})
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
