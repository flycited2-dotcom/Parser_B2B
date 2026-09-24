import os
import tempfile

import pytest

from config.segments import exclusion_flags


@pytest.mark.parametrize(
    ("name", "category", "expected"),
    [
        ("Магнит Косметик", "krasota", ["excluded_chain"]),
        ("Салон МТС", "torgovlya", ["excluded_chain"]),
        ("Пятёрочка", "", ["excluded_chain"]),
        ("Администрация г. Ялта", "", ["excluded_gov"]),
        ("ГБУЗ РК Стоматологическая поликлиника", "medicina", ["excluded_gov"]),
        ("Кафе Ромашка", "", ["excluded_other_base_type"]),
        ("Гостевой дом У моря", "", ["excluded_other_base_type"]),
        ("Автосервис", "Ресторан", ["excluded_other_base_type"]),
        ("Барбершоп Борода", "krasota", []),
        ("Магнитные доски Крым", "reklama", []),
        ("Столовые приборы оптом", "torgovlya", []),
        ("Строительная компания Южный берег", "stroitelstvo", []),
        ("Пудра — студия макияжа", "krasota", []),
    ],
)
def test_exclusion_flags(name, category, expected):
    assert exclusion_flags(name, category) == expected


@pytest.fixture
def isolated_storage(monkeypatch):
    from utils import dedup, storage

    tmpdir = tempfile.mkdtemp(prefix="storage_excl_")
    monkeypatch.setattr(storage, "OUTPUT_DIR", tmpdir)
    monkeypatch.setattr(storage, "OUTPUT_FILE", os.path.join(tmpdir, "result_test.csv"))
    monkeypatch.setattr(storage, "_rows", [])
    monkeypatch.setattr(storage, "_seen", set())
    monkeypatch.setattr(storage, "_header_written", False)
    dedup.close()
    monkeypatch.setattr(dedup, "DEDUP_PATH", os.path.join(tmpdir, "dedup.db"))
    yield storage
    dedup.close()


def test_prepare_item_appends_exclusion_flags_once(isolated_storage):
    row = isolated_storage._prepare_item({
        "name": "Магнит Косметик", "city": "Ялта", "category": "krasota",
        "source": "OSM", "source_id": "node:1",
        "quality_flags": "manual_review|excluded_chain",
    })
    assert row["client_type"] == "krasota"
    assert row["quality_flags"].split("|") == ["manual_review", "excluded_chain"]


def test_prepare_item_keeps_clean_business_unflagged(isolated_storage):
    row = isolated_storage._prepare_item({
        "name": "Окна Юг", "city": "Ялта", "category": "stroitelstvo",
        "source": "OSM", "source_id": "node:2",
    })
    assert row["quality_flags"] == ""


@pytest.mark.parametrize(
    "name",
    [
        "Музыкальная школа № 1",
        "ГАУЗРК «Крымский республиканский стоматологический центр»",
        "Филиал детской поликлиники №2",
        "Ялтинская городская больница №1 Поликлиника №1",
        "ФАП с. Лесное",
        "Фельдшерско-акушерский пункт",
        "ФГАОУ ВО КФУ им. Вернадского",
        "ГБПОУ РК Симферопольский колледж",
        "МКУ Управление образования",
        "МБУДО Детская школа искусств",
        "Гимназия им. Ушинского",
        "Лицей № 3",
        "Дворец водных видов спорта",
        "Муниципальное бюджетное учреждение Центр",
    ],
)
def test_government_bodies_named_in_spec_are_excluded(name):
    assert "excluded_gov" in exclusion_flags(name)


@pytest.mark.parametrize(
    "name",
    ["Поликлиника Медикус", "Школа танцев Grace", "Детский центр Маугли", "Автошкола Луч"],
)
def test_private_businesses_are_not_government(name):
    assert "excluded_gov" not in exclusion_flags(name)


@pytest.mark.parametrize(
    "name",
    [
        "Gloria Jeans", "PlayToday", "Street Beat", "585 Золотой", "Технониколь",
        "ЭТМ", "Главдоставка", "Кий Авиа", "Самолёт Плюс", "Дятьково",
        "Аско-страхование", "СберЛизинг",
    ],
)
def test_federal_brands_found_in_audit_are_chains(name):
    assert "excluded_chain" in exclusion_flags(name)
