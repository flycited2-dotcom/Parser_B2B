import pytest

from config.segments import SEGMENTS
from parsers import osm


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"shop": "car_repair"}, "avto"),
        ({"office": "estate_agent"}, "nedvizhimost"),
        ({"amenity": "dentist"}, "medicina"),
        ({"craft": "carpenter"}, "mebel"),
        ({"amenity": "restaurant"}, "прочее"),
    ],
)
def test_category_by_segment_tags(tags, expected):
    assert osm._category(tags) == expected


def test_raw_category_is_matched_tag():
    assert osm._raw_category({"shop": "car_repair", "name": "СТО"}) == "shop=car_repair"


def test_query_covers_every_segment_tag_and_no_food():
    for segment in SEGMENTS:
        for key, value in segment.osm_tags:
            assert f'nwr["{key}"' in osm.QUERY
            assert value in osm.QUERY
    assert "restaurant" not in osm.QUERY
    assert "fast_food" not in osm.QUERY


def test_all_endpoints_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(osm, "OVERPASS_ENDPOINTS", ["https://invalid.test"])
    monkeypatch.setattr(osm, "http_request", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("boom")))
    with pytest.raises(RuntimeError, match="Overpass"):
        osm._fetch_overpass()


def _clear_overpass_env(monkeypatch):
    for key in ("OVERPASS_ENDPOINTS", "OVERPASS_TIMEOUT", "OVERPASS_TOTAL_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)


def test_default_endpoints_keep_original_order_and_add_mirrors(monkeypatch):
    _clear_overpass_env(monkeypatch)
    endpoints = osm._overpass_endpoints()
    assert endpoints[:3] == [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
    ]
    assert len(endpoints) >= 5 and len(set(endpoints)) == len(endpoints)


def test_endpoints_can_be_overridden_from_environment(monkeypatch):
    _clear_overpass_env(monkeypatch)
    monkeypatch.setenv("OVERPASS_ENDPOINTS", "https://a.test/api; https://b.test/api,https://c.test/api")
    assert osm._overpass_endpoints() == ["https://a.test/api", "https://b.test/api", "https://c.test/api"]


def test_fetch_falls_through_to_a_working_mirror_and_reports_progress(monkeypatch, capsys):
    import json
    from urllib.error import URLError

    _clear_overpass_env(monkeypatch)
    monkeypatch.setattr(osm, "OVERPASS_ENDPOINTS", ["https://one.test/api", "https://two.test/api", "https://three.test/api"])
    calls = []

    def fake_request(req, timeout=0, retries=3):
        calls.append((req.full_url, timeout, retries))
        if "three" not in req.full_url:
            raise URLError("tls handshake failed")
        return json.dumps({"elements": [{"id": 1}, {"id": 2}]}).encode("utf-8")

    monkeypatch.setattr(osm, "http_request", fake_request)
    assert osm._fetch_overpass() == [{"id": 1}, {"id": 2}]
    assert [url for url, _timeout, _retries in calls] == [
        "https://one.test/api", "https://two.test/api", "https://three.test/api"]
    assert all(timeout == 240 for _url, timeout, _retries in calls)
    assert all(retries == 0 for _url, _timeout, retries in calls)  # зеркала — это и есть повторы
    out = capsys.readouterr().out
    assert "зеркало 1/3" in out and "one.test" in out and "сбой" in out
    assert "ответ от three.test" in out and "объектов: 2" in out


def test_total_wait_budget_stops_trying_more_mirrors(monkeypatch, capsys):
    _clear_overpass_env(monkeypatch)
    monkeypatch.setenv("OVERPASS_TOTAL_TIMEOUT", "900")
    monkeypatch.setattr(osm, "OVERPASS_ENDPOINTS", [f"https://m{i}.test/api" for i in range(6)])
    clock = {"now": 0.0}
    calls = []
    monkeypatch.setattr(osm.time, "monotonic", lambda: clock["now"])

    timeouts = []

    def slow_failure(req, timeout=0, retries=3):
        calls.append(req.full_url)
        timeouts.append((timeout, retries))
        clock["now"] += 400
        raise TimeoutError("read timed out")

    monkeypatch.setattr(osm, "http_request", slow_failure)
    with pytest.raises(RuntimeError, match="Overpass"):
        osm._fetch_overpass()
    assert len(calls) == 3  # после ~1200 с бюджет 900 с исчерпан
    # ожидание одного зеркала не превышает остаток общего бюджета, повторов внутри зеркала нет
    assert timeouts == [(240, 0), (240, 0), (100, 0)]
    assert "общий лимит ожидания" in capsys.readouterr().out
