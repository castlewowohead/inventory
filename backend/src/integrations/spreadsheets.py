"""Bounded CSV / OOXML readers; cell numbers never pass through float.

openpyxl supplies column/style utilities and the XLSX writer. For input we read
OOXML <v> text directly: the ordinary workbook reader casts decimal cells to
float before application code gets them. No spreadsheet formulas are executed.
"""

from __future__ import annotations

import csv
import io
import posixpath
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format
from openpyxl.utils.cell import column_index_from_string, get_column_letter

from src.errors import ValidationError

MAX_ROWS = 5000
MAX_COLUMNS = 100
MAX_CELL_CHARS = 10000
MAX_XML_BYTES = 50 * 1024 * 1024
MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass(frozen=True)
class Cell:
    text: str = ""
    kind: str = "text"
    formula: str | None = None
    date_value: str | None = None
    number_format: str | None = None


@dataclass(frozen=True)
class Table:
    sheet: str
    sheets: tuple[str, ...]
    rows: tuple[tuple[int, tuple[Cell, ...]], ...]
    encoding: str | None = None


def _fail(message: str, **details) -> None:
    raise ValidationError(message, details=details or None)


def _cell(text: str | None, **kwargs) -> Cell:
    text = text or ""
    if len(text) > MAX_CELL_CHARS:
        _fail(f"单元格内容超过 {MAX_CELL_CHARS} 字符。")
    return Cell(text=text, **kwargs)


def read_table(content: bytes, extension: str, *, sheet: str | None = None,
               encoding: str = "utf-8-sig", delimiter: str = ",") -> Table:
    if extension == "csv":
        if sheet is not None:
            _fail("CSV 没有工作表，无需指定 sheet。", field="sheet")
        return _read_csv(content, encoding, delimiter)
    if extension != "xlsx":
        _fail("表格仅支持 .csv 和 .xlsx；旧 .xls 请另存为 .xlsx。", field="file")
    try:
        return _read_xlsx(content, sheet)
    except ValidationError:
        raise
    except (BadZipFile, KeyError, ValueError, IndexError, InvalidOperation,
            ET.ParseError, DefusedXmlException, OverflowError, RuntimeError, NotImplementedError) as exc:
        raise ValidationError("无法读取 XLSX：文件损坏、加密或不是受支持的工作簿。",
                              details={"field": "file"}) from exc


def _read_csv(content: bytes, encoding: str, delimiter: str) -> Table:
    if encoding not in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        _fail("encoding 仅支持 utf-8-sig、utf-8、gb18030、utf-16。", field="encoding")
    if delimiter not in (",", ";", "\t"):
        _fail("delimiter 只能是逗号、分号或制表符。", field="delimiter")
    if content.startswith((b"\xff\xfe", b"\xfe\xff")) and encoding == "utf-8-sig":
        encoding = "utf-16"
    try:
        text = content.decode(encoding)
    except UnicodeError as exc:
        raise ValidationError("CSV 编码不匹配，请明确选择编码；不会猜测或替换乱码。",
                              details={"field": "encoding"}) from exc
    if "\x00" in text:
        _fail("CSV 包含空字节，请检查文件和编码。", field="encoding")
    rows = []
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        while True:
            start_line = reader.line_num + 1
            values = next(reader, None)
            if values is None:
                break
            if len(rows) >= MAX_ROWS or len(values) > MAX_COLUMNS:
                _fail(f"表格最多 {MAX_ROWS} 行、{MAX_COLUMNS} 列。", row=start_line)
            rows.append((start_line, tuple(_cell(value) for value in values)))
    except csv.Error as exc:
        raise ValidationError("CSV 引号或列结构无效。", details={"row": reader.line_num}) from exc
    return Table("CSV", ("CSV",), tuple(rows), encoding)


def _xml(archive: ZipFile, name: str):
    return ET.fromstring(archive.read(name), forbid_dtd=True)


def _target(base: str, target: str) -> str:
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                             else posixpath.join(base, target))
    if not path.startswith("xl/") or "\\" in path:
        _fail("工作簿引用了不支持的外部路径。", field="file")
    return path


def _serial_date(text: str, epoch_1904: bool) -> str | None:
    value = Decimal(text)
    if not value.is_finite() or value != value.to_integral_value() or abs(value) > 1000000:
        return None  # 不截掉时刻，也不猜不完整的日期。
    serial = int(value)
    if not epoch_1904 and serial == 60:
        return None  # Excel 的虚构 1900-02-29。
    epoch = date(1904, 1, 1) if epoch_1904 else date(1899, 12, 30)
    if not epoch_1904 and 0 < serial < 60:
        serial += 1
    return (epoch + timedelta(days=serial)).isoformat()


def _read_xlsx(content: bytes, requested: str | None) -> Table:
    with ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if (len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_XML_BYTES
                or len({item.filename for item in entries}) != len(entries)):
            _fail("XLSX 解压后过大或包含重复部件。", field="file")
        workbook = _xml(archive, "xl/workbook.xml")
        relations = _xml(archive, "xl/_rels/workbook.xml.rels")
        paths = {item.get("Id"): _target("xl", item.get("Target", ""))
                 for item in relations if item.get("TargetMode") != "External"}
        sheets = workbook.find(MAIN + "sheets")
        if sheets is None or not len(sheets):
            _fail("工作簿没有工作表。", field="sheet")
        names = tuple(item.get("name", "") for item in sheets)
        if requested is None:
            if len(names) != 1:
                _fail("工作簿有多个工作表，请选择要导入的 sheet。", field="sheet", sheets=list(names))
            requested = names[0]
        if requested not in names:
            _fail("找不到指定工作表。", field="sheet", sheets=list(names))
        chosen = next(item for item in sheets if item.get("name") == requested)
        path = paths[chosen.get(REL + "id")]
        shared = []
        string_path = next((paths.get(item.get("Id")) for item in relations
                            if item.get("Type", "").endswith("/sharedStrings")), None)
        if string_path:
            shared = ["".join(node.itertext()) if node.tag == MAIN + "t" else
                      "".join(t.text or "" for t in node.iter(MAIN + "t"))
                      for node in _xml(archive, string_path)]
        date_styles = set()
        style_formats = []
        style_path = next((paths.get(item.get("Id")) for item in relations
                           if item.get("Type", "").endswith("/styles")), None)
        if style_path:
            styles = _xml(archive, style_path)
            formats = dict(BUILTIN_FORMATS)
            custom = styles.find(MAIN + "numFmts")
            if custom is not None:
                formats.update({int(item.get("numFmtId")): item.get("formatCode", "") for item in custom})
            xfs = styles.find(MAIN + "cellXfs")
            if xfs is not None:
                style_formats = [formats.get(int(item.get("numFmtId", "0")), "") for item in xfs]
                date_styles = {i for i, item in enumerate(xfs)
                               if is_date_format(formats.get(int(item.get("numFmtId", "0")), ""))}
        props = workbook.find(MAIN + "workbookPr")
        epoch_1904 = props is not None and props.get("date1904") in ("1", "true")
        root = _xml(archive, path)
        merged = root.find(MAIN + "mergeCells")
        if merged is not None and len(merged):
            _fail("所选工作表含合并单元格，请整理为一行一条明细后导入。", field="sheet", sheet=requested)
        data = root.find(MAIN + "sheetData")
        if data is None:
            _fail("工作表没有数据。", field="sheet")
        rows = []
        last_row = 0
        for row in data:
            row_number = int(row.get("r", str(last_row + 1)))
            if row_number <= last_row or row_number > MAX_ROWS:
                _fail(f"工作表行号无效或超过 {MAX_ROWS} 行。", row=row_number)
            last_row = row_number
            cells: dict[int, Cell] = {}
            for item in row.findall(MAIN + "c"):
                address = item.get("r", "")
                match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", address)
                if not match or int(match[2]) != row_number:
                    _fail("无效的单元格坐标。", cell=address)
                column = column_index_from_string(match[1])
                if column > MAX_COLUMNS or column in cells:
                    _fail(f"单元格列号重复或超过 {MAX_COLUMNS} 列。", cell=address)
                kind = item.get("t", "n")
                text = item.findtext(MAIN + "v", "")
                formula_node = item.find(MAIN + "f")
                formula = (formula_node.text or "[shared formula]") if formula_node is not None else None
                date_value = None
                if kind == "s":
                    index = int(text)
                    if index < 0:
                        _fail("无效的共享字符串编号。", cell=address)
                    text = shared[index]
                elif kind == "inlineStr":
                    text = "".join(t.text or "" for t in item.iter(MAIN + "t"))
                elif kind == "n" and text and int(item.get("s", "0")) in date_styles:
                    kind = "date"
                    date_value = _serial_date(text, epoch_1904)
                elif kind == "d":
                    kind = "date"
                    date_value = text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None
                style_index = int(item.get("s", "0"))
                number_format = style_formats[style_index] if 0 <= style_index < len(style_formats) else None
                cells[column] = _cell(text, kind=kind, formula=formula, date_value=date_value,
                                      number_format=number_format)
            width = max(cells, default=0)
            rows.append((row_number, tuple(cells.get(i, Cell()) for i in range(1, width + 1))))
        return Table(requested, names, tuple(rows))


def write_table(headers: list[str], rows: list[list[str]], extension: str) -> bytes:
    """Exports store decimal strings exactly; XLSX text cells never become formulas."""
    if extension == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        # Text columns are escaped by the caller; numeric columns retain negative signs.
        writer.writerow(headers)
        writer.writerows(rows)
        return output.getvalue().encode("utf-8-sig")
    if extension != "xlsx":
        _fail("format 必须是 csv 或 xlsx。", field="format")
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Purchases")
    for values in [headers, *rows]:
        cells = []
        for value in values:
            cell = WriteOnlyCell(sheet, value=value)
            cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()
