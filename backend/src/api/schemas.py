"""请求与响应的数据格式处理。

分工（与 `src/services/` 的校验不重复）：

- **本模块只管「形状」**：请求体是不是 JSON 对象、有没有不认识的字段、必填字段在不在、
  查询参数能不能解析成整数，以及把 Service 返回的只读视图转成 JSON 可序列化的字典。
- **业务口径在 Service**：名称去空白后是否为空、长度上限、基础单位白名单、分类是否存在、
  名称是否重复，都由 `src/services/ingredients.py` 判断；数值的解析与精度、行金额与
  数量 × 单价 的一致性、采购日期的合理范围由 `src/services/purchases.py` 判断。
- **数值的字符串化在本模块**：采购记录的三个数值列以 `Decimal` 留在 Service 的视图里
  （步骤 09 的统计要直接拿它们做精确运算），只在转成 JSON 时才用 `format_quantity` /
  `format_money` 变成字符串。

两层都只抛 `src/errors.py` 的异常，因此错误响应结构始终是
`{"error": {"code", "message", "details"}}`，由应用工厂注册的处理器统一转成 HTTP。

未知字段**直接拒绝**而不是忽略：前端把 `name_zh` 写成 `nameZh` 时，忽略意味着用户填的
中文名被静默丢弃，录入的人要到很久以后才发现。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

from flask import Request

from src.errors import ValidationError
from src.services.ingredients import (
    UNSET,
    CategoryView,
    FoodItemPage,
    FoodItemView,
)
from src.services.invoices import (
    CategoryTotal,
    ConfirmOutcome,
    ConfirmResult,
    InvoiceFileView,
    InvoiceLineView,
    InvoicePage,
    InvoiceView,
    MatchCandidateView,
    ReconciliationView,
)
from src.services.prices import (
    AlertReport,
    AlertSettingsView,
    ComparisonView,
    ConversionView,
    ExclusionSummary,
    MonthlyPriceReport,
    WeeklyPriceReport,
    PackSpecPage,
    PackSpecView,
    PendingPackSpecRow,
    Period,
    PeriodStats,
    PriceHistoryPage,
    SourceComparisonReport,
    SourceStats,
    format_percent,
    format_price,
)
from src.services.purchases import (
    LINE_KIND_PURCHASE,
    PurchasePage,
    PurchaseRevisionView,
    PurchaseView,
    format_money,
    format_quantity,
)
from src.services.intake import (
    IntakeBatchView,
    IntakeCandidateView,
    IntakeFileRefView,
    IntakeItemPage,
    IntakeItemView,
    collapse_legacy_readings,
)
from src.services.intake_ingest import IngestedFile, UploadOutcome
from src.services.intake_recognition import RecognizedItem
from src.services.intake_suggestions import (
    AliasView,
    ItemSuggestions,
    PileBoard,
    PileView,
    SourceMemoryEntry,
    SuggestionRun,
    SuggestionView,
)
from src.services.merging import MergePlan
from src.services.suggestions import FoodItemSuggestion, FoodItemSuggestions

# --- 请求 -------------------------------------------------------------------


def json_object(request: Request) -> dict[str, Any]:
    """取出请求体并确认它是一个 JSON 对象。

    `silent=True`：解析失败时不让 Flask 直接抛 400 HTML 异常，而是走本项目统一的
    错误结构，消息里说清期望的是什么。
    """
    payload = request.get_json(silent=True)
    if payload is None:
        raise ValidationError(
            "请求体必须是 JSON，且 Content-Type 为 application/json。"
        )
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象（形如 {\"name\": \"...\"}）。")
    return payload


def reject_unknown_fields(payload: Mapping[str, Any], allowed: Iterable[str]) -> None:
    """拒绝不认识的字段，避免写错字段名时静默丢数据。"""
    allowed_set = set(allowed)
    unknown = sorted(set(payload) - allowed_set)
    if unknown:
        raise ValidationError(
            f"不认识的字段：{', '.join(unknown)}。"
            f"可用字段：{', '.join(sorted(allowed_set))}。",
            details={"unknown_fields": unknown},
        )


def require_present(payload: Mapping[str, Any], required: Iterable[str]) -> None:
    """必填字段必须出现在请求体里（值本身的合法性由 Service 判断）。"""
    missing = sorted(key for key in required if key not in payload)
    if missing:
        raise ValidationError(
            f"缺少必填字段：{', '.join(missing)}。",
            details={"missing_fields": missing},
        )


def require_non_empty(payload: Mapping[str, Any]) -> None:
    """局部更新至少要带一个字段，空请求体按明确的错误拒绝而不是假装改过了。"""
    if not payload:
        raise ValidationError("请求体里没有要修改的字段。")


def field(payload: Mapping[str, Any], key: str) -> Any:
    """取可选字段：没出现就返回 UNSET（保持原值），出现 null 则原样传给 Service。"""
    return payload.get(key, UNSET)


def query_int(
    args: Mapping[str, str], key: str, default: int | None = None
) -> int | None:
    """解析整数查询参数。空值按未提供处理；写了但不是整数则报错。"""
    raw = args.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValidationError(
            f"查询参数 {key} 必须是整数，收到 {raw!r}。", details={"field": key}
        ) from exc


def query_text(args: Mapping[str, str], key: str) -> str | None:
    """取文本查询参数。空白等于未提供。"""
    raw = args.get(key)
    if raw is None or not raw.strip():
        return None
    return raw


# --- 响应 -------------------------------------------------------------------


def category_payload(view: CategoryView) -> dict[str, Any]:
    return {
        "id": view.id,
        "name": view.name,
        "name_zh": view.name_zh,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }


def category_ref_payload(view: CategoryView) -> dict[str, Any]:
    """食材里内嵌的分类：只给展示需要的字段，不重复时间戳。"""
    return {"id": view.id, "name": view.name, "name_zh": view.name_zh}


def food_item_payload(view: FoodItemView) -> dict[str, Any]:
    return {
        "id": view.id,
        "name": view.name,
        "name_zh": view.name_zh,
        "specification": view.specification,
        "base_unit": view.base_unit,
        "category": category_ref_payload(view.category),
        # 步骤 10.13：这条是系统按关键词自动建的，还没人确认过。界面要标出来，
        # 否则一个猜错的分类会一直装成"老板自己选的"。
        "needs_review": view.needs_review,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }


def food_item_page_payload(page: FoodItemPage) -> dict[str, Any]:
    """分页响应。`total` 是符合条件的总条数，供前端判断还有没有下一页。"""
    return {
        "items": [food_item_payload(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


def food_item_suggestion_payload(item: FoodItemSuggestion) -> dict[str, Any]:
    """一条候选食材（步骤 10）。

    `match_reason` 与 `score` 必须一起给界面：只给分数解释不了排序（精确命中与
    包含命中都可能是 1.0），只给理由看不出"有多像"。
    """
    return {
        "food_item": food_item_payload(item.food_item),
        "match_reason": item.match_reason,
        "score": item.score,
        "purchase_count": item.purchase_count,
        "last_purchase_date": (
            item.last_purchase_date.isoformat()
            if item.last_purchase_date is not None
            else None
        ),
        "merged_from": item.merged_from,
    }


def merge_plan_payload(plan: MergePlan) -> dict[str, Any]:
    """合并计划。预览与执行返回**同一个结构**，于是两边说的是同一件事。

    `can_merge` 与 `blockers` 一起给：界面据此决定按钮能不能按，以及按不下去时
    该指到哪一条箱规或哪个单位上。`notes` 里固定带「合并不可逆」那一条。
    """
    return {
        "target": food_item_payload(plan.target),
        "sources": [
            {
                "food_item": food_item_payload(item.food_item),
                "purchase_count": item.purchase_count,
                "invoice_line_count": item.invoice_line_count,
                "pack_spec_count": item.pack_spec_count,
                "duplicate_pack_spec_count": item.duplicate_pack_spec_count,
            }
            for item in plan.sources
        ],
        "can_merge": plan.can_merge,
        "blockers": [
            {"code": item.code, "message": item.message, "details": item.details}
            for item in plan.blockers
        ],
        "moved_purchase_count": plan.moved_purchase_count,
        "moved_invoice_line_count": plan.moved_invoice_line_count,
        "moved_pack_spec_count": plan.moved_pack_spec_count,
        "notes": list(plan.notes),
    }


def food_item_suggestions_payload(result: FoodItemSuggestions) -> dict[str, Any]:
    """建议响应：三条来源各自成列，**不分页**。

    ⚠️ `notes` 不是装饰：扫描触顶、常用窗口排除了哪些记录这类话必须跟着列表一起读
    （与步骤 09 的统计响应同一条原则）。
    """
    return {
        "query": result.query,
        "matches": [food_item_suggestion_payload(item) for item in result.matches],
        "recent": [food_item_suggestion_payload(item) for item in result.recent],
        "frequent": [food_item_suggestion_payload(item) for item in result.frequent],
        "notes": list(result.notes),
    }


# --- 采购记录（步骤 04）-----------------------------------------------------


def query_bool(args: Mapping[str, str], key: str) -> bool | None:
    """解析三态布尔查询参数：未提供返回 None，于是「不筛选」和「筛 false」分得开。

    只认 true/false/1/0/yes/no，其余明确报错。宽松解释（例如把 `undated=maybe`
    当成 false）会让人以为筛过了，实际看到的是全量数据。
    """
    raw = args.get(key)
    if raw is None or not raw.strip():
        return None
    text = raw.strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise ValidationError(
        f"查询参数 {key} 必须是 true 或 false，收到 {raw!r}。", details={"field": key}
    )


def query_kind(args: Mapping[str, str]) -> str:
    """解析 `kind` 查询参数。缺省只要正常采购行。

    取值校验留给 Service（`SEARCH_KINDS` 白名单在那边），本层只负责「没传就用默认」。
    """
    raw = args.get("kind")
    if raw is None or not raw.strip():
        return LINE_KIND_PURCHASE
    return raw.strip()


def food_item_ref_payload(view: FoodItemView) -> dict[str, Any]:
    """采购记录里内嵌的食材：给展示需要的字段，不重复食材自己的时间戳。

    ⚠️ `needs_review` 在这里**也要给**（步骤 10.13）。发票逐行确认那一屏正是
    自动建出来的食材第一次出现在老板眼前的地方——那一格要是和他自己建的食材
    长得一模一样，一个猜错的分类就会一直装成"他自己选的"。
    """
    return {
        "id": view.id,
        "name": view.name,
        "name_zh": view.name_zh,
        "specification": view.specification,
        "base_unit": view.base_unit,
        "category": category_ref_payload(view.category),
        "needs_review": view.needs_review,
    }


def purchase_payload(view: PurchaseView) -> dict[str, Any]:
    """一条采购记录。

    三个数值输出**字符串**：JSON 的数字是双精度浮点，前端的 `JSON.parse` 会把
    `1234567.89` 变成 double，再显示出来就可能多出或少掉一分钱。字符串让精确值
    一路走到界面上（见 docs/stru_dev.md 步骤 04 的 API 数值表示）。

    `source_invoice` 在步骤 05 填上了内容：从发票确认出来的记录给出
    `{invoice_id, invoice_line_id, line_no}`，拿它就能回到
    `GET /api/invoices/<invoice_id>` 看原件（步骤 05 验收：能根据采购记录查到原始
    单据）。手工录入与表格导入的记录仍是 null——它们没有单据。

    `line_kind` 为 `"adjustment"` 的行是退货 / 折让 / 其它调整，金额可以为负，
    且 `food_item` 可以为 `null`（整单折让不指向任何一种食材）。前端在画价格相关的
    东西时必须把这类行排除掉——接口默认也不返回它们。
    """
    return {
        "id": view.id,
        "line_kind": view.line_kind,
        "food_item": (
            food_item_ref_payload(view.food_item)
            if view.food_item is not None
            else None
        ),
        "purchase_date": (
            view.purchase_date.isoformat() if view.purchase_date else None
        ),
        "source_name": view.source_name,
        "original_description": view.original_description,
        "quantity": format_quantity(view.quantity),
        "purchase_unit": view.purchase_unit,
        "purchase_unit_normalized": view.purchase_unit_normalized,
        "unit_price": format_money(view.unit_price),
        "line_amount": format_money(view.line_amount),
        "currency": view.currency,
        "amount_mismatch": view.amount_mismatch,
        "entry_source": view.entry_source,
        "source_invoice": (
            {
                "invoice_id": view.source_invoice.invoice_id,
                "invoice_line_id": view.source_invoice.invoice_line_id,
                "line_no": view.source_invoice.line_no,
            }
            if view.source_invoice is not None
            else None
        ),
        "voided_at": view.voided_at.isoformat() if view.voided_at else None,
        "revision_count": view.revision_count,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }


def purchase_page_payload(page: PurchasePage) -> dict[str, Any]:
    """分页响应。`total` 是符合条件的总条数，供前端判断还有没有下一页。"""
    return {
        "items": [purchase_payload(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


def purchase_revisions_payload(
    views: Iterable[PurchaseRevisionView],
) -> dict[str, Any]:
    """修改历史。`changed_fields` 里的值已经是字符串，原样输出即可。"""
    return {
        "items": [
            {
                "id": view.id,
                "changed_at": view.changed_at.isoformat(),
                "action": view.action,
                "changed_fields": view.changed_fields,
                "reason": view.reason,
            }
            for view in views
        ]
    }


# --- 发票与审核草稿（步骤 05）-----------------------------------------------


def invoice_file_payload(view: InvoiceFileView) -> dict[str, Any]:
    """一个原始文件。`sha256` 给出来是为了让前端能解释「这张传过了」。"""
    return {
        "id": view.id,
        "original_name": view.original_name,
        "size": view.size,
        "sha256": view.sha256,
        "content_type": view.content_type,
        "page_order": view.page_order,
        "created_at": view.created_at.isoformat(),
    }


def match_candidate_payload(view: MatchCandidateView) -> dict[str, Any]:
    """一个匹配候选。

    `same_source` 与 `purchase_count` 是给人看的依据：界面可以写成「这家供应商买过
    7 次」，老板一眼知道该不该用这个候选。候选永远只是候选——确认请求必须显式带上
    选中的 `food_item_id`。

    步骤 10.4 起多三列。`match_reason` 决定界面该怎么说这条候选的来历：
    `description_exact` 可以写成「这句描述你以前确认成它」，`description_fuzzy`
    必须写成「有一句**像**的描述曾经确认成它：<matched_description>」，
    `supplier_history` 则是「这家供应商买过它」。⚠️ `score` 在
    `supplier_history` 上是 `null`——那条候选不是按文本比出来的，摆出一个分数会让
    两种候选看起来是同一把尺子量的。
    """
    return {
        "food_item": food_item_ref_payload(view.food_item),
        "purchase_count": view.purchase_count,
        "last_purchase_id": view.last_purchase_id,
        "last_purchase_date": (
            view.last_purchase_date.isoformat() if view.last_purchase_date else None
        ),
        "same_source": view.same_source,
        "match_reason": view.match_reason,
        "score": view.score,
        "matched_description": view.matched_description,
    }


def match_candidates_payload(
    views: Iterable[MatchCandidateView],
) -> dict[str, Any]:
    return {"candidates": [match_candidate_payload(view) for view in views]}


def invoice_line_payload(view: InvoiceLineView) -> dict[str, Any]:
    """一行草稿。

    四个数值是**可为 null 的字符串**：认不出来的格子就是 null，绝不输出 `"0"`
    ——0 在业务上是「赠品」这种具体含义，「没认出来」不是。

    `raw_fields` 是原始识别证据（原文、状态、置信度、页码与区域），原样给前端：
    点一个字段就能跳到原图对应位置，靠的是这里的 `box`。厂商没给坐标时它是 null，
    不编造。

    `missing_fields` 是「还差什么才能确认」，`match_candidates` 只在详情里有值。
    """
    return {
        "id": view.id,
        "line_no": view.line_no,
        "status": view.status,
        "skip_reason": view.skip_reason,
        "line_kind": view.line_kind,
        "food_item": (
            food_item_ref_payload(view.food_item)
            if view.food_item is not None
            else None
        ),
        "description": view.description,
        "quantity": (
            format_quantity(view.quantity) if view.quantity is not None else None
        ),
        "purchase_unit": view.purchase_unit,
        "unit_price": (
            format_money(view.unit_price) if view.unit_price is not None else None
        ),
        "line_amount": (
            format_money(view.line_amount) if view.line_amount is not None else None
        ),
        "purchase_date": (
            view.purchase_date.isoformat() if view.purchase_date else None
        ),
        "source_name": view.source_name,
        "currency": view.currency,
        "raw_fields": view.raw_fields,
        "review_issues": list(view.review_issues),
        # 步骤 10.C：这一格的食材是怎么来的，或者为什么没能自动来。
        # `reason="needs_base_unit"` 时带着 `suggested_*`，界面据此预填新建表单并把
        # 光标落在基础单位那一格。没有这一列的话，「为什么这一行自动选好了、
        # 那一行没有」在界面上完全看不出来。
        "food_item_match": view.food_item_match,
        "page": view.page,
        "version": view.version,
        "confirmed_at": view.confirmed_at.isoformat() if view.confirmed_at else None,
        "purchase_id": view.purchase_id,
        "missing_fields": list(view.missing_fields),
        "match_candidates": [
            match_candidate_payload(candidate) for candidate in view.match_candidates
        ],
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }


def reconciliation_payload(view: ReconciliationView) -> dict[str, Any]:
    """整单对账。

    `status` 四态：`not_checked`（还有待确认的行，不对账）、`no_total`（单据总额没
    识别出来，没法对）、`balanced`、`mismatch`。两种「没法对」**不会**输出 0 差额
    ——那会看起来像核对过了。`difference` 在这两种情况下是 null。

    步骤 10.A 起多三项：`by_category`（按分类摊开的金额，从大到小）、
    `adjustment_total`（运费 / 折让那一桶）与 `notes`。
    ⚠️ **`adjustment_total` 是独立字段，不在 `by_category` 里**：摆在一起界面就
    可能把它和分类加在一起，而运费不是某一类食材的成本。
    """
    return {
        "status": view.status,
        "invoice_total": (
            format_money(view.invoice_total) if view.invoice_total is not None else None
        ),
        "confirmed_amount": format_money(view.confirmed_amount) if view.confirmed_amount is not None else None,
        "difference": (
            format_money(view.difference) if view.difference is not None else None
        ),
        "tolerance": format_money(view.tolerance),
        "line_count": view.line_count,
        "pending_count": view.pending_count,
        "confirmed_count": view.confirmed_count,
        "skipped_count": view.skipped_count,
        "by_category": [category_total_payload(row) for row in view.by_category],
        "adjustment_total": (
            category_total_payload(view.adjustment_total)
            if view.adjustment_total is not None
            else None
        ),
        "notes": list(view.notes),
    }


def category_total_payload(row: CategoryTotal) -> dict[str, Any]:
    """收口面板上的一格。金额是**字符串**，与采购记录的数值口径一致（不经 float）。"""
    return {
        "category_id": row.category_id,
        "name": row.name,
        "name_zh": row.name_zh,
        "line_count": row.line_count,
        "amount": format_money(row.amount),
    }


def invoice_payload(view: InvoiceView) -> dict[str, Any]:
    """一张单据。

    `status` 是**派生值**（由各行状态算出来，数据库里没这一列）：
    draft / in_review / ready_to_complete / completed / voided。

    `recognition.is_fake` 为 true 表示这张草稿来自 Fake 识别器——本轮只有它。这个
    标记必须一路传到界面上：演示时不能让假数据看起来像真识别。

    `raw_document_fields` 是单据头的原始识别证据。`invoice_date` 为 null 而这里却有
    原文，意思是「单据上写了，但解析不成日历日」——老板因此知道那不是漏识别。

    `possible_duplicate_invoice_ids` 是**软提示**：来源、单号与总额都相同的其它单据。
    不阻止任何操作，同一张单补拍的照片本来就会是另一份文件。
    """
    return {
        "id": view.id,
        "status": view.status,
        "source_name": view.source_name,
        "invoice_number": view.invoice_number,
        "invoice_date": view.invoice_date.isoformat() if view.invoice_date else None,
        "currency": view.currency,
        "invoice_total": (
            format_money(view.invoice_total) if view.invoice_total is not None else None
        ),
        "header_reviewed_fields": list(view.header_reviewed_fields),
        "recognition": {
            "provider": view.recognition_provider,
            "is_fake": view.recognition_is_fake,
            "recognized_at": (
                view.recognized_at.isoformat() if view.recognized_at else None
            ),
            "raw_document_fields": view.raw_document_fields,
        },
        "uploaded_at": view.uploaded_at.isoformat(),
        "completed_at": view.completed_at.isoformat() if view.completed_at else None,
        "voided_at": view.voided_at.isoformat() if view.voided_at else None,
        "updated_at": view.updated_at.isoformat(),
        "files": [invoice_file_payload(item) for item in view.files],
        "intake_sources": list(view.intake_sources),
        "lines": [invoice_line_payload(line) for line in view.lines],
        "reconciliation": reconciliation_payload(view.reconciliation),
        "possible_duplicate_invoice_ids": list(view.possible_duplicate_invoice_ids),
        "entry_source": view.entry_source,
        "import_metadata": view.import_metadata,
    }


def invoice_page_payload(page: InvoicePage) -> dict[str, Any]:
    return {
        "items": [invoice_payload(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


def confirm_result_payload(result: ConfirmResult) -> dict[str, Any]:
    """一行的确认结果。失败的行带着自己的错误码，前端可以就地提示那一行。"""
    payload: dict[str, Any] = {
        "line_id": result.line_id,
        "ok": result.ok,
        "purchase_id": result.purchase_id,
    }
    if not result.ok:
        error: dict[str, Any] = {
            "code": result.error_code,
            "message": result.error_message,
        }
        if result.error_details:
            error["details"] = result.error_details
        payload["error"] = error
    return payload


def confirm_outcome_payload(outcome: ConfirmOutcome) -> dict[str, Any]:
    """一次批量确认的结果。

    `confirmed` + `failed` 合起来是这次提交的行数。**部分成功是正常结果**，所以
    HTTP 状态是 200；哪一行为什么失败看 `results`。
    """
    return {
        "confirmed": outcome.confirmed_count,
        "failed": outcome.failed_count,
        "results": [confirm_result_payload(result) for result in outcome.results],
        "invoice": invoice_payload(outcome.invoice),
    }


# --- 标准化价格与价格统计（步骤 09）-----------------------------------------
#
# 三条贯穿本节的输出约定：
#
# 1. **数值一律字符串**（与步骤 04 同口径）。标准化单价最多 4 位小数、至少 2 位；
#    百分比 2 位。
# 2. **算不出来就是 `null`，不是 0。** 0 是一个具体的价格（赠品行就是 0 元），
#    拿它表示「不知道」会让「没算出来」和「真的免费」变成同一个数。
# 3. **每个统计响应都带 `excluded` 与 `notes`。** 前者是「有几条没算进来、为什么」，
#    后者是必须跟着数字一起读的那几句话（不含运费、不混币种、期间天数）。
#    `docs/imp_notes.md` 要求「均价、涨幅和提醒应能追溯到参与计算的采购记录」，
#    `purchase_ids` 就是那条追溯线索。


def _decimal_or_none(value: Decimal | None, formatter: Any) -> str | None:
    """算不出来的值输出 `null`。见本节第 2 条约定。"""
    return None if value is None else formatter(value)


def pack_spec_payload(view: PackSpecView) -> dict[str, Any]:
    """一条箱规：「1 个包装单位 = pack_size 个 pack_unit」。

    `source_name` 为 `null` 表示**不限来源**：这条箱规对任何来源（包括没填来源的
    手工记录）都生效，但精确命中这家供应商的那条优先。
    """
    payload: dict[str, Any] = {
        "id": view.id,
        "food_item_id": view.food_item_id,
        "source_name": view.source_name,
        "source_name_normalized": view.source_name_normalized,
        "purchase_unit": view.purchase_unit,
        "pack_size": format_quantity(view.pack_size),
        "pack_unit": view.pack_unit,
        "spec_text": view.spec_text,
        "note": view.note,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }
    if view.food_item is not None:
        payload["food_item"] = food_item_ref_payload(view.food_item)
    return payload


def pack_spec_page_payload(page: PackSpecPage) -> dict[str, Any]:
    return {
        "items": [pack_spec_payload(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


def conversion_payload(view: ConversionView) -> dict[str, Any]:
    """一行采购记录的换算结果，或者换不出来的原因。

    `status` 四种取值的含义：

    - `available`：算出来了，`normalized_unit_price` 是每 `base_unit` 多少钱。
    - `pending_pack_spec`：按箱/包计价、还没有箱规 → 去 `POST /api/prices/pack-specs`。
    - `unit_unknown`：单位既不是计量单位也不是包装单位 → 改这条记录的单位。
    - `incompatible_unit`：量纲不同（体积 vs 重量），没有密度不能换 → 改基础单位
      或按实际称重录入。
    """
    return {
        "status": view.status,
        "base_unit": view.base_unit,
        "base_quantity": _decimal_or_none(view.base_quantity, format_quantity),
        "normalized_unit_price": _decimal_or_none(
            view.normalized_unit_price, format_price
        ),
        "pack_spec": (
            pack_spec_payload(view.pack_spec) if view.pack_spec is not None else None
        ),
        "pack_spec_match": view.pack_spec_match,
        "reason": view.reason,
    }


def exclusion_payload(view: ExclusionSummary) -> dict[str, Any]:
    """有几条记录没算进这次统计，分别为什么。

    **全是 0 也要输出这一块**：前端据此决定是否显示「有 N 条没算进来」的提示，
    拿不到这一块就只能假设没有。
    """
    return {
        "total": view.total,
        "undated": view.undated,
        "unit_unknown": view.unit_unknown,
        "pending_pack_spec": view.pending_pack_spec,
        "incompatible_unit": view.incompatible_unit,
        "other_currency": view.other_currency,
    }


def period_payload(period: Period) -> dict[str, Any]:
    """一个统计期间。

    `days` 与 `basis` 必须一起读：13 天的本月与 31 天的上月不能当作同一种东西
    比较，`basis=month_to_date` 就是在说这件事。
    """
    return {
        "key": period.key,
        "date_from": period.date_from.isoformat(),
        "date_to": period.date_to.isoformat(),
        "days": period.days,
        "basis": period.basis,
    }


def period_stats_payload(stats: PeriodStats) -> dict[str, Any]:
    """一个期间的加权均价与它的构成。

    `weighted_average_unit_price` 为 `null` = 这个期间没有可换算的采购记录，
    **不是**均价为 0。`purchase_ids` 是追溯线索：照它能查到参与计算的那几条。
    """
    return {
        "period": period_payload(stats.period),
        "weighted_average_unit_price": _decimal_or_none(
            stats.weighted_average_unit_price, format_price
        ),
        "total_amount": format_money(stats.total_amount),
        "total_base_quantity": format_quantity(stats.total_base_quantity),
        "purchase_count": stats.purchase_count,
        "zero_amount_count": stats.zero_amount_count,
        "amount_mismatch_count": stats.amount_mismatch_count,
        "purchase_ids": list(stats.purchase_ids),
    }


def comparison_payload(view: ComparisonView) -> dict[str, Any]:
    """本期与基期的比较。

    ⚠️ `change_percent` 为 `null` 时**不要显示成 0%**：`status` 说明了是本期没数据、
    基期没数据，还是基期均价为 0（涨幅在数学上没有定义）。把「没有可比数据」
    显示成「价格没变」是在报告一个未发生的事实（docs/stru_dev.md 09.11）。
    """
    return {
        "status": view.status,
        "reason": view.reason,
        "current": period_stats_payload(view.current),
        "base": period_stats_payload(view.base),
        "change_amount": _decimal_or_none(view.change_amount, format_price),
        "change_percent": _decimal_or_none(view.change_percent, format_percent),
    }


def price_history_payload(page: PriceHistoryPage) -> dict[str, Any]:
    """一条食材的采购历史，每行带标准化价格或换不出来的原因。"""
    return {
        "food_item": food_item_ref_payload(page.food_item),
        "currency": page.currency,
        "base_unit": page.base_unit,
        "items": [
            {
                "purchase": purchase_payload(row.purchase),
                "conversion": conversion_payload(row.conversion),
            }
            for row in page.items
        ],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "convertible_count": page.convertible_count,
        "excluded": exclusion_payload(page.excluded),
        "available_currencies": list(page.available_currencies),
        "notes": list(page.notes),
    }


def monthly_prices_payload(report: MonthlyPriceReport) -> dict[str, Any]:
    """月度加权均价序列 + 本期/基期比较。图表由前端画，这里只给可解释的数据。"""
    return {
        "food_item": food_item_ref_payload(report.food_item),
        "currency": report.currency,
        "base_unit": report.base_unit,
        "months": [period_stats_payload(stats) for stats in report.months],
        "comparison": comparison_payload(report.comparison),
        "excluded": exclusion_payload(report.excluded),
        "available_currencies": list(report.available_currencies),
        "notes": list(report.notes),
    }


def weekly_prices_payload(report: WeeklyPriceReport) -> dict[str, Any]:
    """每周加权均价序列及本周与上周同期比较。"""
    return {
        "food_item": food_item_ref_payload(report.food_item),
        "currency": report.currency,
        "base_unit": report.base_unit,
        "weeks": [period_stats_payload(stats) for stats in report.weeks],
        "comparison": comparison_payload(report.comparison),
        "excluded": exclusion_payload(report.excluded),
        "available_currencies": list(report.available_currencies),
        "notes": list(report.notes),
    }


def source_stats_payload(stats: SourceStats) -> dict[str, Any]:
    """一个采购来源的均价。

    `descriptions` 与 `pack_spec` 给出来是为了让人判断**可比性**：系统只保证
    基础计量单位相同，规格与品质（`Ground Beef 80/20` vs `80/20 Ground Chuck`）
    要靠这两样自己看。
    """
    return {
        "source_name": stats.source_name,
        "source_name_normalized": stats.source_name_normalized,
        "weighted_average_unit_price": format_price(
            stats.weighted_average_unit_price
        ),
        "total_amount": format_money(stats.total_amount),
        "total_base_quantity": format_quantity(stats.total_base_quantity),
        "purchase_count": stats.purchase_count,
        "first_purchase_date": stats.first_purchase_date.isoformat(),
        "latest_purchase_date": stats.latest_purchase_date.isoformat(),
        "descriptions": list(stats.descriptions),
        "pack_spec": (
            pack_spec_payload(stats.pack_spec)
            if stats.pack_spec is not None
            else None
        ),
    }


def source_comparison_payload(report: SourceComparisonReport) -> dict[str, Any]:
    """来源比价。按均价从低到高排，`spread_*` 是最贵与最便宜的差。"""
    return {
        "food_item": food_item_ref_payload(report.food_item),
        "currency": report.currency,
        "base_unit": report.base_unit,
        "period": period_payload(report.period),
        "sources": [source_stats_payload(stats) for stats in report.sources],
        "cheapest_source_normalized": report.cheapest_source_normalized,
        "spread_amount": _decimal_or_none(report.spread_amount, format_price),
        "spread_percent": _decimal_or_none(report.spread_percent, format_percent),
        "excluded": exclusion_payload(report.excluded),
        "available_currencies": list(report.available_currencies),
        "notes": list(report.notes),
    }


def alert_settings_payload(view: AlertSettingsView) -> dict[str, Any]:
    return {
        "enabled": view.enabled,
        "threshold_percent": format_percent(view.threshold_percent),
        "updated_at": view.updated_at.isoformat(),
    }


def alerts_payload(report: AlertReport) -> dict[str, Any]:
    """涨价提醒。

    `skipped` 按原因统计「比较不了的食材有几个」——那些不是「没涨」，而是算不出
    涨幅，两者在界面上不能长得一样。
    """
    return {
        "settings": alert_settings_payload(report.settings),
        "threshold_percent": format_percent(report.threshold_percent),
        "current_period": period_payload(report.current),
        "base_period": period_payload(report.base),
        "alerts": [
            {
                "food_item": food_item_ref_payload(row.food_item),
                "currency": row.currency,
                "base_unit": row.base_unit,
                "comparison": comparison_payload(row.comparison),
            }
            for row in report.alerts
        ],
        "compared_count": report.compared_count,
        "skipped": report.skipped,
        "excluded": exclusion_payload(report.excluded),
        "notes": list(report.notes),
    }


def pending_pack_spec_payload(row: PendingPackSpecRow) -> dict[str, Any]:
    """一组待补箱规的采购记录。

    `suggestion` 是**从原始商品描述里读出来的候选**，没有就是 `null`。
    ⚠️ 它必须人确认才生效：真实单据上 `1000g x 10`（每箱 10×1000g）与 `6X30`
    （6 箱 × 每箱 30 磅）同形不同义，差 6 倍，所以裸数字写法一律不给候选。
    """
    return {
        "food_item": food_item_ref_payload(row.food_item),
        "source_name": row.source_name,
        "source_name_normalized": row.source_name_normalized,
        "purchase_unit": row.purchase_unit,
        "purchase_count": row.purchase_count,
        "total_amount": format_money(row.total_amount),
        "latest_purchase_date": (
            row.latest_purchase_date.isoformat()
            if row.latest_purchase_date is not None
            else None
        ),
        "descriptions": list(row.descriptions),
        "suggestion": (
            {
                "pack_size": format_quantity(row.suggestion.pack_size),
                "pack_unit": row.suggestion.pack_unit,
                "spec_text": row.suggestion.spec_text,
                "from_description": row.suggestion.from_description,
                "confirmation_required": True,
            }
            if row.suggestion is not None
            else None
        ),
    }


# --- 整理批次（多文件计划 M02 / M03） ----------------------------------------


def intake_candidate_payload(view: IntakeCandidateView) -> dict[str, Any]:
    """一条系统建议。

    ⚠️ 候选与人工决定在响应里是**两处**，不是一处：`basis` 说明这条建议的依据，
    而它永远不会自己变成 `decided_*`（docs/multifiles.md 第 4 节）。界面要能把
    「系统建议」与「人确认过」显著区分开。
    """
    return {
        "id": view.id,
        "field": view.field,
        "value": view.value,
        "value_normalized": view.value_normalized,
        "basis": view.basis,
        "confidence": str(view.confidence) if view.confidence is not None else None,
        "evidence": view.evidence,
        "created_at": view.created_at.isoformat(),
    }


def intake_file_payload(view: IntakeFileRefView) -> dict[str, Any]:
    """批次里的一个文件：处理到哪一步、切出了几个内容。

    `status` 只有四个离散值（`pending` / `ready` / `failed` / `discarded`），
    **没有百分比**：本地切页没有可信的中间进度，编一个出来只会让人以为卡住了。
    `item_count` 在 `ready` 之后才有值，它就是「这份文件贡献了几张卡片」。
    `handed_off_item_count` 是其中**已经开始填表或已入库**的项数：不为 0 时
    `DELETE .../files/<id>` 会 409，上传页据此把「移除」按钮暗下来并说出原因，
    而不是让人点一下才撞到报错（第 14 节）。

    ⚠️ 不透出 `storage_key`：那是存储布局的内部引用，回看原件走
    `/api/intake/files/<id>/content`。
    """
    return {
        "id": view.id,
        "batch_id": view.batch_id,
        "original_name": view.original_name,
        "upload_order": view.upload_order,
        "status": view.status,
        "item_count": view.item_count,
        "handed_off_item_count": view.handed_off_item_count,
        "sheet_name": view.sheet_name,
        "sha256": view.sha256,
        "size": view.size,
        "content_type": view.content_type,
        "error": (
            None
            if view.error_code is None
            else {"code": view.error_code, "message": view.error_message}
        ),
        "discarded_at": view.discarded_at.isoformat() if view.discarded_at else None,
        "created_at": view.created_at.isoformat(),
    }


def intake_item_payload(view: IntakeItemView) -> dict[str, Any]:
    """一个可归类内容（整理页上的一张卡片）。

    `page_no` / `row_no` 是**回看原件的坐标**：页内容有页号没行号，表格行反之。
    `decided_*` 为 null 不是"识别失败"，而是「还没人定过」——卡片留在待归类区或
    某一天的「商家待定」里。

    ⚠️ `handed_off_at` 不是 null 时这张卡片**已经进了审核**，整理命令会被服务端
    拒成 409。透出它是为了让界面先说清楚「这张已送审，改它要走审核里的分类修订」，
    而不是让人点一下才撞到报错（多文件计划 M12）。`review_invoice_id` 配套说出
    **在填哪一张草稿**，整理页据此给出「继续填表」的入口（第 11 节 ④）。
    """
    return {
        "id": view.id,
        "batch_id": view.batch_id,
        "batch_file_id": view.batch_file_id,
        "kind": view.kind,
        "page_no": view.page_no,
        "row_no": view.row_no,
        "sheet_name": view.sheet_name,
        "original_name": view.original_name,
        "sha256": view.sha256,
        "decided_purchase_date": (
            view.decided_purchase_date.isoformat()
            if view.decided_purchase_date
            else None
        ),
        "decided_source_name": view.decided_source_name,
        "decided_source_name_normalized": view.decided_source_name_normalized,
        "decided_at": view.decided_at.isoformat() if view.decided_at else None,
        "version": view.version,
        "handed_off_at": (
            view.handed_off_at.isoformat() if view.handed_off_at else None
        ),
        "review_invoice_id": view.review_invoice_id,
        "review_status": view.review_status,
        "pile_excluded": view.pile_excluded,
        "row_values": view.row_data.get("values") if isinstance(view.row_data, dict) else None,
        "candidates": [
            intake_candidate_payload(candidate) for candidate in view.candidates
        ],
        "recognition": intake_recognition_payload(view),
    }


def intake_recognition_payload(view: IntakeItemView) -> dict[str, Any]:
    """这张卡片的日期/商家识别跑到哪一步了（多文件计划 M05）。

    ⚠️ `status` 与「有没有候选」**不是同一件事**：读完发现纸面上没有商家时
    `status` 是 `done`、候选是 0 条；没跑过时是 null。前端据此说得出
    「还没读」「读了但纸上没有」「读失败了」三句不同的话，而不是一律显示「未知」。

    `date_raw_text` 与 `date_readings` 是这段原文读出来的值。`6/11/2026` 这类日/月
    顺序定不死的写法按本项目默认的月/日/年读成一条（第 15 节）：`date_ambiguous`
    仍然是 `true`（文本确实定不死，这是事实），`date_assumed_order` 说出它是按哪种
    书写习惯读的。界面据此说「按月/日/年读作 6 月 11 日」，而不是「要人工确认」。
    确认商家并给这家设 `date_order` 之后，`POST .../items/<id>/reparse-date` 用这段
    原文重新解析，**不再调一次模型**。

    不透出整份 payload（里面还有行项目与原始响应，那是 M08 建草稿要用的）：
    卡片上要的是「读到什么、还能不能定死日期」。
    """
    payload = view.recognition_payload or {}
    # 老 payload 里那一对读法在这里收成一条，与卡片上的候选说同一句话（第 15 节）。
    readings = collapse_legacy_readings(payload.get("date_readings") or {})
    date_order = payload.get("date_order") or {}
    return {
        "status": view.recognition_status,
        "recognized_at": (
            view.recognized_at.isoformat() if view.recognized_at else None
        ),
        "provider": payload.get("provider"),
        "models": list(payload.get("models") or []),
        "prompt_sha12": payload.get("prompt_sha12"),
        "is_fake": payload.get("is_fake"),
        "date_raw_text": readings.get("raw_text"),
        "date_ambiguous": bool(readings.get("ambiguous")),
        "date_assumed_order": readings.get("assumed_order"),
        "date_problem": readings.get("problem"),
        "date_readings": list(readings.get("readings") or []),
        "date_order": date_order.get("value"),
        "date_order_from": date_order.get("from"),
        "notes": list(payload.get("notes") or []),
        "error": (
            None
            if view.recognition_error_code is None
            else {
                "code": view.recognition_error_code,
                "message": view.recognition_error_message,
            }
        ),
    }


def intake_item_page_payload(page: IntakeItemPage) -> dict[str, Any]:
    return {
        "items": [intake_item_payload(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


def intake_batch_payload(view: IntakeBatchView) -> dict[str, Any]:
    """一个整理批次。

    `status` 是**派生值**（数据库里没这一列）：open / closed / discarded。
    `files` 里不含已撤掉的文件，所以它就是「这一批现在有什么」。

    `item_count` / `unfinished_item_count` 是这一批「还剩几项没填完」（第 22 节）：
    整理页底下那个「确认完成所有填写」用它决定按不按得动、按不动时说得出还差什么。
    """
    return {
        "id": view.id,
        "status": view.status,
        "item_count": view.item_count,
        "unfinished_item_count": view.unfinished_item_count,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
        "closed_at": view.closed_at.isoformat() if view.closed_at else None,
        "discarded_at": view.discarded_at.isoformat() if view.discarded_at else None,
        "files": [intake_file_payload(item) for item in view.files],
    }


def intake_ingested_payload(result: IngestedFile) -> dict[str, Any]:
    """一次切页的结果。

    `outcome` 三种：`ingested` 切成了、`already_ingested` 早就切过（页的 id 不变）、
    `failed` 失败并带原因。`notes` 是**留痕**——转了方向、缺解码器、某一页的内嵌图
    列不出来，都在这里，不静默发生。
    """
    return {
        "file": intake_file_payload(result.ref),
        "outcome": result.outcome,
        "document_format": result.document_format,
        "page_count": result.page_count,
        "item_ids": [item.id for item in result.items],
        "notes": list(result.notes),
        "error": (
            None
            if result.error_code is None
            else {"code": result.error_code, "message": result.error_message}
        ),
    }


def intake_recognized_payload(result: RecognizedItem) -> dict[str, Any]:
    """一个内容的识别结局（多文件计划 M05）。

    `outcome` 三种：`recognized` 读完了、`already_recognized` 早就读过（**不花钱**，
    这正是不重复收费的闸门）、`failed` 失败并带原因。

    `model_calls` 是**这一次真的发了几次模型调用**：数字 PDF 文字层与 CSV/XLSX 行
    都是 0，扫描页等于配置里的模型个数。把它放进响应是为了让「没有重复收费」成为
    一条可以核对的事实，而不是一句承诺。
    """
    return {
        "item": intake_item_payload(result.item),
        "outcome": result.outcome,
        "provider": result.provider,
        "model_calls": result.calls,
        "date_candidates": list(result.date_candidates),
        "source_candidates": list(result.source_candidates),
        "notes": list(result.notes),
        "error": (
            None
            if result.error_code is None
            else {
                "code": result.error_code,
                "message": result.item.recognition_error_message,
            }
        ),
    }


def intake_upload_payload(outcome: UploadOutcome) -> dict[str, Any]:
    """一次文件上传的结果。

    三个提示字段都是**事实而不是拦截结果**（能走到这里就说明没被拦住）：

    - `reused_bytes`：这份字节库里已经有了，没有再存一份。
    - `shared_with_batch_ids`：同一份字节还在哪些别的批次里。
    - `same_bytes_invoice_ids`：同一份字节在旧单张上传路径上已有的单据。

    `queued` 为 true 表示切页交给后台了，此时 `ingest` 是 null——**不假装已经切完**，
    前端去轮询 `GET /api/intake/batches/<id>`。
    """
    registered = outcome.registered
    return {
        "file": intake_file_payload(outcome.ref),
        "queued": outcome.queued,
        "reused_bytes": registered.reused_bytes,
        "shared_with_batch_ids": list(registered.shared_with_batch_ids),
        "same_bytes_invoice_ids": list(registered.same_bytes_invoice_ids),
        "ingest": (
            None if outcome.ingested is None else intake_ingested_payload(outcome.ingested)
        ),
    }


# --- 多文件计划 M07：商家记忆与相似堆 ----------------------------------------


def intake_alias_payload(view: AliasView) -> dict[str, Any]:
    """一条别名映射：纸面上的这种写法指向人确认过的哪一家。

    ⚠️ `basis` 必须透出：`user` 是人自己说的（重算不会覆盖它），
    `confirmed_paper_reading` / `paper_bilingual` 是系统**观察**到的，可以删掉或
    改指向——这正是 docs/multifiles.md 第 5 节要求的「可更正」。
    """
    return {
        "id": view.id,
        "alias_key": view.alias_key,
        "alias_text": view.alias_text,
        "source_key": view.source_key,
        "source_name": view.source_name,
        "basis": view.basis,
        "evidence": view.evidence,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }


def intake_source_memory_payload(entry: SourceMemoryEntry) -> dict[str, Any]:
    """一家确认过的来源。

    `source_key` 是规范化名（`Costco` 与 `COSTCO` 同一个键），`source_name` 是
    **最近一次人工确认的写法**——不是翻译，也不是模型给的名字。两个计数让人看出
    这条记忆有多少依据。
    """
    return {
        "source_key": entry.source_key,
        "source_name": entry.source_name,
        "confirmed_items": entry.confirmed_items,
        "purchase_records": entry.purchase_records,
        "last_confirmed_at": (
            entry.last_confirmed_at.isoformat() if entry.last_confirmed_at else None
        ),
        "aliases": [intake_alias_payload(alias) for alias in entry.aliases],
    }


def intake_suggestion_payload(view: SuggestionView) -> dict[str, Any]:
    """一条商家建议。

    ⚠️ `score` 只用于**排序**，不是概率、也不是「到几分就能自动确认」的阈值：
    界面按它排先后，确认永远是人点的那一下（第 4 节）。
    """
    return {
        "basis": view.basis,
        "value": view.value,
        "value_normalized": view.value_normalized,
        "score": str(view.score),
        "evidence": view.evidence,
    }


def intake_item_suggestions_payload(view: ItemSuggestions) -> dict[str, Any]:
    """一张卡片这一轮的建议。

    `conflicts` 是**被纸面证据压住**的建议：这一页纸上写着别家，所以同文件/外观
    的建议不生效。它不是错误，而是第 3.2 节要求的行为，必须看得见而不是静默发生。
    """
    return {
        "item_id": view.item_id,
        "kind": view.kind,
        "page_no": view.page_no,
        "row_no": view.row_no,
        "original_name": view.original_name,
        "paper_source": view.paper_source,
        "resolved": view.resolved,
        "suggestions": [
            intake_suggestion_payload(item) for item in view.suggestions
        ],
        "conflicts": [dict(item) for item in view.conflicts],
        "notes": list(view.notes),
    }


def intake_pile_payload(view: PileView) -> dict[str, Any]:
    """一堆版式相同、读不出商家的内容。

    `basis_detail` 把两种堆分开：`layout_template` 量到了模板特征（格线行数、
    字号集合），`layout_unknown` 没量出来——后者只够让人扫视缩略图，系统不会
    据此给商家建议。`excluded_item_ids` 是人一张张排除的例外，它们不在
    `item_ids` 里，也拿不到这一堆的建议。
    """
    return {
        "fingerprint": view.fingerprint,
        "content_class": view.content_class,
        "basis_detail": view.basis_detail,
        "template_evidence": view.template_evidence,
        "features": view.features,
        "item_ids": list(view.item_ids),
        "item_count": view.item_count,
        "excluded_item_ids": list(view.excluded_item_ids),
        "confirmed_sources": [dict(item) for item in view.confirmed_sources],
        "notes": list(view.notes),
    }


def intake_pile_board_payload(board: PileBoard) -> dict[str, Any]:
    return {
        "batch_id": board.batch_id,
        "piles": [intake_pile_payload(pile) for pile in board.piles],
        "unprofiled_item_ids": list(board.unprofiled_item_ids),
        "notes": list(board.notes),
    }


def intake_suggestion_run_payload(run: SuggestionRun) -> dict[str, Any]:
    """一次建议重算的结果。

    ⚠️ `model_calls` 恒为 0，而且是**结构上**的 0：建议服务没有识别器依赖。
    `layout_pending` 不为 0 时再调一次接着量版式，已量过的不会重量。
    """
    return {
        "batch_id": run.batch_id,
        "items": [intake_item_suggestions_payload(item) for item in run.items],
        "piles": [intake_pile_payload(pile) for pile in run.piles],
        "aliases_recorded": [
            intake_alias_payload(alias) for alias in run.aliases_recorded
        ],
        "layout_profiled": run.layout_profiled,
        "layout_pending": run.layout_pending,
        "total": run.total_items,
        "limit": run.limit,
        "offset": run.offset,
        "model_calls": run.model_calls,
        "notes": list(run.notes),
    }
