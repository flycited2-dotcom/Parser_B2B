import csv

from openpyxl import load_workbook

from utils import excel_export
from utils.storage import FIELDS


def test_master_xlsx_shows_segment_title(tmp_path):
    source = tmp_path / "master_all.csv"
    with source.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerow({
            "city": "Ялта", "name": "Окна Юг", "client_type": "stroitelstvo",
            "email": "info@okna-yug.ru", "phone": "+7 (978) 111-22-33",
        })

    path = excel_export.build_xlsx(str(source), str(tmp_path / "master_all.xlsx"))

    sheet = load_workbook(path)["Крым"]
    assert sheet["C1"].value == "Сегмент"
    assert sheet["C2"].value == "Строительство и ремонт"
