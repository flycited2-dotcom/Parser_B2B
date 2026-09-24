from parsers import yandex_maps


def test_coverage_includes_interior_and_resort_cities():
    for city in ("Джанкой", "Белогорск", "Щёлкино", "Форос", "Балаклава"):
        assert city in yandex_maps.CITIES


def test_queries_cover_additional_food_segments():
    query_templates = {query for query, _ in yandex_maps.QUERIES}
    assert "пекарня {city}" in query_templates
    assert "суши {city}" in query_templates
    assert "доставка еды {city}" in query_templates


def test_extract_org_id_uses_terminal_numeric_identifier():
    assert (
        yandex_maps._extract_org_id(
            "https://yandex.ru/maps/org/23_cafe_boulangerie/1281495596/"
        )
        == "1281495596"
    )
    assert (
        yandex_maps._extract_org_id(
            "https://yandex.ru/maps/org/sofram/107315534815/gallery/"
        )
        == "107315534815"
    )
    assert (
        yandex_maps._extract_org_id("https://yandex.ru/maps/org/107315534815/")
        == "107315534815"
    )


def test_extract_org_id_rejects_digits_in_slug_and_short_ids():
    assert (
        yandex_maps._extract_org_id(
            "https://yandex.ru/maps/org/23_cafe_boulangerie/"
        )
        == ""
    )
    assert yandex_maps._extract_org_id("https://yandex.ru/maps/org/cafe/23/") == ""


def test_snapshot_skips_broken_and_duplicate_cards_before_applying_limit():
    rows = [
        {"name": "Без ссылки", "address": "Адрес 0", "hrefs": []},
        {
            "name": "Кафе 1",
            "address": "Адрес 1",
            "hrefs": ["https://yandex.ru/maps/org/cafe/1000001/"],
        },
        {
            "name": "Дубль",
            "address": "Адрес 1",
            "hrefs": ["https://yandex.ru/maps/org/cafe/1000001/"],
        },
        {
            "name": "  Кафе   2 ",
            "address": "  Адрес   2 ",
            "hrefs": ["https://yandex.ru/maps/org/cafe/1000002/?ll=1#map"],
        },
        {
            "name": "Кафе 3",
            "address": "Адрес 3",
            "hrefs": ["https://yandex.ru/maps/org/cafe/1000003/"],
        },
    ]

    result = yandex_maps._normalize_search_snapshot(rows, limit=2)

    assert [item["org_id"] for item in result] == ["1000001", "1000002"]
    assert result[1]["source_url"] == "https://yandex.ru/maps/org/cafe/1000002/"
    assert result[1]["name"] == "Кафе 2"
