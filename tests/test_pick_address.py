"""Тесты pick_address и pick_address_from_html (Спринт 2.3)."""
import pytest

from parsers.email_finder import pick_address, pick_address_from_html


@pytest.mark.parametrize("text", [
    "г. Ялта, ул. Морская, 5",
    "город Симферополь, проспект Кирова, 10",
    "пгт. Партенит, ул. Школьная, 1",
    "посёлок Гурзуф, ул. Чехова, 2",
])
def test_real_address_matched(text):
    assert pick_address(text) != ""


@pytest.mark.parametrize("text", [
    "номера расположены на улице города",  # false-positive прошлой версии
    "спортзал на улице рядом",
    "бронируйте номера в отеле",
    "Конференц-зал на 50 человек",
])
def test_garbage_not_matched(text):
    assert pick_address(text) == ""


def test_json_ld_postal_address():
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Hotel","name":"X","address":'
        '{"@type":"PostalAddress","streetAddress":"ул. Морская, 5",'
        '"addressLocality":"Ялта"}}'
        '</script>'
    )
    got = pick_address_from_html(html)
    assert "Ялта" in got
    assert "Морская" in got


def test_json_ld_absent_returns_empty():
    html = "<html><body>no structured data</body></html>"
    assert pick_address_from_html(html) == ""
