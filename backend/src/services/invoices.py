"""原始单据、审核草稿与逐行确认（步骤 05）。

本模块是 Feature 4 的核心数据流程：**上传 → 草稿 → 修改 / 匹配 → 逐行确认 → 采购
历史**。手工录入（步骤 04）、表格导入（步骤 06）与真实 OCR（步骤 08）最终都走这一套
审核与写入规则。

几条贯穿全模块的口径（决定见 docs/stru_dev.md 步骤 05）：

1. **系统从不自己把数据写进采购历史。** 唯一的入口是老板对某一行点确认，确认时调用
   步骤 04 的 `PurchaseService.create_purchase()`——同一套数值解析、同一套
   「数量 × 单价 ≈ 行金额」容差、同一套错误码。本模块不另立一份数值口径。
2. **认不出来不等于 0。** 行上的当前值全部可空；缺必填值的行**不能确认**，报错里
   列出缺哪几个字段。原始识别证据另存在 `raw_fields`，确认之后也不改动——那是单据
   事实，老板后来把数字改对了，差异由 `purchase_revisions` 解释。
3. **逐行独立。** 批量确认里一行失败不回滚其它行（`imp_notes.md` 4.7：不应因为
   一两个模糊商品而阻止其他正确商品的保存）。每行各自一个事务。
4. **重复确认两道防线。** 服务层先查「这一行有没有未作废的采购记录」给出能看懂的
   409；数据库上 `purchases` 的局部唯一索引挡并发（连点两次、两个标签页）。
5. **匹配只预填候选，不自动确认。** 「这句原始描述以前确认到哪个食材」直接查步骤 04
   的采购历史（规范化后精确命中），同来源优先、最近优先，最多给 5 个候选。确认请求
   必须显式带上选中的食材——系统猜的东西不会自己变成事实。
   步骤 10 在精确命中之后补了一层**模糊**匹配（只在精确层空手时才跑），
   并给每个候选加上 `match_reason` / `score` / `matched_description`。

**本模块不做**：真实 OCR（步骤 07 评估、步骤 08 接入；这里只调注入的识别器，当前
是明确标识的 Fake）、多页归并与图像预处理（步骤 08）、单位换算与价格统计（步骤 09）、
模糊匹配与映射记忆复用（步骤 10）、发票审批流与强制供应商 / 单号 / USD（刻意不做）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, BinaryIO, Final, Iterable, Mapping, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.contracts import (
    DOCUMENT_FIELDS,
    FAKE_PROVIDER,
    FIELD_CURRENCY,
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_TOTAL,
    FIELD_ITEM_DESCRIPTION,
    FIELD_LINE_AMOUNT,
    FIELD_QUANTITY,
    FIELD_SUPPLIER,
    FIELD_UNIT,
    FIELD_UNIT_PRICE,
    LINE_FIELDS,
    RecognitionRequest,
    RecognitionResult,
    RecognizedField,
    RecognizedLine,
)
from src.db import Database
from src.errors import AppError, ConflictError, NotFoundError, ValidationError
from src.integrations.file_storage import LocalFileStorage
from src.integrations.ocr import Recognizer
from src.models import (
    AMOUNT_SCALE,
    DESCRIPTION_MAX_LENGTH,
    EXTRACTION_STATUS_QUEUED,
    EXTRACTION_STATUS_RUNNING,
    INVOICE_NUMBER_MAX_LENGTH,
    LINE_KIND_ADJUSTMENT,
    LINE_KIND_PURCHASE,
    LINE_STATUS_CONFIRMED,
    LINE_STATUS_PENDING,
    LINE_STATUS_SKIPPED,
    PURCHASE_UNIT_MAX_LENGTH,
    QUANTITY_SCALE,
    SKIP_REASON_MAX_LENGTH,
    SOURCE_NAME_MAX_LENGTH,
    UNIT_PRICE_SCALE,
    Invoice,
    InvoiceFile,
    InvoiceLine,
    utcnow,
)
from src.repositories.invoices import (
    InvoiceFileRepository,
    InvoiceLineRepository,
    InvoiceRepository,
)
from src.repositories.extraction import ExtractionJobRepository
from src.repositories.ingredients import FoodItemRepository
from src.repositories.purchases import PurchaseRepository
from src.services import matching
from src.services.ingredients import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    UNSET,
    FoodItemView,
    IngredientService,
    normalize_name,
)
from src.services.purchases import (
    AMOUNT_TOLERANCE,
    CURRENCIES,
    DEFAULT_CURRENCY,
    ENTRY_SOURCE_INVOICE,
    ENTRY_SOURCE_IMPORT,
    LINE_KINDS,
    PurchaseService,
    parse_decimal,
    parse_purchase_date,
    normalize_purchase_unit,
)
from src.services.validation import (
    optional_text,
    require_choice,
    require_limit,
    require_offset,
)

logger = logging.getLogger(__name__)

# --- 口径常量 ---------------------------------------------------------------

# 允许上传的扩展名。`heic` 收下但**不识别**：iPhone 原图常常是它，保留原件是本步骤
# 的职责，解码留给步骤 08（真接上 OCR 时若厂商不吃 HEIC，再加转码依赖）。
ALLOWED_UPLOAD_EXTENSIONS: Final[tuple[str, ...]] = ("jpg", "jpeg", "png", "pdf", "heic")
RECOGNIZABLE_EXTENSIONS: Final[tuple[str, ...]] = ("jpg", "jpeg", "png", "pdf")

# 提取任务「还没结束」的两种状态怎么说给人听。错误消息是给老板看的，
# 不该出现 queued / running 这种词；原始状态另外放进 details 里给排查用。
_EXTRACTION_IN_PROGRESS: Final[dict[str, str]] = {
    EXTRACTION_STATUS_QUEUED: "还在排队等着识别",
    EXTRACTION_STATUS_RUNNING: "正在识别",
}

# 一张单据最多挂多少个文件。多页单据 + 补拍，20 个够用；给个上限是为了别让一次
# 误操作把几百个文件挂到同一张单上。
MAX_FILES_PER_INVOICE: Final[int] = 20

# 单据的整体状态。**不存列**，由各行状态算出来（见 `_invoice_status`）：
# 行状态是唯一的事实来源，再存一列就会出现两份互相打架的真相。
INVOICE_STATUS_DRAFT: Final[str] = "draft"
INVOICE_STATUS_IN_REVIEW: Final[str] = "in_review"
INVOICE_STATUS_READY: Final[str] = "ready_to_complete"
INVOICE_STATUS_COMPLETED: Final[str] = "completed"
INVOICE_STATUS_VOIDED: Final[str] = "voided"

# 对账结果。`not_checked` 与 `no_total` 是两种不同的「没法对」，不能都显示成 0 差额。
RECONCILIATION_NOT_CHECKED: Final[str] = "not_checked"
RECONCILIATION_NO_TOTAL: Final[str] = "no_total"
RECONCILIATION_BALANCED: Final[str] = "balanced"
RECONCILIATION_MISMATCH: Final[str] = "mismatch"

# 行匹配：最多给几个候选，以及回看多少条历史采购。200 条足够覆盖同一句描述的历史，
# 又不会在大表上扫出性能问题。
MATCH_CANDIDATE_LIMIT: Final[int] = 5
MATCH_HISTORY_LIMIT: Final[int] = 200

# 步骤 10.4 的模糊层一次最多比较多少条「描述 → 食材」映射（已按描述+食材+来源聚合，
# 所以这是**不同描述**的条数，不是采购记录条数）。
#
# ⚠️ 截断按 `MAX(purchases.id) DESC`，即**最近确认过的映射一定在里面**。
# 这条功能最容易被察觉的失败方式是「老板刚纠正过的映射，下一张单据就没用上」，
# 按别的顺序截断就会撞上它。精确命中那一层走索引、不受这个上限影响。
MATCH_MAPPING_LIMIT: Final[int] = 2000

# 确认一行必须齐备的四个值。食材另算（调整行可以没有食材）。
REQUIRED_LINE_FIELDS: Final[tuple[str, ...]] = (
    "quantity",
    "purchase_unit",
    "unit_price",
    "line_amount",
)

LINE_STATUSES_EDITABLE: Final[tuple[str, ...]] = (
    LINE_STATUS_PENDING,
    LINE_STATUS_SKIPPED,
)


# --- 上传入参 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """一个待保存的上传文件。

    `content` 是**字节**而不是流：上传后立刻要算 `sha256` 判重，流读完就没了。
    单文件上限由配置的 `UPLOAD_MAX_BYTES` 约束，所以整份读进内存是可接受的。
    """

    content: bytes
    original_name: str
    content_type: str | None = None


# --- 只读视图 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvoiceFileView:
    id: int
    original_name: str
    size: int
    sha256: str
    content_type: str | None
    page_order: int
    created_at: datetime

    @classmethod
    def of(cls, invoice_file: InvoiceFile) -> "InvoiceFileView":
        return cls(
            id=invoice_file.id,
            original_name=invoice_file.original_name,
            size=invoice_file.size,
            sha256=invoice_file.sha256,
            content_type=invoice_file.content_type,
            page_order=invoice_file.page_order,
            created_at=invoice_file.created_at,
        )


@dataclass(frozen=True, slots=True)
class MatchCandidateView:
    """「这句原始描述以前确认到哪个食材」的一个候选。

    `purchase_count` 与 `last_purchase_date` 是给人看的依据——老板能判断这是常买的
    那个还是偶然一次。`same_source` 为真表示同一个采购来源也对上了，这种候选排在前面：
    同一家供应商的同一句描述，几乎一定是同一样东西。

    步骤 10.4 加了两列：

    - `match_reason`：`description_exact`（05.6 那一层）/ `description_fuzzy`
      （10.4 新增）/ `supplier_history`（08.3i）。**理由必须跟着候选一起给界面**
      ——「这句描述你以前就确认成它」和「有一句**像**的描述曾经确认成它」对老板
      的下一步动作完全不同，前者可以扫一眼就过，后者必须看清楚；
    - `score`：模糊命中的相似度（精确命中是 1.0，供应商历史是 `None`——那条候选
      根本不是按文本像不像来的，给个分数会让人以为它也是比出来的）。
    - `matched_description`：模糊命中时，**命中的是历史上哪一句描述**。不给这句话，
      老板就无从判断这个候选该不该用。
    """

    food_item: FoodItemView
    purchase_count: int
    last_purchase_id: int
    last_purchase_date: date | None
    same_source: bool
    match_reason: str = matching.REASON_DESCRIPTION_EXACT
    score: float | None = 1.0
    matched_description: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceLineView:
    """一行的只读快照。

    数值保持 `Decimal`，由 API 层转成字符串（与步骤 04 一致，不经 float）。
    `missing_fields` 是「还差什么才能确认」，`match_candidates` 只在详情视图里算
    （列表页逐行查历史会变成 N+1，而列表页也用不到它）。

    `food_item_match` 是步骤 10.C 留下的**痕迹**：这一格的食材是怎么来的，
    或者为什么没能自动来。三种 `reason`——

    - `description_exact`：老板确认过的那句描述又出现了，直接选中；
    - `keyword_exact` / `keyword_created`：关键词命中食材库 / 按关键词自动建了一条；
    - `needs_base_unit`：关键词剥出来了，但**单位归一不出来所以没有建**。
      这一条带着 `suggested_name` / `suggested_name_zh` / `suggested_category_id`,
      界面据此把新建表单预填好、光标落在基础单位那一格。

    ⚠️ 没有这一列的话，「为什么这一行自动选好了、那一行没有」在界面上完全看不出来。
    """

    id: int
    line_no: int
    status: str
    skip_reason: str | None
    line_kind: str
    food_item: FoodItemView | None
    description: str | None
    quantity: Decimal | None
    purchase_unit: str | None
    unit_price: Decimal | None
    line_amount: Decimal | None
    purchase_date: date | None
    source_name: str | None
    currency: str | None
    raw_fields: dict[str, Any] | None
    review_issues: tuple[dict[str, Any], ...]
    food_item_match: dict[str, Any] | None
    page: int | None
    version: int
    confirmed_at: datetime | None
    purchase_id: int | None
    missing_fields: tuple[str, ...]
    match_candidates: tuple[MatchCandidateView, ...]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(
        cls,
        line: InvoiceLine,
        *,
        candidates: tuple[MatchCandidateView, ...] = (),
    ) -> "InvoiceLineView":
        active = _active_purchase(line)
        return cls(
            id=line.id,
            line_no=line.line_no,
            status=line.status,
            skip_reason=line.skip_reason,
            line_kind=line.line_kind,
            food_item=(
                FoodItemView.of(line.food_item) if line.food_item is not None else None
            ),
            description=line.description,
            quantity=line.quantity,
            purchase_unit=line.purchase_unit,
            unit_price=line.unit_price,
            line_amount=line.line_amount,
            purchase_date=line.purchase_date,
            source_name=line.source_name,
            currency=line.currency,
            raw_fields=json.loads(line.raw_fields) if line.raw_fields else None,
            review_issues=tuple(_review_metadata(line).get("issues", [])),
            food_item_match=_review_metadata(line).get("food_item_match"),
            page=line.page,
            version=line.version,
            confirmed_at=line.confirmed_at,
            purchase_id=active.id if active is not None else None,
            missing_fields=_missing_fields(line, food_item_id=line.food_item_id),
            match_candidates=candidates,
            created_at=line.created_at,
            updated_at=line.updated_at,
        )


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    """收口面板上的一格：这张单据里某一类食材花了多少钱（步骤 10.A）。

    金额口径沿用 `_reconciliation()` 现成的那一条——**以采购记录上的金额为准**
    （那是真正入账的数字，校验过、必要时老板显式确认过），不拿行上的值重算。
    """

    category_id: int | None
    name: str
    name_zh: str | None
    line_count: int
    amount: Decimal


@dataclass(frozen=True, slots=True)
class ReconciliationView:
    """整单对账结果。

    只有所有行都 `confirmed` 或 `skipped` 之后才真的比总额；还有待确认行时
    `status` 是 `not_checked`，单据总额没识别出来时是 `no_total`——这两种「没法对」
    必须和「对上了」分得开，否则界面上会显示成 0 差额，看起来像核对过了。

    差额超过容差也**只是提示**：它不阻止确认任何一行，也不阻止把单据标成完成。
    容差沿用步骤 04 的 `AMOUNT_TOLERANCE`，不另立阈值。
    """

    status: str
    invoice_total: Decimal | None
    confirmed_amount: Decimal | None
    difference: Decimal | None
    tolerance: Decimal
    line_count: int
    pending_count: int
    confirmed_count: int
    skipped_count: int
    by_category: tuple[CategoryTotal, ...] = ()
    """这张单据按分类摊开的金额（步骤 10.A），金额从大到小。

    **它是「让老板理解分类在干什么」的主力**：解释一个字段最有效的方式不是加说明
    文字，是把它的产物摆在旁边。他审完最后一行点「完成单据」，30 秒后就看见自己
    那几次点击变成了一张看得懂的账。

    ⚠️ 它同时是**分类粒度的验收尺子**：分类一旦变得过细，这个面板就退化成行列表
    的重印，它的全部价值（把 15 行压成 4 个数）归零。
    """

    adjustment_total: CategoryTotal | None = None
    """⚠️ **`adjustment` 行单列一桶，不混进任何分类**——与步骤 09 的价格统计同一条
    口径：运费与整单折让不是某一类食材的成本。摆成独立字段而不是 `by_category`
    里的一行，是为了让界面**不可能**把它和分类加在一起。"""

    notes: tuple[str, ...] = ()
    """必须跟着数字一起读的话（同步骤 09 的统计响应）。"""


@dataclass(frozen=True, slots=True)
class InvoiceView:
    """一张单据的只读快照。`status` 是派生值，见 `_invoice_status`。"""

    id: int
    status: str
    source_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    currency: str | None
    invoice_total: Decimal | None
    recognition_provider: str | None
    recognition_is_fake: bool | None
    recognized_at: datetime | None
    raw_document_fields: dict[str, Any] | None
    completed_at: datetime | None
    voided_at: datetime | None
    uploaded_at: datetime
    updated_at: datetime
    files: tuple[InvoiceFileView, ...]
    lines: tuple[InvoiceLineView, ...]
    reconciliation: ReconciliationView
    possible_duplicate_invoice_ids: tuple[int, ...]
    entry_source: str
    import_metadata: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class InvoicePage:
    items: tuple[InvoiceView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ConfirmOption:
    """确认一行时的逐行选项。

    `food_item_id` 为 `UNSET` 表示「用行上已有的食材」；`new_food_item` 是当场新建
    （只要名称、分类与基础单位），两者只能给一个。`version` 给了就做乐观锁检查。
    """

    line_id: int
    version: int | None = None
    food_item_id: Any = UNSET
    new_food_item: Mapping[str, Any] | None = None
    accept_amount_mismatch: bool = False


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    """确认一行的结果。失败的行带着自己的错误码回来，不影响其它行。"""

    line_id: int
    ok: bool
    purchase_id: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConfirmOutcome:
    """一次批量确认的整体结果，连同确认之后的单据快照。"""

    results: tuple[ConfirmResult, ...]
    invoice: InvoiceView

    @property
    def confirmed_count(self) -> int:
        return sum(1 for result in self.results if result.ok)

    @property
    def failed_count(self) -> int:
        return sum(1 for result in self.results if not result.ok)


# --- 模块级辅助 -------------------------------------------------------------


def _expire_collections(session: Session, invoice: Invoice) -> None:
    """让 `files` / `lines` 下次访问时重新查库。

    这两个集合是带 `selectinload` 查出来的，已经"加载过"了；新行是带外键直接插的，
    不会自动出现在集合里。不作废它们的话，刚上传的文件在同一次响应里看不见——
    而那正是前端拿来刷新界面的那份数据。
    """
    session.expire(invoice, ["files", "lines"])


def _active_purchase(line: InvoiceLine):
    """这一行当前有效（未作废）的那条采购记录。

    撤销确认是把采购记录软删，所以一行可能挂着若干已作废的旧记录；有效的最多一条，
    由 `purchases` 上的局部唯一索引保证。
    """
    for purchase in line.purchases:
        if purchase.voided_at is None:
            return purchase
    return None


def _missing_fields(line: InvoiceLine, *, food_item_id: int | None) -> tuple[str, ...]:
    """确认这一行还缺哪些值。

    未识别的数值**不补 0**，所以这些格子就是空的；缺什么必须说清楚，否则老板只会
    看到一句「不能确认」。正常采购行还必须有食材；调整行（退货 / 折让 / 整单让价）
    可以没有。
    """
    missing = [
        name for name in REQUIRED_LINE_FIELDS if getattr(line, name) is None
    ]
    if line.line_kind == LINE_KIND_PURCHASE and food_item_id is None:
        missing.append("food_item_id")
    return tuple(missing)


def _invoice_status(invoice: Invoice) -> str:
    """单据的整体状态，由各行状态派生。

    - `voided`：整张单据作废；
    - `draft`：一行都还没处理（或者还没有任何行）；
    - `in_review`：处理了一部分，还有待确认的行；
    - `ready_to_complete`：所有行都已确认或跳过，但老板还没做收口确认；
    - `completed`：老板点过「完成」。

    `ready_to_complete` 与 `completed` 分开，是因为那一次点击是**漏行唯一会被拦住
    的时刻**：识别少读了一整行，各行之和与单据总额对不上能抓到一部分，但手写单常常
    连总额都没有，那时只剩人眼这一关。
    """
    if invoice.voided_at is not None:
        return INVOICE_STATUS_VOIDED
    if invoice.completed_at is not None:
        return INVOICE_STATUS_COMPLETED
    lines = invoice.lines
    if not lines:
        return INVOICE_STATUS_DRAFT
    pending = sum(1 for line in lines if line.status == LINE_STATUS_PENDING)
    if pending == 0:
        return INVOICE_STATUS_READY
    if pending == len(lines):
        return INVOICE_STATUS_DRAFT
    return INVOICE_STATUS_IN_REVIEW


def _review_metadata(line: InvoiceLine) -> dict:
    return json.loads(line.review_metadata) if line.review_metadata else {}


def _inherited(line: InvoiceLine, invoice: Invoice, field: str):
    # 表格中明确写过的值（包括异常值后来被人工清空）不回退成另一个日期或币种。
    if field in _review_metadata(line).get("overrides", []):
        return getattr(line, field)
    header_field = "invoice_date" if field == "purchase_date" else field
    return getattr(line, field) or getattr(invoice, header_field)


def _reconciliation(invoice: Invoice) -> ReconciliationView:
    """算一次整单对账。口径见 `ReconciliationView` 的文档。"""
    pending = confirmed = skipped = 0
    total_confirmed = Decimal("0.00")
    currencies: set[str] = set()
    # 步骤 10.A：按分类分桶。`None` 这个键是**调整行那一桶**，与任何分类都不相加。
    buckets: dict[int | None, dict[str, Any]] = {}
    for line in invoice.lines:
        if line.status == LINE_STATUS_PENDING:
            pending += 1
        elif line.status == LINE_STATUS_SKIPPED:
            skipped += 1
        else:
            confirmed += 1
            purchase = _active_purchase(line)
            # 以采购记录上的金额为准：那是真正入账的数字（校验过、必要时老板显式
            # 确认过）。行上的值可能在确认之后被改过，拿它对账会对出假差额。
            amount = purchase.line_amount if purchase is not None else line.line_amount
            if amount is not None:
                total_confirmed += amount
                currencies.add(purchase.currency if purchase is not None else
                               (_inherited(line, invoice, "currency") or DEFAULT_CURRENCY))
                _add_to_category(buckets, line, purchase, amount)

    if pending > 0:
        status = RECONCILIATION_NOT_CHECKED
        difference = None
    elif invoice.invoice_total is None:
        status = RECONCILIATION_NO_TOTAL
        difference = None
    elif len(currencies) > 1:
        status, difference = "mixed_currencies", None
    elif invoice.entry_source == ENTRY_SOURCE_IMPORT and not invoice.currency:
        status, difference = "currency_required", None
    elif currencies and currencies != {invoice.currency or DEFAULT_CURRENCY}:
        status, difference = "currency_mismatch", None
    else:
        difference = invoice.invoice_total - total_confirmed
        status = (
            RECONCILIATION_BALANCED
            if abs(difference) <= AMOUNT_TOLERANCE
            else RECONCILIATION_MISMATCH
        )

    by_category, adjustment_total, notes = _category_totals(
        buckets, mixed_currencies=len(currencies) > 1
    )
    return ReconciliationView(
        status=status,
        invoice_total=invoice.invoice_total,
        confirmed_amount=total_confirmed if len(currencies) <= 1 else None,
        difference=difference,
        tolerance=AMOUNT_TOLERANCE,
        line_count=len(invoice.lines),
        pending_count=pending,
        confirmed_count=confirmed,
        skipped_count=skipped,
        by_category=by_category,
        adjustment_total=adjustment_total,
        notes=notes,
    )


# 调整行那一桶的固定名字。它不是一条分类，所以不从数据库里取名。
ADJUSTMENT_BUCKET_NAME: Final[str] = "Adjustments (freight / credits)"
ADJUSTMENT_BUCKET_NAME_ZH: Final[str] = "运费 / 折让等调整"


def _add_to_category(
    buckets: dict[int | None, dict[str, Any]],
    line: InvoiceLine,
    purchase,
    amount: Decimal,
) -> None:
    """把一行已确认的金额记进它所属的桶（步骤 10.A）。

    ⚠️ **调整行一律进 `None` 那一桶**，判据是采购记录上的 `line_kind`
    （步骤 04 定下的那一列），不靠"金额是不是负的"去推断——靠符号推断意味着
    一笔 0 元或正数的费用调整会被算进某一类食材的成本。与步骤 09 的价格统计
    同一条口径：运费与整单折让不是某一类食材的成本。
    """
    kind = purchase.line_kind if purchase is not None else line.line_kind
    if kind == LINE_KIND_ADJUSTMENT:
        bucket = buckets.setdefault(
            None,
            {
                "name": ADJUSTMENT_BUCKET_NAME,
                "name_zh": ADJUSTMENT_BUCKET_NAME_ZH,
                "line_count": 0,
                "amount": Decimal("0.00"),
            },
        )
        bucket["line_count"] += 1
        bucket["amount"] += amount
        return

    food_item = (
        purchase.food_item
        if purchase is not None and purchase.food_item is not None
        else line.food_item
    )
    if food_item is None or food_item.category is None:
        # 采购行必须有食材（步骤 04 的规则，由服务层强制），所以这里理论上到不了。
        # 真到了也不编一个分类出来——宁可少一格，不要凭空多一类。
        return  # pragma: no cover
    category = food_item.category
    bucket = buckets.setdefault(
        category.id,
        {
            "name": category.name,
            "name_zh": category.name_zh,
            "line_count": 0,
            "amount": Decimal("0.00"),
        },
    )
    bucket["line_count"] += 1
    bucket["amount"] += amount


def _category_totals(
    buckets: dict[int | None, dict[str, Any]], *, mixed_currencies: bool
) -> tuple[tuple[CategoryTotal, ...], CategoryTotal | None, tuple[str, ...]]:
    """分桶结果 → 面板上的那几格 + 必须跟着读的话。"""
    notes: list[str] = []
    if mixed_currencies:
        # ⚠️ 币种不混算（步骤 09 的硬规则）。几个币种的钱加在一起得到的数字没有
        # 任何含义，而它看起来仍然像个总额。这种情况下整张面板不出，并说明原因。
        return (), None, (
            "这张单据里有多个币种，所以**不按分类汇总**——几个币种的金额相加"
            "得到的数字没有含义。本轮没有汇率换算（步骤 09 的口径）。",
        )

    adjustment_bucket = buckets.get(None)
    adjustment_total = (
        CategoryTotal(
            category_id=None,
            name=adjustment_bucket["name"],
            name_zh=adjustment_bucket["name_zh"],
            line_count=adjustment_bucket["line_count"],
            amount=adjustment_bucket["amount"],
        )
        if adjustment_bucket is not None
        else None
    )
    if adjustment_total is not None:
        notes.append(
            "运费 / 折让等调整**单列一桶，没有加进任何分类**：它们不是某一类食材的"
            "成本（与步骤 09 的价格统计同一条口径）。"
        )
    rows = [
        CategoryTotal(
            category_id=category_id,
            name=bucket["name"],
            name_zh=bucket["name_zh"],
            line_count=bucket["line_count"],
            amount=bucket["amount"],
        )
        for category_id, bucket in buckets.items()
        if category_id is not None
    ]
    # 金额从大到小；同额按分类名，保证两次请求的顺序一致。
    rows.sort(key=lambda row: (-row.amount, row.name))
    return tuple(rows), adjustment_total, tuple(notes)


def _extension(original_name: str) -> str:
    """取小写扩展名。没有扩展名就返回空串，由调用方按白名单拒绝。"""
    _, _, suffix = original_name.rpartition(".")
    return suffix.strip().lower() if suffix and suffix != original_name else ""


def _try_decimal(raw: str | None, *, scale: int) -> Decimal | None:
    """把识别出来的原文试着解析成精确数值；解析不了就返回 None。

    草稿阶段**不能因为识别结果难看就报错**（那会让整张单传不进来），所以这里是
    「试一下」；真正的拒绝发生在确认那一刻，用的是步骤 04 同一个 `parse_decimal`。
    允许负数：调整行的金额本来就可以是负的。
    """
    if raw is None:
        return None
    try:
        return parse_decimal(
            raw, field="draft", scale=scale, allow_zero=True, allow_negative=True
        )
    except ValidationError:
        return None


def _try_date(raw: str | None) -> date | None:
    """把识别出来的原文试着解析成日历日；解析不了就返回 None（不编造日期）。"""
    if raw is None:
        return None
    try:
        return parse_purchase_date(raw)
    except ValidationError:
        return None


def _field_payload(recognized: RecognizedField) -> dict[str, Any]:
    """一个字段的原始证据，照原样保留：状态、原文、置信度、页码与区域。

    厂商没给坐标时 `box` 就是 None——**不编造坐标**（步骤 02 的契约已经这么要求）。
    """
    box = recognized.box
    return {
        "status": str(recognized.status),
        "raw_text": recognized.raw_text,
        "confidence": recognized.confidence,
        "page": recognized.page,
        "box": (
            {
                "left": box.left,
                "top": box.top,
                "width": box.width,
                "height": box.height,
                "space": str(box.space),
            }
            if box is not None
            else None
        ),
    }


def _raw_fields_json(line: RecognizedLine) -> str:
    payload: dict[str, Any] = {
        "line_text": line.raw_text,
        "fields": {
            name: _field_payload(found)
            for name in LINE_FIELDS
            if (found := line.get(name)) is not None
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _raw_document_fields_json(result: RecognitionResult) -> str:
    payload: dict[str, Any] = {
        "provider": result.provider,
        "is_fake": result.is_fake,
        "model": result.model,
        "warnings": list(result.warnings),
        "fields": {
            name: _field_payload(found)
            for name in DOCUMENT_FIELDS
            if (found := result.document_field(name)) is not None
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _recognized_text(recognized: RecognizedField | None) -> str | None:
    return recognized.raw_text if recognized is not None else None


def _maybe_json(raw: str | None) -> dict[str, Any]:
    """读一列 JSON 文本。坏了就当空——**不让一条坏证据挡住整张单据**。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


# --- 服务 -------------------------------------------------------------------


class InvoiceService:
    """单据的上传、草稿维护、行匹配与逐行确认。无状态，可以按请求创建。"""

    def __init__(
        self,
        database: Database,
        *,
        file_storage: LocalFileStorage,
        recognizer: Recognizer,
        upload_max_bytes: int,
        extraction_scheduler: Any = None,
    ) -> None:
        self._database = database
        self._storage = file_storage
        self._recognizer = recognizer
        self._upload_max_bytes = upload_max_bytes
        # 两个被复用的服务：确认走步骤 04 的写入，当场新建食材走步骤 03 的快速新增。
        # 它们都只依赖同一个 Database，所以直接构造，不引入 DI 容器。
        self._purchases = PurchaseService(database)
        self._ingredients = IngredientService(database)
        # 步骤 08 的提取调度器（`services/extraction.ExtractionService`）。
        #
        # 为什么是注入的、而且可以为 None：提取服务要调用本服务写草稿
        # （`apply_extraction`），本服务要触发提取——双向依赖。用一个只有
        # `schedule(invoice_id)` 的小接口把方向打断：提取服务依赖本服务，
        # 本服务只拿到一个调度钩子。
        #
        # 为 None 时走步骤 05 原来的同步路径（`_recognize_into`）：那是
        # 「只有 Fake 识别器」时期的通路，上传即出草稿。步骤 05 的测试仍然测它。
        self._extraction = extraction_scheduler

    def attach_extraction(self, scheduler: Any) -> None:
        """装上提取调度器。

        两个服务互相需要（提取要写草稿、上传要触发提取），构造时解不开这个环，
        所以先各造一个、再把钩子装上。**只在装配处调用**（`factory.build_services`
        与 `worker.build_service`），业务代码不碰。
        """
        self._extraction = scheduler

    # --- 上传与文件 ---

    def create_invoice(
        self, uploads: Sequence[UploadedFile], *, recognize: bool = True
    ) -> InvoiceView:
        """保存文件并建出一张单据的草稿。

        `recognize=False` 时只存文件、不调识别器：老板可以对着照片手工加行
        （真实 OCR 没接通之前这是最实在的一条路）。
        """
        if not uploads:
            raise ValidationError(
                "上传发票至少需要一个文件。没有单据的零散采购请用 "
                "POST /api/purchases 单条录入。",
                details={"field": "files"},
            )
        self._check_file_count(0, len(uploads))

        with self._database.unit_of_work() as session:
            invoice = InvoiceRepository(session).add(Invoice(currency=DEFAULT_CURRENCY))
            stored = self._store_files(session, invoice, uploads, start_order=1)
            if recognize and self._extraction is None:
                self._recognize_into(session, invoice, stored)
            session.flush()
            _expire_collections(session, invoice)
            view = self._view(session, invoice, with_candidates=True)
            invoice_id = invoice.id

        if recognize and self._extraction is not None:
            # 提取走步骤 08 的调度器。它自己决定本地即时路径（数字 PDF 文字层、
            # Fake）当场跑完，还是入队给 worker（手写件约 15 秒，不挂在 HTTP
            # 请求里）。⚠️ **调度在上传事务之外**：提取失败不该把刚传上来的
            # 单据一起回滚掉——原始文件是证据，先存住再谈识别。
            self._extraction.schedule(invoice_id)
            return self.get_invoice(invoice_id)
        return view

    def create_import(
        self, upload: UploadedFile, *, header: dict, metadata: dict, rows: Sequence[dict]
    ) -> InvoiceView:
        """Persist one spreadsheet review batch atomically, without calling OCR.

        The original file hash stays unique even after a batch is voided. A retry
        returns the existing batch reference; changing options cannot overwrite it.
        """
        saved_keys: list[str] = []
        try:
            with self._database.unit_of_work() as session:
                invoice = InvoiceRepository(session).add(Invoice(
                    entry_source=ENTRY_SOURCE_IMPORT,
                    **{**header, "currency": header.get("currency") or DEFAULT_CURRENCY},
                ))
                stored = self._store_files(session, invoice, [upload], start_order=1,
                                           allowed_extensions=("csv", "xlsx"))
                record = stored[0][0]
                saved_keys.append(record.storage_key)
                invoice.import_metadata = json.dumps(
                    {**metadata, "source_file_id": record.id}, ensure_ascii=False
                )
                repository = InvoiceLineRepository(session)
                for number, item in enumerate(rows, start=1):
                    repository.add(InvoiceLine(
                        invoice_id=invoice.id, line_no=number, status=LINE_STATUS_PENDING,
                        raw_fields=json.dumps({**item["evidence"], "file_id": record.id,
                                               "filename": upload.original_name}, ensure_ascii=False),
                        review_metadata=json.dumps(item["review"], ensure_ascii=False),
                        **item["values"],
                    ))
                session.flush()
                _expire_collections(session, invoice)
                return self._view(session, invoice, with_candidates=True)
        except IntegrityError as exc:
            self._cleanup_import_files(saved_keys)
            with self._database.unit_of_work() as session:
                existing = InvoiceFileRepository(session).find_by_sha256(hashlib.sha256(upload.content).hexdigest())
                if existing:
                    raise ConflictError("此文件已导入，请在原批次继续审核。", details={
                        "invoice_id": existing.invoice_id, "invoice_file_id": existing.id}) from exc
            raise
        except Exception:
            self._cleanup_import_files(saved_keys)
            raise

    def _cleanup_import_files(self, keys: Sequence[str]) -> None:
        # A commit failure can leave an unknown outcome. Delete only if a fresh
        # database read proves that no committed row points at these bytes.
        try:
            with self._database.unit_of_work() as session:
                repository = InvoiceFileRepository(session)
                for key in keys:
                    if not repository.has_storage_key(key):
                        self._storage.delete(key)
        except Exception:
            logger.exception("import cleanup deferred; preserving original files")

    def add_files(self, invoice_id: int, uploads: Sequence[UploadedFile]) -> InvoiceView:
        """给已有单据补传文件（漏拍了一页、或者重拍一张更清楚的）。"""
        if not uploads:
            raise ValidationError("没有要上传的文件。", details={"field": "files"})
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            self._require_not_voided(invoice)
            self._check_file_count(len(invoice.files), len(uploads))
            next_order = max((item.page_order for item in invoice.files), default=0) + 1
            self._store_files(session, invoice, uploads, start_order=next_order)
            session.flush()
            _expire_collections(session, invoice)
            return self._view(session, invoice, with_candidates=True)

    def delete_file(self, invoice_id: int, file_id: int) -> InvoiceView:
        """删掉一个文件（拍坏了、拍歪了）。

        **不能删到一个不剩**：单据必须至少有一个原始文件，否则它就只是一堆没有凭据
        的数字。磁盘上那份字节也一并删除——它是刚上传的废片，留着没有意义；已经确认
        入账的行引用的是单据，不是某个具体文件。

        **指着这个文件的提取任务先处理掉**（见 `_release_extraction_jobs`）：
        `extraction_jobs.invoice_file_id` 上有外键，不先处理就是一条
        `FOREIGN KEY constraint failed` 的 500。

        **已经识别出来的草稿行不跟着删**：人可能已经改过那些行，而「删掉一张拍坏的
        照片」不该顺手抹掉人的劳动。它们的证据里仍记着这个文件编号，界面上据此显示
        「原件已删除、看不了原图」；下一次重新提取会替换掉其中没人碰过的自动行
        （见 `_drop_previous_extraction`）。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            self._require_not_voided(invoice)
            target = self._load_file(session, invoice, file_id)
            if invoice.import_metadata and json.loads(invoice.import_metadata).get("source_file_id") == file_id:
                raise ConflictError("导入表格是这些明细的原始凭据，不能删除；可以作废整个批次。",
                                    details={"invoice_id": invoice_id, "file_id": file_id})
            if len(invoice.files) <= 1:
                raise ConflictError(
                    "这是单据上最后一个文件，不能删除——发票必须保留至少一份原始"
                    "文件。要换一张照片，请先上传新的再删旧的。",
                    details={"invoice_id": invoice_id, "file_id": file_id},
                )
            self._release_extraction_jobs(session, target)
            storage_key = target.storage_key
            try:
                InvoiceFileRepository(session).delete(target)
                session.flush()
            except IntegrityError as exc:
                # 还有东西指着这个文件行。上面那一步已经把提取任务处理过了，所以能
                # 走到这里只有一种情况：刚好有另一次提取在这中间入队。报 409 让人
                # 重试，而不是把数据库的约束名抛成 500。
                raise ConflictError(
                    "这个文件刚刚被另一次操作占用了（很可能是同时发起的重新提取），"
                    "没有删除。请刷新页面再试一次。",
                    details={"invoice_id": invoice_id, "file_id": file_id},
                ) from exc
            # 字节也删掉：它是刚上传的废片。已确认的行引用的是单据，不是某个文件。
            self._storage.delete(storage_key)
            _expire_collections(session, invoice)
            return self._view(session, invoice, with_candidates=True)

    def _release_extraction_jobs(self, session: Session, target: InvoiceFile) -> None:
        """删文件之前，先处理指着它的提取任务（步骤 08.5 的 `extraction_jobs`）。

        两种情况分开对待：

        - **还在排队、或正在跑**：不许删。worker 已经（或即将）按这个文件的
          `storage_key` 去读字节，删掉它只会换来一条看不懂的失败，而那次模型调用
          的钱照样要花。等它跑完或失败再删——提取状态面板上看得见进度，卡住的任务
          另有出口（`recover_stale`）。
        - **已经跑完（成功或失败）**：任务记录留着，只把 `invoice_file_id` 置空，
          并把文件的编号与名字写进 `warnings`。历史任务不删——它是事后唯一能解释
          「这张单当初怎么没出行」的东西。

        ⚠️ **刻意不给那个外键加 `ON DELETE SET NULL`。** 「能不能删」是业务判断：
        正在跑的任务必须拦住，而数据库那一层分不清排队中和已跑完，一律置空等于把
        一次正在花钱的识别悄悄变成一条指向空气的记录。外键保持严格，它正是这个
        缺陷能被发现的原因。
        """
        repository = ExtractionJobRepository(session)
        jobs = repository.for_file(target.id)
        active = next(
            (
                job
                for job in jobs
                if job.status in (EXTRACTION_STATUS_QUEUED, EXTRACTION_STATUS_RUNNING)
            ),
            None,
        )
        if active is not None:
            raise ConflictError(
                f"这个文件{_EXTRACTION_IN_PROGRESS.get(active.status, '还在识别流程里')}"
                f"（提取任务 #{active.id}，状态 {active.status}）。现在删掉它，识别就会"
                "对着一个不存在的文件跑，而那次模型调用的钱照样要花。等这一次提取"
                "跑完或失败之后再删。",
                details={
                    "invoice_id": target.invoice_id,
                    "file_id": target.id,
                    "extraction_job_id": active.id,
                    "status": active.status,
                },
            )
        note_time = utcnow().isoformat(timespec="seconds")
        for job in jobs:
            repository.detach_file(
                job,
                note=(
                    f"这次提取针对的原件（#{target.id} {target.original_name}）"
                    f"已于 {note_time} 被删除；任务记录保留，原件本身看不了了。"
                ),
            )

    def open_file(self, invoice_id: int, file_id: int) -> tuple[InvoiceFileView, BinaryIO]:
        """打开一个原始文件供回看。调用方负责关闭返回的流。"""
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            target = self._load_file(session, invoice, file_id)
            view = InvoiceFileView.of(target)
            return view, self._storage.open(target.storage_key)

    # --- 查询 ---

    def get_invoice(self, invoice_id: int) -> InvoiceView:
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            return self._view(session, invoice, with_candidates=True)

    def search_invoices(
        self,
        *,
        completed: bool | None = None,
        source: str | None = None,
        include_voided: bool = False,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> InvoicePage:
        """按「做完了没有」与采购来源查单据，上传时间倒序。

        `completed=False` 就是老板的待办清单。列表不算行匹配候选（见
        `InvoiceLineView` 的说明）。
        """
        clean_limit = require_limit(limit, max_limit=MAX_LIMIT)
        clean_offset = require_offset(offset)
        clean_source = optional_text(
            source, field="source", max_length=SOURCE_NAME_MAX_LENGTH
        )
        with self._database.unit_of_work() as session:
            rows, total = InvoiceRepository(session).search(
                completed=completed,
                source_normalized=(
                    normalize_name(clean_source) if clean_source else None
                ),
                include_voided=include_voided,
                limit=clean_limit,
                offset=clean_offset,
            )
            return InvoicePage(
                items=tuple(
                    self._view(session, invoice, with_candidates=False)
                    for invoice in rows
                ),
                total=total,
                limit=clean_limit,
                offset=clean_offset,
            )

    def match_candidates_for(
        self, description: str, *, source_name: str | None = None
    ) -> tuple[MatchCandidateView, ...]:
        """「这句原始商品描述以前确认到哪个食材」——独立调用版本。

        前端在老板手输一句描述时可以直接问这个接口，不必先把它存成一行。
        """
        with self._database.unit_of_work() as session:
            return self._match_candidates(
                session, description=description, source_name=source_name
            )

    # --- 单据头与行的维护 ---

    def update_invoice(
        self,
        invoice_id: int,
        *,
        source_name: Any = UNSET,
        invoice_number: Any = UNSET,
        invoice_date: Any = UNSET,
        currency: Any = UNSET,
        invoice_total: Any = UNSET,
    ) -> InvoiceView:
        """修改单据头。未出现的字段保持原值，显式传 null 表示清空。

        改单据头**不动已经确认的采购记录**（继承只发生在确认那一刻），也不改行状态；
        它会影响对账结果，而对账本来就只是提示。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            self._require_not_voided(invoice)

            if source_name is not UNSET:
                clean = optional_text(
                    source_name, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH
                )
                invoice.source_name = clean
                invoice.source_name_normalized = normalize_name(clean) if clean else None
            if invoice_number is not UNSET:
                clean = optional_text(
                    invoice_number,
                    field="invoice_number",
                    max_length=INVOICE_NUMBER_MAX_LENGTH,
                )
                invoice.invoice_number = clean
                invoice.invoice_number_normalized = (
                    normalize_name(clean) if clean else None
                )
            if invoice_date is not UNSET:
                invoice.invoice_date = parse_purchase_date(invoice_date)
            if currency is not UNSET:
                invoice.currency = (
                    None
                    if currency is None
                    else require_choice(
                        currency, field="currency", allowed=CURRENCIES, case="upper"
                    )
                )
            if invoice_total is not UNSET:
                invoice.invoice_total = (
                    None
                    if invoice_total is None
                    else parse_decimal(
                        invoice_total,
                        field="invoice_total",
                        scale=AMOUNT_SCALE,
                        allow_zero=True,
                        allow_negative=False,
                    )
                )
            session.flush()
            return self._view(session, invoice, with_candidates=True)

    def add_line(
        self,
        invoice_id: int,
        *,
        description: Any = None,
        quantity: Any = None,
        purchase_unit: Any = None,
        unit_price: Any = None,
        line_amount: Any = None,
        food_item_id: Any = None,
        line_kind: Any = LINE_KIND_PURCHASE,
        purchase_date: Any = None,
        source_name: Any = None,
        currency: Any = None,
    ) -> InvoiceLineView:
        """手工加一行（识别漏了一行，或者压根没识别，老板对着照片敲）。

        所有字段都可以先不填——草稿本来就是边看边填的。手工加的行 `raw_fields` 为
        NULL，这同时标记了「它不是识别出来的」，于是它可以被删除，而识别出来的行
        只能标成 `skipped`（删掉就对不上原件了）。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            self._require_not_voided(invoice)
            kind = require_choice(line_kind, field="line_kind", allowed=LINE_KINDS)
            values = self._clean_line_values(
                session,
                kind=kind,
                description=description,
                quantity=quantity,
                purchase_unit=purchase_unit,
                unit_price=unit_price,
                line_amount=line_amount,
                food_item_id=food_item_id,
                purchase_date=purchase_date,
                source_name=source_name,
                currency=currency,
            )
            line = InvoiceLine(
                invoice_id=invoice.id,
                line_no=InvoiceRepository(session).next_line_no(invoice.id),
                status=LINE_STATUS_PENDING,
                line_kind=kind,
                **values,
            )
            InvoiceLineRepository(session).add(line)
            invoice.completed_at = None
            session.flush()
            _expire_collections(session, invoice)
            return InvoiceLineView.of(
                line,
                candidates=self._match_candidates(
                    session,
                    description=line.description,
                    source_name=line.source_name or invoice.source_name,
                ),
            )

    def update_line(
        self,
        invoice_id: int,
        line_id: int,
        *,
        version: int | None = None,
        status: Any = UNSET,
        skip_reason: Any = UNSET,
        line_kind: Any = UNSET,
        description: Any = UNSET,
        quantity: Any = UNSET,
        purchase_unit: Any = UNSET,
        unit_price: Any = UNSET,
        line_amount: Any = UNSET,
        food_item_id: Any = UNSET,
        purchase_date: Any = UNSET,
        source_name: Any = UNSET,
        currency: Any = UNSET,
    ) -> InvoiceLineView:
        """修改一行：改数值、改食材、改类别，或者把它标成跳过 / 改回待确认。

        **已确认的行不能在这里改**：那条数据已经进了采购历史，改它要么改采购记录
        （`PATCH /api/purchases/<id>`，有修改日志），要么先撤销确认。原地改一个已经
        入账的行只会让两处数字对不上。

        `status` 只在 `pending` 与 `skipped` 之间切换；确认必须走确认接口，否则
        「确认」这件事就有两个入口、两套校验。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            self._require_not_voided(invoice)
            line = self._load_line(session, invoice, line_id)
            self._check_version(line, version)
            if line.status == LINE_STATUS_CONFIRMED:
                active = _active_purchase(line)
                raise ConflictError(
                    f"第 {line.line_no} 行已经确认入账，不能直接改。"
                    "改数字请改采购记录（会留下修改日志），换食材请先撤销确认。",
                    details={
                        "line_id": line.id,
                        "purchase_id": active.id if active is not None else None,
                    },
                )

            kind = (
                require_choice(line_kind, field="line_kind", allowed=LINE_KINDS)
                if line_kind is not UNSET
                else line.line_kind
            )
            given = {
                "description": description,
                "quantity": quantity,
                "purchase_unit": purchase_unit,
                "unit_price": unit_price,
                "line_amount": line_amount,
                "food_item_id": food_item_id,
                "purchase_date": purchase_date,
                "source_name": source_name,
                "currency": currency,
            }
            changes = {
                name: value for name, value in given.items() if value is not UNSET
            }
            values = self._clean_line_values(session, kind=kind, **changes)
            for name, value in values.items():
                setattr(line, name, value)
            line.line_kind = kind

            if line.review_metadata:
                review = _review_metadata(line)
                reviewed = set(changes)
                if line_kind is not UNSET:
                    reviewed.add("line_kind")
                review["issues"] = [issue for issue in review.get("issues", [])
                                    if issue["field"] not in reviewed]
                review["overrides"] = sorted(set(review.get("overrides", [])) |
                                             (reviewed & {"purchase_date", "source_name", "currency"}))
                line.review_metadata = json.dumps(review, ensure_ascii=False)

            if status is not UNSET:
                new_status = require_choice(
                    status, field="status", allowed=LINE_STATUSES_EDITABLE
                )
                line.status = new_status
                if new_status == LINE_STATUS_PENDING:
                    # 改回待确认就把跳过原因清掉：留着会让界面显示一行自相矛盾的话。
                    line.skip_reason = None
                invoice.completed_at = None
            if skip_reason is not UNSET:
                line.skip_reason = optional_text(
                    skip_reason, field="skip_reason", max_length=SKIP_REASON_MAX_LENGTH
                )

            line.version += 1
            session.flush()
            return InvoiceLineView.of(
                line,
                candidates=self._match_candidates(
                    session,
                    description=line.description,
                    source_name=line.source_name or invoice.source_name,
                ),
            )

    def delete_line(self, invoice_id: int, line_id: int) -> None:
        """删掉一行。

        只允许删**手工加的**行（`raw_fields` 为 NULL）：识别出来的行是单据上真实存在
        的内容，删掉就和原件对不上了——那种情况请标成 `skipped` 并写一句原因。
        已确认的行也不能删，要先撤销确认。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            self._require_not_voided(invoice)
            line = self._load_line(session, invoice, line_id)
            if line.status == LINE_STATUS_CONFIRMED:
                raise ConflictError(
                    f"第 {line.line_no} 行已经确认入账，不能删除；请先撤销确认。",
                    details={"line_id": line.id},
                )
            if line.raw_fields is not None:
                raise ConflictError(
                    f"第 {line.line_no} 行来自单据识别结果，不能删除——"
                    "删了就和原件对不上了。不该入账的行请标成 skipped 并写一句原因。",
                    details={"line_id": line.id},
                )
            InvoiceLineRepository(session).delete(line)
            session.flush()

    # --- 逐行确认 ---

    def confirm_lines(
        self, invoice_id: int, *, options: Sequence[ConfirmOption] | None = None
    ) -> ConfirmOutcome:
        """确认选定的行；`options` 为 None 表示「把所有待确认的行都确认掉」。

        **每行一个事务，逐行独立**：成功的行入账，失败的行原样留着并带回自己的错误。
        这正是验收里「三行只确认两行时只有两条采购记录」那一条，也是
        `imp_notes.md` 4.7 的要求——不能因为一两行模糊就卡住其它正确的行。
        """
        if options is None:
            with self._database.unit_of_work() as session:
                invoice = self._load_invoice(session, invoice_id)
                self._require_not_voided(invoice)
                options = tuple(
                    ConfirmOption(line_id=line.id)
                    for line in invoice.lines
                    if line.status == LINE_STATUS_PENDING
                )
        if not options:
            raise ValidationError(
                "这张单据上没有待确认的行。",
                details={"invoice_id": invoice_id},
            )

        results = tuple(self._confirm_one(invoice_id, option) for option in options)
        return ConfirmOutcome(results=results, invoice=self.get_invoice(invoice_id))

    def unconfirm_line(
        self, invoice_id: int, line_id: int, *, version: int | None = None, reason: Any = None
    ) -> InvoiceLineView:
        """撤销一行的确认：作废它的采购记录，行退回待确认。

        作废而不是删除——采购记录要能解释「这个数字曾经存在过、什么时候被撤销的」
        （步骤 04 的软删 + 修改日志）。行上的**原始识别值不动**，那是单据事实。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            line = self._load_line(session, invoice, line_id)
            self._check_version(line, version)
            if line.status != LINE_STATUS_CONFIRMED:
                raise ConflictError(
                    f"第 {line.line_no} 行当前不是已确认状态（{line.status}），"
                    "没有可撤销的确认。",
                    details={"line_id": line.id, "status": line.status},
                )
            active = _active_purchase(line)
            if active is not None:
                clean_reason = optional_text(
                    reason, field="reason", max_length=SKIP_REASON_MAX_LENGTH
                )
                self._purchases.void_purchase(
                    active.id,
                    reason=clean_reason
                    or f"撤销发票 #{invoice.id} 第 {line.line_no} 行的确认",
                    session=session,
                )
            line.status = LINE_STATUS_PENDING
            line.confirmed_at = None
            line.version += 1
            invoice.completed_at = None
            session.flush()
            session.refresh(line)
            return InvoiceLineView.of(
                line,
                candidates=self._match_candidates(
                    session,
                    description=line.description,
                    source_name=line.source_name or invoice.source_name,
                ),
            )

    # --- 收口与作废 ---

    def complete_invoice(self, invoice_id: int) -> InvoiceView:
        """收口确认：老板审完所有行，确认这张单据处理完了。

        这一下是**漏行唯一会被拦住的时刻**，所以它刻意是个动作而不是自动推断：接口
        把「已确认 N 行、跳过 M 行、几个文件、对账差多少」摆出来，老板对一眼原件再点。
        还有待确认的行时不许完成——那不是"做完了"。

        对账差额超过容差**不阻止完成**，只在响应里带着提示：单据总额本身可能识别错，
        拿它卡住录入是本末倒置。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            self._require_not_voided(invoice)
            if not invoice.lines:
                raise ConflictError(
                    "这张单据上还没有任何行，没法标成完成。"
                    "先识别或手工加行，再逐行确认。",
                    details={"invoice_id": invoice_id},
                )
            pending = [
                line.line_no
                for line in invoice.lines
                if line.status == LINE_STATUS_PENDING
            ]
            if pending:
                raise ConflictError(
                    f"还有 {len(pending)} 行没处理（第 "
                    f"{', '.join(str(number) for number in pending)} 行）。"
                    "每一行都要么确认、要么标成跳过，才能完成这张单据。",
                    details={"invoice_id": invoice_id, "pending_line_nos": pending},
                )
            invoice.completed_at = utcnow()
            session.flush()
            return self._view(session, invoice, with_candidates=False)

    def void_invoice(
        self, invoice_id: int, *, confirmed_line_ids: Sequence[int] | None = None
    ) -> InvoiceView:
        """作废整张单据（传错了、重复了）。

        已确认行需要调用方明确提交当前行 ID；在同一事务内作废对应采购记录，
        保留修改日志，并让这些行退回待确认，避免价格历史继续计入废单。

        **原件跟着一起删。** 这条规则改过一次，原因是实跑踩到的死胡同：作废掉一张
        单据之后再传同一张照片，会撞上 `invoice_files.sha256` 的唯一索引，接口回
        409 并指回那张**界面上已经看不见**的单据——图传不进来，单据也打不开。

        原件删除后可以重新上传相同文件；已作废采购记录仍可追溯，但原图不再可查看。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            if invoice.voided_at is not None:
                raise ConflictError(
                    f"单据 {invoice_id} 已经作废过了。",
                    details={"voided_at": invoice.voided_at.isoformat()},
                )
            confirmed = [line for line in invoice.lines if line.status == LINE_STATUS_CONFIRMED]
            actual_ids = {line.id for line in confirmed}
            if confirmed and confirmed_line_ids is None:
                raise ConflictError(
                    f"这张单据有 {len(confirmed)} 行已经确认入账（第 "
                    f"{', '.join(str(line.line_no) for line in confirmed)} 行）；"
                    "请核对最新状态后再确认删除。",
                    details={
                        "invoice_id": invoice_id,
                        "confirmed_line_nos": [line.line_no for line in confirmed],
                        "confirmed_line_ids": sorted(actual_ids),
                    },
                )
            if confirmed_line_ids is not None and set(confirmed_line_ids) != actual_ids:
                raise ConflictError(
                    "单据的入账行已经变化，请刷新后重新确认删除。",
                    details={"invoice_id": invoice_id, "confirmed_line_ids": sorted(actual_ids)},
                )
            # 先处理指着这些文件的提取任务：还在排队或正在跑的会被拦下来（和删单个
            # 文件同一条规矩——worker 正要去读那份字节）。
            repository = InvoiceFileRepository(session)
            storage_keys = []
            for target in list(invoice.files):
                self._release_extraction_jobs(session, target)
                storage_keys.append(target.storage_key)
                repository.delete(target)
            for line in confirmed:
                active = _active_purchase(line)
                if active is not None:
                    self._purchases.void_purchase(
                        active.id,
                        reason=f"删除单据 #{invoice.id}，撤销第 {line.line_no} 行入账",
                        session=session,
                    )
                line.status = LINE_STATUS_PENDING
                line.confirmed_at = None
                line.version += 1
            invoice.completed_at = None
            invoice.voided_at = utcnow()
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(
                    "这张单据的原件刚刚被另一次操作占用了（很可能是同时发起的重新"
                    "提取），没有删除。请刷新页面再试一次。",
                    details={"invoice_id": invoice_id},
                ) from exc
            _expire_collections(session, invoice)
            view = self._view(session, invoice, with_candidates=False)
        # 字节在事务提交之后再删：先删字节后回滚会留下指向空文件的记录。
        for key in storage_keys:
            self._storage.delete(key)
        return view

    # --- 内部实现 ---

    def _confirm_one(self, invoice_id: int, option: ConfirmOption) -> ConfirmResult:
        """确认一行。自己一个事务，自己捕获自己的错误。"""
        try:
            with self._database.unit_of_work() as session:
                invoice = self._load_invoice(session, invoice_id)
                self._require_not_voided(invoice)
                line = self._load_line(session, invoice, option.line_id)
                self._check_version(line, option.version)

                if line.status == LINE_STATUS_CONFIRMED:
                    active = _active_purchase(line)
                    raise ConflictError(
                        f"第 {line.line_no} 行已经确认过了，不会重复记价。",
                        details={
                            "line_id": line.id,
                            "purchase_id": active.id if active is not None else None,
                        },
                    )
                if line.status == LINE_STATUS_SKIPPED:
                    raise ValidationError(
                        f"第 {line.line_no} 行标成了跳过。要入账请先把它改回待确认。",
                        details={"line_id": line.id, "status": line.status},
                    )

                issues = _review_metadata(line).get("issues", [])
                if issues:
                    raise ValidationError("这一行还有表格字段需要核对，请在程序里修改或确认保存相应字段。",
                                          details={"line_id": line.id, "issues": issues})
                food_item_id = self._resolve_food_item(session, line, option)
                missing = _missing_fields(line, food_item_id=food_item_id)
                if missing:
                    raise ValidationError(
                        f"第 {line.line_no} 行还缺 {', '.join(missing)}，不能确认。"
                        "认不出来的数字不会自动补 0，请先填上。",
                        details={"line_id": line.id, "missing_fields": list(missing)},
                    )

                # 重复确认的第一道防线：给出能看懂的 409。第二道是数据库上的
                # 局部唯一索引（挡并发），撞上了会是 IntegrityError。
                existing = PurchaseRepository(session).find_active_by_invoice_line(
                    line.id
                )
                if existing is not None:
                    raise ConflictError(
                        f"第 {line.line_no} 行已经有一条采购记录（#{existing.id}），"
                        "不会重复记价。",
                        details={"line_id": line.id, "purchase_id": existing.id},
                    )

                purchase = self._purchases.create_purchase(
                    session=session,
                    line_kind=line.line_kind,
                    food_item_id=food_item_id,
                    quantity=line.quantity,
                    purchase_unit=line.purchase_unit,
                    unit_price=line.unit_price,
                    line_amount=line.line_amount,
                    # 继承就发生在这一刻：行上有覆盖值用它，否则用单据头。
                    purchase_date=_inherited(line, invoice, "purchase_date"),
                    source_name=_inherited(line, invoice, "source_name"),
                    original_description=line.description,
                    currency=_inherited(line, invoice, "currency") or DEFAULT_CURRENCY,
                    entry_source=invoice.entry_source,
                    invoice_line_id=line.id,
                    accept_amount_mismatch=option.accept_amount_mismatch,
                )

                line.food_item_id = food_item_id
                line.status = LINE_STATUS_CONFIRMED
                line.confirmed_at = utcnow()
                line.version += 1
                # 步骤 10.13：老板**用这条食材确认过一行**，「待确认」就结束了。
                # 他是在看着裁剪图、数量与单价的情况下按下确认的——这正是这个标记
                # 要等的那一下。清不掉的话，一条已经被他认可的食材会一直在食材页
                # 上挂着「待确认」，那个标记很快就会被当成噪声忽略掉，于是真正
                # 需要看一眼的那些也跟着被忽略。
                self._clear_needs_review(session, food_item_id)
                # 行状态变了，收口确认要重做：否则「完成」会停留在一个过期的判断上。
                invoice.completed_at = None
                session.flush()
                logger.info(
                    "invoice line confirmed invoice=%s line=%s purchase=%s",
                    invoice_id,
                    line.id,
                    purchase.id,
                )
                return ConfirmResult(
                    line_id=option.line_id, ok=True, purchase_id=purchase.id
                )
        except AppError as exc:
            return ConfirmResult(
                line_id=option.line_id,
                ok=False,
                error_code=exc.code,
                error_message=exc.message,
                error_details=exc.details or None,
            )
        except IntegrityError:
            # 并发确认同一行：局部唯一索引挡住了第二个。事务已经回滚，没有写进去
            # 半条记录，所以这里可以明确说"已经确认过了"。
            logger.info(
                "concurrent confirm blocked by unique index invoice=%s line=%s",
                invoice_id,
                option.line_id,
            )
            return ConfirmResult(
                line_id=option.line_id,
                ok=False,
                error_code="conflict",
                error_message="这一行刚刚已经被确认过了（并发提交），不会重复记价。",
                error_details={"line_id": option.line_id},
            )

    def _resolve_food_item(
        self, session: Session, line: InvoiceLine, option: ConfirmOption
    ) -> int | None:
        """确定这一行入账用哪个食材：当场新建 > 请求指定 > 行上已有。"""
        if option.new_food_item is not None and option.food_item_id is not UNSET:
            raise ValidationError(
                "food_item_id 与 new_food_item 只能给一个：无法判断该用哪个食材。",
                details={"field": "new_food_item"},
            )
        if option.new_food_item is not None:
            payload = dict(option.new_food_item)
            unknown = sorted(
                set(payload)
                - {"name", "name_zh", "specification", "category_id", "base_unit", "use_purchase_unit_as_base"}
            )
            if unknown:
                raise ValidationError(
                    f"new_food_item 里有不认识的字段：{', '.join(unknown)}。",
                    details={"unknown_fields": unknown},
                )
            use_line_unit = payload.pop("use_purchase_unit_as_base", False)
            if type(use_line_unit) is not bool:
                raise ValidationError("use_purchase_unit_as_base 必须是布尔值。")
            if use_line_unit:
                name = payload.get("name")
                if isinstance(name, str) and name.strip():
                    normalized = normalize_name(name)
                    repository = FoodItemRepository(session)
                    existing = (repository.find_by_normalized_name(normalized)
                                or repository.find_by_normalized_name_zh(normalized))
                    if existing is not None:
                        return existing.id
                if "base_unit" in payload:
                    raise ValidationError("自动使用采购单位时无需再填写基础单位。")
                unit = normalize_purchase_unit(line.purchase_unit or "")
                if unit is None:
                    raise ValidationError(
                        "这行的采购单位无法识别；请先在本行填写实际单位后再确认入账。",
                        details={"field": "purchase_unit"},
                    )
                payload["base_unit"] = unit
            missing = sorted({"name", "category_id", "base_unit"} - set(payload))
            if missing:
                raise ValidationError(
                    f"当场新建食材至少要给 {', '.join(missing)}。",
                    details={"missing_fields": missing},
                )
            # 复用步骤 03 的快速新增，并加入**同一个事务**：确认失败就不会留下
            # 一条谁也没要的食材。
            created = self._ingredients.create_food_item(
                session=session, allow_purchase_unit_base=use_line_unit, **payload
            )
            return created.id
        if option.food_item_id is not UNSET:
            return option.food_item_id
        return line.food_item_id

    @staticmethod
    def _clear_needs_review(session: Session, food_item_id: int | None) -> None:
        """这条食材已经被人用着确认过一行了，清掉「待确认」（步骤 10.13）。

        在同一个事务里改：确认那一行失败回滚时，这个标记也要跟着回去——否则会
        出现「行没入账，食材却算确认过了」。
        """
        if food_item_id is None:
            return
        food_item = FoodItemRepository(session).get(food_item_id)
        if food_item is not None and food_item.needs_review:
            food_item.needs_review = False

    def _store_files(
        self,
        session: Session,
        invoice: Invoice,
        uploads: Sequence[UploadedFile],
        *,
        start_order: int,
        allowed_extensions: tuple[str, ...] = ALLOWED_UPLOAD_EXTENSIONS,
    ) -> list[tuple[InvoiceFile, UploadedFile]]:
        """校验并保存一批文件，返回（文件记录, 原始上传）配对。

        判重在**存盘之前**：摘要一样就直接拒绝，不留下一份没人用的字节。同一次请求
        里传了两份相同的文件也算重复——否则一次误操作就能把同一张照片挂两遍。
        """
        repository = InvoiceFileRepository(session)
        seen: dict[str, str] = {}
        stored: list[tuple[InvoiceFile, UploadedFile]] = []
        for offset, upload in enumerate(uploads):
            extension = self._check_upload(upload, allowed_extensions=allowed_extensions)
            digest = hashlib.sha256(upload.content).hexdigest()
            if digest in seen:
                raise ValidationError(
                    f"这一批里有两个完全相同的文件（{seen[digest]} 与 "
                    f"{upload.original_name}），只保留一个即可。",
                    details={"sha256": digest},
                )
            seen[digest] = upload.original_name
            duplicate = repository.find_by_sha256(digest)
            if duplicate is not None:
                raise ConflictError(
                    f"这个文件已经上传过了，在单据 #{duplicate.invoice_id} 上"
                    f"（{duplicate.original_name}）。请去那张单据上接着审，"
                    "不用重新建一张。",
                    details={
                        "sha256": digest,
                        "invoice_id": duplicate.invoice_id,
                        "invoice_file_id": duplicate.id,
                    },
                )
            saved = self._storage.save(
                upload.content,
                original_name=upload.original_name,
                content_type=upload.content_type,
            )
            record = repository.add(
                InvoiceFile(
                    invoice_id=invoice.id,
                    storage_key=saved.key,
                    original_name=saved.original_name,
                    size=saved.size,
                    sha256=saved.sha256,
                    content_type=saved.content_type,
                    page_order=start_order + offset,
                )
            )
            stored.append((record, upload))
            logger.info(
                "invoice file stored invoice=%s file=%s ext=%s size=%s",
                invoice.id,
                record.id,
                extension,
                record.size,
            )
        return stored

    def _check_upload(self, upload: UploadedFile, *, allowed_extensions=ALLOWED_UPLOAD_EXTENSIONS) -> str:
        """类型与大小白名单。返回小写扩展名。"""
        if not upload.original_name or not upload.original_name.strip():
            raise ValidationError("上传文件缺少文件名。", details={"field": "files"})
        extension = _extension(upload.original_name)
        if extension not in allowed_extensions:
            raise ValidationError(
                f"不支持的文件类型 {extension or upload.original_name!r}。"
                f"可以上传：{', '.join(allowed_extensions)}。",
                details={
                    "field": "files",
                    "allowed_extensions": list(allowed_extensions),
                },
            )
        if not upload.content:
            raise ValidationError(
                f"文件 {upload.original_name} 是空的。", details={"field": "files"}
            )
        if len(upload.content) > self._upload_max_bytes:
            raise ValidationError(
                f"文件 {upload.original_name} 有 {len(upload.content)} 字节，"
                f"超过了单个文件 {self._upload_max_bytes} 字节的上限。",
                details={
                    "field": "files",
                    "max_bytes": self._upload_max_bytes,
                    "size": len(upload.content),
                },
            )
        return extension

    def _check_file_count(self, existing: int, incoming: int) -> None:
        if existing + incoming > MAX_FILES_PER_INVOICE:
            raise ValidationError(
                f"一张单据最多挂 {MAX_FILES_PER_INVOICE} 个文件"
                f"（已有 {existing} 个，这次要加 {incoming} 个）。",
                details={"field": "files", "max_files": MAX_FILES_PER_INVOICE},
            )

    def _recognize_into(
        self,
        session: Session,
        invoice: Invoice,
        stored: Sequence[tuple[InvoiceFile, UploadedFile]],
    ) -> None:
        """对第一个可识别的文件调一次识别器，把结果变成草稿。

        只识别一个文件、不做多页归并：归并、分流与预处理都要真实厂商的行为做依据，
        属于步骤 08。HEIC 跳过（只存不识别）。识别不出任何行也不报错——老板可以手工
        加行，这比"传不进来"有用得多。
        """
        target: tuple[InvoiceFile, UploadedFile] | None = None
        for record, upload in stored:
            if _extension(upload.original_name) in RECOGNIZABLE_EXTENSIONS:
                target = (record, upload)
                break
        if target is None:
            logger.info("no recognizable file on invoice=%s; draft stays empty", invoice.id)
            return

        record, upload = target
        result = self._recognizer.recognize(
            RecognitionRequest(
                content=upload.content,
                content_type=upload.content_type,
                original_name=upload.original_name,
                source_key=record.storage_key,
            )
        )
        self._apply_document_fields(invoice, result)
        repository = InvoiceLineRepository(session)
        for offset, recognized in enumerate(result.lines, start=1):
            repository.add(self._draft_line(invoice, recognized, line_no=offset))
        logger.info(
            "invoice draft built invoice=%s provider=%s is_fake=%s lines=%s",
            invoice.id,
            result.provider,
            result.is_fake,
            len(result.lines),
        )

    # --- 步骤 08 的提取写入口 ---

    def apply_extraction(
        self, session: Session, invoice_id: int, plan: Any, *, invoice_file_id: int | None
    ) -> int:
        """把一次提取的产出写进草稿，返回新建了几行。

        由 `services/extraction.ExtractionService` 在**它自己的事务里**调用，
        所以这里接收 Session 而不自己开事务——与步骤 04 的
        `create_purchase(session=...)` 同一个套路：要么和任务状态一起提交，
        要么一起回滚，不会留下半张草稿配一条"成功"的任务记录。

        三条口径：

        1. **不覆盖人填过的东西。** 单据头只填**当前为空**的字段；老板手打的
           供应商名不会被一次重新提取冲掉。
        2. **重新提取替换掉上一次的自动行。** 判据是 `version == 1`（没被人改过）
           且 `confirmed_at IS NULL`（没入账）且带着识别证据。改过的行
           （`version > 1`）**保留并跳过替换**——那是人的劳动，不能被一次重试抹掉。
        3. **提取永远不写采购记录。** 采购记录的唯一入口是老板点确认，
           所以「提取重试不导致采购重复入账」在结构上就成立。
        """
        invoice = self._load_invoice(session, invoice_id)
        self._require_not_voided(invoice)

        replaced = self._drop_previous_extraction(session, invoice)
        self._apply_extracted_header(invoice, plan)

        repository = InvoiceLineRepository(session)
        next_no = max((line.line_no for line in invoice.lines), default=0) + 1
        created = 0
        for offset, draft in enumerate(plan.lines):
            evidence = dict(draft.evidence)
            if invoice_file_id is not None:
                evidence.setdefault("file_id", invoice_file_id)
            repository.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    line_no=next_no + offset,
                    status=LINE_STATUS_PENDING,
                    line_kind=LINE_KIND_PURCHASE,
                    raw_fields=json.dumps(evidence, ensure_ascii=False),
                    review_metadata=json.dumps(draft.review, ensure_ascii=False),
                    page=draft.review.get("page"),
                    **draft.values,
                )
            )
            created += 1
        session.flush()
        logger.info(
            "extraction applied invoice=%s created=%s replaced=%s doc_class=%s",
            invoice.id,
            created,
            replaced,
            getattr(plan, "doc_class", None),
        )
        if replaced:
            plan.warnings.append(
                f"重新提取替换掉了上一次自动产生的 {replaced} 条草稿行"
                "（都没有被人改过、也没有入账）。"
            )
        return created

    def _drop_previous_extraction(self, session: Session, invoice: Invoice) -> int:
        """删掉上一次提取自动产生、而且没人碰过的草稿行。

        **人改过的行不删**。判据是 `version > 1`：修改与确认都会把 version 加一
        （见 `_check_version` 旁边那几处 `line.version += 1`），所以 `version == 1`
        等于「从识别出来到现在没人动过」。
        """
        kept_edited = 0
        removed = 0
        for line in list(invoice.lines):
            if line.confirmed_at is not None:
                continue
            if not line.raw_fields:
                continue  # 手工加的行没有识别证据，不属于提取产出
            evidence = _maybe_json(line.raw_fields)
            if "fields" not in evidence or "provider" not in evidence:
                continue  # 步骤 06 的表格批次行：形状不同，不碰
            if line.version > 1:
                kept_edited += 1
                continue
            session.delete(line)
            removed += 1
        if kept_edited:
            logger.info(
                "extraction kept %s edited draft lines on invoice=%s",
                kept_edited,
                invoice.id,
            )
        _expire_collections(session, invoice)
        return removed

    def _apply_extracted_header(self, invoice: Invoice, plan: Any) -> None:
        """只填当前为空的单据头字段，并记下这次提取的来源与原始证据。"""
        invoice.recognition_provider = plan.provider
        invoice.recognition_is_fake = plan.provider == FAKE_PROVIDER
        invoice.recognized_at = utcnow()
        if plan.raw_document_fields:
            invoice.raw_document_fields = plan.raw_document_fields

        header = plan.header or {}
        if invoice.source_name is None and header.get("source_name"):
            invoice.source_name = str(header["source_name"])[:SOURCE_NAME_MAX_LENGTH]
            invoice.source_name_normalized = normalize_name(invoice.source_name)
        if invoice.invoice_number is None and header.get("invoice_number"):
            invoice.invoice_number = str(header["invoice_number"])[
                :INVOICE_NUMBER_MAX_LENGTH
            ]
            invoice.invoice_number_normalized = normalize_name(invoice.invoice_number)
        if invoice.invoice_date is None and header.get("invoice_date"):
            invoice.invoice_date = _try_date(str(header["invoice_date"]))
        if invoice.invoice_total is None and header.get("invoice_total") is not None:
            invoice.invoice_total = header["invoice_total"]
        if invoice.currency is None and header.get("currency"):
            candidate = str(header["currency"]).strip().upper()
            # 明确识别到不支持的币种时留空，避免把它错记成默认美元。
            invoice.currency = candidate if candidate in CURRENCIES else None

    def _apply_document_fields(self, invoice: Invoice, result: RecognitionResult) -> None:
        """把单据头的识别结果落到字段上，解析不了的留空、原文另存。

        注意顺序：**先存原文，再试解析**。解析不出来是常事（`11/08/26`、`FAKE 286.00`
        这类写法），那时字段留空而原文还在，老板看得见单据上到底写了什么。
        """
        invoice.recognition_provider = result.provider
        invoice.recognition_is_fake = result.is_fake
        invoice.recognized_at = result.recognized_at
        invoice.raw_document_fields = _raw_document_fields_json(result)

        supplier = _recognized_text(result.document_field(FIELD_SUPPLIER))
        if supplier:
            try:
                invoice.source_name = optional_text(
                    supplier, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH
                )
            except ValidationError:
                invoice.source_name = supplier[:SOURCE_NAME_MAX_LENGTH]
            invoice.source_name_normalized = (
                normalize_name(invoice.source_name) if invoice.source_name else None
            )

        number = _recognized_text(result.document_field(FIELD_INVOICE_NUMBER))
        if number:
            invoice.invoice_number = number.strip()[:INVOICE_NUMBER_MAX_LENGTH]
            invoice.invoice_number_normalized = normalize_name(invoice.invoice_number)

        invoice.invoice_date = _try_date(
            _recognized_text(result.document_field(FIELD_INVOICE_DATE))
        )
        invoice.invoice_total = _try_decimal(
            _recognized_text(result.document_field(FIELD_INVOICE_TOTAL)),
            scale=AMOUNT_SCALE,
        )

        currency = _recognized_text(result.document_field(FIELD_CURRENCY))
        if currency:
            candidate = currency.strip().upper()
            # 明确识别到不支持的币种时留空，避免把它错记成默认美元。
            invoice.currency = candidate if candidate in CURRENCIES else None

    def _draft_line(
        self, invoice: Invoice, recognized: RecognizedLine, *, line_no: int
    ) -> InvoiceLine:
        """把一条识别出来的行变成待确认的草稿行。

        数值能解析就预填，不能解析就留空——`unit_price` 认不出来绝不会变成 0。
        描述与单位按**原文**保留（不清洗、不猜），原始证据全部存进 `raw_fields`。
        """
        description = _recognized_text(recognized.get(FIELD_ITEM_DESCRIPTION))
        unit = _recognized_text(recognized.get(FIELD_UNIT))
        return InvoiceLine(
            invoice_id=invoice.id,
            line_no=line_no,
            status=LINE_STATUS_PENDING,
            line_kind=LINE_KIND_PURCHASE,
            description=(description.strip()[:DESCRIPTION_MAX_LENGTH] if description else None),
            quantity=_try_decimal(
                _recognized_text(recognized.get(FIELD_QUANTITY)), scale=QUANTITY_SCALE
            ),
            purchase_unit=(unit.strip()[:PURCHASE_UNIT_MAX_LENGTH] if unit else None),
            unit_price=_try_decimal(
                _recognized_text(recognized.get(FIELD_UNIT_PRICE)), scale=UNIT_PRICE_SCALE
            ),
            line_amount=_try_decimal(
                _recognized_text(recognized.get(FIELD_LINE_AMOUNT)), scale=AMOUNT_SCALE
            ),
            raw_fields=_raw_fields_json(recognized),
            page=recognized.page,
        )

    def _clean_line_values(
        self, session: Session, *, kind: str, **given: Any
    ) -> dict[str, Any]:
        """校验行上的业务字段。只处理传进来的那些键。

        数值允许为 null（草稿本来就可以留空），但给了值就按步骤 04 的规则解析——
        不接受 JSON 小数、不接受千分位、超出小数位数直接拒绝而不是四舍五入。
        调整行允许负数与 0 金额（退货 / 折让）。
        """
        allow_negative = kind == LINE_KIND_ADJUSTMENT
        values: dict[str, Any] = {}

        if "description" in given:
            values["description"] = optional_text(
                given["description"],
                field="description",
                max_length=DESCRIPTION_MAX_LENGTH,
            )
        if "purchase_unit" in given:
            values["purchase_unit"] = optional_text(
                given["purchase_unit"],
                field="purchase_unit",
                max_length=PURCHASE_UNIT_MAX_LENGTH,
            )
        if "source_name" in given:
            values["source_name"] = optional_text(
                given["source_name"],
                field="source_name",
                max_length=SOURCE_NAME_MAX_LENGTH,
            )
        if "quantity" in given:
            values["quantity"] = self._optional_decimal(
                given["quantity"],
                field="quantity",
                scale=QUANTITY_SCALE,
                allow_zero=False,
                allow_negative=allow_negative,
            )
        if "unit_price" in given:
            values["unit_price"] = self._optional_decimal(
                given["unit_price"],
                field="unit_price",
                scale=UNIT_PRICE_SCALE,
                allow_zero=True,
                allow_negative=allow_negative,
            )
        if "line_amount" in given:
            values["line_amount"] = self._optional_decimal(
                given["line_amount"],
                field="line_amount",
                scale=AMOUNT_SCALE,
                allow_zero=True,
                allow_negative=allow_negative,
            )
        if "purchase_date" in given:
            values["purchase_date"] = parse_purchase_date(given["purchase_date"])
        if "currency" in given:
            currency = given["currency"]
            values["currency"] = (
                None
                if currency is None
                else require_choice(
                    currency, field="currency", allowed=CURRENCIES, case="upper"
                )
            )
        if "food_item_id" in given:
            food_item_id = given["food_item_id"]
            if food_item_id is not None:
                self._require_food_item(session, food_item_id)
            values["food_item_id"] = food_item_id
        return values

    @staticmethod
    def _optional_decimal(
        value: Any,
        *,
        field: str,
        scale: int,
        allow_zero: bool,
        allow_negative: bool,
    ) -> Decimal | None:
        if value is None:
            return None
        return parse_decimal(
            value,
            field=field,
            scale=scale,
            allow_zero=allow_zero,
            allow_negative=allow_negative,
        )

    def _match_candidates(
        self,
        session: Session,
        *,
        description: str | None,
        source_name: str | None,
    ) -> tuple[MatchCandidateView, ...]:
        """「这句描述以前确认到哪个食材」。**两层**：先精确命中，不够再模糊找。

        第 1 层（05.6）：规范化后完全相同。走
        `original_description_normalized` 上的索引，全表有效、不受条数上限影响。

        第 2 层（10.4）：精确层**一条都没命中**时，把已确认的「描述 → 食材」映射
        取回内存按相似度比一遍。它吃的是三类真实写法：
        标点不同（`CHIX-BRST` 对 `CHIX BRST`）、抽掉元音的缩写（`CNTRY` 对
        `COUNTRY`，来自 Target 小票）、以及尾部粘着包装规格
        （`Beef Stomach - 牛肚6X44.10` 对 `beef stomach 牛肚`，来自 NewMoys
        的真实单据——同一个商品，一次写了箱规一次没写）。

        ⚠️ **第 2 层只在第 1 层空手时才跑。** 精确命中过的描述已经是确定答案，
        再掺几条"像的"进去只会让老板在确定答案旁边多读几条噪声。

        排序规则：**精确层整体优先，层内同来源优先、然后最近优先**。同一家供应商
        的同一句描述几乎一定是同一样东西；跨来源的也给出来，但排后面。一句描述
        历史上指向过多个食材时只排序、**不猜**——确认请求必须显式带上选中的那个。
        """
        if not description or not description.strip():
            return ()
        normalized = normalize_name(description)
        if not normalized:
            return ()
        source_normalized = normalize_name(source_name) if source_name else None
        repository = PurchaseRepository(session)

        rows = repository.description_history(normalized, limit=MATCH_HISTORY_LIMIT)
        if rows:
            grouped: dict[int, dict[str, Any]] = {}
            for row in rows:
                bucket = grouped.setdefault(
                    row.food_item_id,
                    {
                        "count": 0,
                        "last_purchase_id": row.purchase_id,
                        "last_purchase_date": row.purchase_date,
                        "same_source": False,
                    },
                )
                bucket["count"] += 1
                if (
                    source_normalized is not None
                    and row.source_name_normalized == source_normalized
                ):
                    bucket["same_source"] = True
            candidates = self._candidates_from(
                session,
                grouped,
                match_reason=matching.REASON_DESCRIPTION_EXACT,
                score=1.0,
            )
            candidates.sort(
                key=lambda item: (item.same_source, item.last_purchase_id), reverse=True
            )
            return tuple(candidates[:MATCH_CANDIDATE_LIMIT])

        return self._fuzzy_description_candidates(
            session,
            normalized=normalized,
            source_normalized=source_normalized,
            repository=repository,
        )

    def _fuzzy_description_candidates(
        self,
        session: Session,
        *,
        normalized: str,
        source_normalized: str | None,
        repository: PurchaseRepository,
    ) -> tuple[MatchCandidateView, ...]:
        """按相似度在已确认的「描述 → 食材」映射里找候选（步骤 10.4）。

        用**双向**相似度（`matching.similarity`）而不是单向覆盖：两边都是完整的
        商品描述，单向分会把短描述判成"完全被涵盖"——`pork` 对
        `pork spare rib(cube) 靓排粒` 单向是 1.0，而 NewMoys 的单据上它们是两样
        不同的东西（`Pork - CT` 与 `Pork Spare Rib(Cube)` 各有自己的价格）。
        """
        prepared = matching.prepare(normalized)
        if prepared.is_empty:
            # 描述里只有数字、单位与版式字（真实小票上有 `LCC SYSTEM USE ONLY`
            # 这种），比什么都不像。此时不给候选，而不是给一条分数最高的凑数。
            return ()
        mappings = repository.description_mappings(limit=MATCH_MAPPING_LIMIT)
        grouped: dict[int, dict[str, Any]] = {}
        for row in mappings:
            score = matching.similarity(prepared, matching.prepare(row.description_normalized))
            if score < matching.SUGGEST_MIN_SCORE:
                continue
            bucket = grouped.get(row.food_item_id)
            same_source = (
                source_normalized is not None
                and row.source_name_normalized == source_normalized
            )
            if bucket is None:
                grouped[row.food_item_id] = {
                    "count": row.purchase_count,
                    "last_purchase_id": row.last_purchase_id,
                    "last_purchase_date": row.last_purchase_date,
                    "same_source": same_source,
                    "score": score,
                    "matched_description": row.description_normalized,
                }
                continue
            bucket["count"] += row.purchase_count
            bucket["same_source"] = bucket["same_source"] or same_source
            if row.last_purchase_id > bucket["last_purchase_id"]:
                bucket["last_purchase_id"] = row.last_purchase_id
                bucket["last_purchase_date"] = row.last_purchase_date
            if score > bucket["score"]:
                # 同一条食材可能有好几句相似的历史描述：记下**最像的那一句**，
                # 界面要把它显示出来，否则老板不知道这个候选是怎么来的。
                bucket["score"] = score
                bucket["matched_description"] = row.description_normalized
        if not grouped:
            return ()
        candidates = self._candidates_from(
            session, grouped, match_reason=matching.REASON_DESCRIPTION_FUZZY
        )
        # 模糊层按**相似度**优先，同分再看同来源与最近：这一层里"有多像"才是
        # 主要依据，而精确层里所有候选一样像（都是 1.0），那时才轮到来源与时间。
        candidates.sort(
            key=lambda item: (
                item.score or 0.0,
                item.same_source,
                item.last_purchase_id,
            ),
            reverse=True,
        )
        return tuple(candidates[:MATCH_CANDIDATE_LIMIT])

    @staticmethod
    def _candidates_from(
        session: Session,
        grouped: dict[int, dict[str, Any]],
        *,
        match_reason: str,
        score: float | None = None,
    ) -> list[MatchCandidateView]:
        """把「食材 id → 聚合依据」变成候选视图。两层共用，排序由调用方决定。"""
        repository = FoodItemRepository(session)
        candidates: list[MatchCandidateView] = []
        for food_item_id, bucket in grouped.items():
            food_item = repository.get(food_item_id)
            if food_item is None:  # pragma: no cover - 外键保证它在
                continue
            raw_score = score if score is not None else bucket.get("score")
            candidates.append(
                MatchCandidateView(
                    food_item=FoodItemView.of(food_item),
                    purchase_count=bucket["count"],
                    last_purchase_id=bucket["last_purchase_id"],
                    last_purchase_date=bucket["last_purchase_date"],
                    same_source=bucket["same_source"],
                    match_reason=match_reason,
                    score=(
                        round(raw_score, 4) if isinstance(raw_score, float) else raw_score
                    ),
                    matched_description=bucket.get("matched_description"),
                )
            )
        return candidates

    def supplier_candidates(
        self, source_name: str, *, limit: int = MATCH_CANDIDATE_LIMIT
    ) -> tuple[MatchCandidateView, ...]:
        """**这家供应商历史上买过的食材**，最近使用优先（步骤 08.3i）。

        这是手写件的候选来源。05.6 那条「按原始描述精确命中」在手写件上**用不了**
        ——步骤 07 实测中文商品名跨模型分歧率 96.0%，模型给的名字几乎一定是错的，
        而给老板一个错名字比给他一张裁剪图更慢：他得先删掉错的再重填。

        所以手写件上商品名这一格的正确做法是「**裁图给他看 + 让他从食材库里选**」，
        OCR 在这里的职责降为零。本方法提供那张清单。

        ⚠️ 返回的 `purchase_count` 是「这家买过多少次」，不是「这句描述命中多少次」
        ——同一个视图字段在两种候选里含义不同，前端要按来源区分显示。
        """
        normalized = normalize_name(source_name) if source_name else None
        if not normalized:
            return ()
        with self._database.unit_of_work() as session:
            rows = PurchaseRepository(session).supplier_history(
                normalized, limit=MATCH_HISTORY_LIMIT
            )
            if not rows:
                return ()
            grouped: dict[int, dict[str, Any]] = {}
            for row in rows:
                bucket = grouped.setdefault(
                    row.food_item_id,
                    {
                        "count": 0,
                        "last_purchase_id": row.purchase_id,
                        "last_purchase_date": row.purchase_date,
                    },
                )
                bucket["count"] += 1
            repository = FoodItemRepository(session)
            candidates: list[MatchCandidateView] = []
            for food_item_id, bucket in grouped.items():
                food_item = repository.get(food_item_id)
                if food_item is None:  # pragma: no cover - 外键保证它在
                    continue
                candidates.append(
                    MatchCandidateView(
                        food_item=FoodItemView.of(food_item),
                        purchase_count=bucket["count"],
                        last_purchase_id=bucket["last_purchase_id"],
                        last_purchase_date=bucket["last_purchase_date"],
                        same_source=True,
                        match_reason=matching.REASON_SUPPLIER_HISTORY,
                        # ⚠️ **刻意没有分数**：这条候选不是按文本像不像比出来的，
                        # 而是「这家供应商买过它」。给个分数会让界面把它和模糊命中
                        # 摆在同一把尺子上比，而它们量的根本不是同一件事。
                        score=None,
                    )
                )
            # 最近用过的排前面：老板这次要选的，多半就是上次选过的那几样。
            candidates.sort(key=lambda item: item.last_purchase_id, reverse=True)
            return tuple(candidates[: max(1, limit)])

    def _view(
        self, session: Session, invoice: Invoice, *, with_candidates: bool
    ) -> InvoiceView:
        lines: list[InvoiceLineView] = []
        for line in invoice.lines:
            candidates: tuple[MatchCandidateView, ...] = ()
            # 只给**待确认**的行算候选：已确认的行食材已经定了，跳过的行不入账，
            # 给它们算候选只是白查数据库。
            if with_candidates and line.status == LINE_STATUS_PENDING:
                candidates = self._match_candidates(
                    session,
                    description=line.description,
                    source_name=line.source_name or invoice.source_name,
                )
            lines.append(InvoiceLineView.of(line, candidates=candidates))

        duplicates: tuple[int, ...] = ()
        if (
            invoice.source_name_normalized
            and invoice.invoice_number_normalized
            and invoice.invoice_total is not None
        ):
            duplicates = tuple(
                InvoiceRepository(session).find_possible_duplicates(
                    exclude_invoice_id=invoice.id,
                    source_normalized=invoice.source_name_normalized,
                    number_normalized=invoice.invoice_number_normalized,
                    total=invoice.invoice_total,
                )
            )

        return InvoiceView(
            id=invoice.id,
            status=_invoice_status(invoice),
            source_name=invoice.source_name,
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.invoice_date,
            currency=invoice.currency,
            invoice_total=invoice.invoice_total,
            recognition_provider=invoice.recognition_provider,
            recognition_is_fake=invoice.recognition_is_fake,
            recognized_at=invoice.recognized_at,
            raw_document_fields=(
                json.loads(invoice.raw_document_fields)
                if invoice.raw_document_fields
                else None
            ),
            completed_at=invoice.completed_at,
            voided_at=invoice.voided_at,
            uploaded_at=invoice.created_at,
            updated_at=invoice.updated_at,
            files=tuple(InvoiceFileView.of(item) for item in invoice.files),
            lines=tuple(lines),
            reconciliation=_reconciliation(invoice),
            possible_duplicate_invoice_ids=duplicates,
            entry_source=invoice.entry_source,
            import_metadata=json.loads(invoice.import_metadata) if invoice.import_metadata else None,
        )

    # --- 加载与校验 ---

    @staticmethod
    def _load_invoice(session: Session, invoice_id: int) -> Invoice:
        invoice = InvoiceRepository(session).get(invoice_id)
        if invoice is None:
            raise NotFoundError(
                f"单据 {invoice_id} 不存在。", details={"invoice_id": invoice_id}
            )
        return invoice

    @staticmethod
    def _load_line(session: Session, invoice: Invoice, line_id: int) -> InvoiceLine:
        """按 id 取行，并确认它**属于这张单据**。

        不属于时报 404 而不是 403：对调用方而言「这张单据上没有这一行」就是不存在，
        没必要透露别的单据上有没有这个 id。
        """
        for line in invoice.lines:
            if line.id == line_id:
                return line
        raise NotFoundError(
            f"单据 {invoice.id} 上没有第 {line_id} 号行。",
            details={"invoice_id": invoice.id, "line_id": line_id},
        )

    @staticmethod
    def _load_file(session: Session, invoice: Invoice, file_id: int) -> InvoiceFile:
        for item in invoice.files:
            if item.id == file_id:
                return item
        raise NotFoundError(
            f"单据 {invoice.id} 上没有 {file_id} 号文件。",
            details={"invoice_id": invoice.id, "file_id": file_id},
        )

    @staticmethod
    def _require_not_voided(invoice: Invoice) -> None:
        if invoice.voided_at is not None:
            raise ConflictError(
                f"单据 {invoice.id} 已作废，不能再修改。",
                details={
                    "invoice_id": invoice.id,
                    "voided_at": invoice.voided_at.isoformat(),
                },
            )

    @staticmethod
    def _check_version(line: InvoiceLine, version: int | None) -> None:
        """乐观锁。`version` 没给就不检查（命令行与脚本不必关心并发）。"""
        if version is None:
            return
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValidationError(
                "version 必须是整数。", details={"field": "version"}
            )
        if version != line.version:
            raise ConflictError(
                f"第 {line.line_no} 行在你打开之后被改过了"
                f"（你拿到的是第 {version} 版，现在是第 {line.version} 版）。"
                "请刷新后再操作，免得覆盖掉别人刚改的内容。",
                details={
                    "line_id": line.id,
                    "expected_version": version,
                    "current_version": line.version,
                },
            )

    @staticmethod
    def _require_food_item(session: Session, food_item_id: Any) -> None:
        """校验 food_item_id 的类型与存在性。

        与步骤 04 一致报 400 而不是 404：被操作的资源是发票行，食材只是请求数据里的
        一个引用，写错了属于请求不合法。
        """
        if not isinstance(food_item_id, int) or isinstance(food_item_id, bool):
            raise ValidationError(
                "food_item_id 必须是整数。", details={"field": "food_item_id"}
            )
        if FoodItemRepository(session).get(food_item_id) is None:
            raise ValidationError(
                f"食材 {food_item_id} 不存在。",
                details={"field": "food_item_id", "food_item_id": food_item_id},
            )


__all__ = [
    "ALLOWED_UPLOAD_EXTENSIONS",
    "INVOICE_STATUS_COMPLETED",
    "INVOICE_STATUS_DRAFT",
    "INVOICE_STATUS_IN_REVIEW",
    "INVOICE_STATUS_READY",
    "INVOICE_STATUS_VOIDED",
    "MAX_FILES_PER_INVOICE",
    "RECOGNIZABLE_EXTENSIONS",
    "RECONCILIATION_BALANCED",
    "RECONCILIATION_MISMATCH",
    "RECONCILIATION_NOT_CHECKED",
    "RECONCILIATION_NO_TOTAL",
    "ConfirmOption",
    "ConfirmOutcome",
    "ConfirmResult",
    "InvoiceFileView",
    "InvoiceLineView",
    "InvoicePage",
    "InvoiceService",
    "InvoiceView",
    "MatchCandidateView",
    "ReconciliationView",
    "UploadedFile",
]
