"""上传整理批次：混合文件 → 按「实际采购日 + 商家」整理（多文件计划 M02）。

本模块是 M02 的**最小读写原语**，不是整条流程：

- **不碰外部服务**：不调 OCR、不读写磁盘。字节由谁保存、怎么切页，是 M03 / M04
  的事；这里只接收「字节已经存好了，引用是这个、摘要是这个」的事实。于是本步骤
  可以用人工构造的内容把迁移与引用关系验到底，不必先有上传界面。
- **不产生候选**：`replace_candidates` 只是写入原语，真正的日期解析与商家建议在
  M05 / M07。
- **不做批量移动与撤销**：那是 M06。这里只有单个内容的分类命令，因为批量命令的
  逐项结果与撤销栈是 M06 要定的契约，提前定一版只会返工。

三条贯穿本模块的规则：

1. **组是算出来的，不是存出来的。** `list_groups()` 调的是一句 `GROUP BY`，键是
   内容上的人工决定值。所以「移动一张卡片」永远只写一行，组自动跟着变——不存在
   「卡片说它在 A 组、组表说它在 B 组」这种状态。
2. **候选永远不会自己变成决定。** 候选写进 `intake_item_candidates`，决定写进
   `intake_items.decided_*`，两条路径在代码里就是分开的两个方法。系统再有把握也
   要人点一下，这是 docs/multifiles.md 第 4 节的要求。
3. **来源不明就是不明。** 商家留空表示「商家待定」，不会被任何建议悄悄填上一个
   编造的名字。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Mapping, Sequence

from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.models import (
    CONFIDENCE_SCALE,
    CONTENT_TYPE_MAX_LENGTH,
    ERROR_CODE_MAX_LENGTH,
    ERROR_MESSAGE_MAX_LENGTH,
    INTAKE_CANDIDATE_BASES,
    INTAKE_CANDIDATE_BASIS_PAPER,
    INTAKE_CANDIDATE_FIELD_PURCHASE_DATE,
    INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
    INTAKE_CANDIDATE_FIELDS,
    INTAKE_FILE_STATUS_DISCARDED,
    INTAKE_FILE_STATUS_FAILED,
    INTAKE_FILE_STATUS_PENDING,
    INTAKE_FILE_STATUS_READY,
    INTAKE_ITEM_KIND_PAGE,
    INTAKE_ITEM_KIND_ROW,
    INTAKE_RECOGNITION_STATUS_DONE,
    INTAKE_RECOGNITION_STATUS_FAILED,
    INTAKE_RECOGNITION_STATUS_PENDING,
    IntakeReviewLink,
    SHA256_LENGTH,
    SHEET_NAME_MAX_LENGTH,
    SOURCE_NAME_MAX_LENGTH,
    STORAGE_KEY_MAX_LENGTH,
    UPLOAD_NAME_MAX_LENGTH,
    IntakeBatch,
    IntakeBatchFile,
    IntakeFile,
    IntakeItem,
    IntakeItemCandidate,
    utcnow,
)
from src.normalization import DATE_ORDER_ASSUMED, DATE_ORDER_DMY, DATE_ORDER_MDY
from src.repositories.intake_moves import IntakeMoveRepository
from src.repositories.intake import (
    IntakeBatchFileRepository,
    IntakeBatchRepository,
    IntakeFileRepository,
    IntakeItemRepository,
)
from src.repositories.invoices import InvoiceFileRepository
from src.services.ingredients import UNSET, normalize_name
from src.services.validation import (
    optional_text,
    require_limit,
    require_offset,
    require_text,
)

logger = logging.getLogger(__name__)

# 批次的派生状态。与 `invoices` 一样不落列：两个时刻就能说清楚，存一列 status
# 反而会和它们打架。
BATCH_STATUS_OPEN: Final[str] = "open"
BATCH_STATUS_CLOSED: Final[str] = "closed"
BATCH_STATUS_DISCARDED: Final[str] = "discarded"

# 一次列表请求最多返回多少个内容。五千行的 CSV 不该一次变成五千张卡片
# （docs/multifiles.md M04），所以读取一律分页。
MAX_ITEMS_PER_PAGE: Final[int] = 200

# 一个批次最多挂多少个文件。与 `invoices` 的 MAX_FILES_PER_INVOICE 不是同一个数：
# 混合上传本来就是「一次十几份」的场景，但也不能让一次误拖把整个下载目录灌进来。
MAX_FILES_PER_BATCH: Final[int] = 200

# 一次最多取多少个「还没切出内容」的文件。与 MAX_FILES_PER_BATCH 同量级：
# 一次把整批取完，驱动队列的那一方不必自己翻页。
MAX_WAITING_FILES: Final[int] = 200

# 一次驱动识别队列最多处理几个内容（多文件计划 M05）。
#
# ⚠️ 比切页的上限小一个数量级，而且理由不同：切页是本地计算，两百份也就是几秒；
# 识别**每页都要花钱**，而扫描件一页约 5—15 秒。一次五十页既不会让一个请求挂十
# 分钟，也让「没跑完的下次接着跑」保持为常态而不是异常——队列本来就是可重复
# 驱动的（`recognition_status` 就是队列）。
MAX_RECOGNITION_BATCH: Final[int] = 50

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_CONFIDENCE_STEP = Decimal(1).scaleb(-CONFIDENCE_SCALE)


# --- 只读视图 -----------------------------------------------------------------


@dataclass(frozen=True)
class IntakeCandidateView:
    """一条系统建议。`evidence` 原样透出，不在这里解释它的内容。"""

    id: int
    field: str
    value: str
    value_normalized: str | None
    basis: str
    confidence: Decimal | None
    evidence: dict[str, Any] | None
    created_at: datetime

    @classmethod
    def of(cls, candidate: IntakeItemCandidate) -> "IntakeCandidateView":
        return cls(
            id=candidate.id,
            field=candidate.field,
            value=candidate.value,
            value_normalized=candidate.value_normalized,
            basis=candidate.basis,
            confidence=candidate.confidence,
            evidence=_maybe_json(candidate.evidence),
            created_at=candidate.created_at,
        )


@dataclass(frozen=True)
class IntakeItemView:
    """一个可归类内容，带回看原件所需的全部坐标。

    `original_name` / `storage_key` / `sha256` 是**回指**，不是副本：它们来自这个
    内容所属的文件引用与那份字节，一次取齐是因为整理页的每张卡片都要能点开原件。

    `recognition_status`（M05）回答「这一页的日期/商家识别跑过了吗」。⚠️ 它与
    「有没有候选」**不是同一件事**：读完发现纸面上没有商家时状态是 `done`、候选
    是 0 条，而没跑过时两者都是空——分不开这两者就只能把整批再送一遍模型。
    """

    id: int
    batch_id: int
    batch_file_id: int
    kind: str
    page_no: int | None
    row_no: int | None
    sheet_name: str | None
    original_name: str
    storage_key: str
    sha256: str
    decided_purchase_date: date | None
    decided_source_name: str | None
    decided_source_name_normalized: str | None
    decided_at: datetime | None
    version: int
    candidates: tuple[IntakeCandidateView, ...]
    handed_off_at: datetime | None = None
    """M08 送审时写上的时刻。不是 null 就意味着这张卡片**不再接受整理命令**：
    改日期/商家要走审核里的分类修订（`PATCH .../reviews/<invoice_id>/classification`）。
    透出它是因为界面否则只能等服务端 409 才知道这件事（多文件计划 M12）。"""
    review_invoice_id: int | None = None
    """正在填的那张草稿。`handed_off_at` 只说「在填表中」，这一列说**在填哪一张**，
    否则用户退出填表页之后，整理页上就没有回到这张草稿的入口（第 11 节 ④）。"""
    review_status: str | None = None
    """那张草稿处在哪一步：`draft` 还在填、`completed` 已收口入库、`voided` 已作废。
    ⚠️ 没有它，卡片只能一律写「编辑中」——已经入库的那张也写「编辑中」就是假话，
    而被删掉的那张会让人去点一个必然失败的「撤回填表」（第 11 节 ③）。"""
    pile_excluded: bool = False
    row_data: dict[str, Any] | None = None
    """M04 存下来的表格行：解析值、单元格原文、逐行问题。页内容是 None。"""
    recognition_status: str | None = None
    recognized_at: datetime | None = None
    recognition_payload: dict[str, Any] | None = None
    recognition_error_code: str | None = None
    recognition_error_message: str | None = None

    @classmethod
    def of(cls, item: IntakeItem) -> "IntakeItemView":
        batch_file = item.batch_file

        return cls(
            id=item.id,
            batch_id=batch_file.batch_id,
            batch_file_id=item.batch_file_id,
            kind=item.kind,
            page_no=item.page_no,
            row_no=item.row_no,
            sheet_name=batch_file.sheet_name,
            original_name=batch_file.original_name,
            storage_key=batch_file.file.storage_key,
            sha256=batch_file.file.sha256,
            decided_purchase_date=item.decided_purchase_date,
            decided_source_name=item.decided_source_name,
            decided_source_name_normalized=item.decided_source_name_normalized,
            decided_at=item.decided_at,
            version=item.version,
            handed_off_at=item.handed_off_at,
            review_invoice_id=item.review_link.invoice_id if item.review_link else None,
            review_status=_review_status(item),
            pile_excluded=item.pile_excluded,
            candidates=_visible_candidates(item),
            row_data=_maybe_json(item.row_data),
            recognition_status=item.recognition_status,
            recognized_at=item.recognized_at,
            recognition_payload=_maybe_json(item.recognition_payload),
            recognition_error_code=item.recognition_error_code,
            recognition_error_message=item.recognition_error_message,
        )


@dataclass(frozen=True)
class IntakeFileRefView:
    """批次里的一个文件：叫什么、排第几、处理到哪一步、切出了几个内容。

    `storage_key` 是**回指那份字节**的引用（M03 要用它读原件并切页）。它不是绝对
    路径——绝对路径随部署变化，取字节一律经文件存储适配器。

    `handed_off_item_count` 是**这份文件有几页已经进了填表**。它不是统计口径，而是
    「这份文件现在还能不能撤掉」的那条理由：不为 0 时 `discard_file` 会 409，界面据此
    提前把「移除」按钮暗下来并说出原因，而不是让人点一下才撞到报错（第 14 节）。
    """

    id: int
    batch_id: int
    intake_file_id: int
    original_name: str
    upload_order: int
    status: str
    item_count: int | None
    sheet_name: str | None
    storage_key: str
    sha256: str
    size: int
    content_type: str | None
    error_code: str | None
    error_message: str | None
    discarded_at: datetime | None
    created_at: datetime
    handed_off_item_count: int

    @classmethod
    def of(cls, batch_file: IntakeBatchFile) -> "IntakeFileRefView":
        return cls(
            id=batch_file.id,
            batch_id=batch_file.batch_id,
            intake_file_id=batch_file.intake_file_id,
            original_name=batch_file.original_name,
            upload_order=batch_file.upload_order,
            status=batch_file.status,
            item_count=batch_file.item_count,
            sheet_name=batch_file.sheet_name,
            storage_key=batch_file.file.storage_key,
            sha256=batch_file.file.sha256,
            size=batch_file.file.size,
            content_type=batch_file.file.content_type,
            error_code=batch_file.error_code,
            error_message=batch_file.error_message,
            discarded_at=batch_file.discarded_at,
            created_at=batch_file.created_at,
            handed_off_item_count=sum(
                1 for item in batch_file.items if item.handed_off_at is not None
            ),
        )


@dataclass(frozen=True)
class IntakeGroupView:
    """一个派生出来的日期/商家组。

    三种组都由同一个查询产生，不是三条特判：两个键都空 = 左侧待归类；只有日期 =
    那一天的「商家待定」；两个键都有 = 一个确定的组。
    """

    purchase_date: date | None
    source_name: str | None
    source_name_normalized: str | None
    item_count: int
    suggested_count: int = 0


@dataclass(frozen=True)
class IntakeBatchView:
    """一个整理批次。`status` 是派生值，数据库里没有这一列。

    `item_count` / `unfinished_item_count` 同样是**派生值**（第 22 节）：界面上那个
    「确认完成所有填写」只有在这一批一项不剩时才按得动，而整理页只加载展开了的那
    几组，数页面上的卡片数不到收起来的组里还剩几项——同「重试识别失败项」要问服务端
    的那条理由（第 18 节 ①）。不另存一列：填没填完由那几页所在的单据说了算。
    """

    id: int
    status: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    discarded_at: datetime | None
    files: tuple[IntakeFileRefView, ...]
    item_count: int = 0
    unfinished_item_count: int = 0

    @classmethod
    def of(cls, batch: IntakeBatch) -> "IntakeBatchView":
        live = [
            batch_file for batch_file in batch.files
            if batch_file.status != INTAKE_FILE_STATUS_DISCARDED
        ]
        items = [item for batch_file in live for item in batch_file.items]
        return cls(
            id=batch.id,
            status=_batch_status(batch),
            created_at=batch.created_at,
            updated_at=batch.updated_at,
            closed_at=batch.closed_at,
            discarded_at=batch.discarded_at,
            files=tuple(IntakeFileRefView.of(batch_file) for batch_file in live),
            item_count=len(items),
            unfinished_item_count=sum(
                1 for item in items if _review_status(item) != "completed"
            ),
        )


@dataclass(frozen=True)
class RegisteredFile:
    """一次文件登记的结果。

    后两个字段是**提示用的事实**，不是拦截结果：登记能走到这里就说明没被拦住。

    - `reused_bytes`：这份字节库里已经有了，没有再存一份（「原件字节只存一次」）。
    - `shared_with_batch_ids`：同一份字节还在哪些别的批次里。M04 用它提示「这份
      CSV 你上个月传过」，免得同一批采购被静默录两遍。
    - `same_bytes_invoice_ids`：同一份字节在**旧单张上传路径**上已有的单据。两条
      路径的判重表是分开的（见 `models.IntakeFile` 的说明），所以跨路径重复只能
      提示，不能互相阻断。
    """

    ref: IntakeFileRefView
    reused_bytes: bool
    shared_with_batch_ids: tuple[int, ...]
    same_bytes_invoice_ids: tuple[int, ...]


@dataclass(frozen=True)
class DiscardedFile:
    """一次文件移除的结果。

    `ref` 是**删掉之前**那条引用的样子——调用方要据此说出「移除了哪一份」。
    `removed_item_count` 是跟着一起删掉的内容数（界面用它说「3 项内容已撤下」）。

    `bytes_still_referenced` 回答「磁盘上那份字节现在能删了吗」：还有别的批次引用
    着就不能删，否则会把别人的原件一起删掉。本模块不删文件，只给出这个事实。
    """

    ref: IntakeFileRefView
    removed_item_count: int
    bytes_still_referenced: bool


@dataclass(frozen=True)
class IntakeItemPage:
    items: tuple[IntakeItemView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class CandidateInput:
    """一条待写入的候选。

    `value` 是展示值：日期用 ISO 文本（`2026-11-08`），商家用纸面写法。规范化形式
    由本模块按商家名的统一口径算出来，不让调用方各算一遍。
    """

    value: str
    confidence: float | Decimal | None = None
    evidence: dict[str, Any] | None = dataclass_field(default=None)


# --- 内部辅助 -----------------------------------------------------------------


def _batch_status(batch: IntakeBatch) -> str:
    if batch.discarded_at is not None:
        return BATCH_STATUS_DISCARDED
    if batch.closed_at is not None:
        return BATCH_STATUS_CLOSED
    return BATCH_STATUS_OPEN


def collapse_legacy_readings(readings: dict[str, Any]) -> dict[str, Any]:
    """把 2026-11-24 之前存下来的那一对日期读法，在**读出来**的时候收成一条。

    那天之前 `6/3/2026` 的 `recognition_payload` 里会存两条读法（mdy、dmy）、
    没有 `assumed_order`；第 15 节定下默认按月/日/年读之后，新识别的只存一条并写明
    依据。老记录不会自己变，所以这里替它补上同一个结论——否则卡片上只摆一个
    「确认 6 月 3 日」，旁边那句说明却还在说「有两种读法，要人工确认」。

    ⚠️ 只动**读出来的那一份字典**，库里的 payload 一个字不改；两种读法都成立
    之外的情形（只有一条、读不出来、商家路由已经定死）原样返回。
    """
    entries = readings.get("readings")
    if readings.get("assumed_order") or not isinstance(entries, list):
        return readings
    orders = {entry.get("order") for entry in entries if isinstance(entry, dict)}
    if orders != {DATE_ORDER_MDY, DATE_ORDER_DMY}:
        return readings
    kept = [
        entry for entry in entries
        if isinstance(entry, dict) and entry.get("order") == DATE_ORDER_ASSUMED
    ]
    if len(kept) != 1:
        return readings
    return {**readings, "readings": kept, "assumed_order": DATE_ORDER_ASSUMED}


def _visible_candidates(item: IntakeItem) -> tuple["IntakeCandidateView", ...]:
    """这张卡片上要摆出来的候选。

    只做一件事：**把 2026-11-24 之前存下来的那一对日期读法收成一条**。那天之前
    `6/3/2026` 会存成两条候选（mdy 一条、dmy 一条）等人挑；第 15 节定下默认按
    月/日/年读之后，新识别的只会有一条，而库里已有的那些不会自己变。不收的话，
    同一条规则下的两张卡片会长得不一样——老的那张仍然摆两个按钮问「这是几月」。

    ⚠️ 只收**同一段原文、读法恰好是 mdy 与 dmy 的那一对**；原文不同、或者本来就
    只有一条的，一个都不动。候选记录本身也不动——这是**显示**上的收口，原文与
    另一种读法仍留在 `intake_item_candidates` 和 `recognition_payload` 里。
    """
    views = [IntakeCandidateView.of(candidate) for candidate in item.candidates]
    pairs: dict[str, set[str]] = {}
    for view in views:
        if view.field != INTAKE_CANDIDATE_FIELD_PURCHASE_DATE or view.basis != "paper":
            continue
        evidence = view.evidence or {}
        raw = evidence.get("raw_text")
        reading = evidence.get("reading")
        if isinstance(raw, str) and reading in (DATE_ORDER_MDY, DATE_ORDER_DMY):
            pairs.setdefault(raw, set()).add(reading)
    superseded = {
        raw for raw, orders in pairs.items()
        if orders == {DATE_ORDER_MDY, DATE_ORDER_DMY}
    }
    if not superseded:
        return tuple(views)
    kept = []
    for view in views:
        evidence = view.evidence or {}
        if (
            view.field == INTAKE_CANDIDATE_FIELD_PURCHASE_DATE
            and evidence.get("raw_text") in superseded
            and evidence.get("reading") != DATE_ORDER_ASSUMED
            and evidence.get("reading") in (DATE_ORDER_MDY, DATE_ORDER_DMY)
        ):
            continue
        kept.append(view)
    return tuple(kept)


def _review_status(item: IntakeItem) -> str | None:
    """这一页所在的填表草稿走到哪一步了；没有草稿时是 None。"""
    link = item.review_link
    if link is None or link.invoice is None:
        return None
    if link.invoice.voided_at is not None:
        return "voided"
    if link.invoice.completed_at is not None:
        return "completed"
    return "draft"


def _maybe_json(raw: str | None) -> dict[str, Any] | None:
    """存坏了的 JSON 不让整张整理页打不开：解析不出来就当没有。"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("intake candidate evidence is not valid json; ignored")
        return None
    return parsed if isinstance(parsed, dict) else None


def _date_input(value: Any, *, field: str) -> date | None:
    """接受 `date` 或 ISO-8601 日期文本，其余一律报错。

    **不猜**：`11/08/26` 这种写法的日/月顺序文本本身定不死（`normalization` 的同一
    条口径），所以本模块不接收它。把美式日期解析成日历日是 M05 的事，那里有来源
    路由可依据。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValidationError(
            f"字段 {field} 要的是日历日，不是时刻。", details={"field": field}
        )
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as error:
            raise ValidationError(
                f"字段 {field} 不是合法的 ISO 日期（收到 {value!r}）。",
                details={"field": field},
            ) from error
    raise ValidationError(
        f"字段 {field} 必须是日期或 ISO 日期文本。", details={"field": field}
    )


def _confidence_input(value: Any, *, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValidationError(
            f"字段 {field} 必须是 0—1 之间的数。", details={"field": field}
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValidationError(
            f"字段 {field} 必须是 0—1 之间的数（收到 {value!r}）。",
            details={"field": field},
        ) from error
    if not Decimal(0) <= number <= Decimal(1):
        raise ValidationError(
            f"字段 {field} 必须在 0—1 之间（收到 {value!r}）。",
            details={"field": field},
        )
    return number.quantize(_CONFIDENCE_STEP)


def _positive_ordinals(values: Sequence[Any], *, field: str) -> tuple[int, ...]:
    """页号 / 行号列表：必须是正整数且互不重复。

    重复在数据库上也有一条局部唯一索引守着，这里先拦一次是为了给出看得懂的错误
    （「第 2 页出现了两次」比 IntegrityError 有用）。
    """
    if not values:
        raise ValidationError(f"字段 {field} 不能为空。", details={"field": field})
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValidationError(
                f"字段 {field} 只能是从 1 开始的整数（收到 {value!r}）。",
                details={"field": field},
            )
        if value in seen:
            raise ValidationError(
                f"字段 {field} 里 {value} 出现了两次。",
                details={"field": field, "duplicate": value},
            )
        seen.add(value)
        result.append(value)
    return tuple(result)


def _require_sha256(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("sha256 必须是字符串。", details={"field": "sha256"})
    digest = value.strip().lower()
    if not _SHA256_PATTERN.match(digest):
        raise ValidationError(
            f"sha256 必须是 {SHA256_LENGTH} 位十六进制摘要。",
            details={"field": "sha256"},
        )
    return digest


def _require_size(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(
            "size 必须是正整数字节数。", details={"field": "size"}
        )
    return value


class IntakeService:
    """整理批次的读写原语。

    事务边界在每个公开方法上：一次调用里的多次写入共同提交或共同回滚。
    `record_items` 尤其依赖这一条——页切到一半失败时，不能留下「文件已就绪、却只有
    三页内容」这种半套关系。
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- 批次 ---------------------------------------------------------------

    def create_batch(self) -> IntakeBatchView:
        with self._database.unit_of_work() as session:
            batch = IntakeBatchRepository(session).add(IntakeBatch())
            logger.info("intake batch created batch=%s", batch.id)
            return IntakeBatchView.of(batch)

    def get_batch(self, batch_id: int) -> IntakeBatchView:
        with self._database.unit_of_work() as session:
            return IntakeBatchView.of(self._load_batch(session, batch_id))

    def latest_open_batch(self) -> IntakeBatchView | None:
        """最近一个还在整理的批次；没有就返回 None（由调用方决定是否新建）。"""
        with self._database.unit_of_work() as session:
            batch = IntakeBatchRepository(session).latest_open()
            return None if batch is None else IntakeBatchView.of(batch)

    def close_batch(self, batch_id: int) -> IntakeBatchView:
        """整理收口：这一批不再接收新文件。

        **不检查是否全部归好组**：第 5 节明确允许把待定内容留着以后再办，强制清零
        只会逼人乱填一个日期。要拦的是「已确认行漏掉」，那一关在现有单据的收口上。
        """
        with self._database.unit_of_work() as session:
            batch = self._load_batch(session, batch_id)
            self._require_batch_active(batch)
            if batch.closed_at is None:
                batch.closed_at = utcnow()
            session.flush()
            return IntakeBatchView.of(batch)

    def reopen_batch(self, batch_id: int) -> IntakeBatchView:
        """收口之后又想接着整理：把批次放回进行中。作废的批次不能复活。"""
        with self._database.unit_of_work() as session:
            batch = self._load_batch(session, batch_id)
            if batch.discarded_at is not None:
                raise ConflictError(
                    f"批次 #{batch.id} 已经作废，不能重新打开。",
                    details={"batch_id": batch.id, "status": _batch_status(batch)},
                )
            batch.closed_at = None
            session.flush()
            return IntakeBatchView.of(batch)

    def discard_batch(self, batch_id: int) -> IntakeBatchView:
        """整批作废（软删）。原件字节不动——别的批次可能还在引用同一份字节。"""
        with self._database.unit_of_work() as session:
            batch = self._load_batch(session, batch_id)
            if batch.discarded_at is None:
                batch.discarded_at = utcnow()
            session.flush()
            logger.info("intake batch discarded batch=%s", batch.id)
            return IntakeBatchView.of(batch)

    # --- 文件 ---------------------------------------------------------------

    def register_file(
        self,
        batch_id: int,
        *,
        storage_key: str,
        sha256: str,
        size: int,
        original_name: str,
        content_type: str | None = None,
    ) -> RegisteredFile:
        """把一份**已经存好的**字节登记进批次。

        本方法不保存文件：字节怎么落盘是 M03 的事，这里只记「它在这个批次里」。
        分三种情况：

        1. 这份字节库里没有 → 新建一行 `intake_files` + 一条批次引用。
        2. 库里有、但不在这个批次里 → 只加一条引用，**不再存一份字节**。
        3. 这个批次里已有同一份字节 → 撞唯一约束，409 并指回已有的那一条。已回收的
           那条例外：它会被**复活**（页与已有分类原样留着），因为撤掉文件本来就该
           能反悔，而行是刻意保留的。
        """
        clean_key = require_text(
            storage_key, field="storage_key", max_length=STORAGE_KEY_MAX_LENGTH
        )
        digest = _require_sha256(sha256)
        clean_size = _require_size(size)
        clean_name = require_text(
            original_name, field="original_name", max_length=UPLOAD_NAME_MAX_LENGTH
        )
        clean_type = optional_text(
            content_type, field="content_type", max_length=CONTENT_TYPE_MAX_LENGTH
        )

        with self._database.unit_of_work() as session:
            batch = self._load_batch(session, batch_id)
            self._require_batch_active(batch)

            files = IntakeFileRepository(session)
            refs = IntakeBatchFileRepository(session)

            intake_file = files.find_by_sha256(digest)
            reused = intake_file is not None
            if intake_file is None:
                intake_file = files.add(
                    IntakeFile(
                        storage_key=clean_key,
                        sha256=digest,
                        size=clean_size,
                        content_type=clean_type,
                    )
                )

            existing = refs.find_in_batch(
                batch_id=batch.id, intake_file_id=intake_file.id
            )
            if existing is not None:
                if existing.status != INTAKE_FILE_STATUS_DISCARDED:
                    raise ConflictError(
                        f"这个文件已经在本批次里了（{existing.original_name}）。"
                        "同一份文件不用传两次。",
                        details={
                            "sha256": digest,
                            "batch_id": batch.id,
                            "batch_file_id": existing.id,
                        },
                    )
                # 撤掉之后又传回来：复活那一条，页与已有分类原样留着。
                existing.status = (
                    INTAKE_FILE_STATUS_READY
                    if existing.items
                    else INTAKE_FILE_STATUS_PENDING
                )
                existing.discarded_at = None
                existing.original_name = clean_name
                session.flush()
                ref = existing
            else:
                self._check_file_count(batch)
                ref = refs.add(
                    IntakeBatchFile(
                        batch_id=batch.id,
                        intake_file_id=intake_file.id,
                        original_name=clean_name,
                        upload_order=refs.next_upload_order(batch.id),
                        status=INTAKE_FILE_STATUS_PENDING,
                    )
                )

            shared = files.batch_ids_for(intake_file.id, exclude_batch_id=batch.id)
            # 跨路径重复只提示不阻断：旧单张上传路径有自己的 sha256 唯一索引，
            # 两张表的判重是分开的（见 models.IntakeFile）。
            legacy = InvoiceFileRepository(session).find_by_sha256(digest)
            logger.info(
                "intake file registered batch=%s file=%s reused=%s",
                batch.id,
                ref.id,
                reused,
            )
            return RegisteredFile(
                ref=IntakeFileRefView.of(ref),
                reused_bytes=reused,
                shared_with_batch_ids=tuple(shared),
                same_bytes_invoice_ids=(
                    () if legacy is None else (legacy.invoice_id,)
                ),
            )

    def record_items(
        self,
        batch_file_id: int,
        *,
        pages: Sequence[int] | None = None,
        rows: Sequence[int] | None = None,
        sheet_name: str | None = None,
    ) -> tuple[IntakeItemView, ...]:
        """把一个文件切出来的页或表格行登记成可归类内容，并把它标成已就绪。

        页与行**二选一**：一份文件要么按页切（图片、PDF），要么按行切（CSV/XLSX），
        不会两种都有。页号/行号由调用方给出而不是这里按数量生成，因为 M03 允许
        「某几页失败、其余照常」——那时传进来的就不是连续的 1..N。

        整个过程在**一个事务**里：内容、`item_count` 与 `status` 共同提交。切到一半
        出错时文件退回原状，不会留下「已就绪但只有三页」这种半套关系。
        """
        if (pages is None) == (rows is None):
            raise ValidationError(
                "pages 与 rows 必须二选一：一份文件要么按页切，要么按行切。",
                details={"field": "pages"},
            )
        clean_sheet = optional_text(
            sheet_name, field="sheet_name", max_length=SHEET_NAME_MAX_LENGTH
        )
        if pages is not None and clean_sheet is not None:
            raise ValidationError(
                "按页切的文件没有工作表名。", details={"field": "sheet_name"}
            )

        with self._database.unit_of_work() as session:
            batch_file = self._load_batch_file(session, batch_file_id)
            self._require_batch_active(batch_file.batch)
            if batch_file.status not in (
                INTAKE_FILE_STATUS_PENDING,
                INTAKE_FILE_STATUS_FAILED,
            ):
                raise ConflictError(
                    f"文件 {batch_file.original_name} 当前是 {batch_file.status}，"
                    "不能再登记一次内容。",
                    details={
                        "batch_file_id": batch_file.id,
                        "status": batch_file.status,
                    },
                )

            items = IntakeItemRepository(session)
            created: list[IntakeItem] = []
            if pages is not None:
                for page_no in _positive_ordinals(pages, field="pages"):
                    created.append(
                        items.add(
                            IntakeItem(
                                batch_file_id=batch_file.id,
                                kind=INTAKE_ITEM_KIND_PAGE,
                                page_no=page_no,
                            )
                        )
                    )
            else:
                assert rows is not None
                batch_file.sheet_name = clean_sheet
                for row_no in _positive_ordinals(rows, field="rows"):
                    created.append(
                        items.add(
                            IntakeItem(
                                batch_file_id=batch_file.id,
                                kind=INTAKE_ITEM_KIND_ROW,
                                row_no=row_no,
                            )
                        )
                    )

            batch_file.status = INTAKE_FILE_STATUS_READY
            batch_file.item_count = len(created)
            batch_file.error_code = None
            batch_file.error_message = None
            session.flush()
            logger.info(
                "intake items recorded batch_file=%s count=%s",
                batch_file.id,
                len(created),
            )
            return tuple(IntakeItemView.of(item) for item in created)

    def fail_file(
        self, batch_file_id: int, *, error_code: str, error_message: str
    ) -> IntakeFileRefView:
        """标记这个文件解析失败。

        **只动这一条**：原件字节与同批次其它文件一概不动，一个坏文件不该让整批重传
        （与 `extraction_jobs` 失败时不动草稿是同一条）。
        """
        clean_code = require_text(
            error_code, field="error_code", max_length=ERROR_CODE_MAX_LENGTH
        )
        clean_message = require_text(
            error_message, field="error_message", max_length=ERROR_MESSAGE_MAX_LENGTH
        )
        with self._database.unit_of_work() as session:
            batch_file = self._load_batch_file(session, batch_file_id)
            if batch_file.status == INTAKE_FILE_STATUS_DISCARDED:
                raise ConflictError(
                    f"文件 {batch_file.original_name} 已经从批次里撤掉了。",
                    details={"batch_file_id": batch_file.id},
                )
            batch_file.status = INTAKE_FILE_STATUS_FAILED
            batch_file.error_code = clean_code
            batch_file.error_message = clean_message
            session.flush()
            return IntakeFileRefView.of(batch_file)

    def get_file(self, batch_file_id: int) -> IntakeFileRefView:
        """读一个文件引用。M03 / M04 切页前要知道「它在哪、处理到哪一步」。"""
        with self._database.unit_of_work() as session:
            return IntakeFileRefView.of(self._load_batch_file(session, batch_file_id))

    def waiting_files(
        self,
        *,
        batch_id: int | None = None,
        include_failed: bool = False,
        limit: int = MAX_WAITING_FILES,
    ) -> tuple[IntakeFileRefView, ...]:
        """还等着切出内容的文件引用（`pending`，可选连 `failed` 一起）。

        这是 M03 / M04 驱动队列用的**只读**原语：`intake_batch_files.status` 本身
        就是队列，没有另一张任务表（理由见 `repositories/intake.py` 的
        `list_waiting`）。本方法不认识 PDF、不认识 CSV，只回答「哪些文件还没切」。
        """
        clean_limit = require_limit(limit, max_limit=MAX_WAITING_FILES)
        with self._database.unit_of_work() as session:
            if batch_id is not None:
                self._load_batch(session, batch_id)
            rows = IntakeBatchFileRepository(session).list_waiting(
                batch_id=batch_id,
                include_failed=include_failed,
                limit=clean_limit,
            )
            return tuple(IntakeFileRefView.of(ref) for ref in rows)

    def discard_file(self, batch_file_id: int) -> DiscardedFile:
        """把一个文件从批次里移除：**这份文件在这个批次里的记录一并删掉**。

        ⚠️ 2026-11-24 改掉了原来的软删（第 16 节）。原先撤掉只是标成 `discarded`、
        页与分类都留着，再传一次同一份文件就连分类一起复活。用户的决定是
        **「移除就当没传过，重新上传当作全新的文件」**：一份传错的文件留着一份看不见
        的旧分类，下次传回来时会无声地带着它，那比丢掉分类更难查。

        所以这里按顺序删：撤销日志里指向这些内容的条目（空掉的那次移动一并删，
        一个什么都恢复不了的「撤销」是个陷阱）→ 送审链接（正常情况下没有，下面那道
        闸门拦着）→ 内容（候选随 ORM 级联）→ 这条文件引用本身。删完之后同一份字节
        再传进来就是一条全新的引用：新 id、新传入时刻、新切出来的页，什么都不带。

        **不删的两样**：`intake_files` 里那行字节与磁盘上的原件。别的批次可能还引用着
        同一份（`bytes_still_referenced` 回答这件事），而同一份字节重传时会被复用，
        不会存第二份。已经进了填表的文件拦在外面——草稿是对着它填的。
        """
        with self._database.unit_of_work() as session:
            batch_file = self._load_batch_file(session, batch_file_id)
            handed_off = sum(
                1 for item in batch_file.items if item.handed_off_at is not None
            )
            if handed_off:
                # 指得出下一步：填表中的先撤回填表，已入库的走单据里的撤销确认。
                raise ConflictError(
                    f"这份文件有 {handed_off} 项已经开始填表或已入库，不能移除——"
                    "原件要留着供核对。先在整理页撤回填表（已入库的走单据里的"
                    "撤销确认与采购修订），再移除这份文件。",
                    details={
                        "batch_file_id": batch_file_id,
                        "handed_off_item_count": handed_off,
                    },
                )
            removed = IntakeFileRefView.of(batch_file)
            item_ids = [item.id for item in batch_file.items]
            if item_ids:
                IntakeMoveRepository(session).forget_items(item_ids)
                session.execute(
                    delete(IntakeReviewLink).where(
                        IntakeReviewLink.item_id.in_(item_ids)
                    )
                )
            session.delete(batch_file)
            session.flush()
            still = IntakeFileRepository(session).active_reference_count(
                batch_file.intake_file_id
            )
            logger.info(
                "intake file removed batch_file=%s items=%s still_referenced=%s",
                removed.id,
                len(item_ids),
                still,
            )
            return DiscardedFile(
                ref=removed,
                removed_item_count=len(item_ids),
                bytes_still_referenced=still > 0,
            )

    def delete_item(self, batch_id: int, item_id: int, *, version: int) -> None:
        """Delete one unreviewed page/row from the organizing board.

        A reviewed item belongs to a whole invoice and must use void_invoice,
        which also reverses its booked purchases and removes sibling pages.
        When this was the file's last item, remove the batch file reference so
        uploading the same file again starts with fresh classification.
        """
        with self._database.unit_of_work() as session:
            batch = self._load_batch(session, batch_id)
            if batch.closed_at or batch.discarded_at:
                raise ConflictError("这个批次已经结束，不能删除内容。")
            item = self._load_item(session, item_id)
            batch_file = item.batch_file
            if batch_file.batch_id != batch_id:
                raise NotFoundError("内容不在这个批次中。", details={"item_id": item_id})
            if item.version != version:
                raise ConflictError("内容版本已变化，请刷新。", details={"item_id": item_id, "current_version": item.version})
            if item.handed_off_at is not None or session.query(IntakeReviewLink).filter_by(item_id=item_id).first():
                raise ConflictError("这张内容已经开始填表，请删除对应的整张单据。", details={"item_id": item_id})
            IntakeMoveRepository(session).forget_items([item_id])
            session.delete(item)
            session.flush()
            remaining = session.query(IntakeItem).filter_by(batch_file_id=batch_file.id).count()
            if remaining == 0:
                session.expire(batch_file, ["items"])
                session.delete(batch_file)
            else:
                batch_file.item_count = remaining
            session.flush()

    # --- 内容与分组 ---------------------------------------------------------

    def get_item(self, item_id: int) -> IntakeItemView:
        with self._database.unit_of_work() as session:
            return IntakeItemView.of(self._load_item(session, item_id))

    def list_items(
        self,
        batch_id: int,
        *,
        include_discarded: bool = False,
        purchase_date: str | None = None,
        source_name: str | None = None,
        missing: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> IntakeItemPage:
        """分页读这个批次的可归类内容。默认不含已回收文件的内容。"""
        clean_limit = require_limit(limit, max_limit=MAX_ITEMS_PER_PAGE)
        clean_offset = require_offset(offset)
        if missing not in (None, "any", "both", "date", "source", "date_any", "source_any", "recognition_failed"):
            raise ValidationError("missing 筛选值无效。", details={"field": "missing"})
        date_missing = purchase_date == "missing"
        source_missing = source_name == "missing"
        parsed_date = None
        if purchase_date is not None and not date_missing:
            try:
                parsed_date = date.fromisoformat(purchase_date)
            except ValueError as exc:
                raise ValidationError("purchase_date 必须是合法的 ISO 日期。") from exc
        source_key = None
        if source_name is not None and not source_missing:
            source_key = normalize_name(require_text(source_name, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH))
        with self._database.unit_of_work() as session:
            self._load_batch(session, batch_id)
            rows, total = IntakeItemRepository(session).list_for_batch(
                batch_id,
                include_discarded=include_discarded,
                purchase_date=parsed_date,
                date_missing=date_missing,
                source_key=source_key,
                source_missing=source_missing,
                missing=missing,
                limit=clean_limit,
                offset=clean_offset,
            )
            return IntakeItemPage(
                items=tuple(IntakeItemView.of(item) for item in rows),
                total=total,
                limit=clean_limit,
                offset=clean_offset,
            )

    def list_groups(
        self, batch_id: int, *, include_discarded: bool = False
    ) -> tuple[IntakeGroupView, ...]:
        """派生出这个批次的日期/商家组。

        没有「建组」这个动作：组是内容上的人工决定值聚合出来的，所以移动一张卡片
        之后组立刻就对了，不需要任何同步。
        """
        with self._database.unit_of_work() as session:
            self._load_batch(session, batch_id)
            rows = IntakeItemRepository(session).group_rows(
                batch_id, include_discarded=include_discarded
            )
            return tuple(
                IntakeGroupView(
                    purchase_date=purchase_date,
                    source_name=source_name,
                    source_name_normalized=normalized,
                    item_count=count,
                )
                for purchase_date, normalized, source_name, count in rows
            )

    def classify_item(
        self,
        item_id: int,
        *,
        purchase_date: Any = UNSET,
        source_name: Any = UNSET,
        version: int | None = None,
    ) -> IntakeItemView:
        """写下一个内容的**人工决定**：实际采购日、商家，或两者。

        三种调用对应整理页的三个动作：

        - 只给 `purchase_date`：拖到日期标题。商家保持原样（通常还是未知）。
        - 两个都给：拖到该日期下的商家卡片。
        - 给 `None`：显式清空，退回待归类或「商家待定」。

        `UNSET` 与 `None` 必须分开：没传表示保持原值，传 `null` 表示清空。两者混成
        一个值的话，「只定日期」就会顺手把已经确认过的商家抹掉。

        `version` 是乐观锁：带着旧版本号来会被拦住，而不是悄悄覆盖另一个会话刚做的
        移动。只有真的改了值才涨版本——重复发同一条命令（手抖点两下）不该让另一个
        标签页凭空收到 409。
        """
        with self._database.unit_of_work() as session:
            item = self._load_item(session, item_id)
            batch_file = item.batch_file
            self._require_batch_active(batch_file.batch)
            if batch_file.status == INTAKE_FILE_STATUS_DISCARDED:
                raise ConflictError(
                    f"文件 {batch_file.original_name} 已经从批次里撤掉了，"
                    "它的内容不能再归组。",
                    details={"item_id": item.id, "batch_file_id": batch_file.id},
                )
            if item.handed_off_at is not None:
                raise ConflictError(
                    "内容已进入审核，请通过审核修改流程更正。",
                    details={"item_id": item.id},
                )
            self._check_version(item, version)

            changed = False
            if purchase_date is not UNSET:
                new_date = _date_input(purchase_date, field="purchase_date")
                if new_date != item.decided_purchase_date:
                    item.decided_purchase_date = new_date
                    changed = True
            if source_name is not UNSET:
                clean_source = optional_text(
                    source_name,
                    field="source_name",
                    max_length=SOURCE_NAME_MAX_LENGTH,
                )
                new_normalized = (
                    None if clean_source is None else normalize_name(clean_source)
                )
                if (
                    clean_source != item.decided_source_name
                    or new_normalized != item.decided_source_name_normalized
                ):
                    item.decided_source_name = clean_source
                    item.decided_source_name_normalized = new_normalized
                    changed = True

            if changed:
                item.version += 1
                item.decided_at = utcnow()
                session.flush()
                logger.info(
                    "intake item classified item=%s date=%s source=%s",
                    item.id,
                    item.decided_purchase_date,
                    item.decided_source_name_normalized,
                )
            return IntakeItemView.of(item)

    def replace_candidates(
        self,
        item_id: int,
        *,
        field: str,
        basis: str,
        candidates: Sequence[CandidateInput],
    ) -> IntakeItemView:
        """整组替换某个内容在某个维度、某一层依据上的候选。

        ⚠️ **这里一个字都不会写进 `decided_*`。** 候选只是建议；界面可以把高可信
        候选显示在相应组里，但那一格必须标明尚未人工确认（第 4 节）。把建议直接
        写成决定，就等于用一个猜出来的商家名入账。

        整组替换而不是追加：同一层依据重算之后应该只剩最新结果，叠加会让老板在
        列表里看到三个旧值，分不清哪个还算数。
        """
        if field not in INTAKE_CANDIDATE_FIELDS:
            raise ValidationError(
                f"字段 field 只能是 {', '.join(INTAKE_CANDIDATE_FIELDS)} 之一"
                f"（收到 {field!r}）。",
                details={"field": "field", "allowed": list(INTAKE_CANDIDATE_FIELDS)},
            )
        if basis not in INTAKE_CANDIDATE_BASES:
            raise ValidationError(
                f"字段 basis 只能是 {', '.join(INTAKE_CANDIDATE_BASES)} 之一"
                f"（收到 {basis!r}）。",
                details={"field": "basis", "allowed": list(INTAKE_CANDIDATE_BASES)},
            )

        with self._database.unit_of_work() as session:
            item = self._load_item(session, item_id)
            self._write_candidates(
                session, item, field=field, basis=basis, candidates=candidates
            )
            session.expire(item, ["candidates"])
            return IntakeItemView.of(item)

    # --- M05：识别处理状态与保存下来的原文 ---------------------------------

    def mark_recognition_pending(self, item_id: int) -> IntakeItemView:
        """把一个内容排进识别队列。

        刚切出来的内容 `recognition_status` 是 NULL，队列查询**本来就会取到它**，
        所以这一步不是必需的——它的用处是让「已经排过队」在状态上看得见
        （M03 的处理器钩子在切完页之后调它）。已经 `done` 的不动：那正是
        「不对同一页重复收费」的闸门。
        """
        with self._database.unit_of_work() as session:
            item = self._load_item(session, item_id)
            if item.recognition_status is None:
                item.recognition_status = INTAKE_RECOGNITION_STATUS_PENDING
                session.flush()
            return IntakeItemView.of(item)

    def recognition_waiting(
        self,
        *,
        batch_id: int | None = None,
        include_failed: bool = False,
        limit: int = MAX_RECOGNITION_BATCH,
    ) -> tuple[IntakeItemView, ...]:
        """还等着跑日期/商家识别的内容。只读原语，驱动队列用。

        本方法不认识 PDF、不调模型——它只回答「哪些内容还没识别过」。
        """
        clean_limit = require_limit(limit, max_limit=MAX_RECOGNITION_BATCH)
        with self._database.unit_of_work() as session:
            if batch_id is not None:
                self._load_batch(session, batch_id)
            rows = IntakeItemRepository(session).list_recognition_waiting(
                batch_id=batch_id, include_failed=include_failed, limit=clean_limit
            )
            return tuple(IntakeItemView.of(item) for item in rows)

    def record_recognition(
        self,
        item_id: int,
        *,
        payload: dict[str, Any],
        candidates: Mapping[str, Sequence[CandidateInput]],
        basis: str = INTAKE_CANDIDATE_BASIS_PAPER,
        read_at: datetime | None = None,
    ) -> IntakeItemView:
        """一次识别的全部产出：候选 + 原文 + 状态，**写在同一个事务里**。

        ⚠️ **一个字都不会写进 `decided_*`。** 识别出来的日期与商家是候选，界面可以
        把它们显示在相应组里，但那一格必须标明尚未人工确认（第 4 节）。

        `candidates` 是「维度 → 这一层依据上的候选」：

        - 列出来的维度**整组替换**，空列表就是「这一层什么都没读出来」——必须显式
          传空列表，否则上一轮的旧候选会留在那里冒充本轮结果。
        - 没列出来的维度一个字不动。重新解析日期时就靠这一条：只传
          `purchase_date`，已经读出来的商家候选不受影响。

        `read_at` 是**这一页真正被读出来的时刻**，默认现在。重新解析保存下来的原文
        时要把原来的时刻传回来——那一次没有再读一遍，谎报一个新时刻会让「这页什么
        时候花钱识别的」失去意义。

        状态一律落到 `done`，并清掉上一次的失败原因。⚠️ `done` **不等于读出了
        东西**：纸面上本来就没有商家时也是 `done`、候选 0 条，而这正是不再送一遍
        模型的依据。
        """
        for field in candidates:
            if field not in INTAKE_CANDIDATE_FIELDS:
                raise ValidationError(
                    f"候选维度只能是 {', '.join(INTAKE_CANDIDATE_FIELDS)} 之一"
                    f"（收到 {field!r}）。",
                    details={"field": "candidates", "received": field},
                )
        if basis not in INTAKE_CANDIDATE_BASES:
            raise ValidationError(
                f"字段 basis 只能是 {', '.join(INTAKE_CANDIDATE_BASES)} 之一"
                f"（收到 {basis!r}）。",
                details={"field": "basis", "allowed": list(INTAKE_CANDIDATE_BASES)},
            )

        with self._database.unit_of_work() as session:
            item = self._load_item(session, item_id)
            for field, values in candidates.items():
                self._write_candidates(
                    session, item, field=field, basis=basis, candidates=values
                )
            item.recognition_status = INTAKE_RECOGNITION_STATUS_DONE
            item.recognized_at = read_at or utcnow()
            item.recognition_payload = json.dumps(payload, ensure_ascii=False)
            item.recognition_error_code = None
            item.recognition_error_message = None
            session.flush()
            session.expire(item, ["candidates"])
            logger.info(
                "intake recognition recorded item=%s fields=%s",
                item.id,
                sorted(candidates),
            )
            return IntakeItemView.of(item)

    def fail_recognition(
        self,
        item_id: int,
        *,
        error_code: str,
        error_message: str,
        payload: dict[str, Any] | None = None,
    ) -> IntakeItemView:
        """这一个内容识别失败。

        **只动这一个内容**：原件字节、同一文件的其它页、同批次其它内容一概不动
        （与 `fail_file` 和「一条提取任务失败不动草稿」同一条）。已经读出来的候选
        也留着——上一轮读出的商家不该因为这一轮重试失败而消失。
        """
        clean_code = require_text(
            error_code, field="error_code", max_length=ERROR_CODE_MAX_LENGTH
        )
        clean_message = require_text(
            error_message, field="error_message", max_length=ERROR_MESSAGE_MAX_LENGTH
        )
        with self._database.unit_of_work() as session:
            item = self._load_item(session, item_id)
            item.recognition_status = INTAKE_RECOGNITION_STATUS_FAILED
            item.recognition_error_code = clean_code
            item.recognition_error_message = clean_message
            if payload is not None:
                item.recognition_payload = json.dumps(payload, ensure_ascii=False)
            session.flush()
            logger.warning(
                "intake recognition failed item=%s code=%s", item.id, clean_code
            )
            return IntakeItemView.of(item)

    # --- 内部 ---------------------------------------------------------------

    def _write_candidates(
        self,
        session: Session,
        item: IntakeItem,
        *,
        field: str,
        basis: str,
        candidates: Sequence[CandidateInput],
    ) -> None:
        """整组替换某个维度某一层依据上的候选。调用方已在事务里。"""
        repository = IntakeItemRepository(session)
        repository.delete_candidates(item_id=item.id, field=field, basis=basis)
        for index, candidate in enumerate(candidates):
            value, normalized = self._candidate_value(candidate, field=field)
            repository.add_candidate(
                IntakeItemCandidate(
                    item_id=item.id,
                    field=field,
                    value=value,
                    value_normalized=normalized,
                    basis=basis,
                    confidence=_confidence_input(
                        candidate.confidence,
                        field=f"candidates[{index}].confidence",
                    ),
                    evidence=(
                        None
                        if candidate.evidence is None
                        else json.dumps(candidate.evidence, ensure_ascii=False)
                    ),
                )
            )
        session.flush()

    @staticmethod
    def _candidate_value(
        candidate: CandidateInput, *, field: str
    ) -> tuple[str, str | None]:
        """候选的展示值与规范化值。

        日期候选存 ISO 文本，不另存规范化值——ISO 本身就是规范形式。商家候选的
        规范化值与 `intake_items` 的分组键同一个口径，于是「接受这条建议」之后
        分组键不会变。
        """
        if field == INTAKE_CANDIDATE_FIELD_PURCHASE_DATE:
            parsed = _date_input(candidate.value, field="candidate.value")
            if parsed is None:
                raise ValidationError(
                    "日期候选不能是空值：没有候选就不要写这一条。",
                    details={"field": "candidate.value"},
                )
            return parsed.isoformat(), None
        assert field == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
        clean = require_text(
            candidate.value, field="candidate.value", max_length=SOURCE_NAME_MAX_LENGTH
        )
        return clean, normalize_name(clean)

    @staticmethod
    def _check_version(item: IntakeItem, version: int | None) -> None:
        """乐观锁。`version` 没给就不检查（脚本与后台处理不必关心并发）。"""
        if version is None:
            return
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValidationError("version 必须是整数。", details={"field": "version"})
        if version != item.version:
            raise ConflictError(
                "这张卡片在你打开之后被移动过了"
                f"（你拿到的是第 {version} 版，现在是第 {item.version} 版）。"
                "请刷新后再操作，免得覆盖掉另一个窗口刚做的整理。",
                details={
                    "item_id": item.id,
                    "expected_version": version,
                    "current_version": item.version,
                },
            )

    @staticmethod
    def _check_file_count(batch: IntakeBatch) -> None:
        existing = sum(
            1
            for ref in batch.files
            if ref.status != INTAKE_FILE_STATUS_DISCARDED
        )
        if existing + 1 > MAX_FILES_PER_BATCH:
            raise ValidationError(
                f"一个批次最多挂 {MAX_FILES_PER_BATCH} 个文件（已有 {existing} 个）。",
                details={"field": "files", "max_files": MAX_FILES_PER_BATCH},
            )

    @staticmethod
    def _require_batch_active(batch: IntakeBatch) -> None:
        status = _batch_status(batch)
        if status != BATCH_STATUS_OPEN:
            raise ConflictError(
                f"批次 #{batch.id} 当前是 {status}，不能再改动它的内容。",
                details={"batch_id": batch.id, "status": status},
            )

    @staticmethod
    def _load_batch(session: Session, batch_id: int) -> IntakeBatch:
        batch = IntakeBatchRepository(session).get(batch_id)
        if batch is None:
            raise NotFoundError(
                f"找不到整理批次 #{batch_id}。", details={"batch_id": batch_id}
            )
        return batch

    @staticmethod
    def _load_batch_file(session: Session, batch_file_id: int) -> IntakeBatchFile:
        batch_file = IntakeBatchFileRepository(session).get(batch_file_id)
        if batch_file is None:
            raise NotFoundError(
                f"找不到批次文件 #{batch_file_id}。",
                details={"batch_file_id": batch_file_id},
            )
        return batch_file

    @staticmethod
    def _load_item(session: Session, item_id: int) -> IntakeItem:
        item = IntakeItemRepository(session).get(item_id)
        if item is None:
            raise NotFoundError(
                f"找不到可归类内容 #{item_id}。", details={"item_id": item_id}
            )
        return item


__all__ = [
    "BATCH_STATUS_CLOSED",
    "BATCH_STATUS_DISCARDED",
    "BATCH_STATUS_OPEN",
    "MAX_FILES_PER_BATCH",
    "MAX_ITEMS_PER_PAGE",
    "MAX_RECOGNITION_BATCH",
    "MAX_WAITING_FILES",
    "CandidateInput",
    "DiscardedFile",
    "IntakeBatchView",
    "IntakeCandidateView",
    "IntakeFileRefView",
    "IntakeGroupView",
    "IntakeItemPage",
    "IntakeItemView",
    "IntakeService",
    "RegisteredFile",
]
