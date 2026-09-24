"""Тесты VK _clean_site (нормализация URL из contacts)."""
from parsers.vk_groups import _clean_site


def test_valid_site_normalized():
    assert _clean_site("cafe.ru") == "https://cafe.ru"
    assert _clean_site("https://cafe.ru/") == "https://cafe.ru"
    assert _clean_site("http://cafe.ru/contacts") == "http://cafe.ru/contacts"


def test_empty_returns_empty():
    assert _clean_site("") == ""
    assert _clean_site(None) == ""


def test_garbage_returns_empty():
    # Пустые/whitespace-only — нет токена для парсинга → ""
    assert _clean_site("   ") == ""
    assert _clean_site("\n\n") == ""


def test_extra_whitespace_stripped():
    # VK иногда даёт «cafe.ru | подробнее» — берём первый токен
    assert _clean_site("  cafe.ru extra text ") == "https://cafe.ru"
