from parsers import vk_groups
from parsers.vk_groups import _category_for, _group_relevance, _pick_address


def test_food_group_scores_above_barbershop():
    food, food_flags = _group_relevance(
        {"name": "Кафе Маяк", "activity": "Ресторан", "description": "Меню и доставка еды"},
        ["кафе"],
    )
    noise, noise_flags = _group_relevance(
        {"name": "Барбер клуб", "activity": "Барбершоп", "description": "Стрижки"},
        ["бар"],
    )
    assert food >= 0.7
    assert noise < 0.5
    assert "manual_review" in noise_flags
    assert "manual_review" not in food_flags


def test_vk_only_brand_without_primary_food_signal_is_not_outreach_ready():
    score, flags = _group_relevance(
        {
            "name": "Villa Elena Hotel",
            "activity": "Гостиница",
            "description": "На территории работает ресторан и бар",
        },
        ["ресторан"],
    )
    assert score < 0.7
    assert "vk_no_primary_food_signal" in flags
    assert "vk_accommodation_primary" in flags


def test_restaurant_at_hotel_keeps_primary_food_signal():
    score, flags = _group_relevance(
        {
            "name": "Ресторан при отеле Море",
            "activity": "Ресторан",
            "description": "Меню и бронирование столиков",
        },
        ["ресторан"],
    )
    assert score >= 0.7
    assert "vk_accommodation_primary" not in flags


def test_non_food_supplier_is_hard_flagged_even_if_description_mentions_restaurants():
    score, flags = _group_relevance(
        {
            "name": "Мебель на заказ в Ялте",
            "activity": "Мебель",
            "description": "Кухни, мебель для ресторанов и кафе",
        },
        ["ресторан"],
    )
    assert score < 0.5
    assert "vk_non_horeca_primary" in flags


def test_supplier_inactive_consulting_and_aggregator_names_are_hard_flagged():
    cases = [
        (
            {"name": "Магазин для кондитеров ТортСтрой", "activity": "Товары для творчества"},
            "vk_supplier_primary",
        ),
        (
            {"name": "Меловая доска для вашего бара, ресторана", "activity": "Другие товары"},
            "vk_non_food_activity",
        ),
        (
            {"name": "На память о кафе Черкио", "activity": "Общество"},
            "vk_inactive_primary",
        ),
        (
            {"name": "SANGRITA BAR Consult", "activity": "Бар, паб"},
            "vk_consulting_primary",
        ),
        (
            {"name": "Доставка еды из лучших ресторанов", "activity": "Еда"},
            "vk_aggregator_primary",
        ),
    ]
    for group, expected_flag in cases:
        score, flags = _group_relevance(group, ["ресторан"])
        assert score < 0.5
        assert expected_flag in flags


def test_activity_only_food_category_stays_below_outreach_threshold():
    score, flags = _group_relevance(
        {
            "name": "Красный стул",
            "activity": "Кулинария",
            "description": "",
        },
        ["паб"],
    )
    assert score < 0.70
    assert "vk_activity_only_food_signal" in flags
    assert "vk_no_primary_food_signal" not in flags


def test_mixed_or_off_premise_food_is_routed_to_manual_segment():
    for group in (
        {"name": "Ресторан и гостиничный комплекс Шерлок", "activity": "Ресторан"},
        {"name": "Масло Кейтеринг Крым", "activity": "Доставка еды"},
        {"name": "Детская комната и кафе", "activity": "Кафе"},
    ):
        score, flags = _group_relevance(group, ["ресторан"])
        assert score > 0
        assert "vk_manual_business_segment" in flags


def test_vk_address_is_extracted():
    group = {"addresses": [{"city": {"title": "Ялта"}, "address": "ул. Морская, 1"}]}
    assert _pick_address(group) == "Ялта, ул. Морская, 1"


def test_query_falls_back_to_category():
    assert _category_for({"activity": "Сообщество"}, ["пиццерия"]) == "пиццерия"


def test_dry_run_never_emits_invalid_token_alert(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    vk_groups._token_alert_sent = False
    vk_groups._maybe_alert_token_dead(
        {"error_code": 5, "error_msg": "invalid access token"}
    )
    assert vk_groups._token_alert_sent is False
