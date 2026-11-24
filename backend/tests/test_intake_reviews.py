"""M08 acceptance: synthetic PDFs/CSV on a migrated temporary database."""

import io
import json

import pytest
from pypdf import PdfWriter
from sqlalchemy import func, select

from src.models import IntakeItem, Invoice, InvoiceLine, Purchase, PurchaseRevision
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


def test_selection_opens_distinct_invoices_without_including_unselected_pages(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(3), "three.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for index, row in enumerate(rows):
        intake_service.classify_item(row.id, purchase_date="2026-06-08",
                                     source_name="Market", version=row.version)
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = json.dumps({
                "fields": {"invoice_number": {"status": "recognized",
                                               "raw_text": f"INV-{index + 1}"}},
                "lines": [{"fields": {}}],
            })
    rows = [intake_service.get_item(row.id) for row in rows]
    body = {"items": [{"id": row.id, "version": row.version} for row in rows[:2]],
            "suggested_group": {"purchase_date": "2026-06-08", "source_name": "Market"}}
    response = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/selection", json=body)
    assert response.status_code == 201, response.json
    invoice_ids = response.json["invoice_ids"]
    assert len(invoice_ids) == 2 and invoice_ids[0] != invoice_ids[1]
    assert [[source["item_id"] for source in migrated_client.get(f"/api/invoices/{ident}").json["intake_sources"]]
            for ident in invoice_ids] == [[rows[0].id], [rows[1].id]]
    assert intake_service.get_item(rows[2].id).handed_off_at is None
    retry = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/selection", json=body)
    assert retry.status_code == 200 and retry.json["invoice_ids"] == invoice_ids


def test_pages_without_a_printed_number_each_open_their_own_draft(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    """读不出单号就一页一张，哪怕两页在同一个文件里连号、同一天、同一商家。

    合错了金额会相加，而「各行之和对不上总额」这个检查正好因此失效；拆错了在
    填表页一眼看得见。所以默认拆开。
    """
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "two.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for row in rows:
        intake_service.classify_item(row.id, purchase_date="2026-06-08",
                                     source_name="Market", version=row.version)
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = json.dumps({
                "fields": {"invoice_number": {"status": "unreadable", "raw_text": None}},
                "lines": [{"fields": {}}]})
    rows = [intake_service.get_item(row.id) for row in rows]
    response = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/selection", json={
        "items": [{"id": row.id, "version": row.version} for row in rows],
        "suggested_group": {"purchase_date": "2026-06-08", "source_name": "Market"}})

    assert response.status_code == 201, response.json
    assert len(set(response.json["invoice_ids"])) == 2


def test_pages_sharing_a_printed_number_stay_one_draft(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    """同一个纸面单号的两页是一张多页发票，仍然合成一张草稿。"""
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "two.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for row in rows:
        intake_service.classify_item(row.id, purchase_date="2026-06-08",
                                     source_name="Market", version=row.version)
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = json.dumps({
                "fields": {"invoice_number": {"status": "recognized", "raw_text": "A-77"}},
                "lines": [{"fields": {}}]})
    rows = [intake_service.get_item(row.id) for row in rows]
    response = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/selection", json={
        "items": [{"id": row.id, "version": row.version} for row in rows],
        "suggested_group": {"purchase_date": "2026-06-08", "source_name": "Market"}})

    assert response.status_code == 201, response.json
    assert len(set(response.json["invoice_ids"])) == 1


@pytest.mark.parametrize("classifications", [
    (("2026-06-08", "Market"), ("2026-06-09", "Market")),
    (("2026-06-08", "Market"), ("2026-06-08", "Other Market")),
])
def test_selection_rejects_mixed_dates_or_sources_without_creating_any_draft(
    migrated_client, migrated_database, intake_service, intake_ingest_service, classifications,
):
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "mixed.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for row, (day, source) in zip(rows, classifications):
        intake_service.classify_item(row.id, purchase_date=day, source_name=source, version=row.version)
    rows = [intake_service.get_item(row.id) for row in rows]
    response = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/selection", json={
        "items": [{"id": row.id, "version": row.version} for row in rows],
        "suggested_group": {"purchase_date": "2026-06-08", "source_name": "Market"}})
    assert response.status_code == 409
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0


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
    # 读不出单号就一页一张：同一个文件里挨着不能证明是同一张发票。
    assert hints.json["suggestions"] == [
        {"item_ids": [rows[0].id], "basis": "single_page", "invoice_number_normalized": None},
        {"item_ids": [rows[1].id], "basis": "single_page", "invoice_number_normalized": None}]
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


def test_spreadsheet_rows_are_not_split_one_draft_per_row(
    migrated_client, intake_service,
):
    """表格行不走「一张发票一条记录」那条规则。

    `entry_source="import"` 的草稿是**整份表格的审核容器**，不是一张发票；没有
    单号就按行拆开会把一份 CSV 变成几十张假发票。
    """
    batch_id = intake_service.create_batch().id
    csv = """商品描述,数量,采购单位,单价,行金额,采购日期,采购来源
Rice,2,lb,3.00,6.00,2026-11-08,Costco
Oil,1,lb,5.00,5.00,2026-11-08,Costco
""".encode("utf-8-sig")
    uploaded = migrated_client.post(f"/api/intake-batches/{batch_id}/spreadsheets", data={
        "file": (io.BytesIO(csv), "two-rows.csv"), "options": "{}"})
    assert uploaded.status_code == 201, uploaded.json
    rows = list(intake_service.list_items(batch_id).items)
    rows = [intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                         source_name="Costco", version=row.version)
            for row in rows]

    response = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/selection", json={
        "items": [{"id": row.id, "version": row.version} for row in rows]})

    assert response.status_code == 201, response.json
    assert len(set(response.json["invoice_ids"])) == 1
    detail = migrated_client.get(f"/api/invoices/{response.json['invoice_ids'][0]}").json
    assert len(detail["lines"]) == 2


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


def _items_payload(client, batch_id):
    response = client.get(f"/api/intake-batches/{batch_id}/items")
    assert response.status_code == 200, response.json
    return {row["id"]: row for row in response.json["items"]}


def test_withdraw_clears_the_whole_draft_and_keeps_the_classification(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    """第 11 节 ③：撤回填表删的是草稿，不是分类。"""
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "draft.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for row in rows:
        intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                     source_name="Costco", version=row.version)
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = json.dumps({
                "purpose": "intake_grouping", "lines": [{"fields": {}}]})
    rows = [intake_service.get_item(row.id) for row in rows]
    handed = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": row.id, "version": row.version} for row in rows]})
    assert handed.status_code == 201, handed.json
    invoice_id = handed.json["invoice_id"]
    line = migrated_client.get(f"/api/invoices/{invoice_id}").json["lines"][0]
    typed = migrated_client.patch(f"/api/invoices/{invoice_id}/lines/{line['id']}", json={
        "description": "Pork belly", "version": line["version"]})
    assert typed.status_code == 200, typed.json
    # 两张卡片都说得出自己在填哪一张草稿，退出填表页之后才有回去的入口。
    before = _items_payload(migrated_client, batch_id)
    assert {before[row.id]["review_invoice_id"] for row in rows} == {invoice_id}
    assert all(before[row.id]["handed_off_at"] for row in rows)

    first = intake_service.get_item(rows[0].id)
    withdrawn = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/withdraw", json={
        "items": [{"id": first.id, "version": first.version}]})
    assert withdrawn.status_code == 200, withdrawn.json
    # 选了一页，整张草稿的两页一起回来——一张草稿就是一张发票。
    assert withdrawn.json["item_ids"] == sorted(row.id for row in rows)
    assert withdrawn.json["invoice_ids"] == [invoice_id]
    assert withdrawn.json["cleared_lines"] == 2

    after = _items_payload(migrated_client, batch_id)
    for row in rows:
        card = after[row.id]
        assert card["handed_off_at"] is None and card["review_invoice_id"] is None
        assert card["decided_purchase_date"] == "2026-11-08"
        assert card["decided_source_name"] == "Costco"
    assert migrated_client.get(f"/api/invoices/{invoice_id}").status_code == 404
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0
        assert session.scalar(select(func.count()).select_from(InvoiceLine)) == 0
    # 撤回之后还能再开始填表，拿到的是一张全新的空草稿。
    again = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": row.id, "version": after[row.id]["version"]} for row in rows]})
    assert again.status_code == 201, again.json
    # ⚠️ 草稿是真删的，所以 SQLite 可能把同一个 id 发回来；证据是**内容**空了，不是 id 变了。
    assert [line["description"] for line in
            migrated_client.get(f"/api/invoices/{again.json['invoice_id']}").json["lines"]] == [None, None]


def test_a_wrong_file_can_be_taken_back_out_unless_its_pages_are_in_a_draft(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    """第 14 节：传错的文件可以从批次里撤下来，但已经开始填表的那一份要先撤回填表。

    ⚠️ 第 16 节把「撤下来」从软删改成了真删：重传同一份文件是全新的一份，不再带回
    旧分类。下半段断言的就是这件事。
    """
    batch_id = intake_service.create_batch().id
    content = _pdf(2)
    intake_ingest_service.upload(batch_id, UploadedFile(content, "wrong.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for row in rows:
        intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                     source_name="Costco", version=row.version)
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = json.dumps({
                "purpose": "intake_grouping", "lines": [{"fields": {}}]})
    rows = [intake_service.get_item(row.id) for row in rows]
    file_id = rows[0].batch_file_id
    handed = _handoff(migrated_client, batch_id, rows[0])
    assert handed.status_code == 201, handed.json

    # 界面要在点之前就说得出「为什么不能移除」，所以这个数字在文件上透出来。
    listed = migrated_client.get(f"/api/intake-batches/{batch_id}").json["files"][0]
    assert listed["item_count"] == 2 and listed["handed_off_item_count"] == 1
    refused = migrated_client.delete(f"/api/intake-batches/{batch_id}/files/{file_id}")
    assert refused.status_code == 409, refused.json
    assert refused.json["error"]["details"]["handed_off_item_count"] == 1
    assert "撤回填表" in refused.json["error"]["message"]

    first = intake_service.get_item(rows[0].id)
    withdrawn = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/withdraw", json={
        "items": [{"id": first.id, "version": first.version}]})
    assert withdrawn.status_code == 200, withdrawn.json
    assert migrated_client.get(
        f"/api/intake-batches/{batch_id}").json["files"][0]["handed_off_item_count"] == 0

    removed = migrated_client.delete(f"/api/intake-batches/{batch_id}/files/{file_id}")
    assert removed.status_code == 200, removed.json
    assert removed.json["removed_item_count"] == 2
    assert removed.json["bytes_still_referenced"] is False
    assert migrated_client.get(f"/api/intake-batches/{batch_id}").json["files"] == []
    assert migrated_client.get(f"/api/intake-batches/{batch_id}/items").json["total"] == 0

    # 移除就当没传过：同一份文件再传一次是**全新的一份**，旧分类不跟着回来（第 16 节）。
    again = migrated_client.post(
        f"/api/intake-batches/{batch_id}/files",
        data={"file": (io.BytesIO(content), "wrong.pdf")},
        content_type="multipart/form-data")
    assert again.status_code == 201, again.json
    back = migrated_client.get(f"/api/intake-batches/{batch_id}/items").json
    assert back["total"] == 2
    assert {row["decided_purchase_date"] for row in back["items"]} == {None}
    assert {row["decided_source_name"] for row in back["items"]} == {None}
    assert {row["decided_at"] for row in back["items"]} == {None}


def test_withdraw_refuses_a_draft_that_already_booked_a_purchase(
    migrated_client, migrated_database, intake_service,
):
    batch_id = intake_service.create_batch().id
    csv = "商品描述,数量,采购单位,单价,行金额\nRice,2,lb,3.00,6.00\n".encode("utf-8-sig")
    uploaded = migrated_client.post(f"/api/intake-batches/{batch_id}/spreadsheets", data={
        "file": (io.BytesIO(csv), "booked.csv"), "options": "{}"})
    assert uploaded.status_code == 201, uploaded.json
    row = intake_service.list_items(batch_id).items[0]
    row = intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                       source_name="Costco", version=row.version)
    handed = _handoff(migrated_client, batch_id, row)
    assert handed.status_code == 201, handed.json
    invoice_id = handed.json["invoice_id"]
    line = migrated_client.get(f"/api/invoices/{invoice_id}").json["lines"][0]
    confirmed = migrated_client.post(f"/api/invoices/{invoice_id}/confirm", json={
        "lines": [{"line_id": line["id"], "new_food_item": {
            "name": "Rice", "category_id": 5, "base_unit": "lb"}}]})
    assert confirmed.status_code == 200 and confirmed.json["confirmed"] == 1
    current = intake_service.get_item(row.id)
    refused = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/withdraw", json={
        "items": [{"id": current.id, "version": current.version}]})
    assert refused.status_code == 409, refused.json
    assert "撤销" in refused.json["error"]["message"]
    # 一行都没动：采购记录、单据和送审状态照旧。
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 1
        assert session.scalar(select(func.count()).select_from(Invoice)) == 1
    assert intake_service.get_item(row.id).handed_off_at is not None


def test_withdraw_rejects_an_item_that_never_started_a_form(
    migrated_client, intake_service, intake_ingest_service,
):
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(1), "fresh.pdf", "application/pdf"))
    row = intake_service.list_items(batch_id).items[0]
    refused = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/withdraw", json={
        "items": [{"id": row.id, "version": row.version}]})
    assert refused.status_code == 409, refused.json
    assert "还没有开始填表" in refused.json["error"]["message"]


def test_deleting_a_draft_takes_its_pages_off_the_organizing_board(
    migrated_client, intake_service, intake_ingest_service,
):
    """第 17 节 ④：删掉一张单据 = 清空数据并彻底移除，整理页上不留卡片。

    ⚠️ 这一项记的是**改过的**规则。第 11 节 ③ 原本让那几页留下来说「草稿已作废」
    并且可以撤回，为的是不让它们卡在「编辑中」。现在它们干脆不留——用户的话是
    「把这张单据从商家整理页面移除」，而一张删掉的单据在整理页上留几张什么也回答
    不了的卡片，正是第 13 节要删掉的那种东西。同一个文件的**其它页照常留着**。
    """
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "orphan.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    rows = [intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                         source_name="Costco", version=row.version)
            for row in rows]
    handed = _handoff(migrated_client, batch_id, rows[0])
    assert handed.status_code == 201, handed.json
    assert _items_payload(migrated_client, batch_id)[rows[0].id]["review_status"] == "draft"

    voided = migrated_client.delete(f"/api/invoices/{handed.json['invoice_id']}")
    assert voided.status_code == 200, voided.json
    assert voided.json["invoice"]["intake_sources"] == []

    after = _items_payload(migrated_client, batch_id)
    assert rows[0].id not in after, "删掉的单据不该在整理页上留下卡片"
    # 同一份 PDF 的另一页一个字没动，连它的分类一起。
    assert after[rows[1].id]["decided_purchase_date"] == "2026-11-08"
    assert after[rows[1].id]["decided_source_name"] == "Costco"
    # 那一页真的没了：再去碰它是 404，不是一张躲起来的卡片。
    gone = migrated_client.get(f"/api/intake-batches/{batch_id}/items/{rows[0].id}/original")
    assert gone.status_code == 404, gone.json
    # 文件还在批次里（字节没删），所以另一页仍然可以正常开始填表。
    refilled = _handoff(migrated_client, batch_id, intake_service.get_item(rows[1].id))
    assert refilled.status_code == 201, refilled.json


def _refill(client, batch_id, item):
    return client.post(f"/api/intake-batches/{batch_id}/reviews/refill", json={
        "items": [{"id": item.id, "version": item.version}]})


def test_refill_a_completed_invoice_voids_its_purchase_and_opens_an_empty_draft(
    migrated_client, migrated_database, intake_service,
):
    """第 20 节：已完成的单据在卡片上「重新填写」——入账被撤销，不是被拦住。

    ⚠️ 这一条**放宽了第 11 节 ③ 的边界 ③**（已入账的不许撤）。代价必须验到：
    那条采购记录要真的作废，否则同一张纸填两遍，价格历史里会出现两条。
    """
    batch_id = intake_service.create_batch().id
    csv = "商品描述,数量,采购单位,单价,行金额\nRice,2,lb,3.00,6.00\n".encode("utf-8-sig")
    uploaded = migrated_client.post(f"/api/intake-batches/{batch_id}/spreadsheets", data={
        "file": (io.BytesIO(csv), "refill.csv"), "options": "{}"})
    assert uploaded.status_code == 201, uploaded.json
    row = intake_service.list_items(batch_id).items[0]
    row = intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                       source_name="Costco", version=row.version)
    handed = _handoff(migrated_client, batch_id, row)
    assert handed.status_code == 201, handed.json
    first_invoice = handed.json["invoice_id"]
    line = migrated_client.get(f"/api/invoices/{first_invoice}").json["lines"][0]
    confirmed = migrated_client.post(f"/api/invoices/{first_invoice}/confirm", json={
        "lines": [{"line_id": line["id"], "new_food_item": {
            "name": "Rice", "category_id": 5, "base_unit": "lb"}}]})
    assert confirmed.status_code == 200 and confirmed.json["confirmed"] == 1
    completed = migrated_client.post(f"/api/invoices/{first_invoice}/complete", json={})
    assert completed.status_code == 200, completed.json
    assert _items_payload(migrated_client, batch_id)[row.id]["review_status"] == "completed"

    current = intake_service.get_item(row.id)
    refilled = _refill(migrated_client, batch_id, current)
    assert refilled.status_code == 200, refilled.json
    second_invoice = refilled.json["invoice_id"]
    assert refilled.json["previous_invoice_id"] == first_invoice
    assert refilled.json["voided_purchases"] == 1
    # 行上挂过采购记录，所以旧草稿**作废留着**：那条记录是价格历史的凭据。
    assert refilled.json["previous_invoice_kept"] is True
    assert second_invoice != first_invoice

    with migrated_database.unit_of_work() as session:
        purchases = list(session.scalars(select(Purchase)))
        assert len(purchases) == 1 and purchases[0].voided_at is not None
        reasons = [revision.reason for revision in session.scalars(select(PurchaseRevision))]
        assert any(reason and "重新填写" in reason for reason in reasons)
        old = session.get(Invoice, first_invoice)
        assert old.voided_at is not None and old.completed_at is None
        assert all(line.status == "pending" for line in old.lines)
    # 价格历史里不再计入这条撤销掉的采购。
    purchases_now = migrated_client.get("/api/purchases")
    assert purchases_now.status_code == 200 and purchases_now.json["items"] == []

    # 新草稿是空的（识别值照样预填进去，但一行都没确认），分类一个字没动。
    fresh = migrated_client.get(f"/api/invoices/{second_invoice}")
    assert fresh.status_code == 200, fresh.json
    assert fresh.json["status"] != "completed"
    assert [line["status"] for line in fresh.json["lines"]] == ["pending"]
    card = _items_payload(migrated_client, batch_id)[row.id]
    assert card["review_invoice_id"] == second_invoice
    assert card["review_status"] == "draft"
    assert card["handed_off_at"] is not None
    assert card["decided_purchase_date"] == "2026-11-08"
    assert card["decided_source_name"] == "Costco"


def test_refill_hard_deletes_a_draft_that_never_booked_anything(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    """没产生过任何账的草稿**真删**——与撤回填表同一条规则，没有第二套说法。"""
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "refill.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for row in rows:
        intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                     source_name="Costco", version=row.version)
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = json.dumps({
                "purpose": "intake_grouping", "lines": [{"fields": {}}]})
    rows = [intake_service.get_item(row.id) for row in rows]
    handed = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": row.id, "version": row.version} for row in rows]})
    assert handed.status_code == 201, handed.json
    first_invoice = handed.json["invoice_id"]
    line = migrated_client.get(f"/api/invoices/{first_invoice}").json["lines"][0]
    typed = migrated_client.patch(f"/api/invoices/{first_invoice}/lines/{line['id']}", json={
        "description": "Pork belly", "version": line["version"]})
    assert typed.status_code == 200, typed.json

    current = intake_service.get_item(rows[0].id)
    refilled = _refill(migrated_client, batch_id, current)
    assert refilled.status_code == 200, refilled.json
    # 选了一页，整张草稿的两页一起重来——一张草稿就是一张发票。
    assert refilled.json["item_ids"] == sorted(row.id for row in rows)
    assert refilled.json["previous_invoice_kept"] is False
    assert refilled.json["cleared_lines"] == 2 and refilled.json["voided_purchases"] == 0
    # ⚠️ 旧草稿是真删的，而 SQLite 可能把同一个 id 发给新草稿，所以证据是**库里
    # 只剩一张单据、填过的字没了**，不是「那个 id 不见了」（同第 11 节 ③ 那个坑）。
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 1
    second = migrated_client.get(f"/api/invoices/{refilled.json['invoice_id']}")
    assert [row["description"] for row in second.json["lines"]] == [None, None]


def test_refill_refuses_a_selection_that_spans_two_drafts(
    migrated_client, intake_service, intake_ingest_service,
):
    """一张草稿就是一张发票；跨两张时「重新填写」答不出「重填成几张」。"""
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "two.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    for row in rows:
        intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                     source_name="Costco", version=row.version)
    drafts = set()
    for row in rows:
        current = intake_service.get_item(row.id)
        handed = _handoff(migrated_client, batch_id, current)
        assert handed.status_code == 201, handed.json
        drafts.add(handed.json["invoice_id"])
    assert len(drafts) == 2
    refused = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/refill", json={
        "items": [{"id": row.id, "version": intake_service.get_item(row.id).version}
                  for row in rows]})
    assert refused.status_code == 409, refused.json
    assert "一次只能重新填写一张单据" in refused.json["error"]["message"]


def test_refill_rejects_an_item_that_never_started_a_form(
    migrated_client, intake_service, intake_ingest_service,
):
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(1), "fresh.pdf", "application/pdf"))
    row = intake_service.list_items(batch_id).items[0]
    refused = _refill(migrated_client, batch_id, row)
    assert refused.status_code == 409, refused.json
    assert "还没有开始填表" in refused.json["error"]["message"]


def test_the_batch_counts_how_many_items_are_not_finished_yet(
    migrated_client, intake_service,
):
    """第 22 节：整理页底下那个「确认完成所有填写」要问服务端「还剩几项」。

    不能数页面上已加载的卡片：整理页只加载展开了的那几组（同第 18 节 ①
    「重试识别失败项」要问服务端的那条理由）。
    """
    batch_id = intake_service.create_batch().id
    csv = (
        "商品描述,数量,采购单位,单价,行金额\n"
        "Rice,2,lb,3.00,6.00\n"
        "Flour,1,lb,4.00,4.00\n"
    ).encode("utf-8-sig")
    uploaded = migrated_client.post(f"/api/intake-batches/{batch_id}/spreadsheets", data={
        "file": (io.BytesIO(csv), "finish.csv"), "options": "{}"})
    assert uploaded.status_code == 201, uploaded.json

    batch = migrated_client.get(f"/api/intake-batches/{batch_id}").json
    assert batch["item_count"] == 2
    assert batch["unfinished_item_count"] == 2

    invoices = []
    for index, row in enumerate(intake_service.list_items(batch_id).items):
        row = intake_service.classify_item(row.id, purchase_date="2026-11-08",
                                           source_name="Costco", version=row.version)
        handed = _handoff(migrated_client, batch_id, row)
        assert handed.status_code == 201, handed.json
        invoice_id = handed.json["invoice_id"]
        invoices.append(invoice_id)
        # 开始填表还不算填完：计数不动。
        started = migrated_client.get(f"/api/intake-batches/{batch_id}").json
        assert started["unfinished_item_count"] == 2 - index

        line = migrated_client.get(f"/api/invoices/{invoice_id}").json["lines"][0]
        confirmed = migrated_client.post(f"/api/invoices/{invoice_id}/confirm", json={
            "lines": [{"line_id": line["id"], "new_food_item": {
                "name": f"Item {index}", "category_id": 5, "base_unit": "lb"}}]})
        assert confirmed.status_code == 200, confirmed.json
        completed = migrated_client.post(f"/api/invoices/{invoice_id}/complete", json={})
        assert completed.status_code == 200, completed.json

        after = migrated_client.get(f"/api/intake-batches/{batch_id}").json
        assert after["unfinished_item_count"] == 1 - index

    # 重新填写把它退回草稿，这一项又算没填完。
    row = intake_service.list_items(batch_id).items[0]
    refilled = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews/refill", json={
        "items": [{"id": row.id, "version": row.version}]})
    assert refilled.status_code == 200, refilled.json
    assert migrated_client.get(
        f"/api/intake-batches/{batch_id}").json["unfinished_item_count"] == 1


def test_closing_the_batch_ends_it_and_takes_away_resume_organizing(
    migrated_client, intake_service, intake_ingest_service,
):
    """第 22 节：「确认完成所有填写」点下去之后，首页那个「接着整理」得消失。

    那个入口靠的就是 `GET /current`，所以这里直接验它 404。
    """
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(1), "closing.pdf", "application/pdf"))
    assert migrated_client.get("/api/intake-batches/current").json["id"] == batch_id

    closed = migrated_client.post(f"/api/intake-batches/{batch_id}/close")
    assert closed.status_code == 200, closed.json
    assert closed.json["status"] == "closed"
    assert closed.json["closed_at"] is not None

    assert migrated_client.get("/api/intake-batches/current").status_code == 404
    # 批次本身还读得到（URL 里那个 id 仍然有效），只是不再接收新文件。
    again = migrated_client.get(f"/api/intake-batches/{batch_id}")
    assert again.status_code == 200 and again.json["status"] == "closed"
    uploaded = migrated_client.post(f"/api/intake-batches/{batch_id}/files", data={
        "file": (io.BytesIO(_pdf(1)), "late.pdf")}, content_type="multipart/form-data")
    assert uploaded.status_code == 409, uploaded.json
    # 再收一次口是 409，不是静默成功。
    assert migrated_client.post(f"/api/intake-batches/{batch_id}/close").status_code == 409
