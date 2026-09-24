import pytest

from utils.crimea_boundary import is_in_crimea


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (44.4952, 34.1663),  # Ялта
        (44.9521, 34.1024),  # Симферополь
        (44.6167, 33.5254),  # Севастополь
        (45.3562, 36.4674),  # Керчь
        (45.1904, 33.3674),  # Евпатория
    ],
)
def test_known_crimea_points(lat, lon):
    assert is_in_crimea(lat, lon)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (46.6354, 32.6169),  # Херсон
        (45.0355, 38.9753),  # Краснодар
        (44.8000, 32.2000),  # море западнее полуострова
        (None, 34.0),
    ],
)
def test_outside_points(lat, lon):
    assert not is_in_crimea(lat, lon)
