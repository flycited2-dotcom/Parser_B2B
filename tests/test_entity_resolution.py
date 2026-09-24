import csv
import json
import os

import pytest


@pytest.fixture
def isolated_pipeline(monkeypatch, tmp_path):
    from utils import dedup, storage

    dedup.close()
    monkeypatch.setattr(dedup, "DEDUP_PATH", str(tmp_path / "dedup.db"))
    monkeypatch.setattr(storage, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "OUTPUT_FILE", str(tmp_path / "result_test.csv"))
    monkeypatch.setattr(storage, "_rows", [])
    monkeypatch.setattr(storage, "_seen", set())
    monkeypatch.setattr(storage, "_header_written", False)
    yield storage, dedup, tmp_path
    dedup.close()


def test_osm_first_keeps_later_yandex_vk_contacts(isolated_pipeline):
    storage, _, _ = isolated_pipeline

    assert storage.save_item({
        "source": "OSM",
        "source_id": "node/10",
        "name": "Ла Паста",
        "city": "Ялта",
        "address": "ул. Ленина, 10",
        "website": "https://lapasta.example",
        "category": "ресторан",
    })
    assert storage.save_item({
        "source": "Я.Карты",
        "source_id": "org/42",
        "name": "Ла Паста",
        "city": "Ялта",
        "address": "улица Ленина, 10",
        "phone": "+7 978 000-11-22",
        "email": "info@lapasta.example",
        "website": "https://lapasta.example",
        "category": "ресторан",
    })
    assert storage.save_item({
        "source": "VK",
        "source_id": "club77",
        "name": "Ла Паста",
        "city": "Ялта",
        "phone": "+7 978 000-11-22",
        "social": "https://vk.com/lapasta",
        "category": "Ресторан",
    })

    assert len(storage._rows) == 3
    assert storage.cross_source_merge() >= 5
    assert len(storage._rows) == 3  # source observations are retained

    osm = next(row for row in storage._rows if row["source"] == "OSM")
    assert osm["phone"] == "+7 (978) 000-11-22"
    assert osm["email"] == "info@lapasta.example"
    assert osm["social"] == "https://vk.com/lapasta"
    assert osm["sources"] == "OSM|VK|Я.Карты"

    entity_ids = {row["entity_id"] for row in storage._rows}
    assert len(entity_ids) == 1
    provenance = json.loads(osm["provenance"])
    assert {item["source"] for item in provenance["observations"]} == {
        "OSM", "VK", "Я.Карты",
    }
    assert {item["source"] for item in provenance["fields"]["phone"]} == {
        "VK", "Я.Карты",
    }


def test_same_name_branches_with_shared_contacts_are_not_merged(isolated_pipeline):
    storage, _, _ = isolated_pipeline
    shared = {
        "name": "Кофейня Бодрость",
        "city": "Ялта",
        "phone": "+7 978 123-45-67",
        "website": "https://bodrost.example",
        "category": "кофейня",
    }
    storage.save_item({
        **shared,
        "source": "OSM",
        "source_id": "node/1",
        "address": "ул. Ленина, 1",
        "email": "branch1@bodrost.example",
        "latitude": "44.5000",
        "longitude": "34.1500",
    })
    storage.save_item({
        **shared,
        "source": "Я.Карты",
        "source_id": "org/2",
        "address": "ул. Ленина, 99",
        "latitude": "44.5200",
        "longitude": "34.1800",
    })
    # A location-less bridge observation must not transitively collapse both
    # physical branches merely because the chain reuses one phone/domain.
    storage.save_item({
        **shared,
        "source": "VK",
        "source_id": "club/shared",
        "social": "https://vk.com/bodrost",
    })

    storage.cross_source_merge()
    assert len(storage._rows) == 3
    assert len({row["entity_id"] for row in storage._rows}) == 2
    second = next(row for row in storage._rows if row["source"] == "Я.Карты")
    assert second["email"] == ""
    assert second["sources"] == "Я.Карты"


def test_name_and_city_alone_are_not_enough_to_merge(isolated_pipeline):
    storage, _, _ = isolated_pipeline
    storage.save_item({"source": "OSM", "name": "Столовая", "city": "Крым"})
    storage.save_item({"source": "VK", "name": "Столовая", "city": "Крым"})
    storage.cross_source_merge()
    assert len(storage._rows) == 2
    assert len({row["entity_id"] for row in storage._rows}) == 2


def test_same_source_observation_is_enriched_in_place(isolated_pipeline):
    storage, _, _ = isolated_pipeline
    base = {
        "source": "OSM",
        "source_id": "node/501",
        "name": "Кафе Маяк",
        "city": "Феодосия",
        "address": "ул. Портовая, 3",
    }
    assert storage.save_item(base)
    assert not storage.save_item({
        **base,
        "phone": "+7 978 101-20-30",
        "email": "hello@mayak.example",
    })
    assert len(storage._rows) == 1
    assert storage._rows[0]["phone"] == "+7 (978) 101-20-30"
    assert storage._rows[0]["email"] == "hello@mayak.example"


def test_persistent_state_tracks_refresh_without_blocking_it(isolated_pipeline, monkeypatch):
    storage, dedup, tmp_path = isolated_pipeline
    item = {
        "source": "OSM",
        "source_id": "node/99",
        "name": "Кафе Волна",
        "city": "Судак",
        "address": "Набережная, 5",
    }
    assert storage.save_item(item)
    observation_id = storage._rows[0]["observation_id"]
    first_seen = storage._rows[0]["first_seen"]

    # Simulate a new process/run while keeping the same persistent DB.
    monkeypatch.setattr(storage, "_rows", [])
    monkeypatch.setattr(storage, "_seen", set())
    monkeypatch.setattr(storage, "_header_written", False)
    monkeypatch.setattr(storage, "OUTPUT_FILE", str(tmp_path / "result_refresh.csv"))

    assert storage.save_item({**item, "phone": "+7 978 555-44-33"})
    refreshed = storage._rows[0]
    assert refreshed["observation_id"] == observation_id
    assert refreshed["first_seen"] == first_seen
    assert refreshed["phone"] == "+7 (978) 555-44-33"

    conn = dedup._connect()
    row = conn.execute(
        "SELECT first_seen, last_seen, seen_count FROM observations WHERE observation_id = ?",
        (observation_id,),
    ).fetchone()
    assert row[0] == first_seen
    assert row[1] >= row[0]
    assert row[2] == 2


def _write_result(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def test_master_collapses_supported_entity_but_preserves_branches(tmp_path):
    from utils.merger import FIELDNAMES, build_master

    _write_result(tmp_path / "result_20260701_0000.csv", [
        {
            "source": "OSM", "source_id": "node/1", "name": "Ресторан Море",
            "city": "Ялта", "address": "ул. Морская, 1",
            "website": "https://more.example", "parsed_at": "2026-07-01 00:00",
        },
        {
            "source": "OSM", "source_id": "node/2", "name": "Кафе Сеть",
            "city": "Ялта", "address": "ул. Ленина, 1",
            "phone": "+7 978 111-22-33", "website": "https://set.example",
            "parsed_at": "2026-07-01 00:00",
        },
    ], FIELDNAMES)
    _write_result(tmp_path / "result_20260702_0000.csv", [
        {
            "source": "Я.Карты", "source_id": "org/1", "name": "Ресторан Море",
            "city": "Ялта", "address": "улица Морская, 1",
            "phone": "+7 978 000-00-01", "email": "info@more.example",
            "website": "https://more.example", "parsed_at": "2026-07-02 00:00",
        },
        {
            "source": "Я.Карты", "source_id": "org/2", "name": "Кафе Сеть",
            "city": "Ялта", "address": "ул. Ленина, 99",
            "phone": "+7 978 111-22-33", "website": "https://set.example",
            "parsed_at": "2026-07-02 00:00",
        },
    ], FIELDNAMES)

    master = build_master(str(tmp_path))
    with open(master, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert len(rows) == 3
    restaurant = next(row for row in rows if row["name"] == "Ресторан Море")
    # CSV serialization neutralizes leading '+' for spreadsheet safety.
    assert restaurant["phone"] == "'+7 978 000-00-01"
    assert restaurant["email"] == "info@more.example"
    assert restaurant["sources"] == "OSM|Я.Карты"
    assert len(json.loads(restaurant["provenance"])["observations"]) == 2
    assert sum(row["name"] == "Кафе Сеть" for row in rows) == 2


def test_master_refresh_uses_newer_contact_for_same_observation(tmp_path):
    from utils.merger import FIELDNAMES, build_master

    base = {
        "source": "OSM", "source_id": "node/700", "name": "Кафе Бриз",
        "city": "Судак", "address": "Набережная, 7",
    }
    _write_result(tmp_path / "result_20260701_0000.csv", [
        {**base, "phone": "+7 978 000-00-01", "parsed_at": "2026-07-01 00:00"},
    ], FIELDNAMES)
    _write_result(tmp_path / "result_20260708_0000.csv", [
        {**base, "phone": "+7 978 000-00-02", "parsed_at": "2026-07-08 00:00"},
    ], FIELDNAMES)

    master = build_master(str(tmp_path))
    with open(master, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    assert len(rows) == 1
    assert rows[0]["phone"] == "'+7 978 000-00-02"


def test_master_refresh_replaces_stale_observation_quality_flags(tmp_path):
    from utils.merger import FIELDNAMES, build_master

    base = {
        "source": "VK", "source_id": "700", "name": "Кафе Бриз",
        "city": "Судак", "category": "кафе", "confidence": "0.80",
    }
    _write_result(tmp_path / "result_20260701_0000.csv", [
        {
            **base,
            "quality_flags": "vk_negative_terms|manual_review",
            "parsed_at": "2026-07-01 00:00",
        },
    ], FIELDNAMES)
    _write_result(tmp_path / "result_20260708_0000.csv", [
        {**base, "quality_flags": "", "parsed_at": "2026-07-08 00:00"},
    ], FIELDNAMES)

    master = build_master(str(tmp_path))
    with open(master, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert len(rows) == 1
    assert rows[0]["quality_flags"] == ""


def test_master_quarantines_standalone_vk_noise_but_keeps_raw_file(tmp_path):
    from utils.merger import FIELDNAMES, build_master

    raw = tmp_path / "result_20260703_0000.csv"
    _write_result(raw, [
        {
            "source": "VK", "source_id": "1", "name": "Отель Море",
            "city": "Ялта", "email": "hotel@example.org",
            "quality_flags": "vk_no_primary_segment_signal|vk_noise_primary",
        },
        {
            "source": "VK", "source_id": "2", "name": "Кафе Море",
            "city": "Ялта", "email": "cafe@example.org",
            "quality_flags": "", "category": "кафе",
        },
    ], FIELDNAMES)

    master = build_master(str(tmp_path))
    with open(master, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    with open(raw, encoding="utf-8-sig") as handle:
        raw_rows = list(csv.DictReader(handle, delimiter=";"))

    assert [row["name"] for row in rows] == ["Кафе Море"]
    assert len(raw_rows) == 2
    quarantine = tmp_path / "master_quarantine.csv"
    with open(quarantine, encoding="utf-8-sig") as handle:
        quarantined = list(csv.DictReader(handle, delimiter=";"))
    assert len(quarantined) == 1
    assert quarantined[0]["name"] == "Отель Море"
    assert quarantined[0]["quarantine_reason"] == "vk_missing_primary_segment_signal"


def test_vk_weak_observation_can_enrich_strong_cross_source_anchor(tmp_path):
    from utils.merger import FIELDNAMES, build_master

    _write_result(tmp_path / "result_20260704_0000.csv", [
        {
            "source": "OSM", "source_id": "node/9", "name": "Маяк",
            "city": "Ялта", "phone": "+7 978 999-88-77", "category": "кафе",
        },
        {
            "source": "VK", "source_id": "9", "name": "Маяк",
            "city": "Ялта", "phone": "+7 978 999-88-77",
            "email": "info@mayak.example",
            "quality_flags": "vk_no_primary_segment_signal|manual_review",
        },
    ], FIELDNAMES)

    master = build_master(str(tmp_path))
    with open(master, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert len(rows) == 1
    assert rows[0]["email"] == "info@mayak.example"
    assert rows[0]["sources"] == "OSM|VK"
    assert set(rows[0]["quality_flags"].split("|")) == {
        "manual_review",
        "vk_weak_contact_donor",
        "vk_weak_email_donor",
        "vk_weak_observation_linked",
    }
    assert "vk_no_primary_segment_signal" in rows[0]["provenance"]


def test_vk_weak_observation_cannot_override_anchor_contacts(tmp_path):
    from utils.merger import FIELDNAMES, build_master

    _write_result(tmp_path / "result_20260704_0000.csv", [
        {
            "source": "OSM", "source_id": "node/10", "name": "Кафе Маяк",
            "city": "Ялта", "phone": "+7 978 111-11-11",
            "email": "info@mayak.example", "website": "https://mayak.example",
            "address": "ул. Морская, 10", "category": "кафе",
            "confidence": "0.90",
        },
        {
            "source": "VK", "source_id": "10", "name": "Кафе Маяк",
            "city": "Ялта", "phone": "+7 978 999-99-99",
            "email": "hotel@noise.example", "website": "https://noise.example",
            "address": "улица Морская, 10",
            "quality_flags": "vk_noise_primary|vk_no_primary_segment_signal",
        },
    ], FIELDNAMES)

    master = build_master(str(tmp_path))
    with open(master, encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle, delimiter=";"))

    assert row["phone"] == "'+7 978 111-11-11"
    assert row["email"] == "info@mayak.example"
    assert row["website"] == "https://mayak.example"
    assert "hotel@noise.example" not in row["all_emails"]
    assert "noise.example" not in row["all_websites"]
    assert row["quality_flags"] == "vk_weak_observation_linked"
    assert "hotel@noise.example" in row["provenance"]


def test_cross_source_merge_does_not_pre_poison_anchor_from_weak_vk(
    isolated_pipeline,
):
    storage, _, _ = isolated_pipeline
    assert storage.save_item({
        "source": "OSM", "source_id": "node/12", "name": "Кафе Прибой",
        "city": "Ялта", "address": "ул. Морская, 12", "category": "кафе",
    })
    assert storage.save_item({
        "source": "VK", "source_id": "12", "name": "Кафе Прибой",
        "city": "Ялта", "address": "улица Морская, 12",
        "email": "weak@hotel.example",
        "quality_flags": "vk_noise_primary|vk_no_primary_segment_signal",
    })

    storage.cross_source_merge()

    osm = next(row for row in storage._rows if row["source"] == "OSM")
    vk = next(row for row in storage._rows if row["source"] == "VK")
    assert osm["email"] == ""
    assert vk["email"] == "weak@hotel.example"
    assert osm["sources"] == "OSM|VK"
    assert "weak@hotel.example" in osm["provenance"]


def test_master_and_quarantine_roll_back_together_on_publish_failure(
    monkeypatch, tmp_path
):
    from utils import merger

    (tmp_path / "master_all.csv").write_text("old-master\n", encoding="utf-8")
    (tmp_path / "master_quarantine.csv").write_text(
        "old-quarantine\n", encoding="utf-8"
    )
    _write_result(tmp_path / "result_20260704_0000.csv", [{
        "source": "OSM", "source_id": "node/11", "name": "Кафе Волна",
        "city": "Ялта", "category": "кафе",
    }], merger.FIELDNAMES)

    real_replace = merger.os.replace

    def fail_quarantine_publish(source, target):
        if str(source).endswith("master_quarantine.csv.tmp"):
            raise PermissionError("simulated quarantine publish failure")
        return real_replace(source, target)

    monkeypatch.setattr(merger.os, "replace", fail_quarantine_publish)
    with pytest.raises(PermissionError, match="simulated"):
        merger.build_master(str(tmp_path))

    assert (tmp_path / "master_all.csv").read_text(encoding="utf-8") == "old-master\n"
    assert (tmp_path / "master_quarantine.csv").read_text(
        encoding="utf-8"
    ) == "old-quarantine\n"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.rollback"))


def test_canonical_entity_retains_all_alternate_contacts():
    from utils.entity_resolution import canonicalize_cluster
    from utils.storage import FIELDS

    rows = [
        {
            "source": "OSM", "source_id": "node/42", "name": "Кафе Волна",
            "city": "Ялта", "address": "ул. Морская, 2",
            "phone": "+7 978 111-11-11",
            "all_phones": "+7 978 111-11-11 | +7 978 222-22-22",
            "email": "info@volna.example",
        },
        {
            "source": "Я.Карты", "source_id": "org/42", "name": "Кафе Волна",
            "city": "Ялта", "address": "улица Морская, 2",
            "phone": "+7 978 333-33-33",
            "email": "booking@volna.example",
        },
    ]
    entity = canonicalize_cluster(rows, FIELDS)

    assert entity["all_phones"].count("+7 978") == 3
    assert set(entity["all_emails"].split(" | ")) == {
        "info@volna.example", "booking@volna.example",
    }
