import asyncio

import pytest

from parsers import yandex_maps


def _details(status="ok", phone="+79780000000"):
    result = yandex_maps._detail_result(status)
    result["phone"] = phone
    result["all_phones"] = phone
    return result


def test_detail_cache_and_budget_bound_network_requests(monkeypatch):
    calls = []

    async def fake_scrape(_context, org_id):
        calls.append(org_id)
        return _details()

    monkeypatch.setattr(yandex_maps, "_scrape_org_page", fake_scrape)
    state = yandex_maps.YandexRunState(detail_limit=2)

    first = asyncio.run(yandex_maps._get_org_details(None, "1000001", state))
    cached = asyncio.run(yandex_maps._get_org_details(None, "1000001", state))
    asyncio.run(yandex_maps._get_org_details(None, "1000002", state))
    exhausted = asyncio.run(yandex_maps._get_org_details(None, "1000003", state))
    cached_after_stop = asyncio.run(
        yandex_maps._get_org_details(None, "1000001", state)
    )

    assert first == cached == cached_after_stop
    assert exhausted["status"] == "detail_budget_exhausted"
    assert calls == ["1000001", "1000002"]
    assert state.detail_requests == 2
    assert state.cache_hits == 2
    assert state.circuit_reason == "detail_budget_exhausted"


def test_captcha_opens_circuit_and_success_resets_failures():
    state = yandex_maps.YandexRunState(
        detail_limit=20, max_consecutive_failures=3, max_captcha_hits=1
    )
    state.record("timeout")
    state.record("ok")
    assert state.consecutive_failures == 0
    assert not state.stopped

    state.record("captcha")
    assert state.stopped
    assert state.circuit_reason == "yandex_captcha"


def test_consecutive_failures_and_failure_ratio_open_circuit():
    consecutive = yandex_maps.YandexRunState(
        detail_limit=20, max_consecutive_failures=3, max_captcha_hits=2
    )
    for _ in range(3):
        consecutive.record("timeout")
    assert consecutive.circuit_reason == "consecutive_detail_failures"

    ratio = yandex_maps.YandexRunState(
        detail_limit=20,
        max_consecutive_failures=20,
        max_captcha_hits=20,
        failure_window=10,
        failure_ratio=0.5,
    )
    for status in ["timeout", "ok"] * 5:
        ratio.record(status)
    assert ratio.circuit_reason == "high_detail_failure_ratio"


def test_high_empty_detail_ratio_opens_circuit():
    state = yandex_maps.YandexRunState(
        detail_limit=20,
        max_consecutive_failures=20,
        max_captcha_hits=20,
        failure_window=10,
        max_empty_ratio=0.8,
    )
    for status in ["empty"] * 8 + ["ok"] * 2:
        state.record(status)
    assert state.circuit_reason == "high_empty_detail_ratio"


class _VirtualizedPage:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0
        self.scrolls = 0

    async def eval_on_selector_all(self, selector, expression):
        assert selector == ".search-snippet-view"
        assert "querySelectorAll('a[href]')" in expression
        current = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return current

    async def evaluate(self, _expression):
        self.scrolls += 1

    async def wait_for_timeout(self, _milliseconds):
        return None


def _raw_card(org_id):
    return {
        "name": f"Кафе {org_id}",
        "address": "Симферополь",
        "hrefs": [f"https://yandex.ru/maps/org/cafe/{org_id}/"],
    }


def test_collect_accumulates_virtualized_cards_when_dom_count_is_constant():
    page = _VirtualizedPage([
        [_raw_card("1000001"), _raw_card("1000002")],
        [_raw_card("1000002"), _raw_card("1000003")],
        [_raw_card("1000003"), _raw_card("1000004")],
    ])

    result = asyncio.run(
        yandex_maps._collect_search_results(page, n=5, limit=4)
    )

    assert [item["org_id"] for item in result] == [
        "1000001", "1000002", "1000003", "1000004",
    ]
    assert page.scrolls == 2


def test_run_applies_city_and_query_offsets(monkeypatch):
    calls = []

    async def fake_process(_context, query, category, city, remaining, state):
        calls.append((query, category, city, remaining, state.detail_limit))
        return 1

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(yandex_maps, "_process_query", fake_process)
    monkeypatch.setattr(yandex_maps.asyncio, "sleep", no_sleep)
    monkeypatch.setenv("MAX_CITIES", "2")
    monkeypatch.setenv("MAX_QUERIES_PER_SOURCE", "1")
    monkeypatch.setenv("MAX_ITEMS_PER_SOURCE", "10")
    monkeypatch.setenv("YANDEX_CITY_OFFSET", "1")
    monkeypatch.setenv("YANDEX_QUERY_OFFSET", "2")

    assert asyncio.run(yandex_maps.run(None)) == 2
    expected_cities = list(yandex_maps._ALL_CITIES[1:3])
    expected_template, expected_category = yandex_maps._ALL_QUERIES[2]
    assert calls == [
        (
            expected_template.format(city=city),
            expected_category,
            city,
            10 - index,
            10,
        )
        for index, city in enumerate(expected_cities)
    ]


def test_run_state_uses_safe_default_and_validates_env(monkeypatch):
    monkeypatch.delenv("YANDEX_MAX_DETAIL_REQUESTS", raising=False)
    assert yandex_maps._new_run_state(max_items=0).detail_limit == 600
    assert yandex_maps._new_run_state(max_items=25).detail_limit == 25

    monkeypatch.setenv("YANDEX_MAX_DETAIL_REQUESTS", "0")
    assert yandex_maps._new_run_state(max_items=25).detail_limit == 25

    monkeypatch.setenv("YANDEX_FAILURE_RATIO", "2")
    with pytest.raises(ValueError, match="YANDEX_FAILURE_RATIO"):
        yandex_maps._new_run_state(max_items=0)


class _Response:
    def __init__(self, status=200):
        self.status = status


class _MissingOrgMarkupPage:
    def __init__(self, org_id):
        self.url = f"https://yandex.ru/maps/org/cafe/{org_id}/"
        self.closed = False

    async def goto(self, _url, **_kwargs):
        return _Response()

    async def wait_for_selector(self, *_args, **_kwargs):
        raise TimeoutError()

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def query_selector(self, _selector):
        return None

    async def close(self):
        self.closed = True


class _Context:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def test_org_url_without_business_marker_is_markup_error():
    page = _MissingOrgMarkupPage("1000001")

    result = asyncio.run(
        yandex_maps._scrape_org_page(_Context(page), "1000001")
    )

    assert result["status"] == "markup_error"
    assert "detail_markup_error" in result["quality_flags"]
    assert page.closed is True


def test_unexpected_detail_exception_is_recorded(monkeypatch):
    async def broken_scrape(_context, _org_id):
        raise RuntimeError("DOM changed")

    monkeypatch.setattr(yandex_maps, "_scrape_org_page", broken_scrape)
    state = yandex_maps.YandexRunState(detail_limit=2)

    result = asyncio.run(
        yandex_maps._get_org_details(None, "1000001", state)
    )

    assert result["status"] == "navigation_error"
    assert "detail_exception" in result["quality_flags"]
    assert state.statuses["navigation_error"] == 1


class _SearchFailurePage:
    def __init__(self, status=200, goto_error=None):
        self.response = _Response(status)
        self.goto_error = goto_error
        self.url = "https://yandex.ru/maps/"

    async def goto(self, _url, **_kwargs):
        if self.goto_error:
            raise self.goto_error
        return self.response

    async def wait_for_selector(self, *_args, **_kwargs):
        raise TimeoutError()

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def query_selector(self, _selector):
        return None

    async def eval_on_selector_all(self, _selector, _expression):
        return []

    async def evaluate(self, _expression):
        return None

    async def close(self):
        return None


def test_search_block_opens_circuit_immediately():
    state = yandex_maps.YandexRunState(detail_limit=10)
    context = _Context(_SearchFailurePage(status=429))

    assert asyncio.run(
        yandex_maps._process_query(
            context, "ресторан Ялта", "ресторан", "Ялта", run_state=state
        )
    ) == 0
    assert state.circuit_reason == "yandex_search_blocked"


def test_repeated_unrecognized_search_pages_open_circuit(monkeypatch):
    monkeypatch.setenv("YANDEX_MAX_SCROLLS", "1")
    state = yandex_maps.YandexRunState(
        detail_limit=10, max_consecutive_failures=3, max_captcha_hits=2
    )
    for _ in range(3):
        context = _Context(_SearchFailurePage())
        asyncio.run(
            yandex_maps._process_query(
                context, "ресторан Ялта", "ресторан", "Ялта", run_state=state
            )
        )

    assert state.circuit_reason == "consecutive_detail_failures"
    assert state.statuses["search_markup_error"] == 3
