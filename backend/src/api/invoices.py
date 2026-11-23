"""发票上传、审核草稿与逐行确认接口（步骤 05）。

本层只做三件事：解析请求、调用 Service、把结果或异常转成响应。没有业务判断，
也不碰 Session——事务边界在 Service。

接口一览（前缀 `/api`）：

| 方法   | 路径                                             | 说明                                   |
| ------ | ------------------------------------------------ | -------------------------------------- |
| POST   | /api/invoices                                    | 上传文件并建草稿（multipart）           |
| GET    | /api/invoices                                    | 单据列表（`completed=false` 是待办）    |
| GET    | /api/invoices/<id>                               | 详情：文件、行、匹配候选、对账          |
| PATCH  | /api/invoices/<id>                               | 改单据头                                |
| DELETE | /api/invoices/<id>                               | 作废整张单据；已入账行需显式确认        |
| POST   | /api/invoices/<id>/files                          | 补传文件（multipart）                   |
| DELETE | /api/invoices/<id>/files/<file_id>                | 删掉拍坏的文件（不能删到一个不剩）      |
| GET    | /api/invoices/<id>/files/<file_id>/content        | 回看原件                                |
| POST   | /api/invoices/<id>/lines                          | 手工加一行                              |
| PATCH  | /api/invoices/<id>/lines/<line_id>                | 改一行 / 标成跳过 / 改回待确认          |
| DELETE | /api/invoices/<id>/lines/<line_id>                | 删掉手工加的行（识别出来的行用跳过）    |
| POST   | /api/invoices/<id>/confirm                        | 确认选定行或全部待确认行                |
| POST   | /api/invoices/<id>/lines/<line_id>/unconfirm      | 撤销一行的确认                          |
| POST   | /api/invoices/<id>/complete                       | 收口确认：这张单据处理完了              |
| POST   | /api/invoices/<id>/reopen                         | 已完成单据回到审核页                    |
| GET    | /api/invoices/match                               | 问「这句描述以前确认到哪个食材」        |

三处与别处不同的约定：

1. **上传走 `multipart/form-data`**，文件字段名是 `files`（可重复，一次多页）。
   表单字段 `recognize=false` 可以只存文件不识别——老板对着照片手工加行，这是真实
   OCR 没接通之前最实在的一条路。
2. **批量确认返回 200，哪怕有的行失败。** 响应里每一行各自带成功或失败的原因
   （`results`），因为"三行确认两行、第三行报错"是**正常结果**而不是请求失败。
   整个请求级别的错误（单据不存在、整张单作废）才用 4xx。
3. **数值一律是字符串**（与步骤 04 一致）：JSON 的小数是双精度浮点，前端
   `JSON.parse` 会把 `1234567.89` 变成 double。请求也请用字符串，传 JSON 小数会被
   明确拒绝。
"""

from __future__ import annotations

import mimetypes

from flask import Blueprint, Response, current_app, jsonify, request, send_file

from src.api.schemas import (
    confirm_outcome_payload,
    field,
    invoice_line_payload,
    invoice_page_payload,
    invoice_payload,
    json_object,
    match_candidates_payload,
    query_bool,
    query_int,
    query_text,
    reject_unknown_fields,
    require_non_empty,
)
from src.errors import ValidationError
from src.factory import build_services
from src.services.extraction import ExtractionService
from src.services.invoices import ConfirmOption, InvoiceService, UploadedFile
from src.services.ingredients import DEFAULT_LIMIT, UNSET

invoices_bp = Blueprint("invoices", __name__, url_prefix="/api")

UPLOAD_FIELD = "files"

INVOICE_PATCH_FIELDS = (
    "source_name",
    "invoice_number",
    "invoice_date",
    "currency",
    "invoice_total",
)

LINE_FIELDS = (
    "description",
    "quantity",
    "purchase_unit",
    "unit_price",
    "line_amount",
    "food_item_id",
    "line_kind",
    "purchase_date",
    "source_name",
    "currency",
)
# 改一行时还可以带状态与乐观锁版本；`line_kind` 可以改（录错了类别就改），
# 但 `status` 只能在 pending / skipped 之间切——确认必须走确认接口。
LINE_PATCH_FIELDS = LINE_FIELDS + ("status", "skip_reason", "version")

CONFIRM_FIELDS = ("lines", "all")
CONFIRM_LINE_FIELDS = (
    "line_id",
    "version",
    "food_item_id",
    "new_food_item",
    "accept_amount_mismatch",
)
UNCONFIRM_FIELDS = ("version", "reason")


def service() -> InvoiceService:
    """按请求创建 Service。依赖全部来自应用工厂的注入，不读模块级全局量。"""
    return build_services(current_app)[0]


def extraction() -> ExtractionService:
    """按请求创建提取服务（步骤 08）。与单据服务成对装配，见 factory.build_services。"""
    return build_services(current_app)[1]


def _uploads() -> list[UploadedFile]:
    """把 multipart 里的文件读成服务层的入参。

    内容整份读进内存：单文件上限由配置约束，判重又必须先算摘要（流读完就没了）。
    """
    if not request.files:
        raise ValidationError(
            f"请用 multipart/form-data 上传，文件字段名是 {UPLOAD_FIELD}。",
            details={"field": UPLOAD_FIELD},
        )
    items = request.files.getlist(UPLOAD_FIELD)
    if not items:
        raise ValidationError(
            f"没有收到 {UPLOAD_FIELD} 字段的文件。"
            f"已收到的字段：{', '.join(sorted(request.files)) or '（无）'}。",
            details={"field": UPLOAD_FIELD},
        )
    return [
        UploadedFile(
            content=item.read(),
            original_name=item.filename or "",
            content_type=item.mimetype or None,
        )
        for item in items
    ]


def _form_flag(key: str, default: bool) -> bool:
    """读取 multipart 表单里的布尔标记。只认明确的写法，不做宽松解释。"""
    raw = request.form.get(key)
    if raw is None or not raw.strip():
        return default
    text = raw.strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise ValidationError(
        f"表单字段 {key} 必须是 true 或 false，收到 {raw!r}。", details={"field": key}
    )


def _json_flag(payload: dict, key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError(
            f"字段 {key} 必须是 true 或 false。", details={"field": key}
        )
    return value


# --- 单据 -------------------------------------------------------------------


@invoices_bp.post("/invoices")
def create_invoice() -> tuple[Response, int]:
    view = service().create_invoice(
        _uploads(), recognize=_form_flag("recognize", True)
    )
    return jsonify(invoice_payload(view)), 201


@invoices_bp.get("/invoices")
def search_invoices() -> tuple[Response, int]:
    page = service().search_invoices(
        completed=query_bool(request.args, "completed"),
        source=query_text(request.args, "source"),
        include_voided=bool(query_bool(request.args, "include_voided")),
        limit=query_int(request.args, "limit", DEFAULT_LIMIT),
        offset=query_int(request.args, "offset", 0),
    )
    return jsonify(invoice_page_payload(page)), 200


@invoices_bp.get("/invoices/match")
def match_description() -> tuple[Response, int]:
    """问「这句原始商品描述以前确认到哪个食材」。

    放在 `/invoices/match` 而不是某一行下面：老板手输一句描述时还没有行，前端也要
    能先问。命中只是候选，确认时必须显式把选中的食材带上。
    """
    description = query_text(request.args, "description")
    if not description:
        raise ValidationError(
            "请带上 description 查询参数。", details={"field": "description"}
        )
    candidates = service().match_candidates_for(
        description, source_name=query_text(request.args, "source")
    )
    return jsonify(match_candidates_payload(candidates)), 200


@invoices_bp.get("/invoices/<int:invoice_id>")
def get_invoice(invoice_id: int) -> tuple[Response, int]:
    return jsonify(invoice_payload(service().get_invoice(invoice_id))), 200


@invoices_bp.patch("/invoices/<int:invoice_id>")
def update_invoice(invoice_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, INVOICE_PATCH_FIELDS)
    require_non_empty(payload)
    view = service().update_invoice(
        invoice_id,
        source_name=field(payload, "source_name"),
        invoice_number=field(payload, "invoice_number"),
        invoice_date=field(payload, "invoice_date"),
        currency=field(payload, "currency"),
        invoice_total=field(payload, "invoice_total"),
    )
    return jsonify(invoice_payload(view)), 200


@invoices_bp.delete("/invoices/<int:invoice_id>")
def void_invoice(invoice_id: int) -> tuple[Response, int]:
    """作废单据；已入账行的 ID 必须与当前状态完全一致。"""
    confirmed_line_ids = None
    if request.data:
        payload = json_object(request)
        reject_unknown_fields(payload, ("confirmed_line_ids",))
        confirmed_line_ids = payload.get("confirmed_line_ids")
        if (not isinstance(confirmed_line_ids, list)
                or any(type(value) is not int or value <= 0 for value in confirmed_line_ids)
                or len(set(confirmed_line_ids)) != len(confirmed_line_ids)):
            raise ValidationError("confirmed_line_ids 必须是无重复的正整数列表。")
    view = service().void_invoice(invoice_id, confirmed_line_ids=confirmed_line_ids)
    return (
        jsonify(
            {
                "status": "voided",
                "message": "单据已删除，相关入库记录已撤销。修改历史会保留。",
                "invoice": invoice_payload(view),
            }
        ),
        200,
    )


@invoices_bp.post("/invoices/<int:invoice_id>/complete")
def complete_invoice(invoice_id: int) -> tuple[Response, int]:
    """收口确认。

    响应里把「确认了几行、跳过几行、几个文件、对账差多少」都给出来：这一下是漏行
    唯一会被拦住的时刻，界面要能把这些数字摆给老板看。
    """
    view = service().complete_invoice(invoice_id)
    reconciliation = view.reconciliation
    return (
        jsonify(
            {
                "status": "completed",
                "message": (
                    f"已确认 {reconciliation.confirmed_count} 行、"
                    f"跳过 {reconciliation.skipped_count} 行，"
                    f"共 {len(view.files)} 个原始文件。"
                ),
                "invoice": invoice_payload(view),
            }
        ),
        200,
    )


@invoices_bp.post("/invoices/<int:invoice_id>/reopen")
def reopen_invoice(invoice_id: int) -> tuple[Response, int]:
    view = service().reopen_invoice(invoice_id)
    return jsonify(invoice_payload(view)), 200


# --- 文件 -------------------------------------------------------------------


@invoices_bp.post("/invoices/<int:invoice_id>/files")
def add_files(invoice_id: int) -> tuple[Response, int]:
    view = service().add_files(invoice_id, _uploads())
    return jsonify(invoice_payload(view)), 201


@invoices_bp.delete("/invoices/<int:invoice_id>/files/<int:file_id>")
def delete_file(invoice_id: int, file_id: int) -> tuple[Response, int]:
    view = service().delete_file(invoice_id, file_id)
    return jsonify(invoice_payload(view)), 200


@invoices_bp.get("/invoices/<int:invoice_id>/files/<int:file_id>/content")
def get_file_content(invoice_id: int, file_id: int) -> Response:
    """回看原件。原样返回存下来的字节，不压缩、不转格式——它是核对用的证据。"""
    view, stream = service().open_file(invoice_id, file_id)
    mimetype = view.content_type or (
        mimetypes.guess_type(view.original_name)[0] or "application/octet-stream"
    )
    return send_file(
        stream,
        mimetype=mimetype,
        as_attachment=False,
        download_name=view.original_name,
    )


# --- 行 ---------------------------------------------------------------------


@invoices_bp.post("/invoices/<int:invoice_id>/lines")
def add_line(invoice_id: int) -> tuple[Response, int]:
    """手工加一行。所有字段都可以先留空，边看照片边填。"""
    payload = json_object(request)
    reject_unknown_fields(payload, LINE_FIELDS)
    view = service().add_line(
        invoice_id,
        description=payload.get("description"),
        quantity=payload.get("quantity"),
        purchase_unit=payload.get("purchase_unit"),
        unit_price=payload.get("unit_price"),
        line_amount=payload.get("line_amount"),
        food_item_id=payload.get("food_item_id"),
        line_kind=payload.get("line_kind", "purchase"),
        purchase_date=payload.get("purchase_date"),
        source_name=payload.get("source_name"),
        currency=payload.get("currency"),
    )
    return jsonify(invoice_line_payload(view)), 201


@invoices_bp.patch("/invoices/<int:invoice_id>/lines/<int:line_id>")
def update_line(invoice_id: int, line_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, LINE_PATCH_FIELDS)
    require_non_empty(payload)
    view = service().update_line(
        invoice_id,
        line_id,
        version=payload.get("version"),
        status=field(payload, "status"),
        skip_reason=field(payload, "skip_reason"),
        line_kind=field(payload, "line_kind"),
        description=field(payload, "description"),
        quantity=field(payload, "quantity"),
        purchase_unit=field(payload, "purchase_unit"),
        unit_price=field(payload, "unit_price"),
        line_amount=field(payload, "line_amount"),
        food_item_id=field(payload, "food_item_id"),
        purchase_date=field(payload, "purchase_date"),
        source_name=field(payload, "source_name"),
        currency=field(payload, "currency"),
    )
    return jsonify(invoice_line_payload(view)), 200


@invoices_bp.delete("/invoices/<int:invoice_id>/lines/<int:line_id>")
def delete_line(invoice_id: int, line_id: int) -> tuple[Response, int]:
    service().delete_line(invoice_id, line_id)
    return jsonify({"status": "deleted"}), 200


@invoices_bp.post("/invoices/<int:invoice_id>/lines/<int:line_id>/unconfirm")
def unconfirm_line(invoice_id: int, line_id: int) -> tuple[Response, int]:
    payload = request.get_json(silent=True) or {}
    if payload:
        reject_unknown_fields(payload, UNCONFIRM_FIELDS)
    view = service().unconfirm_line(
        invoice_id,
        line_id,
        version=payload.get("version"),
        reason=payload.get("reason"),
    )
    return (
        jsonify(
            {
                "status": "unconfirmed",
                "message": "这一行的采购记录已作废（仍可追溯），行已退回待确认。",
                "line": invoice_line_payload(view),
            }
        ),
        200,
    )


# --- 确认 -------------------------------------------------------------------


@invoices_bp.post("/invoices/<int:invoice_id>/confirm")
def confirm_lines(invoice_id: int) -> tuple[Response, int]:
    """确认选定的行，或者把所有待确认的行一次确认掉（`all: true`）。

    返回 200 并逐行给结果——一行失败不影响其它行，这是产品要求而不是容错：老板不该
    因为一行模糊就没法保存其它九行。
    """
    payload = json_object(request)
    reject_unknown_fields(payload, CONFIRM_FIELDS)
    confirm_all = _json_flag(payload, "all")
    raw_lines = payload.get("lines")

    if confirm_all and raw_lines is not None:
        raise ValidationError(
            "all 与 lines 只能给一个：无法判断要确认哪些行。",
            details={"field": "all"},
        )
    if not confirm_all and raw_lines is None:
        raise ValidationError(
            "请给出要确认的行（lines），或者传 all: true 确认所有待确认的行。",
            details={"field": "lines"},
        )

    options = None if confirm_all else _confirm_options(raw_lines)
    outcome = service().confirm_lines(invoice_id, options=options)
    return jsonify(confirm_outcome_payload(outcome)), 200


def _confirm_options(raw_lines: object) -> tuple[ConfirmOption, ...]:
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ValidationError(
            "lines 必须是非空数组，形如 [{\"line_id\": 1}]。",
            details={"field": "lines"},
        )
    options: list[ConfirmOption] = []
    for index, item in enumerate(raw_lines):
        if not isinstance(item, dict):
            raise ValidationError(
                f"lines[{index}] 必须是对象。", details={"field": "lines"}
            )
        reject_unknown_fields(item, CONFIRM_LINE_FIELDS)
        line_id = item.get("line_id")
        if not isinstance(line_id, int) or isinstance(line_id, bool):
            raise ValidationError(
                f"lines[{index}].line_id 必须是整数。",
                details={"field": "line_id"},
            )
        new_food_item = item.get("new_food_item")
        if new_food_item is not None and not isinstance(new_food_item, dict):
            raise ValidationError(
                f"lines[{index}].new_food_item 必须是对象。",
                details={"field": "new_food_item"},
            )
        accept = item.get("accept_amount_mismatch", False)
        if not isinstance(accept, bool):
            raise ValidationError(
                f"lines[{index}].accept_amount_mismatch 必须是 true 或 false。",
                details={"field": "accept_amount_mismatch"},
            )
        options.append(
            ConfirmOption(
                line_id=line_id,
                version=item.get("version"),
                food_item_id=item.get("food_item_id", UNSET),
                new_food_item=new_food_item,
                accept_amount_mismatch=accept,
            )
        )
    return tuple(options)


# --- 步骤 08：提取任务与供应商路由 -----------------------------------------


def _job_payload(job) -> dict:  # noqa: ANN001 - ExtractionJobView
    """一次提取任务的响应体。

    ⚠️ `prompt_sha12` 一定要给出来：步骤 07 的选型结论绑定在那个提示词上
    （sha256 前 12 位 `12e0246b0377`），换了提示词那批准确率数字就不再适用。
    事后要能从一条任务记录看出"这张单是用哪个被测配置提取的"。
    """
    return {
        "id": job.id,
        "invoice_id": job.invoice_id,
        "invoice_file_id": job.invoice_file_id,
        "status": job.status,
        "attempt": job.attempt,
        "doc_class": job.doc_class,
        "route_source": job.route_source,
        "provider": job.provider,
        "models": list(job.models),
        "prompt_sha12": job.prompt_sha12,
        "pages_total": job.pages_total,
        "pages_done": job.pages_done,
        "lines_created": job.lines_created,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "warnings": list(job.warnings),
        "usage": job.usage,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "created_at": job.created_at,
    }


@invoices_bp.get("/invoices/<int:invoice_id>/extraction")
def get_extraction(invoice_id: int) -> tuple[Response, int]:
    """这张单据的提取任务（最近的在前）。

    **提取状态是持久化的**（08.5）：老板拍完照可以去忙别的，关掉浏览器回来再查。
    失败也在这里——超时、限流、凭据失效都带着 `error_code` 与原因，
    而原始文件与已有草稿一概没动。
    """
    jobs = extraction().jobs_for(invoice_id)
    return (
        jsonify(
            {
                "items": [_job_payload(job) for job in jobs],
                "total": len(jobs),
            }
        ),
        200,
    )


@invoices_bp.post("/invoices/<int:invoice_id>/extraction")
def start_extraction(invoice_id: int) -> tuple[Response, int]:
    """重新提取这张单据。

    两道闸门：① 已经有任务在排队或在跑时返回 409（同一页图调两遍是白花钱）；
    ② 已经有行确认入账时返回 409，并提示先撤销那些行——重新提取会替换掉草稿行，
    和已入账的行对不上，而那种错位不报错。

    **人改过但还没确认的行会保留**，不会被一次重试抹掉。
    """
    jobs = extraction().schedule(invoice_id)
    return (
        jsonify({"items": [_job_payload(job) for job in jobs], "total": len(jobs)}),
        202,
    )


@invoices_bp.get("/invoices/<int:invoice_id>/supplier-candidates")
def get_supplier_candidates(invoice_id: int) -> tuple[Response, int]:
    """**这家供应商历史上买过的食材**，最近使用优先（08.3i）。

    手写件的候选来源。05.6 那条「按原始描述精确命中」在手写件上用不了——
    中文商品名跨模型分歧率 96.0%，模型给的名字几乎一定是错的。所以商品名这一格
    不走 OCR：**裁图给老板看，让他从这张清单里选**。他认的是自己的货，不是认字。

    `source` 查询参数可以覆盖单据头上的来源（单据头的供应商名也常常认不出来）。
    """
    invoices = service()
    source = query_text(request.args, "source")
    if not source:
        source = invoices.get_invoice(invoice_id).source_name
    if not source:
        raise ValidationError(
            "这张单据还没有供应商名称，按供应商给候选就无从下手。"
            "请先在单据头填上来源，或用 ?source= 指定。",
            details={"field": "source"},
        )
    candidates = invoices.supplier_candidates(
        source, limit=query_int(request.args, "limit") or DEFAULT_LIMIT
    )
    return jsonify(match_candidates_payload(candidates)), 200


@invoices_bp.get("/ocr-routes")
def list_ocr_routes() -> tuple[Response, int]:
    """供应商 → 提取路径的全部记录。"""
    routes = extraction().routes()
    return (
        jsonify(
            {
                "items": [
                    {
                        "source_name": route.source_name,
                        "source_name_normalized": route.source_name_normalized,
                        "doc_class": route.doc_class,
                        "decided_from": route.decided_from,
                        "date_order": route.date_order,
                        "note": route.note,
                    }
                    for route in routes
                ],
                "total": len(routes),
            }
        ),
        200,
    )


@invoices_bp.put("/ocr-routes")
def put_ocr_route() -> tuple[Response, int]:
    """人工指定一家供应商走哪条提取路径，以及它的日/月顺序。

    `date_order` 解一个实际麻烦：`6/11/2026` 的日/月顺序**文本本身定不死**，
    所以默认不解析、留空让人填。这里设成 `mdy` 之后，这家的单据就按月/日/年读——
    **每家供应商说一次**，而不是每张单据填一次。设空串清除。
    """
    payload = json_object(request)
    reject_unknown_fields(payload, ("source_name", "doc_class", "date_order", "note"))
    route = extraction().set_route(
        source_name=str(payload.get("source_name") or ""),
        doc_class=str(payload.get("doc_class") or ""),
        date_order=payload.get("date_order"),
        note=payload.get("note"),
    )
    return (
        jsonify(
            {
                "source_name": route.source_name,
                "source_name_normalized": route.source_name_normalized,
                "doc_class": route.doc_class,
                "decided_from": route.decided_from,
                "date_order": route.date_order,
                "note": route.note,
            }
        ),
        200,
    )
