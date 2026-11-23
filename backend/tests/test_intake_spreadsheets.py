"""M04 API acceptance with synthetic CSV/XLSX and migrated temporary databases."""

import csv
import io
import json
from dataclasses import replace

from openpyxl import Workbook
from sqlalchemy import func, select

from src.models import Purchase
from src.factory import create_app
from src.repositories.intake_spreadsheets import IntakeSpreadsheetRepository


HEADERS = ["商品描述", "数量", "采购日期", "采购来源"]


def csv_file(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def post(client, batch_id, content, *, name="synthetic.csv", options=None,
         suffix="", confirmed=False):
    data = {"file": (io.BytesIO(content), name),
            "options": json.dumps(options or {}, ensure_ascii=False)}
    if confirmed:
        data["confirm_duplicate"] = "true"
    return client.post(f"/api/intake-batches/{batch_id}/spreadsheets{suffix}", data=data)


def test_csv_rows_have_original_evidence_and_four_manual_groups(
    migrated_client, intake_service, migrated_database,
):
    batch = intake_service.create_batch()
    content = csv_file([
        ["A", "2", "2026-11-08", "Costco"],
        ["B", "3", "2026-11-08", "Market"],
        ["C", "4", "2026-11-09", "Costco"],
        ["D", "5", "2026-11-09", ""],
    ])
    response = post(migrated_client, batch.id, content)
    assert response.status_code == 201, response.json
    assert response.json["item_count"] == 4
    ref_id = response.json["batch_file_id"]
    file_detail = migrated_client.get(
        f"/api/intake-batches/{batch.id}/spreadsheets/{ref_id}").json
    assert file_detail["import_metadata"]["mapping"]["purchase_date"] == 3
    page = migrated_client.get(f"/api/intake-batches/{batch.id}/spreadsheets/rows?limit=2").json
    assert page["total"] == 4 and len(page["items"]) == 2
    second = migrated_client.get(
        f"/api/intake-batches/{batch.id}/spreadsheets/rows?limit=2&offset=2").json
    rows = page["items"] + second["items"]
    assert [item["source_row"] for item in rows] == [2, 3, 4, 5]
    assert rows[0]["row_data"]["raw_fields"]["cells"]["A"]["raw_text"] == "A"
    ledger_row = migrated_client.get(f"/api/intake-batches/{batch.id}/items").json["items"][0]
    assert ledger_row["row_no"] == 2 and ledger_row["sheet_name"] is None
    assert ledger_row["row_values"]["quantity"] == "2"
    assert rows[3]["row_data"]["raw_fields"]["fields"]["source_name"]["status"] == "missing"
    assert rows[3]["row_data"]["values"]["source_name"] is None
    assert all(item["purchase_date"] is None and item["source_name"] is None for item in rows)
    original = migrated_client.get(
        f"/api/intake-batches/{batch.id}/spreadsheets/{ref_id}/original")
    assert original.status_code == 200 and original.data == content
    for item, date, source in zip(rows, ["2026-11-08", "2026-11-08", "2026-11-09", "2026-11-09"],
                                  ["Costco", "Market", "Costco", None]):
        intake_service.classify_item(item["id"], purchase_date=date, source_name=source,
                                     version=item["version"])
    groups = migrated_client.get(f"/api/intake-batches/{batch.id}/spreadsheets/groups").json["groups"]
    assert {(g["purchase_date"], g["source_name"], g["item_count"]) for g in groups} == {
        ("2026-11-08", "Costco", 1), ("2026-11-08", "Market", 1),
        ("2026-11-09", "Costco", 1), ("2026-11-09", None, 1),
    }
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 0


def test_xlsx_preview_mapping_and_row_issue_are_independent(migrated_client, intake_service):
    book = Workbook()
    sheet = book.active
    sheet.title = "Purchases"
    sheet.append(["Item", "Qty", "Date", "Vendor"])
    sheet.append(["Rice", 2, "11/08/2026", "Market"])
    sheet.append(["Oil", 3, "2026-11-09", ""])
    data = io.BytesIO()
    book.save(data)
    book.close()
    content = data.getvalue()
    batch_id = intake_service.create_batch().id
    options = {"sheet": "Purchases", "mapping": {"description": 1, "quantity": 2,
               "purchase_date": 3, "source_name": 4}}
    preview = post(migrated_client, batch_id, content, name="synthetic.xlsx", options=options,
                   suffix="/preview?limit=1")
    assert preview.status_code == 200, preview.json
    assert preview.json["total"] == 2 and len(preview.json["rows"]) == 1
    assert preview.json["rows"][0]["source_row"] == 2
    assert preview.json["rows"][0]["review_issues"][0]["field"] == "purchase_date"
    created = post(migrated_client, batch_id, content, name="synthetic.xlsx", options=options)
    assert created.status_code == 201, created.json
    rows = migrated_client.get(f"/api/intake-batches/{batch_id}/spreadsheets/rows").json["items"]
    assert rows[0]["sheet"] == "Purchases" and rows[0]["row_data"]["values"]["purchase_date"] is None
    assert rows[1]["row_data"]["values"]["purchase_date"] == "2026-11-09"
    assert rows[1]["row_data"]["values"]["source_name"] is None


def test_duplicate_file_needs_explicit_confirmation_and_old_import_still_works(
    migrated_client, intake_service,
):
    content = csv_file([["A", "2", "2026-11-08", "Costco"]])
    first = intake_service.create_batch().id
    second = intake_service.create_batch().id
    assert post(migrated_client, first, content).status_code == 201
    same_batch = post(migrated_client, first, content)
    assert same_batch.status_code == 409
    assert same_batch.json["error"]["details"]["batch_file_id"]
    warning = post(migrated_client, second, content)
    assert warning.status_code == 409
    assert warning.json["error"]["details"]["shared_with_batch_ids"] == [first]
    assert intake_service.get_batch(second).files == ()
    confirmed = post(migrated_client, second, content, confirmed=True)
    assert confirmed.status_code == 201 and confirmed.json["reused_bytes"] is True
    legacy = migrated_client.post("/api/imports", data={"file": (io.BytesIO(csv_file([
        ["Other", "1", "2026-11-10", "Shop"]])), "legacy.csv"), "options": "{}"})
    assert legacy.status_code == 201, legacy.json


def test_legacy_same_bytes_are_flagged_before_batch_registration(migrated_client, intake_service):
    content = csv_file([["A", "2", "2026-11-08", "Costco"]])
    legacy = migrated_client.post("/api/imports", data={
        "file": (io.BytesIO(content), "legacy.csv"), "options": "{}"})
    assert legacy.status_code == 201, legacy.json
    batch_id = intake_service.create_batch().id
    warning = post(migrated_client, batch_id, content)
    assert warning.status_code == 409
    assert warning.json["error"]["details"]["same_bytes_invoice_ids"]
    assert intake_service.get_batch(batch_id).files == ()
    assert post(migrated_client, batch_id, content, confirmed=True).status_code == 201


def test_failed_row_write_keeps_file_retriable(migrated_client, intake_service, monkeypatch):
    batch_id = intake_service.create_batch().id
    content = csv_file([["A", "2", "2026-11-08", "Costco"]])
    original = IntakeSpreadsheetRepository.save_rows

    def fail_once(self, *args, **kwargs):
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(IntakeSpreadsheetRepository, "save_rows", fail_once)
    assert post(migrated_client, batch_id, content).status_code == 500
    ref = intake_service.get_batch(batch_id).files[0]
    assert ref.status == "failed" and ref.item_count is None
    assert intake_service.list_items(batch_id).total == 0
    monkeypatch.setattr(IntakeSpreadsheetRepository, "save_rows", original)
    retried = post(migrated_client, batch_id, content)
    assert retried.status_code == 201, retried.json
    assert retried.json["batch_file_id"] == ref.id
    assert intake_service.list_items(batch_id).total == 1


def test_large_csv_is_page_bounded(intake_service, migrated_database, storage, config):
    migrated_client = create_app(replace(config, upload_max_bytes=1024 * 1024),
                                 database=migrated_database, file_storage=storage).test_client()
    batch_id = intake_service.create_batch().id
    content = csv_file([[f"item {i}", "1", "2026-11-08", "Costco"] for i in range(4999)])
    preview = post(migrated_client, batch_id, content, suffix="/preview")
    assert preview.status_code == 200 and preview.json["total"] == 4999
    assert len(preview.json["rows"]) == 50
    created = post(migrated_client, batch_id, content)
    assert created.status_code == 201, created.json
    page = migrated_client.get(f"/api/intake-batches/{batch_id}/spreadsheets/rows").json
    assert page["total"] == 4999 and len(page["items"]) == 50
    assert migrated_client.get(
        f"/api/intake-batches/{batch_id}/spreadsheets/rows?limit=5000").status_code == 400
