"""Table import previews/drafts and detail exports. Excel is an input, not an editor."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.errors import ValidationError
from src.integrations.spreadsheets import read_table, write_table
from src.services.ingredients import normalize_name
from src.services.invoices import InvoiceService, UploadedFile
from src.services.purchases import DEFAULT_CURRENCY, PurchaseService, format_money, format_quantity, parse_decimal
from src.services.spreadsheet_parsing import (
    ParsedTable, options_for, parse_table, parse_value,
)
from src.integrations.spreadsheets import Cell

TEMPLATE_HEADERS = ["商品描述", "数量", "单位", "单价", "金额", "采购日期", "采购来源", "币种", "行类型"]
EXPORT_HEADERS = ["purchase_id", "line_kind", "food_item_id", "food_name", "food_name_zh", "category",
                  "purchase_date", "source_name", "original_description", "quantity", "purchase_unit",
                  "unit_price", "line_amount", "currency", "entry_source", "amount_mismatch",
                  "invoice_id", "invoice_line_id", "line_no", "revision_count", "voided_at"]
TEXT_EXPORT_COLUMNS = {3, 4, 5, 7, 8, 10}


class SpreadsheetService:
    def __init__(self, invoices: InvoiceService, purchases: PurchaseService, *, max_bytes: int):
        self._invoices = invoices
        self._purchases = purchases
        self._max_bytes = max_bytes

    def _parse(self, upload: UploadedFile, raw_options: object) -> tuple[ParsedTable, dict, dict]:
        return self.parse_upload(upload, raw_options, max_bytes=self._max_bytes)


    @staticmethod
    def parse_upload(upload: UploadedFile, raw_options: object, *, max_bytes: int) -> tuple[ParsedTable, dict, dict]:
        if not upload.content or len(upload.content) > max_bytes:
            raise ValidationError("文件为空或超过上传大小限制。", details={"field": "file", "max_bytes": max_bytes})
        if len(upload.original_name) > 255:
            raise ValidationError("文件名最多 255 字符。", details={"field": "file"})
        options = options_for(raw_options)
        extension = upload.original_name.rsplit(".", 1)[-1].lower()
        table = read_table(upload.content, extension, sheet=options.get("sheet"),
                           encoding=options["encoding"], delimiter=options["delimiter"])
        header = {}
        # Defaults are explicit user input. Invalid defaults fail the request;
        # invalid file cells stay attached to individual review rows instead.
        for field, value in options["defaults"].items():
            parsed = parse_value(field, Cell(value or ""), options)
            header["invoice_date" if field == "purchase_date" else field] = parsed
        header["currency"] = header.get("currency") or DEFAULT_CURRENCY
        if header.get("invoice_date"):
            header["invoice_date"] = date.fromisoformat(header["invoice_date"])
        source = header.get("source_name")
        header["source_name_normalized"] = normalize_name(source) if source else None
        number = options.get("invoice_number")
        if number and len(number) > 100:
            raise ValidationError("单号最多填写 100 个字符。", details={"field": "invoice_number"})
        header["invoice_number"] = number or None
        header["invoice_number_normalized"] = normalize_name(number) if number else None
        if options.get("invoice_total") is not None:
            header["invoice_total"] = parse_decimal(options["invoice_total"], field="invoice_total",
                                                    scale=2, allow_zero=True)
        parsed = parse_table(table, options)
        return parsed, options, header

    @staticmethod
    def _metadata(parsed: ParsedTable, options: dict) -> dict:
        return {"sheet": parsed.table.sheet, "options": options,
                "mapping": {field: index + 1 for field, index in parsed.mapping.items()},
                "row_count": len(parsed.rows), "blank_rows_ignored": parsed.blank_rows,
                "encoding": parsed.table.encoding,
                "notice": "需要修改时，请打开导入后的单据编辑。重新上传同一份表格可能产生重复记录。"}

    def preview(self, upload: UploadedFile, options: object) -> dict:
        parsed, normalized_options, header = self._parse(upload, options)
        return {"filename": upload.original_name, "sheets": list(parsed.table.sheets),
                "import_metadata": self._metadata(parsed, normalized_options),
                "defaults": {key: str(value) if value is not None else None for key, value in header.items()},
                "rows": [{"source_row": row.number, "values": row.values,
                          "raw_fields": row.evidence, "review_issues": row.review["issues"]}
                         for row in parsed.rows]}

    def create(self, upload: UploadedFile, options: object):
        parsed, normalized_options, header = self._parse(upload, options)
        rows = []
        for row in parsed.rows:
            values = dict(row.values)
            for name in ("quantity", "unit_price", "line_amount"):
                values[name] = Decimal(values[name]) if values[name] is not None else None
            if values["purchase_date"]:
                values["purchase_date"] = date.fromisoformat(values["purchase_date"])
            rows.append({"values": values, "evidence": row.evidence, "review": row.review})
        return self._invoices.create_import(upload, header=header,
            metadata=self._metadata(parsed, normalized_options), rows=rows)

    def export(self, extension: str, **filters) -> bytes:
        if extension not in ("csv", "xlsx"):
            raise ValidationError("请选择 CSV 或 XLSX 格式。", details={"field": "format"})
        records = self._purchases.export_purchases(**filters)
        rows = []
        for item in records:
            food, source = item.food_item, item.source_invoice
            values = [str(item.id), item.line_kind, str(food.id) if food else "",
                      food.name if food else "", (food.name_zh or "") if food else "",
                      food.category.name if food else "",
                      item.purchase_date.isoformat() if item.purchase_date else "",
                      item.source_name or "", item.original_description or "", format_quantity(item.quantity),
                      item.purchase_unit, format_money(item.unit_price), format_money(item.line_amount),
                      item.currency, item.entry_source, "true" if item.amount_mismatch else "false",
                      str(source.invoice_id) if source else "", str(source.invoice_line_id) if source else "",
                      str(source.line_no) if source else "", str(item.revision_count),
                      item.voided_at.isoformat() if item.voided_at else ""]
            if extension == "csv":
                for index in TEXT_EXPORT_COLUMNS:
                    text = values[index]
                    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
                        values[index] = "'" + text
            rows.append(values)
        return write_table(EXPORT_HEADERS, rows, extension)


def template(extension: str) -> bytes:
    return write_table(TEMPLATE_HEADERS, [], extension)
