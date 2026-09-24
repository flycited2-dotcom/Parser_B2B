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
