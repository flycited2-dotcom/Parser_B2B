import pytest

from parsers import osm


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"amenity": "restaurant"}, "ресторан"),
        ({"amenity": "ice_cream"}, "кондитерская"),
        ({"amenity": "cafe", "cuisine": "coffee_shop"}, "кофейня"),
        ({"shop": "bakery"}, "кондитерская"),
        ({"shop": "coffee"}, "кофейня"),
    ],
)
def test_category_coverage(tags, expected):
    assert osm._category(tags) == expected


def test_query_has_food_amenities_and_shops():
    assert "biergarten" in osm.QUERY
    assert "ice_cream" in osm.QUERY
    assert 'nwr["shop"' in osm.QUERY


def test_all_endpoints_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(osm, "OVERPASS_ENDPOINTS", ["https://invalid.test"])
    monkeypatch.setattr(osm, "http_request", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("boom")))
    with pytest.raises(RuntimeError, match="Overpass"):
        osm._fetch_overpass()
