import pytest

from config import segments as seg


def test_twenty_unique_segments_with_all_search_inputs():
    keys = [s.key for s in seg.SEGMENTS]
    assert len(keys) == len(set(keys)) == 20
    for s in seg.SEGMENTS:
        assert s.osm_tags and s.yandex_queries and s.vk_keywords and s.aliases, s.key


@pytest.mark.parametrize("attr", ["aliases", "vk_keywords", "osm_tags"])
def test_search_inputs_are_unique_across_segments(attr):
    owner = {}
    for s in seg.SEGMENTS:
        for value in getattr(s, attr):
            identity = value if attr == "osm_tags" else seg.fold(value)
            assert owner.setdefault(identity, s.key) == s.key, (value, owner[identity], s.key)


def test_every_alias_query_and_keyword_normalizes_to_its_own_segment():
    for s in seg.SEGMENTS:
        for text in (*s.aliases, *s.yandex_queries, *s.vk_keywords, s.key, s.title):
            assert seg.normalize_segment(text) == s.key, (text, s.key)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Автошкола Старт", "obrazovanie"),
        ("Прокат автомобилей", "avto"),
        ("Прокат катамаранов", "turizm"),
        ("Ветеринарная клиника Айболит", "vet"),
        ("Стоматологическая клиника", "medicina"),
        ("Химчистка одежды", "klining_uslugi"),
        ("Винзавод Солнечная долина", "agro_vino"),
        ("Ведущий застройщик Крыма", "nedvizhimost"),
        ("Изготовление мебели", "mebel"),
        ("Пятёрочка", "прочее"),
        ("", "прочее"),
        (None, "прочее"),
    ],
)
def test_normalize_segment_prefers_longest_alias(text, expected):
    assert seg.normalize_segment(text) == expected


def test_osm_segment_and_tag_groups():
    assert seg.osm_segment({"shop": "car_repair"}) == ("avto", "shop=car_repair")
    assert seg.osm_segment({"amenity": "restaurant"}) == ("прочее", "")
    groups = seg.osm_tag_groups()
    assert "car_repair" in groups["shop"]
    assert "estate_agent" in groups["office"]


def test_query_builders_and_titles():
    yandex = seg.yandex_queries()
    assert len(yandex) == 60
    assert ("автосервис {city}", "avto") in yandex
    assert ("грузоперевозки", "logistika") in seg.vk_queries()
    assert seg.segment_title("stroitelstvo") == "Строительство и ремонт"
    assert seg.segment_title("unknown") == "Прочее"
    assert "натяжн" in seg.crawler_triggers()


def test_booking_and_competitor_flags():
    assert seg.SEGMENT_BY_KEY["krasota"].booking_relevant is True
    assert seg.SEGMENT_BY_KEY["torgovlya"].booking_relevant is False
    assert [s.key for s in seg.SEGMENTS if s.competitor] == ["it_svyaz"]
