"""Тесты append-mode storage._flush (Спринт 3.5).

Проверяем, что save_item больше не делает O(N²) rewrite — файл должен расти
ровно на одну строку за вызов, header — один раз.
"""
import csv
import os
import tempfile

import pytest


@pytest.fixture
def isolated_storage(monkeypatch):
    """Сбрасывает глобальное состояние storage и подменяет OUTPUT_FILE
    на временный путь — иначе тесты ходят в реальный output/.
    """
    from utils import storage

    tmpdir = tempfile.mkdtemp(prefix="storage_test_")
    out = os.path.join(tmpdir, "result_test.csv")

    monkeypatch.setattr(storage, "OUTPUT_DIR", tmpdir)
    monkeypatch.setattr(storage, "OUTPUT_FILE", out)
    monkeypatch.setattr(storage, "_rows", [])
    monkeypatch.setattr(storage, "_seen", set())
    monkeypatch.setattr(storage, "_header_written", False)

    # Изолируем persistent observation state в том же temp-каталоге.
    from utils import dedup
    dedup.close()
    monkeypatch.setattr(dedup, "DEDUP_PATH", os.path.join(tmpdir, "dedup.db"))

    yield storage, out
    dedup.close()


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=";"))


def test_save_item_appends(isolated_storage):
    storage, out = isolated_storage
    for i in range(5):
        storage.save_item({
            "name": f"Кафе {i}",
            "city": "Ялта",
            "source": "test",
        })
    rows = _read(out)
    assert len(rows) == 5
    assert [r["name"] for r in rows] == [f"Кафе {i}" for i in range(5)]


def test_header_written_once(isolated_storage):
    storage, out = isolated_storage
    storage.save_item({"name": "A", "city": "Ялта", "source": "test"})
    storage.save_item({"name": "B", "city": "Ялта", "source": "test"})
    with open(out, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    assert rows[0] == storage.FIELDS
    assert sum(row == storage.FIELDS for row in rows) == 1


def test_rewrite_all_preserves_single_header(isolated_storage):
    """_rewrite_all переписывает с нуля: ровно один header даже после
    нескольких append + rewrite комбинаций."""
    storage, out = isolated_storage
    storage.save_item({"name": "A", "city": "Ялта", "source": "OSM"})
    storage.save_item({"name": "B", "city": "Ялта", "source": "VK"})
    # имитируем мутацию (как делает cross_source_merge) и rewrite
    storage._rows[0]["phone"] = "+7 (000) 000-00-00"
    storage._rewrite_all()
    storage.save_item({"name": "C", "city": "Ялта", "source": "Поиск"})

    rows = _read(out)
    assert len(rows) == 3
    with open(out, encoding="utf-8-sig", newline="") as f:
        raw_rows = list(csv.reader(f, delimiter=";"))
    assert raw_rows[0] == storage.FIELDS
    assert sum(row == storage.FIELDS for row in raw_rows) == 1
