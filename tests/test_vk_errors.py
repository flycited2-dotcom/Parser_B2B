import asyncio

import pytest

from parsers import vk_groups

FLOOD = {"error": {"error_code": 9, "error_msg": "Flood control"}}


@pytest.fixture
def vk_env(monkeypatch):
    monkeypatch.setenv("VK_TOKEN", "test-token")
    monkeypatch.setenv("MAX_CITIES", "1")
    monkeypatch.setenv("MAX_QUERIES_PER_SOURCE", "2")
    monkeypatch.setenv("MAX_ITEMS_PER_SOURCE", "0")
    monkeypatch.setattr(vk_groups.time, "sleep", lambda _seconds: None)


def _fake_api(monkeypatch, search, get_by_id):
    def fake_call(method, token, **params):
        return search(params) if method == "groups.search" else get_by_id(params)

    monkeypatch.setattr(vk_groups, "_call", fake_call)


def test_all_searches_failing_raises_with_vk_code_and_message(vk_env, monkeypatch):
    _fake_api(monkeypatch, lambda p: FLOOD, lambda p: {"response": []})

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(vk_groups.run(None))

    assert "code=9" in str(exc.value)
    assert "Flood control" in str(exc.value)


def test_partial_search_failure_is_logged_but_does_not_raise(vk_env, monkeypatch, capsys):
    answers = iter([{"response": {"items": []}}, FLOOD])
    _fake_api(monkeypatch, lambda p: next(answers), lambda p: {"response": []})

    asyncio.run(vk_groups.run(None))

    out = capsys.readouterr().out
    assert "code=9" in out
    assert "Flood control" in out


def test_all_details_requests_failing_raises(vk_env, monkeypatch):
    _fake_api(
        monkeypatch,
        lambda p: {"response": {"items": [{"id": 101}]}},
        lambda p: {"error": {"error_code": 6, "error_msg": "Too many requests per second"}},
    )

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(vk_groups.run(None))

    assert "code=6" in str(exc.value)
    assert "groups.getById" in str(exc.value)
