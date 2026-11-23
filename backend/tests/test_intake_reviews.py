"""M08 acceptance: synthetic PDFs/CSV on a migrated temporary database."""

import io
import json

from pypdf import PdfWriter
from sqlalchemy import func, select

from src.models import IntakeItem, Invoice, Purchase, PurchaseRevision
from src.services.invoices import UploadedFile


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _handoff(client, batch_id, row, *, target=None):
    body = {"items": [{"id": row.id, "version": row.version}]}
    if target is not None:
        body["target_invoice_id"] = target
    return client.post(f"/api/intake-batches/{batch_id}/reviews", json=body)


def test_five_pdf_pages_enter_five_dates_and_retry_is_idempotent(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    content = _pdf(5)
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(content, "synthetic-five.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    dates = ["2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
    for index, (row, day) in enumerate(zip(rows, dates), start=1):
        intake_service.classify_item(row.id, purchase_date=day, source_name="Costco", version=row.version)
        with migrated_database.unit_of_work() as session:
            item = session.get(IntakeItem, row.id)
            item.recognition_payload = json.dumps({
                "purpose": "intake_grouping", "rounds": ["text_layer"],
                "fields": {"invoice_number": {"status": "recognized", "raw_text": f"INV-{index}"}},
                "lines": [{"index": 1, "page": index, "fields": {
                    "quantity": {"status": "recognized", "raw_text": "2"}}}],
            })
    rows = [intake_service.get_item(row.id) for row in rows]
    hints = migrated_client.get(
        f"/api/intake-batches/{batch_id}/review-suggestions?purchase_date=2026-11-08&source_name=Costco")
    assert hints.status_code == 200, hints.json
    assert {tuple(entry["item_ids"]) for entry in hints.json["suggestions"]} == {
        (rows[0].id,)}
    assert all(entry["basis"] == "same_printed_invoice_number" for entry in hints.json["suggestions"])
    invoice_ids = []
    for row in rows:
        response = _handoff(migrated_client, batch_id, row)
        assert response.status_code == 201, response.json
        invoice_ids.append(response.json["invoice_id"])
        detail = migrated_client.get(f"/api/invoices/{invoice_ids[-1]}").json
        assert detail["source_name"] == "Costco"
        assert detail["invoice_date"] == dates[len(invoice_ids) - 1]
        assert detail["reconciliation"]["line_count"] == 1
        assert detail["lines"][0]["quantity"] is None  # grouping OCR did not pass review checks
        assert detail["intake_sources"][0]["page_no"] == len(invoice_ids)
        original = migrated_client.get(
            f"/api/intake-batches/{batch_id}/items/{row.id}/original")
        assert original.status_code == 200 and original.data == content
    assert invoice_ids[0] != invoice_ids[1]
    groups = migrated_client.get(f"/api/intake-batches/{batch_id}/groups").json["groups"]
    assert {group["purchase_date"] for group in groups} == set(dates)
    retry = _handoff(migrated_client, batch_id, rows[0])
    assert retry.status_code == 200 and retry.json["invoice_id"] == invoice_ids[0]
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 5
        assert session.scalar(select(func.count()).select_from(Purchase)) == 0


def test_review_split_and_merge_move_pending_lines_without_duplication(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "two.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for row in rows:
        intake_service.classify_item(row.id, purchase_date="2026-11-08", source_name="Costco", version=1)
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = json.dumps({
                "purpose": "intake_grouping", "lines": [{"fields": {"unit_price": {
                    "status": "unreadable", "raw_text": ""}}}]})
    rows = [intake_service.get_item(row.id) for row in rows]
    hints = migrated_client.get(
        f"/api/intake-batches/{batch_id}/review-suggestions?purchase_date=2026-11-08&source_name=Costco")
    assert hints.status_code == 200, hints.json
    assert hints.json["suggestions"] == [{"item_ids": [rows[0].id, rows[1].id],
        "basis": "same_file_consecutive_pages", "invoice_number_normalized": None}]
    both = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": row.id, "version": row.version} for row in rows]})
    assert both.status_code == 201, both.json
    source_id = both.json["invoice_id"]
    second = intake_service.get_item(rows[1].id)
    split = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/{source_id}/reassign", json={
        "items": [{"id": second.id, "version": second.version}]})
    assert split.status_code == 201, split.json
    target_id = split.json["target_invoice_id"]
    assert len(migrated_client.get(f"/api/invoices/{source_id}").json["lines"]) == 1
    assert len(migrated_client.get(f"/api/invoices/{target_id}").json["lines"]) == 1
    second = intake_service.get_item(second.id)
    merged = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/{target_id}/reassign", json={
        "items": [{"id": second.id, "version": second.version}],
        "target_invoice_id": source_id})
    assert merged.status_code == 200, merged.json
    assert len(migrated_client.get(f"/api/invoices/{source_id}").json["lines"]) == 2
    assert migrated_client.get(f"/api/invoices/{target_id}").json["status"] == "voided"


def test_two_invoices_same_group_close_separately_and_keep_original(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    batch_id = intake_service.create_batch().id
    content = _pdf(2)
    intake_ingest_service.upload(batch_id, UploadedFile(content, "two-invoices.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    invoice_ids = []
    for row in rows:
        row = intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                           source_name="Costco", version=row.version)
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = json.dumps({
                "purpose": "intake_grouping", "lines": [{"fields": {}}]})
        handed = _handoff(migrated_client, batch_id, row)
        assert handed.status_code == 201, handed.json
        invoice_ids.append(handed.json["invoice_id"])
    food = migrated_client.post("/api/food-items", json={
        "name": "Rice", "category_id": 5, "base_unit": "lb"})
    assert food.status_code == 201, food.json
    for invoice_id in invoice_ids:
        detail = migrated_client.get(f"/api/invoices/{invoice_id}").json
        line = detail["lines"][0]
        patched = migrated_client.patch(f"/api/invoices/{invoice_id}/lines/{line['id']}", json={
            "description": "Rice", "quantity": "2", "purchase_unit": "lb",
            "unit_price": "3.00", "line_amount": "6.00", "version": line["version"]})
        assert patched.status_code == 200, patched.json
        confirmed = migrated_client.post(f"/api/invoices/{invoice_id}/confirm", json={
            "lines": [{"line_id": line["id"], "food_item_id": food.json["id"]}]})
        assert confirmed.status_code == 200 and confirmed.json["confirmed"] == 1
        complete = migrated_client.post(f"/api/invoices/{invoice_id}/complete", json={})
        assert complete.status_code == 200, complete.json
        assert complete.json["status"] == "completed"
        assert len(complete.json["invoice"]["intake_sources"]) == 1
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 2
    assert migrated_client.post(f"/api/invoices/{invoice_ids[0]}/extraction").status_code == 409
    ref_id = rows[0].batch_file_id
    assert migrated_client.delete(f"/api/intake-batches/{batch_id}/files/{ref_id}").status_code == 409
    assert migrated_client.get(f"/api/intake-batches/{batch_id}/items/{rows[1].id}/original").data == content


def test_csv_row_handoff_confirm_and_explicit_group_revision(
    migrated_client, migrated_database, intake_service,
):
    batch_id = intake_service.create_batch().id
    csv = "商品描述,数量,采购单位,单价,行金额,采购日期,采购来源\nRice,2,lb,3.00,6.00,2026-11-08,Costco\n".encode("utf-8-sig")
    uploaded = migrated_client.post(f"/api/intake-batches/{batch_id}/spreadsheets", data={
        "file": (io.BytesIO(csv), "synthetic.csv"), "options": "{}"})
    assert uploaded.status_code == 201, uploaded.json
    row = intake_service.list_items(batch_id).items[0]
    row = intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                       source_name="Costco", version=row.version)
    handed = _handoff(migrated_client, batch_id, row)
    assert handed.status_code == 201, handed.json
    invoice_id = handed.json["invoice_id"]
    assert migrated_client.patch(f"/api/invoices/{invoice_id}", json={
        "source_name": "Wrong direct edit"}).status_code == 409
    detail = migrated_client.get(f"/api/invoices/{invoice_id}").json
    assert detail["entry_source"] == "import"
    assert detail["intake_sources"][0]["row_no"] == 2
    assert detail["lines"][0]["description"] == "Rice"
    assert detail["lines"][0]["quantity"] == "2"
    assert detail["lines"][0]["line_amount"] == "6.00"
    line_id = detail["lines"][0]["id"]
    confirmed = migrated_client.post(f"/api/invoices/{invoice_id}/confirm", json={
        "lines": [{"line_id": line_id, "new_food_item": {
            "name": "Rice", "category_id": 5, "base_unit": "lb"}}]})
    assert confirmed.status_code == 200, confirmed.json
    assert confirmed.json["confirmed"] == 1
    duplicate = migrated_client.post(f"/api/invoices/{invoice_id}/confirm", json={
        "lines": [{"line_id": line_id}]})
    assert duplicate.status_code == 200 and duplicate.json["confirmed"] == 0
    revised_row = intake_service.get_item(row.id)
    revised = migrated_client.patch(
        f"/api/intake-batches/{batch_id}/reviews/{invoice_id}/classification", json={
            "scope": "invoice", "items": [{"id": row.id, "version": revised_row.version}],
            "purchase_date": "2026-11-09", "source_name": "Market"})
    assert revised.status_code == 200, revised.json
    assert migrated_client.patch(
        f"/api/intake-batches/{batch_id}/reviews/{invoice_id}/classification", json={
            "scope": "invoice", "items": [{"id": row.id, "version": revised_row.version}],
            "purchase_date": "2026-11-10", "source_name": "Other"}).status_code == 409
    with migrated_database.unit_of_work() as session:
        purchase = session.scalar(select(Purchase))
        assert purchase.purchase_date.isoformat() == "2026-11-09"
        assert purchase.source_name == "Market"
        assert session.scalar(select(func.count()).select_from(Purchase)) == 1
        assert session.scalar(select(func.count()).select_from(PurchaseRevision)) == 1


def test_line_level_exception_survives_group_revision(
    migrated_client, migrated_database, intake_service,
):
    batch_id = intake_service.create_batch().id
    csv = "商品描述,数量,采购单位,单价,行金额\nOil,1,lb,5.00,5.00\n".encode("utf-8-sig")
    uploaded = migrated_client.post(f"/api/intake-batches/{batch_id}/spreadsheets", data={
        "file": (io.BytesIO(csv), "override.csv"), "options": "{}"})
    assert uploaded.status_code == 201, uploaded.json
    row = intake_service.list_items(batch_id).items[0]
    row = intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                       source_name="Costco", version=row.version)
    handed = _handoff(migrated_client, batch_id, row)
    assert handed.status_code == 201, handed.json
    invoice_id = handed.json["invoice_id"]
    line = migrated_client.get(f"/api/invoices/{invoice_id}").json["lines"][0]
    patched = migrated_client.patch(f"/api/invoices/{invoice_id}/lines/{line['id']}", json={
        "purchase_date": "2026-11-07", "source_name": "Special Shop",
        "version": line["version"]})
    assert patched.status_code == 200, patched.json
    confirmed = migrated_client.post(f"/api/invoices/{invoice_id}/confirm", json={
        "lines": [{"line_id": line["id"], "new_food_item": {
            "name": "Oil", "category_id": 5, "base_unit": "lb"}}]})
    assert confirmed.status_code == 200 and confirmed.json["confirmed"] == 1
    version = intake_service.get_item(row.id).version
    changed = migrated_client.patch(
        f"/api/intake-batches/{batch_id}/reviews/{invoice_id}/classification", json={
            "scope": "invoice", "items": [{"id": row.id, "version": version}],
            "purchase_date": "2026-11-09", "source_name": "Market"})
    assert changed.status_code == 200, changed.json
    with migrated_database.unit_of_work() as session:
        purchase = session.scalar(select(Purchase))
        assert purchase.purchase_date.isoformat() == "2026-11-07"
        assert purchase.source_name == "Special Shop"
        assert session.scalar(select(func.count()).select_from(PurchaseRevision)) == 0
