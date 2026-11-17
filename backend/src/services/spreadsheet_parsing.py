"""Spreadsheet field mapping and conservative, exact normalization (step 06).

The original cells and issues remain evidence. Parsed values are editable draft
values, never confirmed purchases. Missing values are not derived from others.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from openpyxl.utils.cell import get_column_letter

from src.errors import ValidationError
from src.integrations.spreadsheets import Cell, Table
from src.models import DESCRIPTION_MAX_LENGTH, PURCHASE_UNIT_MAX_LENGTH, SOURCE_NAME_MAX_LENGTH
from src.normalization import normalize_date_text, normalize_number_text
from src.services.purchases import CURRENCIES, parse_decimal, parse_purchase_date

ALIASES = {
    "description": ("description", "original_description", "item", "item name", "product", "商品描述", "品名", "名称", "商品名称", "食材名称"),
    "quantity": ("quantity", "qty", "weight", "数量", "重量", "采购数量"),
    "purchase_unit": ("purchase_unit", "unit", "uom", "单位", "采购单位", "计价单位"),
    "unit_price": ("unit_price", "unit price", "price", "rate", "单价", "价格"),
    "line_amount": ("line_amount", "line total", "amount", "extended price", "金额", "行金额", "小计"),
    "purchase_date": ("purchase_date", "date", "invoice date", "采购日期", "日期"),
    "source_name": ("source_name", "source", "supplier", "vendor", "采购来源", "供应商", "来源"),
    "currency": ("currency", "币种", "货币"),
    "line_kind": ("line_kind", "kind", "行类型", "类型"),
}
FIELDS = tuple(ALIASES)
SHARED_FIELDS = ("purchase_date", "source_name", "currency")
SCALES = {"quantity": 4, "unit_price": 4, "line_amount": 2}
TEXT_LENGTHS = {"description": DESCRIPTION_MAX_LENGTH, "purchase_unit": PURCHASE_UNIT_MAX_LENGTH,
                "source_name": SOURCE_NAME_MAX_LENGTH}
OPTION_FIELDS = {"sheet", "header_row", "mapping", "encoding", "delimiter", "number_format",
                 "date_order", "defaults", "invoice_number", "invoice_total"}


def clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def key(text: str) -> str:
    return re.sub(r"[\s_\-]+", "", clean(text).casefold())


def fail(message: str, field: str | None = None) -> None:
    raise ValidationError(message, details={"field": field} if field else None)


def options_for(raw: object) -> dict:
    if not isinstance(raw, dict):
        fail("options 必须是 JSON 对象。", "options")
    unknown = set(raw) - OPTION_FIELDS
    if unknown:
        fail(f"不支持的导入选项：{', '.join(sorted(unknown))}。", "options")
    options = {"header_row": 1, "mapping": {}, "encoding": "utf-8-sig", "delimiter": ",",
               "number_format": "strict", "date_order": "strict", "defaults": {}, **raw}
    header = options["header_row"]
    if isinstance(header, bool) or not isinstance(header, int) or not 1 <= header <= 5000:
        fail("header_row 必须是 1 至 5000 的原始行号。", "header_row")
    if options.get("sheet") is not None and not isinstance(options["sheet"], str):
        fail("sheet 必须是工作表名称。", "sheet")
    for name, allowed in (("number_format", ("strict", "decimal_dot", "decimal_comma")),
                          ("date_order", ("strict", "mdy", "dmy")),
                          ("encoding", ("utf-8-sig", "utf-8", "gb18030", "utf-16")),
                          ("delimiter", (",", ";", "\t"))):
        if options[name] not in allowed:
            fail(f"{name} 必须是 {' / '.join(allowed)}。", name)
    if not isinstance(options["mapping"], dict) or set(options["mapping"]) - set(FIELDS):
        fail("mapping 只能把已支持的字段名映射到列标题或从 1 开始的列号。", "mapping")
    defaults = options["defaults"]
    if not isinstance(defaults, dict) or set(defaults) - set(SHARED_FIELDS):
        fail("defaults 只支持 purchase_date、source_name、currency。", "defaults")
    for name, value in defaults.items():
        if value is not None and not isinstance(value, str):
            fail(f"defaults.{name} 必须是字符串或 null。", name)
    for name in ("invoice_number", "invoice_total"):
        if options.get(name) is not None and not isinstance(options[name], str):
            fail(f"{name} 必须用字符串表示。", name)
    return options


def parse_number(cell: Cell, field: str, number_format: str) -> str:
    if cell.number_format and "%" in cell.number_format:
        fail("百分比单元格不是采购数量或金额。", field)
    if cell.kind in ("b", "e", "date"):
        fail("布尔值、错误值或日期不能作为数量/价格。", field)
    text = clean(cell.text).replace("−", "-")
    if cell.kind == "n":
        try:
            value = Decimal(text)
        except InvalidOperation:
            fail("无效的 Excel 数值。", field)
        if not value.is_finite() or abs(value.as_tuple().exponent) > 20:
            fail("Excel 数值超出支持范围。", field)
        text = format(value, "f")
    else:
        # 纯文本那一段的规则由 src/normalization.py 统一提供：步骤 08 的 OCR
        # 通路调的是同一份代码，于是同一张单据走表格和走拍照不会得到不同的数值。
        # `strip_trailing_unit=False`：表格有独立的单位列，数量格里出现 `kg`
        # 说明列映射错了，报错让人改映射比默默吃掉单位正确。
        normalized = normalize_number_text(
            text, number_format=number_format, strip_trailing_unit=False
        )
        if not normalized.ok:
            fail(f"{normalized.problem}。", field)
        text = normalized.text or ""
    value = parse_decimal(text, field=field, scale=SCALES[field], allow_zero=True, allow_negative=True)
    return format(value, "f")


def parse_date(cell: Cell, order: str) -> str:
    text = clean(cell.text)
    if cell.kind == "date":
        if not cell.date_value:
            fail("Excel 日期无效或包含时刻，请填写 YYYY-MM-DD。", "purchase_date")
        text = cell.date_value
    elif cell.kind in ("n", "b", "e"):
        fail("没有日期格式的数值不会猜成 Excel 日期。", "purchase_date")
    # 日期写法的识别同样由 src/normalization.py 统一提供（与 OCR 通路同一份）。
    normalized = normalize_date_text(text, date_order=order)
    if not normalized.ok:
        fail(f"{normalized.problem}。", "purchase_date")
    try:
        result = date.fromisoformat(normalized.text or "")
        return parse_purchase_date(result).isoformat()
    except ValueError as exc:
        raise ValidationError("日期不存在或格式无效。", details={"field": "purchase_date"}) from exc


def parse_value(field: str, cell: Cell, options: dict):
    text = clean(cell.text)
    if not text:
        return None
    if field in SCALES:
        return parse_number(cell, field, options["number_format"])
    if field == "purchase_date":
        return parse_date(cell, options["date_order"])
    if cell.kind in ("b", "e"):
        fail("单元格包含布尔值或 Excel 错误。", field)
    if field == "currency":
        text = {"人民币": "CNY", "美元": "USD", "RMB": "CNY"}.get(text.upper(), text.upper())
        if text not in CURRENCIES:
            fail(f"币种只支持 {' / '.join(CURRENCIES)}。", field)
    elif field == "line_kind":
        text = {"采购": "purchase", "调整": "adjustment", "退货": "adjustment", "折让": "adjustment"}.get(text, text.lower())
        if text not in ("purchase", "adjustment"):
            fail("行类型必须是 purchase（采购）或 adjustment（调整）。", field)
    elif len(text) > TEXT_LENGTHS[field]:
        fail(f"字段最多 {TEXT_LENGTHS[field]} 字符。", field)
    return text


@dataclass(frozen=True)
class ParsedRow:
    number: int
    values: dict
    evidence: dict
    review: dict


@dataclass(frozen=True)
class ParsedTable:
    table: Table
    mapping: dict[str, int]
    rows: tuple[ParsedRow, ...]
    blank_rows: int


def parse_table(table: Table, options: dict) -> ParsedTable:
    headers = next((cells for number, cells in table.rows if number == options["header_row"]), None)
    if headers is None:
        fail("找不到指定表头行。", "header_row")
    if any(cell.formula is not None for cell in headers):
        fail("表头必须是固定文字，不能是公式。", "header_row")
    mapping = {}
    for field, aliases in ALIASES.items():
        if field in options["mapping"]:
            selector = options["mapping"][field]
            if selector is None:
                continue  # explicit ignore
            if isinstance(selector, int) and not isinstance(selector, bool):
                matches = [selector - 1] if 1 <= selector <= len(headers) else []
            elif isinstance(selector, str):
                matches = [i for i, cell in enumerate(headers) if cell.text == selector]
            else:
                fail("列映射必须是标题、列号或 null（忽略）。", "mapping")
            if not matches:
                fail(f"找不到字段 {field} 指定的列。", "mapping")
        else:
            keys = {key(alias) for alias in aliases}
            matches = [i for i, cell in enumerate(headers) if key(cell.text) in keys]
        if len(matches) > 1:
            fail(f"字段 {field} 对应多列，请用列号明确 mapping。", "mapping")
        if matches:
            mapping[field] = matches[0]
    if len(set(mapping.values())) != len(mapping):
        fail("同一列不能映射到多个字段。", "mapping")
    if "description" not in mapping or not set(SCALES).intersection(mapping):
        fail("至少需要商品描述列和数量/单价/金额中的一列，其余缺值可在程序里补。", "mapping")
    rows = []
    blank = 0
    for number, cells in table.rows:
        if number <= options["header_row"]:
            continue
        if not any(cell.text.strip() or cell.formula is not None for cell in cells):
            blank += 1
            continue
        if len(cells) > len(headers) and any(cell.text or cell.formula is not None for cell in cells[len(headers):]):
            raise ValidationError("数据列多于表头；请检查含逗号字段的引号或列映射。",
                                  details={"sheet": table.sheet, "row": number, "field": "columns"})
        values = {name: None for name in FIELDS}
        values["line_kind"] = "purchase"
        issues = []
        overrides = []
        evidence = {"source": "spreadsheet", "sheet": table.sheet, "row": number,
                    "cells": {get_column_letter(i + 1): {"raw_text": cell.text, "kind": cell.kind,
                              "formula": cell.formula, "number_format": cell.number_format}
                              for i, cell in enumerate(cells)}, "fields": {}}
        for field, index in mapping.items():
            cell = cells[index] if index < len(cells) else Cell()
            column = get_column_letter(index + 1)
            location = {"field": field, "sheet": table.sheet, "row": number,
                        "column": column, "cell": f"{column}{number}"}
            raw = {"raw_text": cell.text, "column": column, "header": headers[index].text,
                   "formula": cell.formula, "status": "missing" if not cell.text else "recognized"}
            evidence["fields"][field] = raw
            if field in SHARED_FIELDS and (cell.text.strip() or cell.formula is not None):
                overrides.append(field)
            try:
                value = parse_value(field, cell, options)
                if value is not None or field != "line_kind":
                    values[field] = value
            except ValidationError as exc:
                raw["status"] = "invalid"
                issues.append({**location, "code": "invalid_value", "message": exc.message})
            if cell.formula is not None:
                issues.append({**location, "code": "formula_requires_review",
                               "message": "公式未执行；显示的缓存结果可能过期，请在程序中核对并保存此字段。"})
        # Explicit currency marks must never disappear during numeric cleanup.
        marks = set()
        for field in ("unit_price", "line_amount"):
            if field in mapping and mapping[field] < len(cells):
                money_cell = cells[mapping[field]]
                text = clean(money_cell.text).upper()
                if money_cell.number_format:
                    text += " " + money_cell.number_format.upper()
                marks.update(re.findall(r"\b(?:USD|CNY|EUR|GBP|CAD|AUD|JPY)\b", text))
                if "RMB" in text:
                    marks.add("CNY")
                for symbol, currency in (("$", "USD"), ("¥", "CNY"), ("€", "EUR"), ("£", "GBP")):
                    if symbol in text:
                        marks.add(currency)
        effective_currency = values["currency"] or options["defaults"].get("currency") or "USD"
        effective_currency = clean(effective_currency).upper()
        effective_currency = {"人民币": "CNY", "美元": "USD", "RMB": "CNY"}.get(effective_currency, effective_currency)
        if marks and marks != {effective_currency}:
            issues.append({"field": "currency", "sheet": table.sheet, "row": number,
                           "column": None, "cell": None, "code": "currency_conflict",
                           "message": f"金额中的币种 {', '.join(sorted(marks))} 与当前币种 {effective_currency} 不一致，请核对。"})
        evidence["issues"] = issues.copy()
        rows.append(ParsedRow(number, values, evidence, {"issues": issues, "overrides": overrides}))
    if not rows:
        fail("表头之后没有可导入的明细行。", "file")
    return ParsedTable(table, mapping, tuple(rows), blank)
