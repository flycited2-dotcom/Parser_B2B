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
