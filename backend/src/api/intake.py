"""混合上传的整理批次：建批次、逐文件上传、处理状态与回看原件（多文件计划 M03）。

本层只做三件事：解析请求、调用 Service、把结果或异常转成响应。没有业务判断，
也不碰 Session——事务边界在 Service。

接口一览（前缀 `/api`）：

| 方法   | 路径                                               | 说明                                    |
| ------ | -------------------------------------------------- | --------------------------------------- |
| POST   | /api/intake-batches                                | 新建一个整理批次                         |
| GET    | /api/intake-batches/current                        | **恢复**：最近一个有文件的开放批次       |
| GET    | /api/intake-batches/<id>                           | 批次与逐文件处理状态（轮询这个）         |
| GET    | /api/intake-batches/<id>/items                     | 分页读可归类内容（页/行卡片）            |
| POST   | /api/intake-batches/<id>/files                     | **逐文件**上传（multipart，字段 `file`） |
| POST   | /api/intake-batches/<id>/process                   | 驱动切页队列：恢复未切的、可选重试失败的 |
| DELETE | /api/intake-batches/<id>/files/<file_id>            | 把一个文件从批次里撤掉（可反悔）         |
| GET    | /api/intake-batches/<id>/files/<file_id>/original   | 回看整份原件                             |
| GET    | /api/intake-batches/<id>/items/<item_id>/original   | 回看某一页的原件（页号在响应头里）       |
| POST   | /api/intake-batches/<id>/recognize                 | 驱动日期/商家识别队列（M05）             |
| POST   | /api/intake-batches/<id>/items/<item_id>/reparse-date | 用保存的原文按已确认商家重解析日期（M05）|
| GET    | /api/intake-batches/<id>/groups                   | 分页读日期/商家派生组（M06）             |
| GET    | /api/intake-batches/<id>/groups/items             | 分页读指定组的内容（M06）               |
| POST   | /api/intake-batches/<id>/moves                    | 批量人工归组（M06）                     |
| POST   | /api/intake-batches/<id>/moves/<move_id>/undo     | 撤销最近一次移动（M06）                 |
| POST   | /api/intake-batches/<id>/suggest-sources          | 重算商家建议：同文件 / 历史别名 / 版式（M07）|
| GET    | /api/intake-batches/<id>/piles                    | 相似外观的待办堆（M07，只读）           |
| GET    | /api/source-memory                                | 人确认过的来源与别名（M07，跨批次）     |
| GET    | /api/source-memory/aliases                        | 别名列表（M07）                         |
| POST   | /api/source-memory/aliases                        | 记下或**更正**一条别名（M07）           |
| DELETE | /api/source-memory/aliases/<alias_id>             | 撤销一条别名（M07）                     |

⚠️ **图片与 PDF 走 `/files`，CSV 与 XLSX 走 M04 的
`/api/intake-batches/<id>/spreadsheets`**：两者切出来的内容落在同一张
`intake_items` 上（页 / 行），但入口分开——表格要列映射与预览，图片要的是页数。
批次本身（建、恢复、状态、内容列表）只有这一处。

六处与别处不同的约定：

1. **一次一个文件**，字段名是 `file`（不可重复）。与 `POST /api/invoices` 的
   `files`（同一张单据的多页）刻意不同：这里十几份文件属于**不同**的单据，
   一次请求一份，第 2 份失败不会连累第 1 份。
2. **上传只存字节、只登记，不等切页。** 响应里的 `queued` 说明切页是不是交给后台
   了；是的话 `ingest` 为 null，前端去轮询批次状态。**没有百分比**，只有离散状态。
3. **文件与内容嵌在批次下**，并且**真的校验归属**：`/<batch_id>/files/<file_id>`
   里两个 id 对不上就是 404，而不是照样把别的批次的原件返回去。
4. **「现在有没有可继续整理的文件」用 `GET /current`**，没有就是 404 而不是
   空对象：前端据此决定「接着整理」还是「新建一批」，两者不能长得一样。
5. **回看原件原样返回存下来的字节**，不压缩、不转格式——它是核对用的证据。PDF 的
   某一页返回的是**整份 PDF**，页号在 `X-Intake-Page` 响应头里；本项目没有 PDF
   栅格化依赖，后端生成不出"第 3 页那张图"，由前端的查看器跳页。
6. **撤掉文件返回「那份字节还有人用吗」**（`bytes_still_referenced`）：同一份字节
   可能还在别的批次里，所以撤掉不等于能删盘上的文件。
7. **识别队列与切页队列是两条**（M05）：`/process` 把文件切成页，`/recognize` 为
   页与表格行读日期/商家候选。分开是因为代价不同——切页是本地计算，识别**每页要
   花钱**。两条都可重复驱动；`/recognize` 对已经识别过的内容**一次调用都不会发
   生**，响应里的 `model_calls` 如实报出这一次真的调了几次。
8. **`reparse-date` 一次模型都不调**：它用保存下来的日期原文配上**人工确认过的**
   商家的 `date_order` 重新解析。商家还没确认就是 400——拿相似度猜出来的商家去定
   日历日，错了也不会报错。
9. **`/suggest-sources` 也是一次模型都不调**（M07）：同文件参考、历史别名与版式
   相似全部是本地计算与数据库查询，建议服务**结构上**没有识别器依赖。它写的全是
   候选；纸面读出的来源与同文件/外观建议冲突时**单页证据优先**，被压住的那条进
   `conflicts` 让人看得见。
10. **整堆命名走 M06 那一条 `/moves` 命令**（`action: "source"`，只写来源、保留
   日期），不另开一条写路径：相似堆里的几张卡片常常各属不同日期，而拖拽、多选
   批量与点选目标组必须执行同一条后端命令（第 4 节）。
"""

from __future__ import annotations

import mimetypes

from flask import Blueprint, Response, current_app, jsonify, request, send_file

from src.api.schemas import (
    intake_alias_payload,
    intake_batch_payload,
    intake_file_payload,
    intake_ingested_payload,
    intake_item_page_payload,
    intake_item_payload,
    intake_pile_board_payload,
    intake_recognized_payload,
    intake_source_memory_payload,
    intake_suggestion_run_payload,
    intake_upload_payload,
    json_object,
    query_bool,
    query_int,
    reject_unknown_fields,
)
from src.errors import NotFoundError, ValidationError
from src.factory import (
    build_intake_services,
    build_intake_suggestion_service,
    get_database,
)
from src.services.intake import IntakeService
from src.services.intake_ingest import IntakeIngestService
from src.services.intake_moves import IntakeMoveService
from src.services.intake_recognition import IntakeRecognitionService
from src.services.intake_reviews import IntakeReviewService
from src.services.intake_suggestions import IntakeSuggestionService
from src.services.invoices import UploadedFile

intake_bp = Blueprint("intake", __name__, url_prefix="/api/intake-batches")

# 商家记忆**不属于某一个批次**：它跨批次复用（第 3.2 节第 5 层「人工确认一次后，
# 记住来源和适用线索，供后续批次建议使用」），所以挂在自己的前缀下，而不是塞进
# `/api/intake-batches/<id>/...` 里让人以为它只对这一批有效。
source_memory_bp = Blueprint(
    "source_memory", __name__, url_prefix="/api/source-memory"
)

UPLOAD_FIELD = "file"

PROCESS_FIELDS = ("retry_failed",)

# 默认一页读多少张卡片。与食材/采购列表的默认值同量级；上限在 Service
# （`MAX_ITEMS_PER_PAGE`），五千行的表格不会一次变成五千张卡片。
DEFAULT_ITEM_LIMIT = 50


def services() -> tuple[IntakeService, IntakeIngestService, IntakeRecognitionService]:
    """按请求装配。依赖全部来自应用工厂，不读模块级全局量。"""
    return build_intake_services(current_app)


def _upload() -> UploadedFile:
    """把 multipart 里的那**一个**文件读成服务层的入参。

    内容整份读进内存：单文件上限由配置约束，判重又必须先算摘要（流读完就没了）。
    """
    items = request.files.getlist(UPLOAD_FIELD)
    if not items or set(request.files) != {UPLOAD_FIELD} or len(items) != 1:
        raise ValidationError(
            f"请用 multipart/form-data 上传**一个**文件，字段名是 {UPLOAD_FIELD}。"
            "一次传一个，这样其中一个失败不会连累其它文件。",
            details={
                "field": UPLOAD_FIELD,
                "received_fields": sorted(request.files),
            },
        )
    item = items[0]
    return UploadedFile(
        content=item.read(),
        original_name=item.filename or "",
        content_type=item.mimetype or None,
    )


def _require_in_batch(batch_id: int, actual_batch_id: int, *, what: str, ident: int) -> None:
    """路径里的两个 id 必须指同一个东西。

    对不上就是 404：批次 A 的 URL 不该能取到批次 B 的原件，而「换个批次号试试」
    也不该变成一条可用的读法。
    """
    if actual_batch_id != batch_id:
        raise NotFoundError(
            f"{what} #{ident} 不在批次 #{batch_id} 里。",
            details={"batch_id": batch_id, "id": ident},
        )


# --- 批次 -------------------------------------------------------------------


@intake_bp.post("")
def create_batch() -> tuple[Response, int]:
    intake, _, _ = services()
    return jsonify(intake_batch_payload(intake.create_batch())), 201


@intake_bp.get("/current")
def current_batch() -> tuple[Response, int]:
    """最近一个有未移除文件的开放批次。

    没有就 404：前端据此决定「接着整理」还是「新建一批」。返回一个空对象会让这两种
    情形长得一样。
    """
    intake, _, _ = services()
    view = intake.latest_open_batch()
    if view is None:
        raise NotFoundError(
            "现在没有可继续整理的文件批次。上传文件时会新建一个。",
            details={"status": "open"},
        )
    return jsonify(intake_batch_payload(view)), 200


@intake_bp.get("/<int:batch_id>")
def get_batch(batch_id: int) -> tuple[Response, int]:
    """批次详情，含逐文件的处理状态。**轮询这个接口**看切页进展。"""
    intake, _, _ = services()
    return jsonify(intake_batch_payload(intake.get_batch(batch_id))), 200


@intake_bp.post("/<int:batch_id>/close")
def close_batch(batch_id: int) -> tuple[Response, int]:
    """这一批填完了：收口，不再接收新文件（第 22 节）。

    收口之后 `GET /current` 就不再返回它，首页那个「接着整理」因此消失——
    这正是用户要的「此次多文件填表结束」。

    ⚠️ **服务端不检查是否全部填完**：第 5 节允许把待定内容留着以后再办。
    「全部填完才按得动」是界面上的一道闸（用 `unfinished_item_count`），不是新的业务规则。
    已经收过口的批次再收一次是 409。
    """
    intake, _, _ = services()
    return jsonify(intake_batch_payload(intake.close_batch(batch_id))), 200


@intake_bp.get("/<int:batch_id>/items")
def list_items(batch_id: int) -> tuple[Response, int]:
    intake, _, _ = services()
    page = intake.list_items(
        batch_id,
        include_discarded=bool(query_bool(request.args, "include_discarded")),
        purchase_date=request.args.get("purchase_date"),
        source_name=request.args.get("source_name"),
        missing=request.args.get("missing"),
        limit=query_int(request.args, "limit", DEFAULT_ITEM_LIMIT),
        offset=query_int(request.args, "offset", 0),
    )
    return jsonify(intake_item_page_payload(page)), 200


@intake_bp.delete("/<int:batch_id>/items/<int:item_id>")
def delete_item(batch_id: int, item_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, ("version",))
    version = payload.get("version")
    if type(version) is not int or version < 1:
        raise ValidationError("version 必须是正整数。")
    intake, _, _ = services()
    intake.delete_item(batch_id, item_id, version=version)
    return jsonify({"deleted": True, "item_id": item_id}), 200


def _moves() -> IntakeMoveService:
    return IntakeMoveService(get_database(current_app))


@intake_bp.post("/<int:batch_id>/date-groups")
def create_date_group(batch_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, {"purchase_date"})
    _moves().create_date_group(batch_id, payload.get("purchase_date"))
    return jsonify({"purchase_date": payload["purchase_date"]}), 201


@intake_bp.delete("/<int:batch_id>/date-groups/<string:purchase_date>")
def delete_date_group(batch_id: int, purchase_date: str) -> tuple[Response, int]:
    _moves().delete_date_group(batch_id, purchase_date)
    return jsonify({"purchase_date": purchase_date, "deleted": True}), 200


@intake_bp.get("/<int:batch_id>/groups")
def list_classification_groups(batch_id: int) -> tuple[Response, int]:
    service = _moves()
    reader = service.suggested_groups if query_bool(request.args, "suggested") else service.groups
    page = reader(batch_id, limit=query_int(request.args, "limit", 50),
                  offset=query_int(request.args, "offset", 0))
    return jsonify({"groups": [{"purchase_date": row.purchase_date.isoformat() if row.purchase_date else None,
        "source_name": row.source_name, "source_name_normalized": row.source_name_normalized,
        "item_count": row.item_count, "suggested_count": row.suggested_count} for row in page.groups],
        "total": page.total, "limit": page.limit, "offset": page.offset}), 200


@intake_bp.get("/<int:batch_id>/groups/items")
def list_classification_group_items(batch_id: int) -> tuple[Response, int]:
    # Omitted purchase_date/source_name represent the unclassified and
    # date-only buckets. Supplying both addresses a supplier group.
    service = _moves()
    reader = service.suggested_group_items if query_bool(request.args, "suggested") else service.group_items
    page = reader(batch_id,
        purchase_date=request.args.get("purchase_date"), source_name=request.args.get("source_name"),
        limit=query_int(request.args, "limit", 50), offset=query_int(request.args, "offset", 0))
    return jsonify(intake_item_page_payload(page)), 200


def _move_payload(result) -> dict:
    return {"move_id": result.id, "request_key": result.request_key,
            "undone": result.undone, "items": [intake_item_payload(item) for item in result.items]}


@intake_bp.post("/<int:batch_id>/moves")
def move_items(batch_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, {"request_key", "action", "items", "purchase_date", "source_name"})
    result = _moves().move(batch_id, request_key=payload.get("request_key"),
        action=payload.get("action"), items=payload.get("items"),
        purchase_date=payload.get("purchase_date"), source_name=payload.get("source_name"))
    return jsonify(_move_payload(result)), 200


@intake_bp.post("/<int:batch_id>/moves/<int:move_id>/undo")
def undo_move(batch_id: int, move_id: int) -> tuple[Response, int]:
    result = _moves().undo(batch_id, move_id)
    return jsonify(_move_payload(result)), 200


@intake_bp.post("/<int:batch_id>/reviews")
def handoff_review(batch_id: int) -> tuple[Response, int]:
    """Send an explicit set of contents into one existing review draft."""
    payload = json_object(request)
    reject_unknown_fields(payload, {"items", "target_invoice_id", "suggested_group"})
    result = IntakeReviewService(get_database(current_app)).handoff(
        batch_id, items=payload.get("items"),
        target_invoice_id=payload.get("target_invoice_id"),
        suggested_group=payload.get("suggested_group"))
    return jsonify(result), 201 if result["created"] else 200


@intake_bp.post("/<int:batch_id>/reviews/selection")
def handoff_review_selection(batch_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, {"items", "suggested_group"})
    result = IntakeReviewService(get_database(current_app)).handoff_selection(
        batch_id, items=payload.get("items"), suggested_group=payload.get("suggested_group"))
    return jsonify(result), 201 if result["created"] else 200


@intake_bp.post("/<int:batch_id>/reviews/withdraw")
def withdraw_review(batch_id: int) -> tuple[Response, int]:
    """撤回填表：删掉草稿上填过的数据，内容回到「已分类、还没填表」。

    撤的是整张草稿（一张草稿就是一张发票），已经确认入账的行拦在这里不许撤。
    """
    payload = json_object(request)
    reject_unknown_fields(payload, {"items"})
    result = IntakeReviewService(get_database(current_app)).withdraw(
        batch_id, items=payload.get("items"))
    return jsonify(result), 200


@intake_bp.post("/<int:batch_id>/reviews/refill")
def refill_review(batch_id: int) -> tuple[Response, int]:
    """重新填写：清掉这张单据填过的数据，并为同一批页重新开一张空草稿。

    已经入账的行在这一步被**撤销**（采购记录作废、留修改日志），不是被拦住——
    不撤就会重复记价。日期和商家这些整理阶段的人工决定一个字不动。
    """
    payload = json_object(request)
    reject_unknown_fields(payload, {"items"})
    result = IntakeReviewService(get_database(current_app)).refill(
        batch_id, items=payload.get("items"))
    return jsonify(result), 200


@intake_bp.get("/<int:batch_id>/review-suggestions")
def review_membership_suggestions(batch_id: int) -> tuple[Response, int]:
    result = IntakeReviewService(get_database(current_app)).suggestions(
        batch_id, purchase_date=request.args.get("purchase_date"),
        source_name=request.args.get("source_name"),
        limit=query_int(request.args, "limit", 200),
        offset=query_int(request.args, "offset", 0))
    return jsonify(result), 200


@intake_bp.patch("/<int:batch_id>/reviews/<int:invoice_id>/classification")
def revise_review_classification(batch_id: int, invoice_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, {"scope", "items", "purchase_date", "source_name"})
    result = IntakeReviewService(get_database(current_app)).revise_classification(
        batch_id, invoice_id, items=payload.get("items"),
        purchase_date=payload.get("purchase_date"),
        source_name=payload.get("source_name"), scope=payload.get("scope"))
    return jsonify(result), 200


@intake_bp.post("/<int:batch_id>/reviews/<int:invoice_id>/reassign")
def reassign_review_contents(batch_id: int, invoice_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, {"items", "target_invoice_id"})
    result = IntakeReviewService(get_database(current_app)).reassign(
        batch_id, invoice_id, items=payload.get("items"),
        target_invoice_id=payload.get("target_invoice_id"))
    return jsonify(result), 201 if result["created"] else 200


# --- 文件 -------------------------------------------------------------------


@intake_bp.post("/<int:batch_id>/files")
def upload_file(batch_id: int) -> tuple[Response, int]:
    """收一个文件。返回 201 表示字节已经存下来、已经登记进批次。

    **切页不一定做完了**：看 `queued`。字节收下之后浏览器怎么关都不影响它。
    """
    _, ingest, _ = services()
    outcome = ingest.upload(batch_id, _upload())
    return jsonify(intake_upload_payload(outcome)), 201


@intake_bp.post("/<int:batch_id>/process")
def process_batch(batch_id: int) -> tuple[Response, int]:
    """把这个批次里还没切的文件切完。

    这是**恢复入口**：进程重启、后台没跑成、用户关掉浏览器又回来，调一次就接着切。
    已经切好的文件不会被重切（页的 id 稳定），所以重复调用是安全的。
    `retry_failed=true` 连失败的也重试一遍。

    返回 200 **哪怕有的文件失败**：逐项结果各自带成功或原因，「五份切好三份、
    两份坏了」是**正常结果**而不是请求失败（与批量确认同一条）。
    """
    retry_failed = False
    if request.data:
        payload = json_object(request)
        reject_unknown_fields(payload, PROCESS_FIELDS)
        raw = payload.get("retry_failed", False)
        if not isinstance(raw, bool):
            raise ValidationError(
                "字段 retry_failed 必须是 true 或 false。",
                details={"field": "retry_failed"},
            )
        retry_failed = raw

    intake, ingest, _ = services()
    results = ingest.ingest_waiting(batch_id=batch_id, retry_failed=retry_failed)
    return (
        jsonify(
            {
                "results": [intake_ingested_payload(item) for item in results],
                "batch": intake_batch_payload(intake.get_batch(batch_id)),
            }
        ),
        200,
    )


@intake_bp.delete("/<int:batch_id>/files/<int:file_id>")
def discard_file(batch_id: int, file_id: int) -> tuple[Response, int]:
    """把一个文件从批次里移除：**这份文件在这个批次里的记录一并删掉**。

    ⚠️ **不可反悔**（2026-11-24 起，docs/multifiles.md 第 16 节）：引用、切出来的页、
    页上的日期/商家决定与撤销日志一起删。同一份文件再传进来是**全新的一份**，
    不带任何旧分类。`file` 是删掉之前那条引用的样子，`removed_item_count` 是跟着
    删掉的内容数。

    `bytes_still_referenced` 回答「盘上那份字节现在能删了吗」——别的批次可能还在
    用同一份字节，所以移除不等于能删文件。已有内容进了填表的文件拦在外面（409）。
    """
    intake, _, _ = services()
    _require_in_batch(
        batch_id, intake.get_file(file_id).batch_id, what="文件", ident=file_id
    )
    removed = intake.discard_file(file_id)
    return (
        jsonify(
            {
                "file": intake_file_payload(removed.ref),
                "removed_item_count": removed.removed_item_count,
                "bytes_still_referenced": removed.bytes_still_referenced,
            }
        ),
        200,
    )


@intake_bp.get("/<int:batch_id>/files/<int:file_id>/original")
def get_file_original(batch_id: int, file_id: int) -> Response:
    """回看整份原件。原样返回存下来的字节——它是核对用的证据。"""
    _, ingest, _ = services()
    ref, stream = ingest.open_file(file_id)
    _require_in_batch(batch_id, ref.batch_id, what="文件", ident=file_id)
    return _send(stream, ref.content_type, ref.original_name)


@intake_bp.get("/<int:batch_id>/items/<int:item_id>/original")
def get_item_original(batch_id: int, item_id: int) -> Response:
    """回看某一页（或某一行）的原件。

    ⚠️ 返回的是**整份原件**，页号/行号在响应头 `X-Intake-Page` / `X-Intake-Row`
    里。PDF 的某一页在后端生成不出单页图（本项目只有 pypdf，不读像素），由前端的
    查看器跳到那一页；返回一张占位图冒充原件是不能接受的。
    """
    _, ingest, _ = services()
    item, stream = ingest.open_item(item_id)
    _require_in_batch(batch_id, item.batch_id, what="可归类内容", ident=item_id)
    response = _send(stream, None, item.original_name)
    if item.page_no is not None:
        response.headers["X-Intake-Page"] = str(item.page_no)
    if item.row_no is not None:
        response.headers["X-Intake-Row"] = str(item.row_no)
    response.headers["X-Intake-Item-Kind"] = item.kind
    return response


# --- 日期 / 商家识别（M05） --------------------------------------------------


@intake_bp.post("/<int:batch_id>/recognize")
def recognize_batch(batch_id: int) -> tuple[Response, int]:
    """为这个批次里还没识别过的内容读日期与商家候选。

    这是**恢复入口**，而且**重复调用安全且免费**：已经识别过的内容一律跳过
    （`outcome` 为 `already_recognized`），一次模型调用都不会发生——这正是
    「整理阶段已有结果进入审核时不得对同一页重复收费」那条要求的落点。
    响应里的 `model_calls` 是**这一次真的发了几次调用**，文字层与表格行都是 0。

    返回 200 **哪怕有的内容失败**：逐项结果各自带结局或原因，「十页读好八页、
    两页读不出」是正常结果而不是请求失败（与 `/process`、批量确认同一条）。
    `retry_failed=true` 连失败的一起重试。

    ⚠️ **不预填任何东西**：产出全部是候选，`decided_*` 一个字都不会被写。
    """
    retry_failed = False
    if request.data:
        payload = json_object(request)
        reject_unknown_fields(payload, PROCESS_FIELDS)
        raw = payload.get("retry_failed", False)
        if not isinstance(raw, bool):
            raise ValidationError(
                "字段 retry_failed 必须是 true 或 false。",
                details={"field": "retry_failed"},
            )
        retry_failed = raw

    intake, _, recognition = services()
    intake.get_batch(batch_id)
    results = recognition.recognize_waiting(
        batch_id=batch_id, retry_failed=retry_failed,
        limit=query_int(request.args, "limit", 50),
    )
    return (
        jsonify(
            {
                "results": [intake_recognized_payload(item) for item in results],
                "model_calls": sum(item.calls for item in results),
                "groups_hint": (
                    "识别候选会显示在整理页右侧的系统建议组；人工修改与送审会写入决定值。"
                ),
            }
        ),
        200,
    )


@intake_bp.post("/<int:batch_id>/items/<int:item_id>/reparse-date")
def reparse_item_date(batch_id: int, item_id: int) -> tuple[Response, int]:
    """用保存下来的日期原文，按**已确认商家**的 `date_order` 重新解析日期。

    ⚠️ **一次模型调用都不会发生**（`model_calls` 恒为 0）：`6/11/2026` 这类写法的
    两种读法在识别时就都留着了，这里只是把原文按这家的习惯再读一遍。

    商家还没人工确认就是 400：拿相似度猜出来的商家去定一个日历日，猜错不报错——
    它只是把这笔采购算进了错误的月份。

    改的是**候选**，不是 `decided_purchase_date`：候选永远不会自己变成决定。
    """
    intake, _, recognition = services()
    _require_in_batch(
        batch_id, intake.get_item(item_id).batch_id, what="可归类内容", ident=item_id
    )
    result = recognition.reparse_date(item_id)
    return (
        jsonify(
            {
                "item": intake_item_payload(result.item),
                "model_calls": result.calls,
                "notes": list(result.notes),
            }
        ),
        200,
    )


# --- 商家记忆与相似堆（M07） ------------------------------------------------


def _suggestions() -> IntakeSuggestionService:
    return build_intake_suggestion_service(current_app)


@intake_bp.post("/<int:batch_id>/suggest-sources")
def suggest_sources(batch_id: int) -> tuple[Response, int]:
    """为这个批次的卡片重算商家建议（第 3.2 节的第 2—4 层）。

    ⚠️ **一次模型调用都不会发生**（`model_calls` 恒为 0，而且是结构上的 0：建议
    服务没有识别器依赖）。三层线索分别是：同一份文件里已确认的页、历史来源与
    别名、版式相似的待办堆。

    ⚠️ **不预填任何东西**：产出全部写进候选，`decided_*` 一个字都不会动。纸面上
    读出的来源与同文件/外观的建议冲突时，**单页纸面证据优先**，被压住的那条进
    `conflicts` 让人看得见。

    版式要量像素（一页约 0.3—0.6 秒），所以一次最多量 20 页：`layout_pending`
    不为 0 时再调一次接着量，已量过的不会重量。大批次用 `limit` / `offset` 分几次
    跑——重复驱动是安全且免费的。
    """
    if request.data:
        payload = json_object(request)
        reject_unknown_fields(payload, ("limit", "offset"))
    run = _suggestions().suggest_batch(
        batch_id,
        limit=query_int(request.args, "limit", DEFAULT_ITEM_LIMIT),
        offset=query_int(request.args, "offset", 0),
    )
    return jsonify(intake_suggestion_run_payload(run)), 200


@intake_bp.get("/<int:batch_id>/piles")
def list_piles(batch_id: int) -> tuple[Response, int]:
    """这个批次里「相似的 N 张、商家未知」分成哪几堆。

    **只读**：不量新的版式、不写候选（那是 `/suggest-sources`），所以刷新整理页
    不会悄悄花掉几十秒。堆是**算出来的**（同一指纹的未决内容），没有「堆」这张表。
    """
    return jsonify(intake_pile_board_payload(_suggestions().piles(batch_id))), 200


@source_memory_bp.get("")
def list_source_memory() -> tuple[Response, int]:
    """人确认过的来源 + 它们的别名。

    这是 M07 第一条子项的「规范化名称」：键是规范化写法，展示名是**最近一次人工
    确认的写法**。⚠️ 它**不是**应付账款的供应商模块——没有联系人、地址、账期，
    一家商家在这里存在只因为有人在某张卡片或某条采购记录上确认过这个名字。
    """
    entries = _suggestions().source_memory(
        limit=query_int(request.args, "limit", DEFAULT_ITEM_LIMIT)
    )
    return (
        jsonify(
            {
                "sources": [intake_source_memory_payload(item) for item in entries],
                "notes": [
                    "展示名是最近一次人工确认的写法；**不翻译**，也不凭缩写扩写"
                    "法定名称（第 3.1 节）。",
                    "别名只影响建议：它不会合并两家的采购记录，也不会改任何"
                    "已确认的分类。",
                ],
            }
        ),
        200,
    )


@source_memory_bp.get("/aliases")
def list_aliases() -> tuple[Response, int]:
    aliases = _suggestions().list_aliases(
        limit=query_int(request.args, "limit", DEFAULT_ITEM_LIMIT),
        offset=query_int(request.args, "offset", 0),
    )
    return jsonify({"aliases": [intake_alias_payload(item) for item in aliases]}), 200


@source_memory_bp.post("/aliases")
def set_alias() -> tuple[Response, int]:
    """记下或**更正**一条别名：纸面上这种写法就是那一家。

    同一个写法再提交一次就是改指向（`basis` 变成 `user`，之后重算建议不会覆盖
    它）。⚠️ 它只影响建议，不合并任何已有数据。
    """
    payload = json_object(request)
    reject_unknown_fields(payload, ("alias_text", "source_name", "evidence"))
    view = _suggestions().set_alias(
        alias_text=payload.get("alias_text"),
        source_name=payload.get("source_name"),
        evidence=payload.get("evidence"),
    )
    return jsonify({"alias": intake_alias_payload(view)}), 200


@source_memory_bp.delete("/aliases/<int:alias_id>")
def delete_alias(alias_id: int) -> tuple[Response, int]:
    """撤销一条别名。建议立刻不再出现，历史分类与采购记录一个字都不动。"""
    view = _suggestions().delete_alias(alias_id)
    return jsonify({"alias": intake_alias_payload(view)}), 200


def _send(stream, content_type: str | None, original_name: str) -> Response:
    mimetype = content_type or (
        mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    )
    return send_file(
        stream,
        mimetype=mimetype,
        as_attachment=False,
        download_name=original_name,
    )
