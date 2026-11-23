"""M12 acceptance: the whole chain on a migrated temporary database.

What this file is for, and what it is **not**:

- It walks 混合上传 → 整理 → 送审 → 填写 → 确认 → 收口 → 价格历史回查 in one test,
  across the three ingest kinds that exist (图片、PDF 页、表格行). Each step on
  its own already has tests under its own `Mxx`; what nobody tested before is
  that the **handover points** line up — that a page classified in M06 is the
  same page M08 hands to the invoice draft, and that the purchase M05 never
  touched shows up in the M09 price history under the supplier the user typed
  on the organizing board.
- Every recognition result here is the **Fake** recognizer or a hand-written
  payload. Nothing in this file says anything about real OCR accuracy, and the
  PDFs are blank synthetic pages, not customer originals.
"""

import csv
import io
import json

from pypdf import PdfWriter
from sqlalchemy import func, select

from src.models import IntakeItem, Purchase
from src.services.invoices import UploadedFile


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _png() -> bytes:
    """A 1×1 PNG. Enough to be sniffed as an image and become one page."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, format="PNG")
    return out.getvalue()


def _csv() -> bytes:
    """One row in the import template's own Chinese headers (步骤 06 / M04)."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["商品描述", "数量", "采购日期", "采购来源"])
    writer.writerow(["Rice", "4", "2026-11-09", "FengYuan"])
    return stream.getvalue().encode("utf-8-sig")


def _grouping_payload() -> str:
    """The shape M08 reads off a page: a grouping round with one empty line."""
    return json.dumps({"purpose": "intake_grouping", "lines": [{"fields": {}}]})


def _classify(intake_service, item, *, date, source):
    return intake_service.classify_item(
        item.id, purchase_date=date, source_name=source, version=item.version)


def _fill_and_close(client, invoice_id, food_item_id, *, quantity, unit_price, amount):
    """Fill every pending line, confirm it, then 收口 — the existing review flow."""
    detail = client.get(f"/api/invoices/{invoice_id}").json
    confirmed = 0
    for line in detail["lines"]:
        if line["status"] != "pending":
            continue
        patched = client.patch(f"/api/invoices/{invoice_id}/lines/{line['id']}", json={
            "description": "Rice", "quantity": quantity, "purchase_unit": "lb",
            "unit_price": unit_price, "line_amount": amount, "version": line["version"]})
        assert patched.status_code == 200, patched.json
        ok = client.post(f"/api/invoices/{invoice_id}/confirm", json={
            "lines": [{"line_id": line["id"], "food_item_id": food_item_id}]})
        assert ok.status_code == 200, ok.json
        confirmed += ok.json["confirmed"]
    complete = client.post(f"/api/invoices/{invoice_id}/complete", json={})
    assert complete.status_code == 200, complete.json
    assert complete.json["status"] == "completed"
    return confirmed


def test_mixed_batch_reaches_price_history_through_the_existing_review(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    """One batch of an image, a 2-page PDF and a CSV ends up in price history.

    The three ingest kinds are deliberately in the **same batch**: the loop has
    to hold for a batch the user actually uploads, not only for a batch made of
    one kind of file. Pages and rows become separate drafts (M08 refuses to mix
    them), which is why two drafts are closed here and not one.
    """
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_png(), "receipt.png", "image/png"))
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "two-pages.pdf", "application/pdf"))
    uploaded_csv = migrated_client.post(f"/api/intake-batches/{batch_id}/spreadsheets", data={
        "file": (io.BytesIO(_csv()), "rows.csv"), "options": "{}"})
    assert uploaded_csv.status_code == 201, uploaded_csv.json

    items = list(intake_service.list_items(batch_id).items)
    pages = [row for row in items if row.kind == "page"]
    rows = [row for row in items if row.kind == "row"]
    assert len(pages) == 3 and len(rows) == 1

    # --- 整理：同一天同一商家 ------------------------------------------------
    decided = []
    for page in pages:
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, page.id).recognition_payload = _grouping_payload()
        decided.append(_classify(intake_service, page, date="2026-11-08", source="Costco"))
    decided_row = _classify(intake_service, rows[0], date="2026-11-09", source="FengYuan")

    groups = migrated_client.get(f"/api/intake-batches/{batch_id}/groups").json["groups"]
    assert {(g["purchase_date"], g["source_name"], g["item_count"]) for g in groups} == {
        ("2026-11-08", "Costco", 3), ("2026-11-09", "FengYuan", 1)}

    # --- 送审：组不是发票，选中的内容才是 -----------------------------------
    # The image alone is one invoice; the two PDF pages are another. The group
    # holds both, and only an explicit selection decides where one invoice ends.
    by_name = {row.original_name: row for row in decided}
    image_handoff = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": by_name["receipt.png"].id, "version": by_name["receipt.png"].version}]})
    assert image_handoff.status_code == 201, image_handoff.json
    pdf_pages = [row for row in decided if row.original_name == "two-pages.pdf"]
    pdf_handoff = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": row.id, "version": row.version} for row in pdf_pages]})
    assert pdf_handoff.status_code == 201, pdf_handoff.json
    row_handoff = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": decided_row.id, "version": decided_row.version}]})
    assert row_handoff.status_code == 201, row_handoff.json
    assert len({image_handoff.json["invoice_id"], pdf_handoff.json["invoice_id"],
                row_handoff.json["invoice_id"]}) == 3

    # --- 填写 → 确认 → 收口 -------------------------------------------------
    food = migrated_client.post("/api/food-items", json={
        "name": "Rice", "category_id": 5, "base_unit": "lb"})
    assert food.status_code == 201, food.json
    food_id = food.json["id"]
    assert _fill_and_close(migrated_client, image_handoff.json["invoice_id"], food_id,
                           quantity="2", unit_price="3.00", amount="6.00") == 1
    assert _fill_and_close(migrated_client, pdf_handoff.json["invoice_id"], food_id,
                           quantity="5", unit_price="2.80", amount="14.00") == 2
    assert _fill_and_close(migrated_client, row_handoff.json["invoice_id"], food_id,
                           quantity="4", unit_price="3.50", amount="14.00") == 1

    # --- 价格历史回查 -------------------------------------------------------
    history = migrated_client.get(f"/api/prices/food-items/{food_id}/history?limit=50")
    assert history.status_code == 200, history.json
    records = [row["purchase"] for row in history.json["items"]]
    assert len(records) == 4
    # The supplier and date the user typed on the board are what price history
    # compares by — not a file name and not an upload timestamp.
    assert {(r["purchase_date"], r["source_name"]) for r in records} == {
        ("2026-11-08", "Costco"), ("2026-11-09", "FengYuan")}
    # Each record still points back at the invoice line it was confirmed from.
    assert all(r["source_invoice"] is not None for r in records)
    sources = migrated_client.get(f"/api/prices/food-items/{food_id}/sources").json
    assert {entry["source_name"] for entry in sources["sources"]} == {"Costco", "FengYuan"}
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 4

    # --- 回看原件仍然经批次的内容引用 ---------------------------------------
    original = migrated_client.get(
        f"/api/intake-batches/{batch_id}/items/{pdf_pages[1].id}/original")
    assert original.status_code == 200
    assert original.data[:5] == b"%PDF-"


def test_one_file_spans_two_suppliers_and_an_undated_page_cannot_be_handed_off(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    """两条 M12 点到的合成边界：同文件跨商家，以及缺日期不能送审。

    同文件跨商家是「文件不是分类单位」的反例：三页同一份 PDF，两页归 Costco、
    一页归 FengYuan，所以两张草稿各自成立，而第三页留在原处。缺日期的那一页**不是
    识别失败**，只是还没人定过——送审必须被拒掉而不是建出一张没有采购日的单据。
    """
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(4), "one-file.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)
    assert len(rows) == 4
    for row in rows:
        with migrated_database.unit_of_work() as session:
            session.get(IntakeItem, row.id).recognition_payload = _grouping_payload()

    costco = [_classify(intake_service, rows[0], date="2026-11-08", source="Costco"),
              _classify(intake_service, rows[1], date="2026-11-08", source="Costco")]
    fengyuan = _classify(intake_service, rows[2], date="2026-11-09", source="FengYuan")
    # 第四页只定了日期：它留在那一天的「商家待定」里。
    undated_source = _classify(intake_service, rows[3], date="2026-11-09", source=None)

    groups = {(g["purchase_date"], g["source_name"]): g["item_count"]
              for g in migrated_client.get(f"/api/intake-batches/{batch_id}/groups").json["groups"]}
    assert groups == {("2026-11-08", "Costco"): 2, ("2026-11-09", "FengYuan"): 1,
                      ("2026-11-09", None): 1}

    first = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": row.id, "version": row.version} for row in costco]})
    assert first.status_code == 201, first.json
    second = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": fengyuan.id, "version": fengyuan.version}]})
    assert second.status_code == 201, second.json
    assert first.json["invoice_id"] != second.json["invoice_id"]

    # 跨商家不能混成一张单据，缺商家的那一页也不能送审。
    mixed = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": costco[0].id, "version": costco[0].version},
                  {"id": fengyuan.id, "version": fengyuan.version}]})
    assert mixed.status_code == 409, mixed.json
    refused = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": undated_source.id, "version": undated_source.version}]})
    assert refused.status_code == 409, refused.json
    assert "商家" in refused.json["error"]["message"]

    # 缺日期同样被拒，并且被拒的那一页仍然留在批次里可以继续整理。
    other = intake_service.create_batch().id
    intake_ingest_service.upload(other, UploadedFile(_pdf(1), "no-date.pdf", "application/pdf"))
    only_source = list(intake_service.list_items(other).items)[0]
    only_source = intake_service.classify_item(
        only_source.id, source_name="Costco", version=only_source.version)
    assert only_source.decided_purchase_date is None
    no_date = migrated_client.post(f"/api/intake-batches/{other}/reviews", json={
        "items": [{"id": only_source.id, "version": only_source.version}]})
    assert no_date.status_code == 409, no_date.json
    assert migrated_client.get(f"/api/intake-batches/{other}/items").json["items"][0][
        "handed_off_at"] is None


def test_handed_off_contents_are_marked_and_refuse_organizing_moves(
    migrated_client, migrated_database, intake_service, intake_ingest_service,
):
    """`handed_off_at` is on the wire, and a move of such a content is a 409.

    The organizing page had no way to know this state (M10 验证记录 ④): the card
    looked movable and only the server's 409 stopped it. Exposing the field is
    what lets the page say "这一项已经进入审核" before the user tries.
    """
    batch_id = intake_service.create_batch().id
    intake_ingest_service.upload(batch_id, UploadedFile(_pdf(2), "pair.pdf", "application/pdf"))
    rows = list(intake_service.list_items(batch_id).items)

    listed = migrated_client.get(f"/api/intake-batches/{batch_id}/items").json["items"]
    assert [item["handed_off_at"] for item in listed] == [None, None]

    first = _classify(intake_service, rows[0], date="2026-11-08", source="Costco")
    with migrated_database.unit_of_work() as session:
        session.get(IntakeItem, first.id).recognition_payload = _grouping_payload()
    handed = migrated_client.post(f"/api/intake-batches/{batch_id}/reviews", json={
        "items": [{"id": first.id, "version": first.version}]})
    assert handed.status_code == 201, handed.json

    after = {item["id"]: item for item in
             migrated_client.get(f"/api/intake-batches/{batch_id}/items").json["items"]}
    assert after[first.id]["handed_off_at"] is not None
    assert after[rows[1].id]["handed_off_at"] is None
    # The group listing feeds the same cards, so it carries the flag too.
    grouped = migrated_client.get(
        f"/api/intake-batches/{batch_id}/groups/items"
        "?purchase_date=2026-11-08&source_name=Costco").json["items"]
    assert [item["handed_off_at"] for item in grouped] == [after[first.id]["handed_off_at"]]

    moved = migrated_client.post(f"/api/intake-batches/{batch_id}/moves", json={
        "request_key": "m12-after-handoff", "action": "group",
        "items": [{"id": first.id, "version": after[first.id]["version"]}],
        "purchase_date": "2026-11-09", "source_name": "FengYuan"})
    assert moved.status_code == 409, moved.json
    still = migrated_client.get(f"/api/intake-batches/{batch_id}/items").json["items"]
    assert {item["id"]: item["decided_source_name"] for item in still}[first.id] == "Costco"
