"""Тесты нормализации category → client_type (HoReCa-таксономия)."""
import pytest

from utils.categories import normalize


@pytest.mark.parametrize("raw, expected", [
    ("ресторан", "ресторан"),
    ("Restaurant", "ресторан"),
    ("кафе", "кафе"),
    ("Cafe", "кафе"),
    ("фаст-фуд", "фастфуд"),
    ("fast_food", "фастфуд"),
    ("бар", "бар"),
    ("паб", "паб"),
    ("ночной клуб", "клуб"),
    ("nightclub", "клуб"),
    ("кофе", "кофейня"),
    ("pizzeria", "пиццерия"),
])
def test_normalize_known_aliases(raw, expected):
    assert normalize(raw) == expected


def test_normalize_empty_returns_default():
    assert normalize("") == "прочее"
    assert normalize(None) == "прочее"


def test_normalize_unknown_returns_default():
    assert normalize("автомойка") == "прочее"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Пекарня", "кондитерская"),
        ("Бургерная", "фастфуд"),
        ("Доставка еды", "фастфуд"),
        ("Суши-бар", "ресторан"),
    ],
)
def test_normalize_extended_horeca_aliases(raw, expected):
    assert normalize(raw) == expected
