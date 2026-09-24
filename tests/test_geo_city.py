"""Тесты определения города по координатам."""
from utils.geo_city import CITY_BBOXES, detect_city_by_coords, normalize_city_name


def test_yalta_center():
    # Координаты выбраны так, чтобы не попасть в bbox микрорайонов
    # (Ливадия 34.130-34.170, Массандра 34.180-34.215). Микрорайоны идут
    # в CITY_BBOXES раньше Ялты, поэтому совпадают первыми.
    assert detect_city_by_coords(44.500, 34.175) == "Ялта"


def test_simferopol_center():
    assert detect_city_by_coords(44.952, 34.102) == "Симферополь"


def test_sevastopol_center():
    assert detect_city_by_coords(44.616, 33.525) == "Севастополь"


def test_open_sea_returns_empty():
    """Точка в Чёрном море — не Крым → пустая строка."""
    assert detect_city_by_coords(43.0, 32.0) == ""


def test_none_returns_empty():
    assert detect_city_by_coords(None, None) == ""


def test_city_aliases_are_canonical():
    assert normalize_city_name("Малореречен.") == "Малореченское"
    assert normalize_city_name("  г. Севастополь ") == "Севастополь"


def test_exact_duplicate_bboxes_do_not_shadow_each_other():
    boxes = [(lat_min, lat_max, lon_min, lon_max) for _, lat_min, lat_max, lon_min, lon_max in CITY_BBOXES]
    assert len(boxes) == len(set(boxes))
    assert detect_city_by_coords(44.5, None) == ""
    assert detect_city_by_coords(None, 34.1) == ""
