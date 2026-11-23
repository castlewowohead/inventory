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
from src.services.intake_spreadsheets import IntakeSpreadsheetService

spreadsheets_bp = Blueprint("spreadsheets", __name__, url_prefix="/api")


def service() -> SpreadsheetService:
    app = current_app
    config = get_config(app)
    database = get_database(app)
    invoices = InvoiceService(database, file_storage=get_file_storage(app),
                              recognizer=get_recognizer(app), upload_max_bytes=config.upload_max_bytes)
    return SpreadsheetService(invoices, PurchaseService(database), max_bytes=config.upload_max_bytes)


def intake_service() -> IntakeSpreadsheetService:
    app = current_app
    return IntakeSpreadsheetService(get_database(app), get_file_storage(app),
                                    max_bytes=get_config(app).upload_max_bytes)


def _input():
    items = request.files.getlist("file")
    if len(items) != 1 or set(request.files) != {"file"}:
        raise ValidationError("请选择一个 CSV 或 XLSX 表格文件。", details={"field": "file"})
    if set(request.form) - {"options"} or len(request.form.getlist("options")) > 1:
        raise ValidationError("表格导入设置有误，请重新选择文件并重试。", details={"field": "options"})
    try:
        options = json.loads(request.form.get("options", "{}"))
    except (ValueError, TypeError) as exc:
        raise ValidationError("表格导入设置有误，请重新选择文件并重试。", details={"field": "options"}) from exc
    item = items[0]
    content = item.read(get_config(current_app).upload_max_bytes + 1)
    return UploadedFile(content, item.filename or "", item.mimetype), options


def _intake_input(*, allow_confirmation: bool = False):
    allowed = {"options", "confirm_duplicate"} if allow_confirmation else {"options"}
    if set(request.form) - allowed or any(len(request.form.getlist(k)) > 1 for k in allowed):
        raise ValidationError("表格导入设置有误。", details={"field": "options"})
    if len(request.files.getlist("file")) != 1 or set(request.files) != {"file"}:
        raise ValidationError("请选择一个 CSV 或 XLSX 表格文件。", details={"field": "file"})
    try:
        options = json.loads(request.form.get("options", "{}"))
    except (ValueError, TypeError) as exc:
        raise ValidationError("表格导入设置不是有效 JSON。", details={"field": "options"}) from exc
    confirmation = request.form.get("confirm_duplicate", "false")
    if confirmation not in ("true", "false"):
        raise ValidationError("confirm_duplicate 必须是 true 或 false。", details={"field": "confirm_duplicate"})
    item = request.files["file"]
    content = item.read(get_config(current_app).upload_max_bytes + 1)
    return UploadedFile(content, item.filename or "", item.mimetype), options, confirmation == "true"


@spreadsheets_bp.post("/intake-batches/<int:batch_id>/spreadsheets/preview")
def preview_intake_spreadsheet(batch_id: int):
    upload, options, _ = _intake_input()
    return jsonify(intake_service().preview(batch_id, upload, options,
        limit=query_int(request.args, "limit", 50), offset=query_int(request.args, "offset", 0))), 200


@spreadsheets_bp.post("/intake-batches/<int:batch_id>/spreadsheets")
def upload_intake_spreadsheet(batch_id: int):
    upload, options, confirmed = _intake_input(allow_confirmation=True)
    return jsonify(intake_service().upload(batch_id, upload, options,
                                           confirm_duplicate=confirmed)), 201


@spreadsheets_bp.get("/intake-batches/<int:batch_id>/spreadsheets/rows")
def intake_spreadsheet_rows(batch_id: int):
    return jsonify(intake_service().list_rows(batch_id,
        limit=query_int(request.args, "limit", 50), offset=query_int(request.args, "offset", 0)))


@spreadsheets_bp.get("/intake-batches/<int:batch_id>/spreadsheets/rows/<int:item_id>")
def intake_spreadsheet_row(batch_id: int, item_id: int):
    return jsonify(intake_service().get_row(batch_id, item_id))


@spreadsheets_bp.get("/intake-batches/<int:batch_id>/spreadsheets/groups")
def intake_spreadsheet_groups(batch_id: int):
    return jsonify({"groups": intake_service().groups(batch_id)})


@spreadsheets_bp.get("/intake-batches/<int:batch_id>/spreadsheets/<int:batch_file_id>")
def intake_spreadsheet_file(batch_id: int, batch_file_id: int):
    return jsonify(intake_service().get_file(batch_id, batch_file_id))


@spreadsheets_bp.get("/intake-batches/<int:batch_id>/spreadsheets/<int:batch_file_id>/original")
def intake_spreadsheet_original(batch_id: int, batch_file_id: int):
    key, name = intake_service().original(batch_id, batch_file_id)
    return send_file(get_file_storage(current_app).path_for(key), as_attachment=True, download_name=name)


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
