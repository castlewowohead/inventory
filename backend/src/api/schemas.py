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
