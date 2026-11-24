"""真实 OCR 提取：分流、分层校验、草稿写入与识别证据（步骤 08）。

本模块是步骤 08 的主干。它把一个已经存好的原始文件变成步骤 05 的待确认草稿，
中间经过三条路径之一与四层校验。**它不写采购记录**——采购记录的唯一入口是老板
对某一行点确认（步骤 05 的口径），所以「提取重试不导致采购重复入账」在结构上
就成立。

## 分流（三条路径，耗时差一个数量级）

| 情况 | 路径 | 预期耗时 |
| --- | --- | --- |
| 数字 PDF（带文字层） | 直接抽文字层，**根本不调 OCR** | 近 0，无成本 |
| 印刷体扫描件 | 1 轮内容识别（多模型并行）+ 格线检测 | 约 5—7 秒 |
| 手写件 | 2 轮（先结构后内容）+ 多模型并行 | 约 15 秒 |

**分流依据必须与识别出的内容无关**（步骤 08 已定）：

1. **PDF 里有没有可解析的文字层**——文件结构的客观事实。⚠️ 判据是
   「文字层**真的解析出了表头与行**」，不是「文本长度超过某个阈值」
   （08.4 的「不移植」点名了那条旧阈值）。
2. **按供应商路由**（主）——供应商是固定的，首次确认时记下这家走哪条路。
3. **本地笔画判别**（兜底）——新供应商首次出现时量笔画宽度分布，判不准就倒向
   保守那条路。

⚠️ **禁止用「各数字算得平」当分流开关**：步骤 07 的 `8 × 117 = 936` 已经证明
算术自洽的编造完全存在，拿它当放行条件等于专门给编造开绿灯。

## 分层校验

- **第 0 层　墨迹检测**：⚠️ **本批次（批次 1）只做到行容量上限。**
  逐格墨迹判定需要先做图像配准，步骤 07 已实测只靠像素阈值分不开空行与写字行
  （三组失败数据见 `docs/impl_record.md` 步骤 07 第 4 节）。本模块现在用的是
  `documents.grid` 的**行容量**——模型报的行数超过表格物理行格数，多出来的
  一律丢弃。这一条是可以逐像素复核的硬事实，也是对「把固定结构填满」唯一确定
  有效的防线（实测：ChangShun p01 纸上只写了 4 行，`qwen-vl-ocr` 报出 15 行，
  而那张表恰好印着 15 个行格）。
- **第 1 层　先定结构，不读内容**：先问「哪几行写了东西」，再读内容。
  它的产出只当**上限之一**用，与行容量取较小者。
- **第 2 层　多模型并行读**：一致才预填并标注「多模型一致」，分歧置
  `UNREADABLE` 必须人填。见 `integrations/crosscheck.py`。
- **第 3 层　对照整页原件填空**：保留模型原文与行号证据，供人工审核。

## 手写件一律不预填

步骤 07 确认连中文母语使用者都认不出这些连笔速记，模型在这些格子上读出的任何值
都没有可信来源。所以手写件比上面的分层再严一档：**所有字段以 `UNREADABLE`
进草稿**，模型只负责提取原文与行顺序。

两个模型各自读到的原文**仍然原样保留**在 `raw_fields.fields.<name>.model_values`
里——既满足契约第 1 条「保留原文」，也让审核界面能把两种读法摆在整页原件旁边，
让人核对，而不是在两个数字之间二选一。

**数字字段的交叉验证预填口子默认关闭**（08.3k）：数字跨模型分歧率 66.9% 意味着
「多模型一致」本身就很少成立，而一旦成立又无法排除两个模型一致地错
（TangFa p07 的 `5 85` 被两个模型都读成 `185`，差 20 倍）。

## 一处与文档正文不同的做法，写明理由

步骤 08 的分流表写的是「印刷体扫描件 → 1 轮**单模型**」。本模块对印刷件也跑
**2 个模型并行**，理由有三条，都来自文档自身：

1. 第 4 节外部准备写明「步骤 07 的实测已证明多模型交叉是**必需的防线**，
   不再是『可选增强』；取几个已定：2 个」。
2. 分流表给「1 轮单模型」的依据是耗时，而文档同时写明「**成本与延迟不构成
   约束**」；并行发请求的总耗时等于最慢的那个，不是相加，所以省不下时间。
3. 单模型在印刷件上字段准确率 96.6%，即约 3.4% 的字段会被**预填成错值**，
   而文档自己指出「给人一个已填好的 117.00 让他核对，他会倾向于点确认」。
   交叉验证把这部分变成需人填，与「宁可让人填，也不要悄悄存一个错价格」一致。

**「1 轮 / 2 轮」的区别保留**：轮次指**串行的识别回合**——印刷件只跑内容这一轮，
手写件多跑一轮结构。轮内用几个模型是并行的事，与轮次无关。
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from src.config import OCR_WORKER_INLINE, Config
from src.contracts import (
    DOCUMENT_FIELDS,
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
    FieldStatus,
    RecognitionRequest,
    RecognitionResult,
    RecognizedField,
)
from src.db import Database
from src.documents.anchor import MIN_ANCHOR_BANDS, RowAnchor, row_anchor_from
from src.documents.classify import InkStyle, profile_ink
from src.documents.files import DocumentFormat, FormatError, decode_image, sniff_format
from src.documents.grid import (
    GridError,
    PageGrid,
    detect_grid,
    row_number_tiles,
)
from src.documents.ink import InkError, InkGate, gate_rows
from src.documents.pdf import (
    PdfError,
    extract_page_images,
    inspect_pdf,
    read_text_fragments,
)
from src.documents.textlayer import (
    EXTRA_FIELD_PACK_SPEC,
    EXTRA_FIELD_SUPPLIER_CODE,
    TEXT_LAYER_PROVIDER,
    parse_text_layout,
)
from src.errors import AppError, ConflictError, NotFoundError, ValidationError
from src.integrations.crosscheck import CrossCheckOutcome
from src.integrations.dashscope import CONTENT_PROMPT_SHA12, OcrServiceError
from src.integrations.file_storage import LocalFileStorage
from src.integrations.ocr import CrossCheckRecognizer, Recognizer
from src.models import (
    AMOUNT_SCALE,
    DESCRIPTION_MAX_LENGTH,
    DOC_CLASS_DIGITAL_PDF,
    DOC_CLASS_HANDWRITTEN_SCAN,
    DOC_CLASS_PRINTED_SCAN,
    DOC_CLASS_UNKNOWN,
    EXTRACTION_STATUS_QUEUED,
    EXTRACTION_STATUS_RUNNING,
    PURCHASE_UNIT_MAX_LENGTH,
    QUANTITY_SCALE,
    ROUTE_FROM_LOCAL_DISCRIMINATOR,
    ROUTE_FROM_MANUAL,
    ROUTE_FROM_PDF_TEXT_LAYER,
    ROUTE_FROM_SUPPLIER,
    UNIT_PRICE_SCALE,
    ExtractionJob,
    Invoice,
    InvoiceFile,
    OcrRoute,
    utcnow,
)
from src.normalization import (
    DATE_ORDER_ASSUMED,
    DATE_ORDER_STRICT,
    NUMBER_FORMAT_AUTO,
    date_text_readings,
    fold_width,
    normalize_date_text,
    normalize_number_text,
)
from src.repositories.extraction import ExtractionJobRepository, OcrRouteRepository
from src.repositories.ingredients import CategoryRepository, FoodItemRepository
from src.repositories.invoices import InvoiceRepository
from src.repositories.intake_reviews import IntakeReviewRepository
from src.repositories.purchases import PurchaseRepository
from src.services.ingredients import (
    BASE_UNITS,
    UNCATEGORIZED_NAME_NORMALIZED,
    IngredientService,
    normalize_name,
    normalize_unit_token,
)
from src.services.matching import extract_keywords, guess_category_key
from src.services.merging import merged_name_lookup
from src.services.purchases import parse_decimal
from src.services.validation import optional_text

logger = logging.getLogger(__name__)

# 能送去识别的扩展名。`heic` 在这里——步骤 05 只存不识别，解码是本步骤的事
# （`documents.files.decode_image` 转成 JPEG 再送）。
RECOGNIZABLE_FORMATS = (
    DocumentFormat.JPEG,
    DocumentFormat.PNG,
    DocumentFormat.HEIC,
    DocumentFormat.PDF,
)

# 一次提取最多处理多少页。多页 PDF 的每一页都要调模型，20 页约 2 分钟。
MAX_PAGES_PER_JOB = 40

# 一张单据最多产出多少草稿行。行容量上限是按页算的，这是整单的兜底：
# 没有行容量可用（小票、非表格版式）时，仍然不该让一次解析失败产出上千行。
MAX_LINES_PER_JOB = 300

# `running` 超过这么久就算卡住（进程被 kill 时状态没人改）。
# 手写多页单据最慢的情形：40 页 × 20 秒 ≈ 13 分钟，留到 30 分钟足够宽裕。
STALE_RUNNING_AFTER = timedelta(minutes=30)

# 预填来源标记，落在 `review_metadata.prefill` 上。
PREFILL_TEXT_LAYER = "text_layer"
PREFILL_CROSSCHECK = "crosscheck"
PREFILL_SUPPRESSED_HANDWRITTEN = "suppressed_handwritten"
PREFILL_NONE = "none"

# 查「这句描述以前确认成过哪个食材」时最多回看多少条采购记录（步骤 10.C）。
# 与 `services/invoices.py` 的 `MATCH_HISTORY_LIMIT` 同一个量级、同一个用途：
# 200 条足够覆盖同一句描述的历史，又不会在大表上扫出性能问题。
DESCRIPTION_MEMORY_LIMIT = 200

# 数值字段 → scale（沿用步骤 04，不另立）。
_FIELD_SCALES = {
    FIELD_QUANTITY: QUANTITY_SCALE,
    FIELD_UNIT_PRICE: UNIT_PRICE_SCALE,
    FIELD_LINE_AMOUNT: AMOUNT_SCALE,
}


# --- 只读视图 ---------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionJobView:
    """一次提取任务的对外视图。"""

    id: int
    invoice_id: int
    invoice_file_id: int | None
    status: str
    attempt: int
    doc_class: str | None
    route_source: str | None
    provider: str | None
    models: tuple[str, ...]
    prompt_sha12: str | None
    pages_total: int | None
    pages_done: int
    lines_created: int
    error_code: str | None
    error_message: str | None
    warnings: tuple[str, ...]
    usage: dict[str, Any]
    started_at: str | None
    finished_at: str | None
    created_at: str | None

    @classmethod
    def of(cls, job: ExtractionJob) -> "ExtractionJobView":
        return cls(
            id=job.id,
            invoice_id=job.invoice_id,
            invoice_file_id=job.invoice_file_id,
            status=job.status,
            attempt=job.attempt,
            doc_class=job.doc_class,
            route_source=job.route_source,
            provider=job.provider,
            models=tuple(_load_json_list(job.models)),
            prompt_sha12=job.prompt_sha12,
            pages_total=job.pages_total,
            pages_done=job.pages_done,
            lines_created=job.lines_created,
            error_code=job.error_code,
            error_message=job.error_message,
            warnings=tuple(_load_json_list(job.warnings)),
            usage=_load_json_dict(job.usage),
            started_at=job.started_at.isoformat() if job.started_at else None,
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
            created_at=job.created_at.isoformat() if job.created_at else None,
        )


@dataclass(frozen=True)
class OcrRouteView:
    """一家供应商的提取路径。"""

    source_name: str | None
    source_name_normalized: str
    doc_class: str
    decided_from: str
    date_order: str | None
    note: str | None

    @classmethod
    def of(cls, route: OcrRoute) -> "OcrRouteView":
        return cls(
            source_name=route.source_name,
            source_name_normalized=route.source_name_normalized,
            doc_class=route.doc_class,
            decided_from=route.decided_from,
            date_order=route.date_order,
            note=route.note,
        )


# --- 提取计划（写入草稿之前的中间产物）-------------------------------------


@dataclass(frozen=True)
class _JobContext:
    """执行一条任务所需的一切，**从数据库里一次读完**。

    为什么要这么一个东西：第 2 段（调模型）不持有事务，所以那段代码不能再碰
    ORM 对象——会话一关，惰性加载就会炸，而且那种报错出现在「调了几十秒之后」，
    排查起来最费时间。把需要的值先抄出来，第 2 段就只跟纯数据打交道。
    """

    job_id: int
    invoice_id: int
    file_id: int | None
    storage_key: str | None
    original_name: str | None
    source_name: str | None
    source_name_normalized: str | None


@dataclass
class LineDraft:
    """一条待写入的草稿行，形状与步骤 06 导入路径一致（values / evidence / review）。"""

    values: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionPlan:
    """一次提取的完整产出，写库之前先在内存里拼好。

    分成两步（先拼计划、再一次写库）的理由：调模型要几秒到几十秒，不能把数据库
    事务开着等；而写库那一下必须是原子的，否则提取中途失败会留下半张草稿。
    """

    doc_class: str
    route_source: str
    provider: str
    models: list[str] = field(default_factory=list)
    prompt_sha12: str | None = None
    pages_total: int = 0
    pages_done: int = 0
    header: dict[str, Any] = field(default_factory=dict)
    raw_document_fields: str | None = None
    lines: list[LineDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


# --- 模块级辅助 -------------------------------------------------------------


def _load_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _load_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _box_payload(field_value: RecognizedField) -> tuple[dict | None, str | None]:
    """保留识别厂商提供的原始坐标；没有坐标就不补造。"""
    box = field_value.box
    if box is not None:
        return (
            {
                "left": box.left,
                "top": box.top,
                "width": box.width,
                "height": box.height,
                "space": str(box.space),
            },
            "vendor",
        )
    return None, None


def _try_decimal(text: str | None, *, scale: int) -> Decimal | None:
    """把规范化之后的文本转成精确数值。超出 scale 直接拒绝（不舍入），返回 None。

    用的是步骤 04 的 `parse_decimal`，不另立口径。草稿阶段解析失败**不报错**——
    留空 + 挂提示，等人填。
    """
    if text is None:
        return None
    try:
        return parse_decimal(
            text, field="draft", scale=scale, allow_zero=True, allow_negative=True
        )
    except ValidationError:
        return None


class ExtractionService:
    """提取任务的编排：入队、执行、查询与路由。"""

    def __init__(
        self,
        database: Database,
        *,
        file_storage: LocalFileStorage,
        recognizer: Recognizer,
        invoice_service: Any,
        config: Config | None = None,
    ) -> None:
        self._database = database
        self._storage = file_storage
        self._recognizer = recognizer
        self._invoices = invoice_service
        self._config = config

    # --- 入队 ---------------------------------------------------------------

    def schedule(self, invoice_id: int) -> list[ExtractionJobView]:
        """给一张单据的每个可识别文件建一条提取任务。

        **一个文件一条任务**：一张单据挂几张照片时，某一张拍坏了只影响它自己那
        一条，其它照片照常出行。

        本地即时的路径（Fake 识别器、数字 PDF 文字层）会**在当前请求里直接跑完**，
        要调模型的那些才入队给 worker。这正是步骤 08 分流表的本意：
        「三条路径的耗时差一个数量级，不应该让最容易的单据陪最难的单据一起等」。
        """
        with self._database.unit_of_work() as session:
            invoice = self._load_invoice(session, invoice_id)
            if IntakeReviewRepository(session).links_for_invoice(invoice_id):
                raise ConflictError("这张草稿来自整理批次，已复用保存的识别证据；请在审核中修正，不要重复提取。",
                                    details={"invoice_id": invoice_id})
            repository = ExtractionJobRepository(session)
            active = repository.active_for_invoice(invoice_id)
            if active is not None:
                raise ConflictError(
                    f"这张单据已经有一个提取任务在进行中（#{active.id}，"
                    f"状态 {active.status}）。等它跑完再提取，不然同一页图会调两遍。",
                    details={"extraction_job_id": active.id, "status": active.status},
                )
            self._check_rerun_allowed(invoice)
            created: list[int] = []
            for record in invoice.files:
                if not _is_recognizable_name(record.original_name):
                    # 表格批次（csv / xlsx）不走识别——步骤 06 明确「导入路径不调
                    # 识别器」：表格本来就是结构化的，让它走识别只会凭空引入错误。
                    continue
                job = repository.add(
                    ExtractionJob(
                        invoice_id=invoice.id,
                        invoice_file_id=record.id,
                        status=EXTRACTION_STATUS_QUEUED,
                        provider=self._recognizer.provider,
                        models=json.dumps(
                            list(getattr(self._recognizer, "models", ())),
                            ensure_ascii=False,
                        ),
                    )
                )
                created.append(job.id)
            session.flush()

        results: list[ExtractionJobView] = []
        for job_id in created:
            results.append(self.run_job(job_id) if self._runs_inline() else self.view(job_id))
        return results

    def _runs_inline(self) -> bool:
        """这次提取是不是在当前请求里直接跑完。

        两种情况直接跑：① 配置写明 `inline`；② 识别器是 Fake（本地、瞬时，
        没有网络调用可等）。其余交给 worker——手写件约 15 秒，挂在 HTTP 请求里
        会让老板对着转圈等（08.3g）。

        ⚠️ 两种方式跑的是**同一份提取代码**，只差一个调度器；任务行一样落库，
        于是 `GET /api/invoices/<id>/extraction` 在两种方式下都查得到状态。
        """
        if self._recognizer.is_fake:
            return True
        if self._config is None:
            return True
        return self._config.ocr_worker_mode == OCR_WORKER_INLINE

    def _check_rerun_allowed(self, invoice: Invoice) -> None:
        """已经有行确认入账时不许重新提取。

        重新提取会**替换掉**自动产生的草稿行（见 `_write_plan`）。如果某些行已经
        确认成了采购记录，替换之后采购记录指向的行号就与新草稿对不上了——
        而那种错位不报错，只会在日后对账时显示成一笔来源不明的采购。
        所以这里直接挡住，并告诉人该怎么走：先撤销那些行。
        """
        confirmed = [line for line in invoice.lines if line.confirmed_at is not None]
        if confirmed:
            raise ConflictError(
                f"这张单据已经有 {len(confirmed)} 行确认入账了。重新提取会替换掉"
                "草稿行，和已入账的行对不上——请先对那些行点「撤销确认」，再重新提取。",
                details={
                    "confirmed_line_ids": [line.id for line in confirmed],
                    "invoice_id": invoice.id,
                },
            )

    # --- 执行 ---------------------------------------------------------------

    def run_job(self, job_id: int) -> ExtractionJobView:
        """跑一条提取任务。

        流程刻意分成三段，中间**不持有数据库事务**：

        1. 短事务：占下任务（置 `running`、加一次 attempt），读出文件引用。
        2. 不开事务：读文件、分流、调模型、拼出计划。这一段可能要几十秒。
        3. 短事务：把计划写进草稿，标记任务完成。

        ⚠️ 第 2 段失败时，第 1 段已经提交了——所以**原始文件与已有草稿都在**，
        只是任务记一条 `failed` 与原因（08.5 的验收条件）。把三段合成一个长事务
        会让一次超时把整张单据的上传一起回滚掉，那是最糟的失败方式。
        """
        return self._execute(self._claim(job_id))

    def _claim(self, job_id: int) -> "_JobContext":
        """占下一条任务并读出执行所需的一切。一个短事务，提交后才去调模型。"""
        with self._database.unit_of_work() as session:
            job = ExtractionJobRepository(session).get(job_id)
            if job is None:
                raise NotFoundError(
                    f"提取任务 #{job_id} 不存在。",
                    details={"extraction_job_id": job_id},
                )
            if job.status == EXTRACTION_STATUS_RUNNING:
                raise ConflictError(
                    f"提取任务 #{job_id} 正在跑，不重复执行——同一页图调两遍是白花钱。",
                    details={"extraction_job_id": job_id},
                )
            job.status = EXTRACTION_STATUS_RUNNING
            job.attempt += 1
            job.started_at = utcnow()
            return self._context_of(session, job)

    @staticmethod
    def _context_of(session: Session, job: ExtractionJob) -> "_JobContext":
        record = (
            session.get(InvoiceFile, job.invoice_file_id)
            if job.invoice_file_id
            else None
        )
        return _JobContext(
            job_id=job.id,
            invoice_id=job.invoice_id,
            file_id=job.invoice_file_id,
            storage_key=record.storage_key if record else None,
            original_name=record.original_name if record else None,
            source_name=job.invoice.source_name,
            source_name_normalized=job.invoice.source_name_normalized,
        )

    def _execute(self, context: "_JobContext") -> ExtractionJobView:
        try:
            plan = self._build_plan(
                storage_key=context.storage_key,
                original_name=context.original_name,
                file_id=context.file_id,
                source_name=context.source_name,
                source_name_normalized=context.source_name_normalized,
            )
        except (AppError, OcrServiceError, FormatError, PdfError, GridError) as error:
            return self._fail(context.job_id, error)
        except Exception as error:  # noqa: BLE001
            # 预期之外的失败也要落库并说清楚，不能让任务永远停在 running——
            # 「那张单一直在识别中」是最难发现的那种故障。
            logger.exception("extraction job %s crashed", context.job_id)
            return self._fail(context.job_id, error)
        return self._write_plan(context.job_id, context.invoice_id, plan)

    def _fail(self, job_id: int, error: BaseException) -> ExtractionJobView:
        code = getattr(error, "code", None) or type(error).__name__
        message = str(error)[:600]
        with self._database.unit_of_work() as session:
            repository = ExtractionJobRepository(session)
            job = repository.get(job_id)
            if job is None:  # pragma: no cover - 任务被并发删掉
                raise NotFoundError(f"提取任务 #{job_id} 不存在。")
            repository.finish(
                job, succeeded=False, error_code=str(code)[:40], error_message=message
            )
            logger.warning(
                "extraction job %s failed code=%s retryable=%s",
                job_id,
                code,
                getattr(error, "retryable", None),
            )
            return ExtractionJobView.of(job)

    def _write_plan(
        self, job_id: int, invoice_id: int, plan: ExtractionPlan
    ) -> ExtractionJobView:
        """把计划写进草稿，并标记任务完成。一个事务，要么全写进去要么都不写。"""
        with self._database.unit_of_work() as session:
            repository = ExtractionJobRepository(session)
            job = repository.get(job_id)
            if job is None:  # pragma: no cover
                raise NotFoundError(f"提取任务 #{job_id} 不存在。")
            self._resolve_food_items(session, plan)
            created = self._invoices.apply_extraction(
                session, invoice_id, plan, invoice_file_id=job.invoice_file_id
            )
            job.doc_class = plan.doc_class
            job.route_source = plan.route_source
            job.provider = plan.provider
            job.models = json.dumps(plan.models, ensure_ascii=False)
            job.prompt_sha12 = plan.prompt_sha12
            job.pages_total = plan.pages_total
            job.pages_done = plan.pages_done
            job.lines_created = created
            job.usage = json.dumps(plan.usage, ensure_ascii=False)
            job.warnings = json.dumps(plan.warnings, ensure_ascii=False)
            repository.finish(job, succeeded=True)
            logger.info(
                "extraction job %s done doc_class=%s lines=%s pages=%s/%s",
                job_id,
                plan.doc_class,
                created,
                plan.pages_done,
                plan.pages_total,
            )
            return ExtractionJobView.of(job)

    # --- 关键词认食材（步骤 10.C）------------------------------------------

    def _resolve_food_items(self, session, plan: ExtractionPlan) -> None:
        """按商品描述里的关键词给草稿行认食材，认不出就按口径自动建一条。

        **闸门是单据类别，不是模型置信度**（10.C）：手写件一律不碰，
        用的就是 08.3h 那一条已经存在的闸门（`PREFILL_SUPPRESSED_HANDWRITTEN`），
        不另开口子。理由不变——步骤 07 实测中文商品名跨模型分歧率 **96.0%**，
        手写件上模型读出的任何名字几乎一定是错的，而给老板一个错名字比给他一张
        裁剪图更慢：他得先删掉错的再重填。

        三条分支，**分界线是基础单位能不能定出来，不是关键词像不像**：

        | 情况 | 做什么 |
        | --- | --- |
        | 关键词精确命中食材库 | 直接选中，老板零点击 |
        | 没命中，**单位能归一** | 自动建好、自动选中、标 `needs_review` |
        | 没命中，**单位归一不出来** | **不建**，把猜测放进 `review` 让界面预填表单 |

        ⚠️ **绝不拿 `each` 兜底**。`samples/real/ground_truth/` 的 48 行里，
        34 行小票**纸面根本没印单位**、1 行是包装单位「箱」——占 73%。给它们
        随便填一个 `each`，和步骤 09「算不出来时标准化价格是 `null` 而不是 `0`」
        是同一类错误：把"不知道"写成一个具体的值，不报错、不可察觉，然后喂进换算。

        ⚠️ **认出食材 ≠ 确认这一行**。与 05.6 同一条口径：预填候选可以，
        替老板点确认不行。这里只填 `food_item_id`，行仍然是 `pending`。

        ⚠️ **命中判定是"规范化后完全相同"，不走模糊分数**。`pork` 与 `park` 的
        模糊比值是 0.75——当候选没问题，拿来**自动选中**就会把一条采购记录静默
        挂到错误的食材上。模糊匹配的用武之地是候选列表（`services/suggestions.py`），
        那里有人看着。
        """
        food_items = FoodItemRepository(session)
        categories = CategoryRepository(session)
        uncategorized: int | None = None

        for draft in plan.lines:
            if draft.review.get("prefill") == PREFILL_SUPPRESSED_HANDWRITTEN:
                continue
            description = draft.values.get("description")

            # ⚠️ **老板亲手确认过的映射排第一，关键词排第二。** 顺序反了会出一个
            # 静默的错：他明明把这句描述确认成过 A，而关键词从同一句话里剥出来的
            # 词碰巧命中了 B，于是系统替他选了 B——不报错，而且看起来像是他自己
            # 选的。`imp_notes.md` 4.8 写的也是「下次出现相同的供应商商品描述时，
            # **优先复用已确认的映射**」。
            remembered = self._lookup_by_description(session, description)
            if remembered is not None:
                draft.values["food_item_id"] = remembered
                draft.review["food_item_match"] = {
                    "reason": "description_exact",
                    "description": description,
                    "food_item_id": remembered,
                }
                continue

            keywords = extract_keywords(description)
            if keywords.is_empty:
                continue

            hit = self._lookup_by_keywords(session, food_items, keywords)
            if hit is not None:
                draft.values["food_item_id"] = hit.id
                draft.review["food_item_match"] = {
                    "reason": "keyword_exact",
                    "keyword": keywords.name or keywords.name_zh,
                    "food_item_id": hit.id,
                }
                continue

            unit_token = normalize_unit_token(draft.values.get("purchase_unit"))
            if uncategorized is None:
                uncategorized = self._uncategorized_id(categories)
            guessed = self._guess_category_id(categories, keywords, uncategorized)
            if unit_token not in BASE_UNITS:
                # 单位定不出来就**不建**：建出来的那条食材的基础单位只能是编的，
                # 而它会一直被用来算标准化价格。改成把表单预填好，让老板点一下。
                draft.review["food_item_match"] = {
                    "reason": "needs_base_unit",
                    "keyword": keywords.name or keywords.name_zh,
                    "suggested_name": keywords.name or keywords.name_zh,
                    "suggested_name_zh": keywords.name_zh,
                    "suggested_category_id": guessed,
                    "unit_raw": draft.values.get("purchase_unit"),
                }
                continue

            created = self._create_from_keywords(
                session, keywords, base_unit=unit_token, category_id=guessed
            )
            draft.values["food_item_id"] = created
            draft.review["food_item_match"] = {
                "reason": "keyword_created",
                "keyword": keywords.name or keywords.name_zh,
                "food_item_id": created,
                "category_id": guessed,
                "needs_review": True,
            }

    @staticmethod
    def _lookup_by_description(session, description: str | None) -> int | None:
        """这句描述以前被确认成过哪个食材（05.6 的那条记忆）。

        ⚠️ **历史上指向过不止一个食材时不选**。05.6 的原话是「一句描述历史上指向
        过多个食材时只排序、**不猜**」——那种情况下候选列表会把几条都摆出来，
        由老板点一下；系统替他挑一个，挑错了是看不出来的。

        这条**不受 10.C 的单据类别闸门影响**吗？受：调用方在手写件上根本不会
        走到这里。但它本身的可信度与关键词不同——它是老板亲手确认过的事实，
        所以排在关键词之前。
        """
        if not description or not description.strip():
            return None
        normalized = normalize_name(description)
        if not normalized:
            return None
        rows = PurchaseRepository(session).description_history(
            normalized, limit=DESCRIPTION_MEMORY_LIMIT
        )
        found = {row.food_item_id for row in rows}
        return next(iter(found)) if len(found) == 1 else None

    @staticmethod
    def _lookup_by_keywords(session, food_items: FoodItemRepository, keywords):
        """关键词 → 已存在的食材。英文名、中文名，以及**被合并掉的旧叫法**。

        旧叫法那一条是 10.3 的产物：老板亲手把两条重复食材并过一次，那是比任何
        相似度都确定的一次纠正，这里直接用上（10.6）。
        """
        for value in (keywords.name, keywords.name_zh):
            if not value:
                continue
            normalized = normalize_name(value)
            if not normalized:
                continue
            found = food_items.find_by_normalized_name(normalized)
            if found is not None:
                return found
            found = food_items.find_by_normalized_name_zh(normalized)
            if found is not None:
                return found
            merge = merged_name_lookup(session, normalized)
            if merge is not None:
                found = food_items.get(merge.target_food_item_id)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _uncategorized_id(categories: CategoryRepository) -> int | None:
        """「未分类」那条分类的 id。按 `name_normalized` 找，**不拿显示名做判据**
        ——老板改个显示名（`imp_notes.md` 允许）就全乱了。"""
        row = categories.find_by_normalized_name(UNCATEGORIZED_NAME_NORMALIZED)
        return row.id if row is not None else None

    @staticmethod
    def _guess_category_id(
        categories: CategoryRepository, keywords, fallback: int | None
    ) -> int | None:
        """关键词头词 → 分类 id；词表认不出、或那条分类被改名/删了，就落未分类。

        ⚠️ **查不到就落未分类，绝不按词表去新建分类**。真实样本上算过：按描述新建
        类别的话，NewMoys 一家肉铺 5 页 13 行就会造出 7 个类别，而这 7 个全部属于
        「肉类」——10.A 的收口面板会就此退化成行列表的重印。
        """
        key = guess_category_key(keywords)
        if key is not None:
            row = categories.find_by_normalized_name(key)
            if row is not None:
                return row.id
        return fallback

    def _create_from_keywords(
        self, session, keywords, *, base_unit: str, category_id: int | None
    ) -> int | None:
        """自动建一条食材并标 `needs_review`。名字撞了就改用已存在的那一条。

        撞名在同一张单据上是会发生的：真实样本里 Target 小票 14 行有 13 个不同
        描述、Lowe's 4 行有 3 个——剩下的就是重复行。同一个事务里前一行刚建好的
        食材，后一行查得到（`create_food_item` 会 flush），所以**正常路径不会撞**；
        这个分支挡的是「英文名没撞、中文名撞了」那种。
        """
        if category_id is None:
            # 「未分类」被删了（本该删不掉，见 `delete_category` 的守卫）。
            # 没有分类就建不出食材，但这不该让整张单据失败——退回"让老板填"。
            logger.warning("no uncategorized category available; skipping auto-create")
            return None
        service = IngredientService(self._database)
        try:
            created = service.create_food_item(
                name=keywords.name or keywords.name_zh,
                name_zh=keywords.name_zh if keywords.name else None,
                category_id=category_id,
                base_unit=base_unit,
                needs_review=True,
                session=session,
            )
            return created.id
        except ConflictError as error:
            existing = (error.details or {}).get("existing") or {}
            if existing.get("id"):
                return existing["id"]
            # 唯一约束兜底那条路径（查重与写入之间被别人插了一条）不带 `existing`，
            # 所以自己再查一次。拿不到就返回 None——宁可让老板自己选，也不要把
            # 这一行挂到一个猜出来的食材上。
            found = FoodItemRepository(session).find_by_normalized_name(
                normalize_name(keywords.name or keywords.name_zh or "")
            )
            return found.id if found is not None else None
        except ValidationError:
            # 关键词超长、或单位在这条路径上又被拒——不让一行的猜测毁掉整张单据。
            logger.warning("auto-create from keywords refused", exc_info=True)
            return None

    def run_pending(self, *, limit: int = 1) -> int:
        """取并跑掉最多 `limit` 条排队任务。worker 循环调它，返回实际跑了几条。

        ⚠️ **占用与执行必须分开提交**：`claim_next()` 在一个短事务里把任务置成
        `running` 并提交，之后才去调模型。否则两个 worker 线程会取到同一条任务，
        同一页图被调两遍——花两份钱，还可能产出两套草稿行。
        """
        done = 0
        for _ in range(max(1, limit)):
            with self._database.unit_of_work() as session:
                job = ExtractionJobRepository(session).claim_next()
                if job is None:
                    break
                context = self._context_of(session, job)
            try:
                self._execute(context)
            except AppError:
                logger.exception("extraction job %s could not run", context.job_id)
            done += 1
        return done

    def recover_stale(self) -> list[ExtractionJobView]:
        """把卡在 `running` 的任务退回排队（进程被 kill 过）。

        不自动无限重试：退回排队后 worker 会再试一次，仍然失败就按正常失败处理。
        表现上「那张单一直在识别中」是最难发现的故障，所以这条出口必须有。
        """
        recovered: list[ExtractionJobView] = []
        with self._database.unit_of_work() as session:
            repository = ExtractionJobRepository(session)
            for job in repository.stale_running(older_than=STALE_RUNNING_AFTER):
                repository.requeue(
                    job,
                    reason=(
                        f"上一次执行超过 {STALE_RUNNING_AFTER} 没有结束（进程可能被"
                        "重启过），已退回排队重试。原始文件与草稿都没有动。"
                    ),
                )
                recovered.append(ExtractionJobView.of(job))
        return recovered

    # --- 查询 ---------------------------------------------------------------

    def view(self, job_id: int) -> ExtractionJobView:
        with self._database.unit_of_work() as session:
            job = ExtractionJobRepository(session).get(job_id)
            if job is None:
                raise NotFoundError(f"提取任务 #{job_id} 不存在。")
            return ExtractionJobView.of(job)

    def jobs_for(self, invoice_id: int) -> list[ExtractionJobView]:
        with self._database.unit_of_work() as session:
            self._load_invoice(session, invoice_id)
            return [
                ExtractionJobView.of(job)
                for job in ExtractionJobRepository(session).for_invoice(invoice_id)
            ]

    # --- 供应商路由 ---------------------------------------------------------

    def routes(self) -> list[OcrRouteView]:
        with self._database.unit_of_work() as session:
            return [OcrRouteView.of(item) for item in OcrRouteRepository(session).list_all()]

    def set_route(
        self,
        *,
        source_name: str,
        doc_class: str,
        date_order: str | None = None,
        note: str | None = None,
    ) -> OcrRouteView:
        """人工指定一家供应商的提取路径（以及日/月顺序）。"""
        cleaned = optional_text(source_name, field="source_name", max_length=120)
        if not cleaned:
            raise ValidationError(
                "要指定路由必须给出供应商名称。", details={"field": "source_name"}
            )
        if doc_class not in (
            DOC_CLASS_DIGITAL_PDF,
            DOC_CLASS_PRINTED_SCAN,
            DOC_CLASS_HANDWRITTEN_SCAN,
        ):
            raise ValidationError(
                f"doc_class={doc_class!r} 无效。可选："
                f"{DOC_CLASS_DIGITAL_PDF}、{DOC_CLASS_PRINTED_SCAN}、"
                f"{DOC_CLASS_HANDWRITTEN_SCAN}。",
                details={"field": "doc_class"},
            )
        if date_order is not None and date_order not in ("", "strict", "mdy", "dmy"):
            raise ValidationError(
                f"date_order={date_order!r} 无效。可选：strict、mdy、dmy，"
                "或空串表示清除。",
                details={"field": "date_order"},
            )
        with self._database.unit_of_work() as session:
            route = OcrRouteRepository(session).upsert(
                source_name_normalized=normalize_name(cleaned),
                source_name=cleaned,
                doc_class=doc_class,
                decided_from=ROUTE_FROM_MANUAL,
                date_order=date_order,
                note=note,
            )
            return OcrRouteView.of(route)

    def remember_route(
        self, *, source_name: str | None, doc_class: str, decided_from: str
    ) -> None:
        """记下「这家供应商走哪条路」。供应商名为空时什么都不做。

        供应商名是分流的键。手写件上供应商名常常也认不出来，那时就记不下来——
        于是这家下次仍然走兜底判别。这是实话，不是缺陷：没有键就没有路由。
        """
        if not source_name or not source_name.strip():
            return
        with self._database.unit_of_work() as session:
            OcrRouteRepository(session).upsert(
                source_name_normalized=normalize_name(source_name),
                source_name=source_name.strip()[:120],
                doc_class=doc_class,
                decided_from=decided_from,
            )

    # --- 计划构建 -----------------------------------------------------------

    def _build_plan(
        self,
        *,
        storage_key: str | None,
        original_name: str | None,
        file_id: int | None,
        source_name: str | None,
        source_name_normalized: str | None,
    ) -> ExtractionPlan:
        if storage_key is None:
            raise ValidationError("这条提取任务没有对应的原始文件。")
        content = self._read_file(storage_key)

        if self._recognizer.is_fake:
            return self._plan_from_fake(content, file_id=file_id, storage_key=storage_key)

        fmt = sniff_format(content)
        if fmt not in RECOGNIZABLE_FORMATS:
            raise FormatError(
                f"{original_name or '文件'} 的格式（{fmt}）不能识别。"
                "原始文件已经保存，可以对着它手工加行。"
            )

        route = self._route_for(source_name_normalized)
        if fmt is DocumentFormat.PDF:
            return self._plan_from_pdf(
                content,
                original_name=original_name,
                file_id=file_id,
                route=route,
                source_name=source_name,
            )
        return self._plan_from_image(
            content,
            original_name=original_name,
            file_id=file_id,
            page=1,
            route=route,
            source_name=source_name,
        )

    def _plan_from_fake(
        self, content: bytes, *, file_id: int | None, storage_key: str | None
    ) -> ExtractionPlan:
        """Fake 识别器的路径：**跳过全部图像分析**。

        为什么单列一条分支而不是走通用路径：图像解码、笔画判别、格线检测这三步
        存在的理由都是服务真实模型调用——Fake 识别器**根本不看文件内容**
        （它只算一个摘要用于追溯，对任何输入返回同样的结构）。让它去解码一张
        测试夹具里几十字节的假文件只会得到一个解码失败，而那个失败毫无信息量。

        这不是"两套真相"：`is_fake` 本来就是契约的一部分
        （`contracts.py` 第 5 条「假数据必须自报」），整条链路都按它分支。

        ⚠️ **产出仍然带着 `is_fake=True` 一路传到接口响应**——演示时不能让假数据
        看起来像真识别。
        """
        request = RecognitionRequest(
            content=content,
            content_type=None,
            original_name=None,
            source_key=storage_key,
        )
        result = self._recognizer.recognize(request)
        plan = ExtractionPlan(
            doc_class=DOC_CLASS_UNKNOWN,
            route_source=ROUTE_FROM_MANUAL,
            provider=result.provider,
            models=[result.model] if result.model else [],
            prompt_sha12=None,
            pages_total=max(1, result.page_count),
            pages_done=max(1, result.page_count),
            warnings=list(result.warnings)
            + [
                "⚠️ 本次提取走的是 **Fake 识别器**：跳过了图像解码、笔画判别与格线"
                "检测（它们只服务真实模型调用），产出的每一条文本都带 FAKE 标记。"
                "**这不能说明 OCR 可用。**"
            ],
        )
        plan.header = self._header_from(result, date_order=DATE_ORDER_STRICT)
        plan.raw_document_fields = self._raw_document_json(
            result, doc_class=DOC_CLASS_UNKNOWN
        )
        for line in result.lines:
            plan.lines.append(
                self._draft_from_fields(
                    {name: line.get(name) for name in LINE_FIELDS},
                    page=line.page or 1,
                    file_id=file_id,
                    doc_class=DOC_CLASS_UNKNOWN,
                    prefill=PREFILL_NONE,
                    grid=None,
                    extras={},
                    line_text=line.raw_text,
                    model_values={},
                    agreements={},
                    provider=result.provider,
                    models=[],
                    date_order=DATE_ORDER_STRICT,
                )
            )
        return plan

    def _route_for(self, source_name_normalized: str | None) -> OcrRoute | None:
        if not source_name_normalized:
            return None
        with self._database.unit_of_work() as session:
            route = OcrRouteRepository(session).get(source_name_normalized)
            if route is None:
                return None
            session.expunge(route)
            return route

    def _plan_from_pdf(
        self,
        content: bytes,
        *,
        original_name: str | None,
        file_id: int | None,
        route: OcrRoute | None,
        source_name: str | None,
    ) -> ExtractionPlan:
        """PDF：先试文字层，解析不出表头才按扫描页送 OCR。"""
        inspection = inspect_pdf(content)
        if inspection.page_count > MAX_PAGES_PER_JOB:
            raise ValidationError(
                f"这份 PDF 有 {inspection.page_count} 页，超过一次提取 "
                f"{MAX_PAGES_PER_JOB} 页的上限。请按单据拆开再传。"
            )

        text_plan = self._plan_from_text_layer(
            content, inspection=inspection, file_id=file_id, route=route
        )
        if text_plan is not None:
            if source_name:
                self.remember_route(
                    source_name=source_name,
                    doc_class=DOC_CLASS_DIGITAL_PDF,
                    decided_from=ROUTE_FROM_PDF_TEXT_LAYER,
                )
            return text_plan

        images = extract_page_images(content)
        usable = [item for item in images if item.payload]
        if not usable:
            reasons = [note for item in images for note in item.notes]
            raise ValidationError(
                "这份 PDF 既解析不出可用的文字层，也取不出整页图像，因此没有产出"
                "任何草稿行。原始文件已保存，可以对着它手工加行。"
                + ("具体原因：" + "；".join(reasons) if reasons else "")
            )

        plan: ExtractionPlan | None = None
        for item in usable[:MAX_PAGES_PER_JOB]:
            page_plan = self._plan_from_image(
                item.payload,
                original_name=original_name,
                file_id=file_id,
                page=item.page,
                route=route,
                source_name=source_name if item.page == usable[0].page else None,
                extra_warnings=tuple(item.notes),
            )
            plan = page_plan if plan is None else _merge_plans(plan, page_plan)
        assert plan is not None
        plan.pages_total = len(images)
        skipped = [item for item in images if not item.payload]
        for item in skipped:
            plan.warnings.extend(item.notes)
        return plan

    def _plan_from_text_layer(
        self,
        content: bytes,
        *,
        inspection,
        file_id: int | None,
        route: OcrRoute | None,
    ) -> ExtractionPlan | None:
        """试着从文字层解析。解析不出表头就返回 None，由调用方改走扫描页。

        ⚠️ 判据是**真的解析出了表头与行**，不是「文本长度超过某个阈值」。
        一张扫描件 PDF 也可能带着页眉水印与页码，长度轻松过阈值而正文一个字没有。
        """
        if not inspection.text_pages:
            return None
        date_order = (route.date_order if route else None) or DATE_ORDER_STRICT
        plan = ExtractionPlan(
            doc_class=DOC_CLASS_DIGITAL_PDF,
            route_source=ROUTE_FROM_PDF_TEXT_LAYER,
            provider=TEXT_LAYER_PROVIDER,
            models=[],
            prompt_sha12=None,
            pages_total=inspection.page_count,
        )
        parsed_any = False
        for number in inspection.text_pages:
            facts = inspection.page(number)
            if facts is None:  # pragma: no cover
                continue
            fragments = read_text_fragments(content, page=number)
            parse = parse_text_layout(
                fragments,
                page=number,
                page_width=facts.width,
                page_height=facts.height,
            )
            plan.warnings.extend(f"第 {number} 页：{note}" for note in parse.notes)
            if not parse.header_found:
                continue
            parsed_any = True
            plan.pages_done += 1
            if not plan.header:
                plan.header = self._header_from(parse.result, date_order=date_order)
                plan.raw_document_fields = self._raw_document_json(
                    parse.result, doc_class=DOC_CLASS_DIGITAL_PDF
                )
            for offset, line in enumerate(parse.result.lines):
                extras = (
                    parse.extra_fields[offset]
                    if offset < len(parse.extra_fields)
                    else {}
                )
                plan.lines.append(
                    self._draft_from_fields(
                        {name: line.get(name) for name in LINE_FIELDS},
                        page=number,
                        file_id=file_id,
                        doc_class=DOC_CLASS_DIGITAL_PDF,
                        prefill=PREFILL_TEXT_LAYER,
                        grid=None,
                        extras=extras,
                        line_text=line.raw_text,
                        model_values={},
                        agreements={},
                        provider=TEXT_LAYER_PROVIDER,
                        models=[],
                        date_order=date_order,
                    )
                )
        if not parsed_any:
            return None
        plan.warnings.insert(
            0,
            "走的是**数字 PDF 文字层**路径：字符来自文件本身，没有调用任何 OCR 服务"
            "（近 0 耗时、零成本）。⚠️ 文字层里没有「认不出」这种状态——要么有"
            "字符，要么纸上没有，所以空值一律是 missing 而不是 unreadable。",
        )
        return plan

    def _plan_from_image(
        self,
        content: bytes,
        *,
        original_name: str | None,
        file_id: int | None,
        page: int,
        route: OcrRoute | None,
        source_name: str | None,
        extra_warnings: tuple[str, ...] = (),
    ) -> ExtractionPlan:
        """扫描页：分流 → 格线 → （手写件先问结构）→ 内容识别 → 裁决 → 草稿。"""
        decoded = decode_image(content, original_name=original_name)
        warnings: list[str] = list(extra_warnings) + list(decoded.notes)

        doc_class, route_source, route_note = self._classify(decoded, route)
        warnings.append(route_note)
        date_order = (route.date_order if route else None) or DATE_ORDER_STRICT
        if date_order == DATE_ORDER_STRICT:
            warnings.append(
                "这家供应商没有设置日/月顺序，所以 `6/11/2026` 这类写法按本项目"
                "默认的**月/日/年**读（docs/multifiles.md 第 15 节）。"
                "这家确实写日/月/年时，在供应商路由上设 date_order=dmy。"
            )

        grid = self._detect_grid(decoded, warnings)
        anchor, ink = self._anchor_page(decoded, grid, warnings)

        # 第 1 层：手写件先问结构，不读内容。
        structure = None
        if doc_class in (DOC_CLASS_HANDWRITTEN_SCAN, DOC_CLASS_UNKNOWN):
            structure = self._ask_structure(decoded, page=page, warnings=warnings)

        outcome, result, models, provider = self._recognize(
            decoded, page=page, warnings=warnings
        )

        row_cap = self._row_cap(
            grid, structure, warnings, reported=len(result.lines), ink=ink
        )
        rows_for_lines = self._rows_for_lines(anchor, ink, row_cap, warnings)
        # ⚠️ `unknown` **不再**与手写件同等对待。原先两者一起走「一律不预填」，
        # 代价是一张印刷件只要笔画指标落在中间带，就算两个模型逐字读出同一个值，
        # 界面上也是 15 个空格子——老板对着一张读得完全正确的单子从零手打。
        # 手写件那条最硬的规矩不动：那里模型读出的值没有可信来源。
        prefill = (
            PREFILL_SUPPRESSED_HANDWRITTEN
            if doc_class == DOC_CLASS_HANDWRITTEN_SCAN
            else (PREFILL_CROSSCHECK if models else PREFILL_NONE)
        )
        if doc_class == DOC_CLASS_UNKNOWN and prefill == PREFILL_CROSSCHECK:
            warnings.append(
                "⚠️ **这一页的印刷/手写判别落在中间带**（doc_class=unknown），"
                "按交叉一致预填：只有两个模型逐字读出同一个值的字段才进草稿，"
                "分歧的一律留空等人填。预填值在界面上标成**待核对**，不是已核实。"
                "判别器如果把这家供应商判错了，长期解法是在供应商路由上显式设 "
                "doc_class，而不是让所有判不出的单据都退回全手打。"
            )
        if prefill == PREFILL_SUPPRESSED_HANDWRITTEN:
            warnings.append(
                "⚠️ **手写件：所有字段一律不预填。** 步骤 07 确认连中文母语使用者"
                "都认不出这些连笔速记，模型读出的任何值都没有可信来源。模型在这里"
                "只提供原文与行顺序；两个模型各自读到的原文"
                "保留在 raw_fields 里供对照，但**不作为草稿值**。"
            )

        plan = ExtractionPlan(
            doc_class=doc_class,
            route_source=route_source,
            provider=provider,
            models=list(models),
            prompt_sha12=CONTENT_PROMPT_SHA12 if models else None,
            pages_total=1,
            pages_done=1,
            warnings=warnings,
        )
        plan.header = self._header_from(
            result, date_order=date_order, suppress=prefill == PREFILL_SUPPRESSED_HANDWRITTEN
        )
        plan.raw_document_fields = self._raw_document_json(
            result, doc_class=doc_class, outcome=outcome
        )

        for offset, line in enumerate(result.lines):
            if offset >= row_cap:
                break
            verdicts = (
                outcome.line_agreements[offset]
                if outcome is not None and offset < len(outcome.line_agreements)
                else {}
            )
            printed_row = (
                rows_for_lines[offset] if offset < len(rows_for_lines) else None
            )
            plan.lines.append(
                self._draft_from_fields(
                    {name: line.get(name) for name in LINE_FIELDS},
                    page=page,
                    file_id=file_id,
                    doc_class=doc_class,
                    prefill=prefill,
                    grid=grid,
                    anchor=anchor,
                    ink=ink,
                    printed_row=printed_row,
                    extras={},
                    line_text=line.raw_text,
                    model_values={
                        name: tuple(verdict.values) for name, verdict in verdicts.items()
                    },
                    agreements={
                        name: verdict.note for name, verdict in verdicts.items()
                    },
                    provider=provider,
                    models=list(models),
                    date_order=date_order,
                )
            )
        if source_name:
            self.remember_route(
                source_name=source_name, doc_class=doc_class, decided_from=route_source
            )
        return plan

    def _classify(self, decoded, route: OcrRoute | None) -> tuple[str, str, str]:
        """定这一页走哪条路。返回（类别, 依据, 一句说明）。"""
        if route is not None and route.doc_class in (
            DOC_CLASS_PRINTED_SCAN,
            DOC_CLASS_HANDWRITTEN_SCAN,
        ):
            return (
                route.doc_class,
                ROUTE_FROM_SUPPLIER,
                f"按供应商路由走 {route.doc_class}（这家上次是 {route.decided_from} "
                "定下来的）。供应商是固定的，所以这是最可靠的分流依据。",
            )
        profile = profile_ink(decoded.image)
        if profile.style is InkStyle.PRINTED:
            return (
                DOC_CLASS_PRINTED_SCAN,
                ROUTE_FROM_LOCAL_DISCRIMINATOR,
                f"本地笔画判别：{profile.reason}"
                f"（iqr/med={profile.iqr_ratio:.2f}、cv={profile.cv:.2f}）"
                "⚠️ 这个判别器只在 6 张印刷件、2 家供应商上标定过，cv 的间隔只有 3%；"
                "它只是新供应商首次出现时的兜底，真正可靠的是供应商路由。",
            )
        if profile.style is InkStyle.NON_UNIFORM:
            return (
                DOC_CLASS_HANDWRITTEN_SCAN,
                ROUTE_FROM_LOCAL_DISCRIMINATOR,
                f"本地笔画判别：{profile.reason}",
            )
        return (
            DOC_CLASS_UNKNOWN,
            ROUTE_FROM_LOCAL_DISCRIMINATOR,
            f"本地笔画判别判不出来：{profile.reason} 按**最保守**那条路处理"
            "（不预填），而不是按最省钱那条。",
        )

    # --- 批次 2：锚定与墨迹闸门 ---------------------------------------------

    def _anchor_page(
        self, decoded, grid: PageGrid | None, warnings: list[str]
    ) -> tuple[RowAnchor, InkGate]:
        """建立「行带 ↔ 印刷行号」的锚定，再跑墨迹闸门。

        三件事的依赖关系是一条链，断在哪里就停在哪里，**不往下猜**：

        1. 没有格线 → 没有行带 → 什么都做不了；
        2. 有行带但读不出行号（这一家的纸上没印行号，或两个模型对不上）→
           锚定不成立 → 无法判定每条商品行的墨迹；
        3. 锚定成立 → 知道哪几条行带是数据行 → 墨迹闸门才有**页内配准**的基础
           （同一列的数据格是同一个印刷方框，逐像素中位数就是空格子模板）。

        ⚠️ 第 3 步对第 2 步的依赖正是批次 1 没能做成墨迹判定的原因之一：步骤 07 的
        「跨页同序号行带中位数」之所以失败，一半就是因为不同页检测到的行带数是
        21 / 24 / 25 三种，序号根本对不上。
        """
        blank_ink = InkGate(determined=False, reason="未尝试")
        if grid is None or not grid.determined:
            return (
                RowAnchor(established=False, reason="格线未判定，没有行带可锚定。"),
                InkGate(determined=False, reason="格线未判定，没有格子可判墨迹。"),
            )
        if grid.row_capacity < MIN_ANCHOR_BANDS:
            reason = (
                f"这一页只切出 {grid.row_capacity} 条行带（少于 {MIN_ANCHOR_BANDS} 条），"
                "不是那种印着行号的预印表格——不发请求去试。"
            )
            warnings.append(f"行号锚定：{reason}")
            return (
                RowAnchor(established=False, reason=reason),
                InkGate(determined=False, reason="没有锚定，墨迹闸门缺少数据行范围。"),
            )

        reader = getattr(self._recognizer, "read_row_numbers", None)
        if reader is None:
            reason = "当前识别器不支持读行号列（fake 通路）。"
            return RowAnchor(established=False, reason=reason), blank_ink

        try:
            tiles = row_number_tiles(decoded.image, grid)
        except GridError as error:
            reason = f"拼不出行号列：{error}"
            warnings.append(f"行号锚定：{reason}")
            return RowAnchor(established=False, reason=reason), blank_ink

        try:
            readings = reader(self._tile_request(tiles))
        except OcrServiceError as error:
            reason = f"读行号列的调用失败（{error.code}）：{error}"
            warnings.append(f"行号锚定：{reason} 本页无法运行墨迹校验。")
            return RowAnchor(established=False, reason=reason), blank_ink

        anchor = row_anchor_from(
            list(readings), band_indexes=tuple(band.index for band in grid.rows)
        )
        if not anchor.established:
            warnings.append(
                f"⚠️ 行号锚定**未建立**：{anchor.reason} 本页无法运行墨迹校验。"
            )
            return anchor, InkGate(
                determined=False, reason="没有锚定，不知道哪几条行带是数据行。"
            )
        warnings.append(
            f"行号锚定成立：最左列读出 1—{anchor.printed_rows} 号，分别落在行带 "
            f"{list(anchor.bands)}；表头带是第 {anchor.header_band} 条。"
            "⚠️ 两个模型逐格读法完全一致才算数。"
        )

        ink = self._ink_gate(decoded, grid, anchor, warnings)
        return anchor, ink

    def _ink_gate(
        self, decoded, grid: PageGrid, anchor: RowAnchor, warnings: list[str]
    ) -> InkGate:
        """第 0 层：页内配准的墨迹闸门，判每一条数据行是不是空的。"""
        bands = tuple(
            band for band in grid.rows if band.index in set(anchor.bands)
        )
        try:
            gate = gate_rows(decoded.image, grid, bands)
        except InkError as error:
            warnings.append(f"⚠️ 墨迹闸门未判定：{error} 本页退回只用行容量作上限。")
            return InkGate(determined=False, reason=str(error))
        if not gate.determined:
            warnings.append(f"⚠️ 墨迹闸门未判定：{gate.reason}")
            return gate
        written = [anchor.bands.index(index) + 1 for index in gate.inked]
        warnings.append(
            f"第 0 层（墨迹闸门，本地逐像素，不调模型）：{len(anchor.bands)} 条数据行里 "
            f"{len(gate.inked)} 行有墨，商品行号 {written}。"
            "⚠️ 这是**我们自己算出来的事实**——同一列的数据格是同一个印刷方框，"
            "逐像素取中位数得到空格子模板再相减，判不准时往「有墨」偏。"
        )
        return gate

    def _tile_request(self, tiles) -> RecognitionRequest:
        """把拼图编码成一次识别请求。拼图是 PNG——无损，不会把淡笔画压没。"""
        buffer = io.BytesIO()
        tiles.convert("RGB").save(buffer, format="PNG")
        return RecognitionRequest(
            content=buffer.getvalue(),
            content_type="image/png",
            original_name=None,
            source_key=None,
        )

    def _detect_grid(self, decoded, warnings: list[str]) -> PageGrid | None:
        try:
            grid = detect_grid(decoded.image)
        except GridError as error:
            warnings.append(f"格线检测失败：{error} 本页无法计算行容量与墨迹证据。")
            return None
        if not grid.determined:
            warnings.append(
                f"格线检测未判定：{grid.reason} 因此本页**没有行容量上限**，"
                "请直接看整页原件核对行数。"
            )
            return grid
        warnings.append(
            f"格线检测：去斜 {grid.skew_degrees:+.2f}°、{grid.rule_count} 条横线、"
            f"行容量 {grid.row_capacity}、{len(grid.columns)} 个列带。"
        )
        return grid

    def _ask_structure(self, decoded, *, page: int, warnings: list[str]):
        """第 1 层：先问「哪几行写了东西」。失败不影响后续，只是少一个上限。"""
        asker = getattr(self._recognizer, "recognize_structure", None)
        if asker is None:
            return None
        request = RecognitionRequest(
            content=decoded.payload,
            content_type=decoded.media_type,
            original_name=None,
            source_key=None,
        )
        try:
            answer = asker(request)
        except OcrServiceError as error:
            warnings.append(
                f"第 1 层（先问结构）调用失败（{error.code}）：{error} "
                "本页改为只用行容量作上限。"
            )
            return None
        if not answer.parsed:
            warnings.append(f"第 1 层：{answer.note}")
            return None
        warnings.append(
            f"第 1 层（先定结构，不读内容）：模型说写了东西的行是 "
            f"{list(answer.written_rows)}，表格印着 {answer.printed_row_slots} 个行格，"
            f"handwritten={answer.handwritten}。这个回答**只当上限之一**用："
            "它比模型报出的行数少时会截断，那时另给一条警告。格线检测的行容量"
            "不再参与截断——它判错过，而删掉一行真实采购是看不见的错误。"
        )
        return answer

    def _recognize(self, decoded, *, page: int, warnings: list[str]):
        """第 2 层：内容识别。多模型时并行跑并逐字段裁决。"""
        request = RecognitionRequest(
            content=decoded.payload,
            content_type=decoded.media_type,
            original_name=None,
            source_key=None,
        )
        if isinstance(self._recognizer, CrossCheckRecognizer):
            outcome: CrossCheckOutcome = self._recognizer.cross_check(request)
            result = outcome.result
            warnings.extend(result.warnings)
            warnings.append(
                f"第 2 层（多模型并行读）：{len(self._recognizer.models)} 个模型，"
                f"{outcome.disagreement_count} 个字段有分歧 → 这些字段置 UNREADABLE "
                "必须人填。⚠️ 一致也不等于对：实测过两个模型把 `5 85` 一致读成 "
                "`185`（差 20 倍）。"
            )
            return outcome, result, list(self._recognizer.models), result.provider
        result = self._recognizer.recognize(request)
        warnings.extend(result.warnings)
        models = list(getattr(self._recognizer, "models", ()))
        if not self._recognizer.is_fake and len(models) < 2:
            warnings.append(
                "⚠️ 当前只配了 1 个模型，**没有交叉验证这道防线**。"
                "步骤 07 实测多模型交叉是必需的防线，不是可选增强。"
            )
        return None, result, models, result.provider

    @staticmethod
    def _band_for(grid: PageGrid | None, anchor: RowAnchor | None, printed_row: int | None):
        """印刷行号 → 行带对象。任何一环缺失都返回 None，**不退而求其次**。"""
        if grid is None or not grid.determined or anchor is None or printed_row is None:
            return None
        if not anchor.established:
            return None
        index = anchor.band_of(printed_row)
        return None if index is None else grid.row(index)

    def _rows_for_lines(
        self, anchor: RowAnchor, ink: InkGate, row_cap: int, warnings: list[str]
    ) -> list[int]:
        """第 k 条草稿行对应**第几号商品行**（印刷行号）。对不上就返回空列表。

        ⚠️ **判据是「前 n 行有墨，且正好是第 1…n 号」，不是「按顺序凑」。**

        这条判据是**自验的**：它只在墨迹闸门算出的有墨行里、**最靠上的那一段恰好是
        1、2、…、n**（n = 进草稿的行数）时才成立。少一行、跳一行、或者第 1 号行没墨，
        三种情形都不满足，于是自动落到「不建立」。

        为什么不要求「有墨行数 == 草稿行数」：纸面上**有墨的行不等于商品行**。实测
        ChangShun 那一叠单据几乎每页都在商品行下面划一条作废斜线、写一个合计、
        压一个签名——闸门判它们有墨是**对的**（它管的是「这一格是不是空的」），
        但按个数相等去要求，17 页里只有 1 页能建立。改成前缀判据之后，12 页判定成功
        的全部成立，而且逐页核对过：ChangShun p08 纸面正好 5 行商品，第 6—7 行是
        斜线与合计 124.30，有墨行 [1…7] 的前 5 个正是那 5 行商品。

        ⚠️ **这条判据依赖一个假设：商品行从第 1 行起自上而下连续填写。** 这批样本
        （TangFa 12 页 + ChangShun 12 页，共 24 页判定成功）**无一例外**，但它是
        纸面习惯，不是我们能算出来的事实。它唯一挡不住的情形是：第 1 行有一点与商品
        无关的笔迹，而真正的商品行从第 2 行开始——那样整条对应会**整体差一行**。
        这一条写进 `impl_record.md` 步骤 08 第 12 节的边界里。

        配错一行的后果是静默的：老板照着**相邻那一行**的字迹选食材，不会有任何报错。
        """
        if not anchor.established or not ink.determined or row_cap <= 0:
            return []
        inked = [anchor.bands.index(band) + 1 for band in ink.inked]
        head = inked[:row_cap]
        if head != list(range(1, row_cap + 1)):
            warnings.append(
                f"⚠️ 行↔商品行的对应关系**不建立**：进草稿 {row_cap} 行，而墨迹闸门算出"
                f"有墨的商品行是 {inked}——最靠上的 {row_cap} 行不是第 1…{row_cap} 号。"
                "可能是某一行商品被跳过了，也可能第 1 行上有与商品无关的笔迹。"
                "按顺序硬凑会给错行号证据，所以宁可不给。"
            )
            return []
        if len(inked) > row_cap:
            warnings.append(
                f"墨迹闸门另外判出第 {inked[row_cap:]} 号行也有墨，但模型没报成商品行——"
                "这几行多半是作废斜线、合计或签名。它们不参与对应，也不产出草稿行。"
            )
        warnings.append(
            f"行↔商品行对应建立：第 1—{row_cap} 条草稿行分别是纸面第 {head} 号商品行，"
            "对应关系作为墨迹和行号证据保留。"
        )
        return head

    def _row_cap(
        self,
        grid: PageGrid | None,
        structure,
        warnings: list[str],
        *,
        reported: int,
        ink: InkGate | None = None,
    ) -> int:
        """第 0 层（本批次做到的部分）：行数上限。

        ⚠️ **行容量不再当上限用**，与墨迹闸门同样降级，理由也一样：全量实跑抓到
        了代价。NEW MOY'S p01 纸上 5 行、两个模型都读出 5 行，而格线检测只找到
        8 条横线、算出「行容量 3」，于是 `Beef Eye Round $185.56` 与
        `Duck Feet $167.40` 两条真实采购行被当成「确证的编造」删掉了——合计
        352.96，正好是这张单据对不上的差额。

        行容量原本被称作「能逐像素复核的硬事实」，但它硬的是**检测结果**，不是
        **纸上的事实**：检测本身会把一张 15 行的表判成 3 行。而当初用来论证它的
        那个例子（ChangShun p01 纸上 4 行、`qwen-vl-ocr` 报 15 行）其实也没被它
        拦住——那张表恰好印着 15 个行格，上限是 15。

        所以它改为**记一条警告**：编造出来的行会进草稿，但它看得见——老板对着原件
        发现纸上没这一行，跳过即可，而且每一行本来就要他点确认才入账；被删掉的行
        则没有任何地方会说出来。剩下的硬上限只有 `MAX_LINES_PER_JOB`。
        """
        cap = min(reported, MAX_LINES_PER_JOB)
        if grid is not None and grid.determined and grid.row_capacity:
            if reported > grid.row_capacity:
                warnings.append(
                    f"⚠️ 模型报出 {reported} 行，而格线检测只认出 "
                    f"{grid.row_capacity} 个行格。**两边都可能错**，所以这里既不丢行也不"
                    "补行：{} 行全部进草稿，请对着原件核对行数——纸上没有的行跳过，"
                    "纸上有而这里没有的行用「手工加一行」补。".format(reported)
                )
        if ink is not None and ink.determined and reported > len(ink.inked):
            # ⚠️ **墨迹闸门刻意不当上限用。** 它一度是：有墨行数比模型报的少就截断。
            # 全量实跑抓到了这样做的代价——TangFa p14 纸上第 1 行和第 3 行都写了字
            # （第 3 行是 `4 x30 / 8.99 / 1079.00`），但那一行铅笔极淡，得分只有
            # 0.0233，低于阈值 0.040 被判成空行，于是**一条真实的采购行被悄悄删掉了**。
            #
            # 删掉一行真实数据和填进一个错价格是同一类错误：老板都不会收到任何提示。
            # 所以闸门只做两件事——① 作为证据记下来；② 支撑「行↔商品行」的对应
            # （那条判据是自验的，闸门漏判时它会拒绝建立对应，而不是配错）。
            # **唯一的硬上限仍然是行容量**，那个是表格物理行格数，不依赖笔迹深浅。
            warnings.append(
                f"墨迹闸门判出 {len(ink.inked)} 行有墨，而模型报了 {reported} 行。"
                "⚠️ **这个差额不用来截断**：实测淡铅笔的行会被判成空（TangFa p14 第 3 行"
                "得分 0.0233 < 阈值 0.040），拿它当上限会删掉真实采购行。差额记在这里"
                "供人核对，上限仍然只看行容量。"
            )
        if structure is not None and structure.written_rows:
            written = len(structure.written_rows)
            if written < cap:
                # ⚠️ 这条**仍然截断**（第 1 层是先定结构、不读内容的那一问，手写件上
                # 经过验证），但警告必须带 ⚠️：界面只把带 ⚠️ 且提到丢行的警告摆到
                # 页面上，不带的会留在诊断详情里——那正是这次两条真实采购行被悄悄
                # 删掉而没人看见的原因。
                warnings.append(
                    f"⚠️ 第 1 层说只有 {written} 行写了东西，比模型报出的 {cap} 行少，"
                    f"已按 {written} 行截断。请对着原件核对有没有漏行；缺的用"
                    "「手工加一行」补。"
                )
                cap = written
        if cap < reported:
            warnings.append(
                f"本页最终只产出 {cap} 行（模型报了 {reported} 行）。"
                "⚠️ 截断是**丢弃多报**，不是漏行：上限来自表格的物理行格数。"
            )
        return cap

    # --- 草稿构建 -----------------------------------------------------------

    def _draft_from_fields(
        self,
        fields: dict[str, RecognizedField | None],
        *,
        page: int,
        file_id: int | None,
        doc_class: str,
        prefill: str,
        grid: PageGrid | None,
        anchor: RowAnchor | None = None,
        ink: InkGate | None = None,
        printed_row: int | None = None,
        extras: dict[str, str],
        line_text: str | None,
        model_values: dict[str, tuple[str | None, ...]],
        agreements: dict[str, str | None],
        provider: str,
        models: list[str],
        date_order: str,
    ) -> LineDraft:
        """把一行的识别字段变成一条草稿行。

        三件事同时做到：

        1. **原文全部保留**在 `evidence.fields.<name>.raw_text` 与 `model_values`
           里，入账后永不改动（步骤 05 的口径）。
        2. **规范化显式可见**（08.3e）：全角转半角、去千分位、剥货币符号每一步都
           记进 `review.normalization`，`raw_text` 原样不动。解析不了就留空 +
           记一条问题，**不报错**（报错会把整张单挡在门外）。
        3. **手写件不预填**：`values` 全部为空，但证据照样齐全。
        """
        suppress = prefill == PREFILL_SUPPRESSED_HANDWRITTEN
        values: dict[str, Any] = {}
        evidence_fields: dict[str, Any] = {}
        # ⚠️ 未决问题用**与步骤 06 导入通路相同的结构**（`field` / `code` /
        # `message`）。三条入口写同一张表、也该让前端只认一种形状——
        # 一边给字符串、一边给对象，界面就得分支，而分支迟早会漏掉一边。
        issues: list[dict[str, Any]] = []
        normalization: dict[str, list[str]] = {}

        band = self._band_for(grid, anchor, printed_row)
        for name in LINE_FIELDS:
            found = fields.get(name)
            # 原始识别证据只保留厂商提供的坐标，不再生成格子裁图坐标。
            box, box_source = (
                _box_payload(found) if found is not None else (None, None)
            )
            raw_text = found.raw_text if found is not None else None
            status = str(found.status) if found is not None else str(FieldStatus.MISSING)
            evidence_fields[name] = {
                "status": status,
                "raw_text": raw_text,
                "confidence": found.confidence if found is not None else None,
                "page": (found.page if found is not None else None) or page,
                "box": box,
                "box_source": box_source,
                "model_values": list(model_values.get(name, ())),
                "agreement": agreements.get(name),
            }
            if suppress or raw_text is None:
                continue
            prepared, field_notes, problem = self._prepare_value(
                name, raw_text, date_order=date_order
            )
            if field_notes:
                normalization[name] = field_notes
            if problem:
                issues.append(
                    {
                        "field": name,
                        "code": "unparsable_value",
                        "raw_text": raw_text,
                        "message": problem,
                    }
                )
            if prepared is not None:
                values[name] = prepared

        draft_values = {
            "description": _clip(values.get(FIELD_ITEM_DESCRIPTION), DESCRIPTION_MAX_LENGTH),
            "purchase_unit": _clip(values.get(FIELD_UNIT), PURCHASE_UNIT_MAX_LENGTH),
            "quantity": values.get(FIELD_QUANTITY),
            "unit_price": values.get(FIELD_UNIT_PRICE),
            "line_amount": values.get(FIELD_LINE_AMOUNT),
        }

        # ⚠️ quantity 为 0 必须当可疑处理，不能按步骤 04 允许的「0 元赠品行」放行。
        # 步骤 07 实测两个模型都把 IKEA 小票的**税率代码列当成了数量列**，报出
        # quantity: 0 / 3，而且连带把行金额改成 0 或 329.70 以保持算术自洽——
        # 校验全过。`quantity: 0` 不触发任何算术校验，却让这条采购记录彻底失真。
        if draft_values["quantity"] is not None and draft_values["quantity"] == 0:
            issues.append(
                {
                    "field": "quantity",
                    "code": "suspicious_zero_quantity",
                    "raw_text": (fields.get(FIELD_QUANTITY).raw_text
                                 if fields.get(FIELD_QUANTITY) else None),
                    "message": (
                        "数量识别成 0——⚠️ **按可疑处理，已清空**。实测模型会把税率"
                        "代码列当成数量列报出 `quantity: 0`，并连带把行金额抹成 0 "
                        "以保持算术自洽，校验抓不到。真的是 0 元赠品行请人工填 0。"
                    ),
                }
            )
            draft_values["quantity"] = None

        review: dict[str, Any] = {
            "doc_class": doc_class,
            "prefill": prefill,
            "page": page,
        }
        if printed_row is not None and band is not None:
            # 这条草稿行对应纸面上第几号商品行，供墨迹证据与审核诊断使用。
            review["printed_row"] = printed_row
            review["row_band"] = band.index
            if ink is not None and ink.determined:
                verdict = ink.verdict(band.index)
                if verdict is not None:
                    review["ink"] = verdict.summary()
        if issues:
            review["issues"] = issues
        if normalization:
            review["normalization"] = normalization
        if suppress:
            review["prefill_note"] = (
                "手写件默认不预填：模型读到的原文在 raw_fields 里，但不作为草稿值。"
                "商品名需对照整页原件，从食材库里选。"
            )

        evidence: dict[str, Any] = {
            "line_text": line_text,
            "provider": provider,
            "models": models,
            "prompt_sha12": CONTENT_PROMPT_SHA12 if models else None,
            "fields": evidence_fields,
            "file_id": file_id,
        }
        if extras:
            # 契约之外但真实单据上到处都是：包装规格（步骤 09 的 case/pack 换算要用）、
            # 供应商货号（步骤 10 的映射记忆要用）。与契约字段分开放。
            evidence["extra_fields"] = {
                key: extras[key]
                for key in (EXTRA_FIELD_PACK_SPEC, EXTRA_FIELD_SUPPLIER_CODE)
                if extras.get(key)
            }
        if grid is not None and grid.determined:
            evidence["grid"] = _grid_payload(grid, anchor=anchor, ink=ink)
        if printed_row is not None and band is not None:
            # ⚠️ 放进 `evidence`（= `raw_fields`）而不是只放 `review_metadata`：
            # 审核界面读的是行视图上的 `raw_fields`，`review_metadata` 只有 issues
            # 会被透出来。行号和墨迹结论作为可见证据保留。
            evidence["printed_row"] = printed_row
            evidence["row_band"] = band.index
            if ink is not None and ink.determined:
                verdict = ink.verdict(band.index)
                if verdict is not None:
                    evidence["ink"] = verdict.summary()
        return LineDraft(values=draft_values, evidence=evidence, review=review)

    def _prepare_value(
        self, name: str, raw_text: str, *, date_order: str
    ) -> tuple[Any, list[str], str | None]:
        """把一个原文变成草稿值。返回（值, 改动说明, 问题）。"""
        scale = _FIELD_SCALES.get(name)
        if scale is None:
            # 文本字段（描述、单位）：只做全角转半角，不清洗、不猜。
            folded, notes = fold_width(raw_text)
            return folded.strip() or None, notes, None
        normalized = normalize_number_text(raw_text, number_format=NUMBER_FORMAT_AUTO)
        if not normalized.ok:
            return None, list(normalized.notes), normalized.problem
        value = _try_decimal(normalized.text, scale=scale)
        if value is None:
            return (
                None,
                list(normalized.notes),
                f"{raw_text!r} 规范化成 {normalized.text!r} 之后仍然存不下来"
                f"（小数位超过 {scale} 位会被拒绝而不是四舍五入）。请人工填写。",
            )
        return value, list(normalized.notes), None

    def _header_from(
        self, result: RecognitionResult, *, date_order: str, suppress: bool = False
    ) -> dict[str, Any]:
        """单据头的预填值。手写件同样不预填。"""
        if suppress:
            return {}
        header: dict[str, Any] = {}
        number = _text_of(result.document_field(FIELD_INVOICE_NUMBER))
        if number:
            header["invoice_number"] = number
        total = _text_of(result.document_field(FIELD_INVOICE_TOTAL))
        if total:
            normalized = normalize_number_text(total, number_format=NUMBER_FORMAT_AUTO)
            if normalized.ok:
                parsed = _try_decimal(normalized.text, scale=AMOUNT_SCALE)
                if parsed is not None:
                    header["invoice_total"] = parsed
        date_text = _text_of(result.document_field(FIELD_INVOICE_DATE))
        if date_text:
            # 日/月顺序定不死时按项目默认的月/日/年读（第 15 节）：整理页已经这么
            # 读了，填表页再说一句「读不出来」只会让同一张纸有两种说法。原文仍在
            # `raw_document_fields` 里，预填值是可改的。
            readings = date_text_readings(
                date_text,
                date_order=date_order,
                allow_short_year=True,
                assume_order=DATE_ORDER_ASSUMED,
            )
            if readings.readings:
                header["invoice_date"] = readings.readings[0].text
        supplier = _text_of(result.document_field(FIELD_SUPPLIER))
        if supplier:
            header["source_name"] = supplier
        currency = _text_of(result.document_field(FIELD_CURRENCY))
        if currency:
            header["currency"] = currency.strip().upper()
        return header

    def _raw_document_json(
        self, result: RecognitionResult, *, doc_class: str,
        outcome: CrossCheckOutcome | None = None,
    ) -> str:
        return json.dumps(
            {
                "provider": result.provider,
                "is_fake": result.is_fake,
                "model": result.model,
                "doc_class": doc_class,
                "warnings": list(result.warnings),
                "fields": {
                    name: {
                        "status": str(found.status),
                        "raw_text": found.raw_text,
                        "confidence": found.confidence,
                        "page": found.page,
                        "box": _box_payload(found)[0],
                        "model_values": list(outcome.agreements[name].values)
                        if outcome is not None and name in outcome.agreements else [],
                        "agreement": outcome.agreements[name].note
                        if outcome is not None and name in outcome.agreements else None,
                    }
                    for name in DOCUMENT_FIELDS
                    if (found := result.document_field(name)) is not None
                },
                "raw_response": result.raw_response,
            },
            ensure_ascii=False,
        )

    # --- 读文件与页面图 -----------------------------------------------------

    def _read_file(self, storage_key: str) -> bytes:
        """整份读进内存。

        ⚠️ 要整份读是必须的：识别请求要 base64 编码整张图，PDF 的页面树也要随机
        访问，都没法流式处理。单文件上限由 `UPLOAD_MAX_BYTES` 约束（默认 20 MB）。
        """
        try:
            return self._storage.read(storage_key)
        except FileNotFoundError as error:
            raise NotFoundError(
                f"原始文件不在存储里了（{storage_key}）：{error}"
                "——草稿与任务记录都还在，但没法重新提取。"
            ) from None

    def _page_image(self, content: bytes, *, page: int):
        """取某一页的图像对象（PDF 取内嵌整页图，图片文件直接解码）。"""
        if sniff_format(content) is DocumentFormat.PDF:
            images = extract_page_images(content, pages=(page,))
            usable = [item for item in images if item.payload]
            if not usable:
                raise NotFoundError(f"第 {page} 页没有可用的整页图像，裁不出图。")
            return decode_image(usable[0].payload).image
        return decode_image(content).image

    @staticmethod
    def _load_invoice(session: Session, invoice_id: int) -> Invoice:
        invoice = InvoiceRepository(session).get(invoice_id)
        if invoice is None:
            raise NotFoundError(
                f"单据 #{invoice_id} 不存在。", details={"invoice_id": invoice_id}
            )
        return invoice


# --- 模块级小工具 -----------------------------------------------------------


def _text_of(found: RecognizedField | None) -> str | None:
    return found.raw_text if found is not None and found.is_recognized else None


# 能送去识别的扩展名。`heic` 在内——步骤 05 只存不识别，解码是本步骤的事。
# `csv` / `xlsx` **刻意不在内**：步骤 06 明确「导入路径不调识别器」。
_RECOGNIZABLE_SUFFIXES = frozenset({"jpg", "jpeg", "png", "heic", "pdf"})


def _is_recognizable_name(original_name: str | None) -> bool:
    if not original_name:
        return False
    _, _, suffix = original_name.rpartition(".")
    return suffix.strip().lower() in _RECOGNIZABLE_SUFFIXES if suffix else False


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _grid_payload(
    grid: PageGrid,
    *,
    anchor: RowAnchor | None = None,
    ink: InkGate | None = None,
) -> dict[str, Any]:
    """页面的表格几何 + 锚定结论，原样记进 `raw_fields.grid`。

    ### 批次 2 补上的那一半

    批次 1 这里只给几何、`row_anchor` 固定写 `"unestablished"`，因为哪一条行带对应
    哪一行商品定不下来。批次 2 改成**读最左侧那一列印着的行号**：那是印刷数字，
    模型最准的那一类（96.6%），再用本地规则（长度 / 连号 / 连续 / 两模型一致）卡死。
    成立就把 `row_anchor` 写成 `"row_numbers"` 并给出 `data_bands`；不成立仍然诚实
    写 `"unestablished"`，接口行为退回批次 1。

    ### 批次 1 试过并失败的四条，留在这里免得再试一遍

    ⚠️ **哪一条行带对应哪一行商品，本批次建立不起来。** 2026-11-11 实测过三条
    路，都不行：

    | 试过的锚定方式 | 结果 |
    | --- | --- |
    | 行带序号直接当商品行号 | ❌ 前三条行带是 `NAME` / `ADDRESS` / `数量 QTY.`，真正的第 1 条数据行是**第 4 条行带** |
    | 按行带高度认表头 | ❌ TangFa / ChangShun 的行带高度完全一致（26.4—27.6‰） |
    | 按行带墨迹密度认表头 | ❌ TangFa 表头带 0.0487 反而**低于**数据带 0.1083 |
    | 按「竖线只在数据行里存在」认表头 | ❌ 去斜后竖线位置漂移 1—2 像素，逐列采样覆盖率在表头带与数据带上都是 0.04；ChangShun 的某条竖线则贯穿全表（含表头）始终是 1.0 |

    这与步骤 07 第 4 节撞的是同一堵墙：**只靠像素阈值分不开**，要先做图像配准。

    **给批次 2 的线索**（这一条是有价值的发现，不是失败记录）：这批预印单据的
    最左侧列印着**行号**（1、2、3、4…）。那是**印刷数字**，不是手写——而印刷体
    恰恰是模型最准的那一类（步骤 07 实测 96.6%）。按行号列去锚定，比按像素阈值
    猜表头可靠得多。
    ⚠️ 第四条的失败数据是在**镜像坐标**上量的——`grid._rule_columns` 当时把每条竖线
    都记成了它关于页面中线的镜像位置（批次 2 修掉了，见该函数的说明）。所以
    「表头带与数据带的逐列覆盖率都是 0.04」不是那条路走不通，是**量错了地方**。
    """
    anchored = anchor is not None and anchor.established
    payload: dict[str, Any] = {
        "skew_degrees": grid.skew_degrees,
        "rule_count": grid.rule_count,
        "row_capacity": grid.row_capacity,
        "column_count": len(grid.columns),
        "content_left": grid.content_left,
        "content_right": grid.content_right,
        "table_left": grid.table_left,
        "table_right": grid.table_right,
        "rows": [
            {"index": band.index, "top": band.top, "bottom": band.bottom}
            for band in grid.rows
        ],
        "columns": [
            {"index": band.index, "left": band.left, "right": band.right}
            for band in grid.columns
        ],
        "row_anchor": anchor.source if anchored else "unestablished",
    }
    if anchored:
        payload["data_bands"] = list(anchor.bands)
        payload["header_band"] = anchor.header_band
        payload["row_anchor_note"] = (
            f"最左列印着的行号 1—{anchor.printed_rows} 分别落在行带 "
            f"{list(anchor.bands)}。两个模型逐格读法完全一致、且通过本地的"
            "「长度 / 连号 / 连续」三关才写在这里；任何一关不过就写 unestablished。"
        )
    else:
        payload["row_anchor_note"] = (
            "行带序号与商品行号之间的对应关系**没有建立**"
            + (f"：{anchor.reason}" if anchor is not None else "。")
            + " 因此本页没有逐行墨迹校验结论。"
        )
    if ink is not None:
        payload["ink"] = ink.summary()
    return payload


def _merge_plans(first: ExtractionPlan, second: ExtractionPlan) -> ExtractionPlan:
    """把多页的计划并成一个。

    单据头取**第一页**解析出来的那份：多页 PDF 里每一页都可能印着单号与日期，
    后面的页覆盖前面的只会让结果取决于页序。页间的差异在 warnings 里看得到。
    """
    first.lines.extend(second.lines)
    first.warnings.extend(second.warnings)
    first.pages_done += second.pages_done
    first.pages_total += second.pages_total
    if not first.header:
        first.header = second.header
    if not first.raw_document_fields:
        first.raw_document_fields = second.raw_document_fields
    for key, value in second.usage.items():
        if isinstance(value, (int, float)) and isinstance(first.usage.get(key), (int, float)):
            first.usage[key] += value
        else:
            first.usage.setdefault(key, value)
    if first.doc_class != second.doc_class:
        first.warnings.append(
            f"⚠️ 同一份文件里不同页判成了不同类别（{first.doc_class} 与 "
            f"{second.doc_class}）。整单按第一页的类别记账，但分类不一致本身值得看一眼。"
        )
    return first


__all__ = [
    "ExtractionJobView",
    "ExtractionPlan",
    "ExtractionService",
    "LineDraft",
    "MAX_LINES_PER_JOB",
    "MAX_PAGES_PER_JOB",
    "OcrRouteView",
    "PREFILL_CROSSCHECK",
    "PREFILL_NONE",
    "PREFILL_SUPPRESSED_HANDWRITTEN",
    "PREFILL_TEXT_LAYER",
    "STALE_RUNNING_AFTER",
]
