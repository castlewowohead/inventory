"""Step 06 HTTP boundary: file/options -> Service -> JSON or attachment."""

import io
import json

from flask import Blueprint, current_app, jsonify, request, send_file

from src.api.schemas import invoice_payload, query_bool, query_int, query_kind, query_text
from src.errors import ValidationError
from src.factory import get_config, get_database, get_file_storage, get_recognizer
from src.services.invoices import InvoiceService, UploadedFile
from src.services.purchases import PurchaseService
from src.services.spreadsheets import SpreadsheetService, template

spreadsheets_bp = Blueprint("spreadsheets", __name__, url_prefix="/api")


def service() -> SpreadsheetService:
    app = current_app
    config = get_config(app)
    database = get_database(app)
    invoices = InvoiceService(database, file_storage=get_file_storage(app),
                              recognizer=get_recognizer(app), upload_max_bytes=config.upload_max_bytes)
    return SpreadsheetService(invoices, PurchaseService(database), max_bytes=config.upload_max_bytes)


def _input():
    items = request.files.getlist("file")
    if len(items) != 1 or set(request.files) != {"file"}:
        raise ValidationError("请用 file 字段上传一个 CSV / XLSX 文件。", details={"field": "file"})
    if set(request.form) - {"options"} or len(request.form.getlist("options")) > 1:
        raise ValidationError("导入选项请放在唯一的 options JSON 对象中。", details={"field": "options"})
    try:
        options = json.loads(request.form.get("options", "{}"))
    except (ValueError, TypeError) as exc:
        raise ValidationError("options 不是有效 JSON。", details={"field": "options"}) from exc
    item = items[0]
    content = item.read(get_config(current_app).upload_max_bytes + 1)
    return UploadedFile(content, item.filename or "", item.mimetype), options


def _download(content: bytes, extension: str, name: str):
    mime = "text/csv; charset=utf-8" if extension == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return send_file(io.BytesIO(content), mimetype=mime, as_attachment=True,
                     download_name=f"{name}.{extension}")


@spreadsheets_bp.post("/imports/preview")
def preview():
    upload, options = _input()
    return jsonify(service().preview(upload, options)), 200


@spreadsheets_bp.post("/imports")
def create_import():
    upload, options = _input()
    return jsonify(invoice_payload(service().create(upload, options))), 201


@spreadsheets_bp.get("/imports/template")
def get_template():
    extension = request.args.get("format", "xlsx")
    return _download(template(extension), extension, "purchase-import-template")


@spreadsheets_bp.get("/purchases/export")
def export():
    allowed = {"format", "food_item_id", "source", "date_from", "date_to", "undated", "kind", "include_voided"}
    if set(request.args) - allowed:
        raise ValidationError("导出使用筛选条件，不接受分页或未知参数。")
    extension = request.args.get("format", "xlsx")
    content = service().export(extension,
        food_item_id=query_int(request.args, "food_item_id"), source=query_text(request.args, "source"),
        date_from=query_text(request.args, "date_from"), date_to=query_text(request.args, "date_to"),
        undated=query_bool(request.args, "undated"), kind=query_kind(request.args),
        include_voided=bool(query_bool(request.args, "include_voided")))
    return _download(content, extension, "purchase-details")
