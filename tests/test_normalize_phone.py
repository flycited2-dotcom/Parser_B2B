"""Тесты нормализации телефона."""
import pytest

from utils.storage import normalize_phone


@pytest.mark.parametrize("raw, expected", [
    ("8 800 123 45 67", "+7 (800) 123-45-67"),
    ("+7(800)1234567", "+7 (800) 123-45-67"),
    ("78001234567", "+7 (800) 123-45-67"),
    ("8 (800) 123-45-67", "+7 (800) 123-45-67"),
    # 10 цифр без префикса — добавить +7
    ("8001234567", "+7 (800) 123-45-67"),
])
def test_normalize_phone_canonical(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    None,
])
def test_normalize_phone_empty(raw):
    assert normalize_phone(raw) == ""


def test_normalize_phone_garbage_returns_stripped_original():
    # 5 цифр — не телефон, возвращаем как есть (stripped)
    assert normalize_phone("12345") == "12345"


def test_normalize_phone_with_letters():
    # буквы рядом с числами игнорируются, корректные 11 цифр → нормализуется
    assert normalize_phone("тел: 8-800-123-45-67") == "+7 (800) 123-45-67"
