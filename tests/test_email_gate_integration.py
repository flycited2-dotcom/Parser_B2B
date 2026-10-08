"""Шлюз качества email подключён в storage и merger (outreach — в test_outreach_export)."""
import os
import tempfile

import pytest

from utils import merger


@pytest.fixture
def isolated_storage(monkeypatch):
    from utils import dedup, storage

    tmpdir = tempfile.mkdtemp(prefix="storage_email_")
    monkeypatch.setattr(storage, "OUTPUT_DIR", tmpdir)
    monkeypatch.setattr(storage, "OUTPUT_FILE", os.path.join(tmpdir, "result_test.csv"))
    monkeypatch.setattr(storage, "_rows", [])
    monkeypatch.setattr(storage, "_seen", set())
    monkeypatch.setattr(storage, "_header_written", False)
    dedup.close()
    monkeypatch.setattr(dedup, "DEDUP_PATH", os.path.join(tmpdir, "dedup.db"))
    yield storage
    dedup.close()


def test_storage_repairs_and_drops_emails_of_new_observations(isolated_storage):
    row = isolated_storage._prepare_item({
        "name": "ТК Кит", "city": "Ялта", "category": "logistika", "source": "OSM",
        "source_id": "node:9", "website": "https://tk-kit.com",
        "email": "rating@mail.ru",
        "all_emails": "rating@mail.ru | graqre@gx-xvg.pbz | vasb@gx-xvg.pbz",
    })
    assert row["email"] == "tender@tk-kit.com"
    assert row["all_emails"] == "tender@tk-kit.com | info@tk-kit.com"


def test_storage_keeps_good_email_and_defaults_all_emails(isolated_storage):
    row = isolated_storage._prepare_item({
        "name": "Окна Юг", "city": "Ялта", "category": "stroitelstvo", "source": "OSM",
        "source_id": "node:10", "website": "https://okna-yug.ru", "email": "info@okna-yug.ru",
    })
    assert row["email"] == "info@okna-yug.ru"
    assert row["all_emails"] == "info@okna-yug.ru"


def test_storage_leaves_rows_without_email_untouched(isolated_storage):
    row = isolated_storage._prepare_item({
        "name": "Окна Юг", "city": "Ялта", "category": "stroitelstvo", "source": "OSM",
        "source_id": "node:11", "website": "https://okna-yug.ru",
    })
    assert row["email"] == ""
    assert row["all_emails"] == ""


def test_master_rows_from_historical_result_files_are_email_clean():
    row = merger._normalize_row({
        "name": "X", "city": "Ялта", "website": "https://x.ru", "source": "OSM",
        "source_id": "node:1", "email": "mail@mail.ru", "all_emails": "mail@mail.ru | %20info@x.ru",
    })
    assert row["email"] == "info@x.ru"
    assert row["all_emails"] == "info@x.ru"
