from config.segments import SEGMENTS
from parsers import vk_groups
from parsers.vk_groups import (
    QUERIES, QUERY_SEGMENT, _category_for, _group_relevance, _pick_address,
)
from utils.quality import VK_QUARANTINE_FLAGS


def test_queries_cover_every_segment_keyword():
    assert set(QUERIES) == {kw for s in SEGMENTS for kw in s.vk_keywords}
    assert QUERY_SEGMENT["автосервис"] == "avto"


def test_business_group_passes_gate():
    score, flags = _group_relevance(
        {"name": "Автосервис Мотор", "activity": "Автомобили", "description": "Ремонт и ТО"},
        ["автосервис"],
    )
    assert score >= 0.7
    assert not set(flags) & VK_QUARANTINE_FLAGS
    assert "manual_review" not in flags


def test_group_without_segment_signal_is_quarantined():
    score, flags = _group_relevance(
        {"name": "Мы из Ялты", "activity": "Сообщество", "description": ""},
        ["ремонт квартир"],
    )
    assert score < 0.5
    assert "vk_no_primary_segment_signal" in flags
    assert "manual_review" in flags


def test_noise_community_is_quarantined_even_with_segment_word():
    score, flags = _group_relevance(
        {"name": "Барахолка Симферополь одежда", "activity": "Сообщество"},
        ["магазин одежды"],
    )
    assert score < 0.5
    assert "vk_noise_primary" in flags


def test_category_prefers_activity_then_name_then_query():
    assert _category_for({"activity": "Салон красоты", "name": "Лилия"}, ["фитнес"]) == "krasota"
    assert _category_for({"activity": "Сообщество", "name": "Натяжные потолки Ялта"}, []) == "stroitelstvo"
    assert _category_for({"activity": "Сообщество", "name": "Мастерская Ивана"}, ["грузоперевозки"]) == "logistika"


def test_vk_address_is_extracted():
    group = {"addresses": [{"city": {"title": "Ялта"}, "address": "ул. Морская, 1"}]}
    assert _pick_address(group) == "Ялта, ул. Морская, 1"


def test_dry_run_never_emits_invalid_token_alert(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    vk_groups._token_alert_sent = False
    vk_groups._maybe_alert_token_dead({"error_code": 5, "error_msg": "invalid access token"})
    assert vk_groups._token_alert_sent is False
