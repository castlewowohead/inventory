"""Step 06 acceptance: exact input -> durable review -> purchases -> detail export."""

import csv
import io
import json
from datetime import date
from decimal import Decimal
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import func, select

from src.errors import ValidationError, ConflictError
from src.factory import create_app
from src.integrations.spreadsheets import Cell, read_table
from src.models import FoodItem, Invoice, InvoiceFile, InvoiceLine, Purchase
from src.services.invoices import ConfirmOption, UploadedFile
from src.services.spreadsheet_parsing import options_for, parse_date, parse_number
from src.services.spreadsheets import SpreadsheetService, TEMPLATE_HEADERS

HEADER = ["商品描述", "数量", "单位", "单价", "金额", "采购日期", "采购来源", "币种", "行类型"]
ROW = ["鸡胸肉", "2", "lb", "6.35", "12.70", "2026-10-01", "Costco", "USD", "purchase"]


def csv_bytes(rows, header=HEADER, encoding="utf-8-sig", delimiter=","):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue().encode(encoding)


def xlsx_bytes(rows, header=HEADER, configure=None):
    book = Workbook()
    sheet = book.active
    sheet.title = "采购明细"
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    if configure:
        configure(book, sheet)
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def replace_xml(content, path, old, new):
    output = io.BytesIO()
    with ZipFile(io.BytesIO(content)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == path:
                assert old in data
                data = data.replace(old, new)
            target.writestr(item.filename, data)
    return output.getvalue()


@pytest.fixture
def spreadsheets(invoice_service, purchase_service):
    return SpreadsheetService(invoice_service, purchase_service, max_bytes=1024 * 1024)


def upload(content=None, name="采购.csv"):
    return UploadedFile(content or csv_bytes([ROW]), name)


def post_file(client, content, name="采购.csv", options=None, path="/api/imports"):
    return client.post(path, data={"file": (io.BytesIO(content), name),
                                  "options": json.dumps(options or {}, ensure_ascii=False)})


@pytest.mark.parametrize("text,fmt,expected", [
    ("$1,234.56", "decimal_dot", "1234.56"),
    ("€1.234,56", "decimal_comma", "1234.56"),
    ("EUR 12,40", "decimal_comma", "12.40"),
    ("(20.00)", "strict", "-20.00"),
    ("（２０．００）", "strict", "-20.00"),
    ("￥１２３．４５", "strict", "123.45"),
    ("0.07", "strict", "0.07"),
    ("1234567.89", "strict", "1234567.89"),
])
def test_exact_amounts_and_chinese_input(text, fmt, expected):
    assert parse_number(Cell(text), "line_amount", fmt) == expected


@pytest.mark.parametrize("text,fmt", [
    ("1,234", "strict"), ("1,2,3", "decimal_dot"), ("1.234,56", "decimal_dot"),
    ("7.75%", "strict"), ("NaN", "strict"), ("Infinity", "strict"),
    ("12.345", "strict"), ("(-20.00)", "strict"), ("1 2", "strict"),
    ("1e2", "strict"),
])
def test_amounts_are_not_guessed_or_silently_rounded(text, fmt):
    with pytest.raises(ValidationError):
        parse_number(Cell(text), "line_amount", fmt)


@pytest.mark.parametrize("text,order,expected", [
    ("2026年10月4日", "strict", "2026-10-04"),
    ("２０２６／１０／０４", "strict", "2026-10-04"),
    ("14 October 2026", "strict", "2026-10-14"),
    ("Oct 4th, 2026", "strict", "2026-10-04"),
    ("13/10/2026", "strict", "2026-10-13"),
    ("04/10/2026", "mdy", "2026-04-10"),
    ("04/10/2026", "dmy", "2026-10-04"),
])
def test_dates_use_explicit_order_and_real_calendar(text, order, expected):
    assert parse_date(Cell(text), order) == expected


@pytest.mark.parametrize("text", ["04/10/2026", "11/08/26", "2026-02-30", "2026-13-01", "Januarygarbage 2, 2026"])
def test_ambiguous_or_invalid_dates_need_review(text):
    with pytest.raises(ValidationError):
        parse_date(Cell(text), "strict")


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_import_is_durable_review_not_ocr_or_direct_purchase(
    spreadsheets, invoice_service, purchase_service, food_item, migrated_database, config, storage, extension, monkeypatch
):
    monkeypatch.setattr(invoice_service._recognizer, "recognize", lambda *a: pytest.fail("OCR was called"))
    bad = ["牛肉", "1", "kg", "", "35.00", "03/04/2026", "肉铺", "USD", "purchase"]
    content = csv_bytes([ROW, bad]) if extension == "csv" else xlsx_bytes([ROW, bad])
    view = spreadsheets.create(upload(content, f"采购.{extension}"), {})
    assert view.entry_source == "import"
    assert view.recognition_provider is None
    assert view.recognition_is_fake is None
    assert purchase_service.search_purchases().total == 0
    first, second = view.lines
    assert first.quantity == Decimal("2")
    assert second.unit_price is None
    assert second.purchase_date is None
    assert second.review_issues[0]["field"] == "purchase_date"
    assert second.raw_fields["row"] == 3
    outcome = invoice_service.confirm_lines(view.id, options=[
        ConfirmOption(first.id, food_item_id=food_item.id), ConfirmOption(second.id, food_item_id=food_item.id)])
    assert (outcome.confirmed_count, outcome.failed_count) == (1, 1)
    purchase = purchase_service.get_purchase(outcome.results[0].purchase_id)
    assert purchase.entry_source == "import"
    assert purchase.source_invoice.invoice_id == view.id
    assert purchase.line_amount == Decimal("12.70")
    with pytest.raises(ConflictError) as conflict:
        spreadsheets.create(upload(content, f"renamed.{extension}"), {})
    assert conflict.value.details["invoice_id"] == view.id
    assert purchase_service.search_purchases().total == 1
    raw = second.raw_fields
    invoice_service.update_line(view.id, second.id, unit_price="35.00", purchase_date=None)
    fixed = invoice_service.get_invoice(view.id).lines[1]
    assert fixed.raw_fields == raw
    assert not fixed.review_issues
    assert invoice_service.confirm_lines(view.id, options=[ConfirmOption(second.id, food_item_id=food_item.id)]).confirmed_count == 1
    invoice_service.update_invoice(view.id, invoice_date="2026-10-01")
    invoice_service.complete_invoice(view.id)
    migrated_database.dispose()
    restarted = create_app(config, database=migrated_database, file_storage=storage).test_client()
    payload = restarted.get(f"/api/invoices/{view.id}").get_json()
    assert payload["status"] == "completed"
    assert payload["lines"][1]["raw_fields"] == raw
    assert payload["entry_source"] == "import"
    assert restarted.get(f"/api/invoices/{view.id}/files/{view.files[0].id}/content").data == content


def test_row_values_override_shared_defaults_and_unknown_date_does_not_inherit(spreadsheets, invoice_service, food_item, purchase_service):
    rows = [ROW, ["B", "1", "kg", "8", "8", "", "", "", "purchase"],
            ["C", "1", "kg", "8", "8", "03/04/2026", "", "", "purchase"]]
    view = spreadsheets.create(upload(csv_bytes(rows)), {"defaults": {"purchase_date": "2026-10-10", "source_name": "共享来源", "currency": "CNY"}})
    third = view.lines[2]
    invoice_service.update_line(view.id, third.id, purchase_date=None)
    result = invoice_service.confirm_lines(view.id, options=[ConfirmOption(line.id, food_item_id=food_item.id) for line in view.lines])
    assert result.confirmed_count == 3
    purchases = [purchase_service.get_purchase(item.purchase_id) for item in result.results]
    assert purchases[0].purchase_date == date(2026, 10, 1)
    assert purchases[0].currency == "USD"
    assert purchases[1].purchase_date == date(2026, 10, 10)
    assert purchases[1].source_name == "共享来源"
    assert purchases[1].currency == "CNY"
    assert purchases[2].purchase_date is None


def test_preview_mapping_sheet_and_original_coordinates(migrated_client, migrated_database):
    content = xlsx_bytes([["供应商的货品", "3", "7.50"]], ["货物", "件数", "实付"],
                         lambda book, sheet: book.create_sheet("说明"))
    missing = post_file(migrated_client, content, "vendor.xlsx", path="/api/imports/preview")
    assert missing.status_code == 400
    assert missing.json["error"]["details"]["sheets"] == ["采购明细", "说明"]
    options = {"sheet": "采购明细", "mapping": {"description": "货物", "quantity": "件数", "line_amount": 3}}
    response = post_file(migrated_client, content, "vendor.xlsx", options, "/api/imports/preview")
    assert response.status_code == 200, response.json
    row = response.json["rows"][0]
    assert row["values"]["quantity"] == "3"
    assert row["values"]["unit_price"] is None  # 7.50 / 3 is not an original price.
    assert row["raw_fields"]["fields"]["line_amount"]["column"] == "C"
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0


def test_xlsx_numeric_cells_use_xml_decimal_text_not_float(spreadsheets):
    content = xlsx_bytes([["精度", 1, "lb", 1, 1]])
    content = replace_xml(content, "xl/worksheets/sheet1.xml", b'<c r="D2" t="n"><v>1</v></c>',
                          b'<c r="D2" t="n"><v>9999999999.0001</v></c>')
    row = spreadsheets.preview(upload(content, "exact.xlsx"), {})["rows"][0]
    assert row["values"]["unit_price"] == "9999999999.0001"
    assert row["raw_fields"]["cells"]["D"]["raw_text"] == "9999999999.0001"


@pytest.mark.parametrize("cache", [b"", b"12.70"])
def test_formula_cache_needs_program_review(spreadsheets, invoice_service, food_item, cache):
    row = ROW.copy()
    row[4] = "=B2*D2"
    content = xlsx_bytes([row])
    if cache:
        content = replace_xml(content, "xl/worksheets/sheet1.xml", b"<f>B2*D2</f><v />", b"<f>B2*D2</f><v>" + cache + b"</v>")
    view = spreadsheets.create(upload(content, "formula.xlsx"), {})
    line = view.lines[0]
    assert line.line_amount == (Decimal("12.70") if cache else None)
    assert line.review_issues[0]["code"] == "formula_requires_review"
    assert invoice_service.confirm_lines(view.id, options=[ConfirmOption(line.id, food_item_id=food_item.id)]).failed_count == 1
    invoice_service.update_line(view.id, line.id, line_amount="12.70")
    assert invoice_service.confirm_lines(view.id, options=[ConfirmOption(line.id, food_item_id=food_item.id)]).confirmed_count == 1
    assert invoice_service.get_invoice(view.id).lines[0].raw_fields["cells"]["E"]["formula"] == "B2*D2"


@pytest.mark.parametrize("epoch_1904", [False, True])
def test_excel_native_date_uses_workbook_epoch(spreadsheets, epoch_1904):
    from openpyxl.utils.datetime import CALENDAR_MAC_1904
    row = ROW.copy()
    row[5] = date(2026, 10, 4)
    def configure(book, sheet):
        if epoch_1904:
            book.epoch = CALENDAR_MAC_1904
    payload = spreadsheets.preview(upload(xlsx_bytes([row], configure=configure), "dates.xlsx"), {})
    assert payload["rows"][0]["values"]["purchase_date"] == "2026-10-04"


def test_mixed_currencies_do_not_reconcile_to_a_fake_total(spreadsheets, invoice_service, food_item):
    other = ROW.copy()
    other[7] = "CNY"
    view = spreadsheets.create(upload(csv_bytes([ROW, other])), {"invoice_total": "25.40", "defaults": {"currency": "USD"}})
    outcome = invoice_service.confirm_lines(view.id, options=[ConfirmOption(row.id, food_item_id=food_item.id) for row in view.lines])
    assert outcome.confirmed_count == 2
    assert outcome.invoice.reconciliation.status == "mixed_currencies"
    assert outcome.invoice.reconciliation.confirmed_amount is None
    assert outcome.invoice.reconciliation.difference is None
    invoice_service.update_invoice(view.id, invoice_date="2026-10-01")
    assert invoice_service.complete_invoice(view.id).status == "completed"


def test_import_defaults_to_usd_and_allows_cny_override(spreadsheets):
    default = spreadsheets.create(upload(csv_bytes([ROW])), {})
    assert default.currency == "USD"
    another = ROW.copy()
    another[0] = "另一种商品"
    chosen = spreadsheets.create(upload(csv_bytes([another])), {"defaults": {"currency": "CNY"}})
    assert chosen.currency == "CNY"


def test_currency_symbols_and_percentage_styles_are_not_silently_discarded(spreadsheets):
    row = ROW.copy()
    row[3] = "EUR 6.35"
    payload = spreadsheets.preview(upload(csv_bytes([row])), {})
    assert payload["rows"][0]["review_issues"][0]["field"] == "currency"
    content = xlsx_bytes([ROW], configure=lambda book, sheet: setattr(sheet["D2"], "number_format", "0.00%"))
    result = spreadsheets.preview(upload(content, "percent.xlsx"), {})
    assert result["rows"][0]["values"]["unit_price"] is None
    assert result["rows"][0]["review_issues"][0]["field"] == "unit_price"


def test_imported_original_cannot_be_deleted_after_attaching_a_photo(spreadsheets, invoice_service):
    view = spreadsheets.create(upload(), {})
    invoice_service.add_files(view.id, [UploadedFile(b"extra photo", "extra.png")])
    with pytest.raises(ConflictError):
        invoice_service.delete_file(view.id, view.files[0].id)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "gb18030", "utf-16"])
def test_csv_encodings_and_quoted_multiline_fields(spreadsheets, encoding):
    row = ROW.copy()
    row[0] = '牛肉, "切片"\n第二行描述'
    payload = spreadsheets.preview(upload(csv_bytes([row], encoding=encoding)), {"encoding": encoding})
    assert payload["rows"][0]["values"]["description"] == row[0]
    assert payload["rows"][0]["source_row"] == 2


@pytest.mark.parametrize("options", [[], {"header_row": True}, {"mapping": []}, {"mapping": {"quantity": True}},
                                      {"number_format": "guess"}, {"defaults": {"currency": 123}},
                                      {"invoice_total": 12.3}, {"defaults": {"unknown": "x"}}])
def test_invalid_options_fail_as_400(migrated_client, options):
    response = migrated_client.post("/api/imports", data={"file": (io.BytesIO(csv_bytes([ROW])), "a.csv"),
                                                         "options": json.dumps(options)})
    assert response.status_code == 400, response.json


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_export_contains_only_selected_completed_invoice_details(
    migrated_client, spreadsheets, invoice_service, purchase_service, food_item, extension
):
    view = spreadsheets.create(upload(csv_bytes([ROW, ROW])), {})
    outcome = invoice_service.confirm_lines(view.id, options=[ConfirmOption(view.lines[0].id, food_item_id=food_item.id)])
    purchase_id = outcome.results[0].purchase_id
    purchase_service.update_purchase(purchase_id, unit_price="7.00", line_amount="14.00")
    purchase_service.create_purchase(line_kind="adjustment", quantity="1", purchase_unit="each",
                                     unit_price="-2", line_amount="-2", source_name="Costco")
    payload = {"format": extension, "invoice_ids": [view.id]}
    assert migrated_client.post("/api/invoices/export", json=payload).status_code == 409
    invoice_service.update_line(view.id, view.lines[1].id, status="skipped", skip_reason="不是采购行")
    invoice_service.update_invoice(view.id, invoice_date="2026-10-01")
    invoice_service.complete_invoice(view.id)
    other_row = ROW.copy()
    other_row[0] = "另一张单据"
    other = spreadsheets.create(upload(csv_bytes([other_row]), "other.csv"), {})
    invoice_service.confirm_lines(other.id, options=[ConfirmOption(other.lines[0].id, food_item_id=food_item.id)])
    invoice_service.update_invoice(other.id, invoice_date="2026-10-01")
    invoice_service.complete_invoice(other.id)
    response = migrated_client.post("/api/invoices/export", json=payload)
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    if extension == "csv":
        rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))
    else:
        book = load_workbook(io.BytesIO(response.data), read_only=True)
        values = list(book.active.values)
        rows = [dict(zip(values[0], row)) for row in values[1:]]
        book.close()
    assert len(rows) == 1  # Other invoice, skipped row, and adjustment are excluded.
    assert rows[0]["line_amount"] == "14.00"
    assert rows[0]["revision_count"] == "1"
    assert rows[0]["invoice_id"] == str(view.id)
    assert rows[0]["entry_source"] == "import"
    both = migrated_client.post("/api/invoices/export", json={"format": "csv", "invoice_ids": [view.id, other.id]})
    assert len(list(csv.DictReader(io.StringIO(both.data.decode("utf-8-sig"))))) == 2
    purchase_service.void_purchase(purchase_id)
    after_void = migrated_client.post("/api/invoices/export", json={"format": "csv", "invoice_ids": [view.id]})
    assert len(list(csv.DictReader(io.StringIO(after_void.data.decode("utf-8-sig"))))) == 0
    invoice_service.reopen_invoice(view.id)
    assert migrated_client.post("/api/invoices/export", json=payload).status_code == 409


def test_selected_export_escapes_formula_text_and_requires_selection(
    migrated_client, spreadsheets, invoice_service, food_item
):
    formula_row = ROW.copy()
    formula_row[0] = "=HYPERLINK(1)"
    view = spreadsheets.create(upload(csv_bytes([formula_row] * 25)), {})
    outcome = invoice_service.confirm_lines(view.id, options=[
        ConfirmOption(line.id, food_item_id=food_item.id) for line in view.lines
    ])
    assert outcome.confirmed_count == 25
    invoice_service.update_invoice(view.id, invoice_date="2026-10-01")
    invoice_service.complete_invoice(view.id)
    response = migrated_client.post("/api/invoices/export", json={"format": "csv", "invoice_ids": [view.id]})
    rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert len(rows) == 25
    assert rows[0]["original_description"] == "'=HYPERLINK(1)"
    response = migrated_client.post("/api/invoices/export", json={"format": "xlsx", "invoice_ids": [view.id]})
    book = load_workbook(io.BytesIO(response.data))
    assert book.active["I2"].data_type == "s"
    assert book.active["I2"].value == "=HYPERLINK(1)"
    book.close()
    assert migrated_client.post("/api/invoices/export", json={"format": "csv", "invoice_ids": []}).status_code == 400
    assert migrated_client.post("/api/invoices/export", json={"format": "csv", "invoice_ids": [view.id, view.id]}).status_code == 400
    assert migrated_client.post("/api/invoices/export", json={"format": "csv", "invoice_ids": [view.id, 999999]}).status_code == 409
    assert migrated_client.get("/api/purchases/export").status_code == 404


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_downloadable_templates(migrated_client, extension):
    response = migrated_client.get(f"/api/imports/template?format={extension}")
    assert response.status_code == 200
    table = read_table(response.data, extension)
    assert [cell.text for cell in table.rows[0][1]] == TEMPLATE_HEADERS


def test_import_creation_rolls_back_all_rows_and_removes_orphan_file(spreadsheets, migrated_database, storage, monkeypatch):
    from src.repositories.invoices import InvoiceLineRepository
    original = InvoiceLineRepository.add
    def broken(self, line):
        if line.line_no == 2:
            raise RuntimeError("simulated write failure")
        return original(self, line)
    monkeypatch.setattr(InvoiceLineRepository, "add", broken)
    with pytest.raises(RuntimeError):
        spreadsheets.create(upload(csv_bytes([ROW, ROW])), {})
    with migrated_database.unit_of_work() as session:
        for model in (Invoice, InvoiceFile, InvoiceLine, Purchase):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    assert not any(path.is_file() for path in storage.root.rglob("*"))


def test_bad_row_does_not_leave_new_food_item_behind(spreadsheets, invoice_service, migrated_database):
    row = ROW.copy()
    row[4] = "99.00"
    view = spreadsheets.create(upload(csv_bytes([row])), {})
    outcome = invoice_service.confirm_lines(view.id, options=[ConfirmOption(view.lines[0].id,
        new_food_item={"name": "Must Roll Back", "category_id": 1, "base_unit": "lb"})])
    assert outcome.failed_count == 1
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(FoodItem)) == 0
        assert session.scalar(select(func.count()).select_from(Purchase)) == 0


def test_malformed_files_locate_errors_and_never_create_a_batch(migrated_client, migrated_database):
    for content, name in ((b"not a zip", "broken.xlsx"), (b"legacy", "old.xls"),
                          (b'description,quantity\n"unclosed,2', "bad.csv"),
                          (b"description,quantity\na,1,extra", "bad-columns.csv")):
        response = post_file(migrated_client, content, name)
        assert response.status_code == 400, response.json
    with migrated_database.unit_of_work() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0


def test_changed_file_does_not_update_existing_purchase(spreadsheets, invoice_service, food_item, purchase_service):
    first = spreadsheets.create(upload(), {})
    confirmed = invoice_service.confirm_lines(first.id, options=[ConfirmOption(first.lines[0].id, food_item_id=food_item.id)])
    changed = ROW.copy()
    changed[3:5] = ["7", "14"]
    second = spreadsheets.create(upload(csv_bytes([changed])), {})
    assert second.id != first.id
    assert second.status == "draft"
    assert purchase_service.search_purchases().total == 1
    assert purchase_service.get_purchase(confirmed.results[0].purchase_id).line_amount == Decimal("12.70")


def test_duplicate_headers_require_mapping_by_position(spreadsheets):
    content = csv_bytes([["A", "2", "99", "1"]], ["description", "quantity", "quantity", "unit_price"])
    with pytest.raises(ValidationError):
        spreadsheets.preview(upload(content), {})
    preview = spreadsheets.preview(upload(content), {"mapping": {"quantity": 2}})
    assert preview["rows"][0]["values"]["quantity"] == "2"


def test_xlsx_merge_and_xml_entities_are_rejected(spreadsheets):
    merged = xlsx_bytes([ROW], configure=lambda book, sheet: sheet.merge_cells("A2:B2"))
    with pytest.raises(ValidationError):
        spreadsheets.preview(upload(merged, "merged.xlsx"), {})
    content = xlsx_bytes([ROW])
    content = replace_xml(content, "xl/workbook.xml", b"<workbook ", b'<!DOCTYPE workbook [<!ENTITY a "boom">]><workbook ')
    with pytest.raises(ValidationError):
        spreadsheets.preview(upload(content, "entity.xlsx"), {})
