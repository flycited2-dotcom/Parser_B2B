import csv

from openpyxl import load_workbook

from utils.outreach_export import OUTREACH_HEADERS, build_outreach_exports
from utils.storage import FIELDS


def _write_master(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def test_outreach_export_matches_sender_contract_and_quality_gates(tmp_path):
    master = tmp_path / "master_all.csv"
    _write_master(master, [
        {
            "entity_id": "ent-good", "name": "Ресторан Море", "city": "Ялта",
            "client_type": "ресторан", "email": "info@more.ru",
            "phone": "+7 978 111-22-33", "website": "https://more.ru",
            "source": "OSM", "sources": "OSM|Я.Карты", "confidence": "0.98",
        },
        {
            "entity_id": "ent-alt", "name": "Кафе Бриз", "city": "Судак",
            "client_type": "кафе", "email": "noreply@briz.ru",
            "all_emails": "noreply@briz.ru | booking@briz.ru",
            "website": "https://briz.ru", "source": "Я.Карты", "confidence": "0.95",
        },
        {
            "entity_id": "ent-hotel", "name": "Отель", "city": "Ялта",
            "client_type": "отель", "email": "info@hotel.ru", "confidence": "0.99",
        },
        {
            "entity_id": "ent-low", "name": "Бар Сомнение", "city": "Керчь",
            "client_type": "бар", "email": "bar@maybe.ru", "confidence": "0.40",
        },
        {
            "entity_id": "ent-duplicate", "name": "Дубль", "city": "Ялта",
            "client_type": "ресторан", "email": "info@more.ru", "confidence": "0.80",
        },
        {
            "entity_id": "ent-manual", "name": "Кейтеринг", "city": "Ялта",
            "client_type": "ресторан", "email": "info@catering.ru",
            "confidence": "0.99", "quality_flags": "vk_manual_business_segment",
        },
        {
            "entity_id": "ent-donor", "name": "Кафе Донор", "city": "Ялта",
            "client_type": "кафе", "email": "weak@donor.ru",
            "confidence": "0.99",
            "quality_flags": "manual_review|vk_weak_contact_donor",
        },
    ])

    result = build_outreach_exports(str(master), str(tmp_path), run_id="test-run")

    assert result["ready_rows"] == 2
    assert result["review_rows"] == 5
    assert result["approved_for_send"] is False
    with open(result["ready_csv"], encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    assert list(rows[0]) == OUTREACH_HEADERS
    assert {row["Email"] for row in rows} == {"info@more.ru", "booking@briz.ru"}
    with open(result["review_csv"], encoding="utf-8-sig") as handle:
        review_rows = list(csv.DictReader(handle, delimiter=";"))
    manual = next(row for row in review_rows if row["entity_id"] == "ent-manual")
    assert manual["review_reason"] == "manual_business_segment"
    donor = next(row for row in review_rows if row["entity_id"] == "ent-donor")
    assert donor["review_reason"] == "weak_vk_contact_donor"

    workbook = load_workbook(result["ready_xlsx"], read_only=True)
    assert workbook.sheetnames == ["Контакты", "Метаданные"]
    assert [cell.value for cell in next(workbook["Контакты"].iter_rows())] == OUTREACH_HEADERS
    metadata = dict(workbook["Метаданные"].iter_rows(values_only=True))
    assert metadata["approved_for_send"] is False
    assert metadata["consumer"] == "Email_horeca_send"
