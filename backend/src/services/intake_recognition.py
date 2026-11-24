"""整理批次的日期/商家候选与日期解析（多文件计划 M05）。

本模块回答整理页左侧那张卡片上的两个问题：**这是哪一天的采购、是哪家商家**。
它只产生**候选**，一个字都不会写进 `intake_items.decided_*`——那是人的决定
（M02 的三条规则之二，docs/multifiles.md 第 4 节）。

### 一页走哪条路：按**文件结构**分，不按商家分

| 内容 | 路径 | 成本 |
| --- | --- | --- |
| PDF 的某一页，**有文字层** | 抽文字层的标签字段，**根本不调 OCR** | 零 |
| PDF 的某一页，没有文字层 | 取整页图 → 识别器 | 一次调用 |
| 图片（JPG / PNG / HEIC） | 解码 → 识别器 | 一次调用 |
| CSV / XLSX 的某一行（M04） | 读那一行已经解析好的单元格，**不经图像 OCR** | 零 |

⚠️ **分流依据与「这页看起来像哪家」无关**。步骤 08 已经定过这一条，这里再紧一档：

### 不拿没人确认过的商家去套它的 OCR 路由

`ocr_routes` 上记着每家商家走哪条提取路径、以及这家写的是月/日/年还是日/月/年
（`date_order`）。整理阶段**只认人工确认过的商家**（`decided_source_name_normalized`），
候选再像也不算：拿一个相似度猜出来的商家去套它的 `date_order`，等于用相似度决定
了一个日历日，而那种错误**不报错**——它只是把这笔采购算进了错误的月份。

所以没人确认商家时 `date_order` 一律是 `strict`，`6/11/2026` 于是**两种读法都
留着**（`normalization.date_text_readings`），而不是按上传日、系统地区或哪个模型
更自信去挑一个。等商家确认了，`reparse_date()` 用**保存下来的原文**配上这家的
`date_order` 重新解析一遍——**不再调一次模型**。

### 为什么识别结果要整份存下来

`intake_items.recognition_payload` 存的是这一页读出来的原文、出处、用了哪个
provider / 哪几个模型 / 哪版提示词，**以及行项目**。两个理由都来自 M05 自己的
要求：

1. **商家确认后要用保存的日期原文重新解析**——解析不出来的日期根本不会产生候选，
   原文只活在候选里就丢了。
2. **整理阶段已有的识别结果进入审核时不得对同一页重复收费**——M08 建草稿时读这份
   payload，而不是把同一页再送一遍模型。

⚠️ payload 里的 `purpose` / `rounds` 说明它是**候选那一轮**的产出：只跑了内容这
一轮，没有步骤 08 的结构提问、格线行容量与墨迹闸门。M08 不能把它当成已经过那几层
校验的结果——手写件那几层是防编造的，少跑一层不会报错，只会悄悄多出几行。

### 失败落在**这一个内容**上

取不到字节、取不出整页图、模型调用失败，都写在 `intake_items.recognition_status`
上（`failed` + 原因），**原件一个字节都不动**，同一份 PDF 的其它页、同批次其它
内容照常。表格行同理：一行读不出来不影响别的行（M05 第三条子项）。重试走队列。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from typing import Any, Final, Sequence

from src.contracts import (
    DOCUMENT_FIELDS,
    FIELD_INVOICE_DATE,
    FIELD_SUPPLIER,
    FIELD_SUPPLIER_CHINESE,
    FIELD_SUPPLIER_ENGLISH,
    FIELD_SUPPLIER_EVIDENCE,
    LINE_FIELDS,
    FieldStatus,
    RecognitionRequest,
    RecognitionResult,
)
from src.db import Database
from src.documents.files import (
    DocumentFormat,
    FormatError,
    decode_image,
    sniff_format,
)
from src.documents.pdf import (
    PdfError,
    extract_page_images,
    inspect_pdf,
    read_text_fragments,
)
from src.documents.textlayer import TEXT_LAYER_PROVIDER, parse_text_layout
from src.errors import ConflictError, NotFoundError, ValidationError
from src.integrations.crosscheck import CrossCheckOutcome
from src.integrations.dashscope import CONTENT_PROMPT_SHA12, OcrServiceError
from src.integrations.file_storage import LocalFileStorage
from src.integrations.ocr import CrossCheckRecognizer, Recognizer
from src.models import (
    INTAKE_CANDIDATE_BASIS_PAPER,
    INTAKE_CANDIDATE_FIELD_PURCHASE_DATE,
    INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
    INTAKE_FILE_STATUS_DISCARDED,
    INTAKE_ITEM_KIND_PAGE,
    INTAKE_ITEM_KIND_ROW,
    INTAKE_RECOGNITION_STATUS_DONE,
)
from src.normalization import (
    DATE_ORDER_ASSUMED,
    DATE_ORDER_STRICT,
    DateReadings,
    date_text_readings,
)
from src.repositories.extraction import OcrRouteRepository
from src.services.intake import (
    MAX_RECOGNITION_BATCH,
    CandidateInput,
    IntakeItemView,
    IntakeService,
)
from src.services.intake_ingest import IntakeItemProcessor, PageSource

logger = logging.getLogger(__name__)

# --- 结局与原因 -------------------------------------------------------------

OUTCOME_RECOGNIZED: Final[str] = "recognized"
OUTCOME_ALREADY_RECOGNIZED: Final[str] = "already_recognized"
"""这一页早就识别过了。**不是错误，而是不重复收费的那条闸门**：重新打开批次、
重复驱动队列、两个调度器抢着跑，都走这一条，一次模型调用都不会发生。"""
OUTCOME_FAILED: Final[str] = "failed"

ERROR_BYTES_MISSING: Final[str] = "bytes_missing"
ERROR_CONTENT_UNSUPPORTED: Final[str] = "content_unsupported"
ERROR_PDF_UNREADABLE: Final[str] = "pdf_unreadable"
ERROR_IMAGE_UNREADABLE: Final[str] = "image_unreadable"
ERROR_PAGE_IMAGE_MISSING: Final[str] = "page_image_missing"
ERROR_OCR_FAILED: Final[str] = "ocr_failed"
ERROR_ROW_DATA_MISSING: Final[str] = "row_data_missing"

# payload 里 `provider` 的两个本地取值。另一个取值是识别器自己报的 provider。
PROVIDER_TEXT_LAYER: Final[str] = TEXT_LAYER_PROVIDER
PROVIDER_SPREADSHEET: Final[str] = "spreadsheet"

# payload 的用途标记。⚠️ M08 读这份 payload 建草稿时必须看它：这一轮只为了
# **归组**读了抬头，没有跑步骤 08 的结构提问与格线行容量。
PAYLOAD_PURPOSE: Final[str] = "intake_grouping"
PAYLOAD_VERSION: Final[int] = 1

# `date_order` 是怎么定下来的。写进 payload 是为了让「这个日期为什么这么读」
# 永远有一个指得到的理由。
DATE_ORDER_FROM_DEFAULT: Final[str] = "default_strict"
"""没有人工确认过的商家 → 一律 strict，歧义日期保留两种读法。"""
DATE_ORDER_FROM_CONFIRMED_SOURCE: Final[str] = "confirmed_source_route"
"""商家是**人工确认**过的，用的是这家 `ocr_routes` 上的 `date_order`。"""
DATE_ORDER_FROM_SPREADSHEET: Final[str] = "spreadsheet_import_option"
"""表格行：日期已经由导入时选的 `date_order` 解析过了（M04）。"""

# 表格行里这两个字段的位置。与 M04 存下来的 `row_data` 结构一致
# （`services/intake_spreadsheets.py` 的 `save_rows`）。
ROW_FIELD_PURCHASE_DATE: Final[str] = "purchase_date"
ROW_FIELD_SOURCE_NAME: Final[str] = "source_name"


# --- 结果视图 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecognizedItem:
    """一个内容的识别结局。

    `calls` 是**这一次真的发了几次模型调用**（文字层与表格行是 0）。它存在的理由
    只有一个：让「不重复收费」成为一条**可以断言**的事实，而不是一句承诺。
    """

    item: IntakeItemView
    outcome: str
    provider: str | None = None
    calls: int = 0
    notes: tuple[str, ...] = dataclass_field(default_factory=tuple)

    @property
    def error_code(self) -> str | None:
        return self.item.recognition_error_code

    @property
    def date_candidates(self) -> tuple[str, ...]:
        return tuple(
            candidate.value
            for candidate in self.item.candidates
            if candidate.field == INTAKE_CANDIDATE_FIELD_PURCHASE_DATE
        )

    @property
    def source_candidates(self) -> tuple[str, ...]:
        return tuple(
            candidate.value
            for candidate in self.item.candidates
            if candidate.field == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
        )


# --- 服务 -------------------------------------------------------------------


class IntakeRecognitionService:
    """为批次里的内容产生日期/商家候选，并保存原文与处理状态。

    落库一律经 M02 的 `IntakeService`（`record_recognition` / `fail_recognition`），
    本模块自己不开 Session——与 M03 的摄入服务同一条分层，于是「候选与状态共同
    提交」那条事务保证继续由 M02 的一个事务提供。
    """

    def __init__(
        self,
        intake: IntakeService,
        *,
        database: Database,
        file_storage: LocalFileStorage,
        recognizer: Recognizer,
    ) -> None:
        self._intake = intake
        self._database = database
        self._storage = file_storage
        self._recognizer = recognizer

    # --- 队列 ---------------------------------------------------------------

    def recognize_waiting(
        self,
        *,
        batch_id: int | None = None,
        retry_failed: bool = False,
        limit: int = MAX_RECOGNITION_BATCH,
    ) -> tuple[RecognizedItem, ...]:
        """把还没识别的内容跑完，按文件上传序与页号/行号。

        这是**恢复入口**，而且**重复调用是安全且免费的**：已经 `done` 的内容一律
        跳过（`already_recognized`），一次模型调用都不会发生。进程重启、用户关掉
        浏览器又回来、两个标签页同时点一下，都走这一条。

        `retry_failed=True` 连失败的一起重试——识别失败常常是环境问题（凭据没配、
        整页图取不出来），装好之后不该逼人重新上传原件。
        """
        waiting = self._intake.recognition_waiting(
            batch_id=batch_id, include_failed=retry_failed, limit=limit
        )
        results: list[RecognizedItem] = []
        for view in waiting:
            try:
                results.append(self.recognize_item(view.id))
            except ConflictError:
                # 另一个调度器抢先跑完了，或者文件刚被撤掉。如实记一条，不重跑。
                results.append(
                    RecognizedItem(
                        item=self._intake.get_item(view.id),
                        outcome=OUTCOME_ALREADY_RECOGNIZED,
                    )
                )
        logger.info(
            "intake recognition drained batch=%s items=%s calls=%s",
            batch_id,
            len(results),
            sum(item.calls for item in results),
        )
        return tuple(results)

    def recognize_item(self, item_id: int) -> RecognizedItem:
        """读一个内容的日期与商家候选。

        四种结局都落在**内容的状态**上，调用方不必解释异常：

        - 读完了 → `done`，候选写好（可能是 0 条——纸面上本来就没有商家）。
        - 早就读过 → 原样返回，**不花钱**（`already_recognized`）。
        - 字节取不到、整页图取不出、模型调用失败 → `failed` + 原因，可重试，
          **原件不动**。
        - 文件已从批次撤掉 → 冲突（撤掉的东西不该被偷偷读回来）。
        """
        view = self._intake.get_item(item_id)
        if view.recognition_status == INTAKE_RECOGNITION_STATUS_DONE:
            return RecognizedItem(item=view, outcome=OUTCOME_ALREADY_RECOGNIZED)
        if self._intake.get_file(view.batch_file_id).status == (
            INTAKE_FILE_STATUS_DISCARDED
        ):
            raise ConflictError(
                f"文件 {view.original_name} 已经从批次里撤掉了，不再识别它的内容。",
                details={"item_id": view.id, "batch_file_id": view.batch_file_id},
            )
        if view.kind == INTAKE_ITEM_KIND_ROW:
            return self._recognize_row(view)
        return self._recognize_page(view)

    # --- 表格行（M04 的结构化候选） -----------------------------------------

    def _recognize_row(self, view: IntakeItemView) -> RecognizedItem:
        """表格行的候选：**用原单元格证据，不经图像 OCR**。

        表格里写明的日期与商家就是纸面事实，所以优先级最高：M04 已经按导入时选的
        `date_order` / `number_format` 把那一格解析过了，这里不再解析第二遍——
        同一个格子两套口径就会出现「预览里是 11 月 8 日、卡片上是 8 月 11 日」。

        那一格**没解析出来**（歧义日期、无效值）时才回到原文：对原文跑
        `date_text_readings`，歧义就留两条候选。⚠️ 这里依然不用任何商家路由，
        表格的 `date_order` 是导入时人选的，与某家商家的习惯无关。

        一行读不出来不影响别的行——每一行是独立的一次调用，失败只写在这一行上。
        """
        row_data = view.row_data
        if not row_data:
            return self._failed(
                view,
                ERROR_ROW_DATA_MISSING,
                f"第 {view.row_no} 行没有保存下来的单元格数据，无法生成候选。"
                "请在批次里重新处理这份表格（原表格仍然存着）。",
            )

        values = row_data.get("values") or {}
        fields = (row_data.get("raw_fields") or {}).get("fields") or {}
        notes: list[str] = []
        payload_fields: dict[str, Any] = {}

        # --- 日期 ---
        date_candidates: list[CandidateInput] = []
        date_cell = fields.get(ROW_FIELD_PURCHASE_DATE) or {}
        date_raw = _text_or_none(date_cell.get("raw_text"))
        parsed_date = _text_or_none(values.get(ROW_FIELD_PURCHASE_DATE))
        readings: DateReadings | None = None
        if parsed_date is not None:
            date_candidates.append(
                CandidateInput(
                    value=parsed_date,
                    evidence=_row_evidence(
                        view,
                        row_data,
                        date_cell,
                        raw_text=date_raw,
                        reading="spreadsheet_parsed",
                        note="表格里这一格明确写着日期，按导入时选的日/月顺序解析。",
                    ),
                )
            )
        elif date_raw is not None:
            readings = date_text_readings(
                date_raw, allow_short_year=True, assume_order=DATE_ORDER_ASSUMED
            )
            for reading in readings.readings:
                date_candidates.append(
                    CandidateInput(
                        value=reading.text,
                        evidence=_row_evidence(
                            view,
                            row_data,
                            date_cell,
                            raw_text=date_raw,
                            reading=reading.order,
                            note=reading.note,
                            ambiguous=readings.ambiguous,
                        ),
                    )
                )
            if not readings.ok:
                notes.append(
                    f"第 {view.row_no} 行的日期格写着 {date_raw!r}，解析不出来"
                    f"（{readings.problem}）。原文留着，这一行的日期待定。"
                )
        payload_fields[FIELD_INVOICE_DATE] = {
            "raw_text": date_raw,
            "parsed": parsed_date,
            "cell": date_cell.get("cell") or _cell_ref(date_cell, view.row_no),
            "column": date_cell.get("column"),
            "header": date_cell.get("header"),
            "ambiguous": bool(readings.ambiguous) if readings else False,
            "problem": readings.problem if readings else None,
        }

        # --- 商家 ---
        source_candidates: list[CandidateInput] = []
        source_cell = fields.get(ROW_FIELD_SOURCE_NAME) or {}
        source_raw = _text_or_none(source_cell.get("raw_text"))
        parsed_source = _text_or_none(values.get(ROW_FIELD_SOURCE_NAME))
        if parsed_source is not None:
            source_candidates.append(
                CandidateInput(
                    value=parsed_source,
                    evidence=_row_evidence(
                        view,
                        row_data,
                        source_cell,
                        raw_text=source_raw,
                        note="表格里这一格明确写着来源。",
                    ),
                )
            )
        payload_fields[FIELD_SUPPLIER] = {
            "raw_text": source_raw,
            "parsed": parsed_source,
            "cell": source_cell.get("cell") or _cell_ref(source_cell, view.row_no),
            "column": source_cell.get("column"),
            "header": source_cell.get("header"),
        }

        issues = row_data.get("review_issues") or []
        notes.extend(
            f"第 {view.row_no} 行 {issue.get('field')}：{issue.get('message')}"
            for issue in issues
            if issue.get("field") in (ROW_FIELD_PURCHASE_DATE, ROW_FIELD_SOURCE_NAME)
        )

        payload = {
            "payload_version": PAYLOAD_VERSION,
            "purpose": PAYLOAD_PURPOSE,
            "provider": PROVIDER_SPREADSHEET,
            "models": [],
            "prompt_sha12": None,
            "is_fake": False,
            "rounds": [],
            "source": {
                "kind": INTAKE_ITEM_KIND_ROW,
                "row_no": view.row_no,
                "sheet_name": view.sheet_name,
                "original_name": view.original_name,
            },
            "date_order": {
                "value": None,
                "from": DATE_ORDER_FROM_SPREADSHEET,
                "confirmed_source": None,
            },
            "fields": payload_fields,
            "notes": notes,
        }
        item = self._intake.record_recognition(
            view.id,
            payload=payload,
            candidates={
                INTAKE_CANDIDATE_FIELD_PURCHASE_DATE: date_candidates,
                INTAKE_CANDIDATE_FIELD_SOURCE_NAME: source_candidates,
            },
            basis=INTAKE_CANDIDATE_BASIS_PAPER,
        )
        return RecognizedItem(
            item=item,
            outcome=OUTCOME_RECOGNIZED,
            provider=PROVIDER_SPREADSHEET,
            calls=0,
            notes=tuple(notes),
        )

    # --- 页（图片 / PDF） ---------------------------------------------------

    def _recognize_page(self, view: IntakeItemView) -> RecognizedItem:
        try:
            content = self._storage.read(view.storage_key)
        except (NotFoundError, ValidationError, OSError) as error:
            return self._failed(
                view,
                ERROR_BYTES_MISSING,
                f"原件读不出来（{view.original_name}）：{error}。"
                "原件引用还在，重新上传这个文件即可。",
            )

        fmt = sniff_format(content)
        if fmt is DocumentFormat.PDF:
            return self._recognize_pdf_page(view, content)
        if fmt in (DocumentFormat.JPEG, DocumentFormat.PNG, DocumentFormat.HEIC):
            return self._recognize_image_page(view, content, fmt)
        return self._failed(
            view,
            ERROR_CONTENT_UNSUPPORTED,
            f"{view.original_name} 的内容既不是图片也不是 PDF（文件头认不出来），"
            "没法识别它的日期与商家。原件已保存，可以手工填这张卡片。",
        )

    def _recognize_pdf_page(
        self, view: IntakeItemView, content: bytes
    ) -> RecognizedItem:
        """PDF 的一页：**先用文字层**，没有文字层才取整页图送识别器。

        ⚠️ 有文字层就**绝不**调 OCR：字符来自文件本身，比任何模型都准，而且零成本
        （步骤 08 的同一条）。这一页的文字层事实在摄入时已经算过一次
        （`PageSource.has_text_layer`），这里重新算是因为它是**本地计算、不花钱**，
        而重算比把一个可能过期的布尔值带过来更可靠。
        """
        page_no = view.page_no or 1
        try:
            inspection = inspect_pdf(content)
        except PdfError as error:
            return self._failed(view, ERROR_PDF_UNREADABLE, f"{error}")

        facts = inspection.page(page_no)
        if facts is None:
            return self._failed(
                view,
                ERROR_PDF_UNREADABLE,
                f"这份 PDF 里没有第 {page_no} 页（共 {inspection.page_count} 页）。"
                "原件可能被换成了另一份同名文件，请重新上传。",
            )

        if facts.has_text:
            fragments = read_text_fragments(content, page=page_no)
            parse = parse_text_layout(
                fragments,
                page=page_no,
                page_width=facts.width,
                page_height=facts.height,
            )
            notes = (
                "走的是**数字 PDF 文字层**：字符来自文件本身，没有调用任何 OCR "
                "服务（零成本）。⚠️ 文字层里没有「认不出」这种状态——要么有字符，"
                "要么纸上没有。",
            ) + tuple(parse.notes)
            return self._record_page(
                view,
                result=parse.result,
                outcome_detail=None,
                provider=PROVIDER_TEXT_LAYER,
                models=(),
                prompt_sha12=None,
                rounds=("text_layer",),
                calls=0,
                notes=notes,
                page_facts={
                    "has_text_layer": True,
                    "fragment_count": facts.fragment_count,
                    "image_count": facts.image_count,
                    "header_found": parse.header_found,
                },
            )

        try:
            images = extract_page_images(content, pages=(page_no,))
        except PdfError as error:
            return self._failed(view, ERROR_PDF_UNREADABLE, f"{error}")
        usable = next((item for item in images if item.payload), None)
        if usable is None:
            reasons = "；".join(note for item in images for note in item.notes)
            return self._failed(
                view,
                ERROR_PAGE_IMAGE_MISSING,
                f"第 {page_no} 页既没有文字层，也取不出整页图像，因此读不出日期与"
                "商家。原件已保存，可以回看原件手工填这张卡片。"
                + (f"具体原因：{reasons}" if reasons else ""),
            )
        return self._recognize_with_model(
            view,
            payload=usable.payload,
            media_type=usable.media_type,
            page_no=page_no,
            page_facts={
                "has_text_layer": False,
                "fragment_count": facts.fragment_count,
                "image_count": facts.image_count,
                "width": usable.width,
                "height": usable.height,
            },
            extra_notes=tuple(usable.notes),
        )

    def _recognize_image_page(
        self, view: IntakeItemView, content: bytes, fmt: DocumentFormat
    ) -> RecognizedItem:
        """一张图片：解码摆正方向，再送识别器。

        ⚠️ HEIC 缺解码器时**失败而不是硬送过去**：识别服务只收 JPEG / PNG
        （`integrations/ocr.SUPPORTED_MEDIA_TYPES`），把一份 HEIC 标成 image/jpeg
        送出去只会得到一份「识别结果变差」而不是报错。装上 pillow-heif 之后重试，
        原件一直都在。
        """
        try:
            decoded = decode_image(content, original_name=view.original_name)
        except FormatError as error:
            return self._failed(view, ERROR_IMAGE_UNREADABLE, f"{error}")
        with decoded.image:
            width = float(decoded.image.width)
            height = float(decoded.image.height)
        return self._recognize_with_model(
            view,
            payload=decoded.payload,
            media_type=decoded.media_type,
            page_no=view.page_no or 1,
            page_facts={
                "has_text_layer": None,
                "source_format": str(fmt),
                "width": width,
                "height": height,
            },
            extra_notes=tuple(decoded.notes),
        )

    def _recognize_with_model(
        self,
        view: IntakeItemView,
        *,
        payload: bytes,
        media_type: str,
        page_no: int,
        page_facts: dict[str, Any],
        extra_notes: tuple[str, ...] = (),
    ) -> RecognizedItem:
        """一次内容识别。多模型时并行跑并逐字段裁决。

        ⚠️ **只跑内容这一轮**。步骤 08 对手写件还有一轮结构提问、格线行容量与墨迹
        闸门，那几层是防「把固定结构填满」的编造；这里不跑它们，因为本轮要的只是
        抬头上的日期与商家，而**候选不会被预填成任何值**。payload 里的 `rounds`
        如实记下这一点，M08 据此决定还要不要补那几层。
        """
        request = RecognitionRequest(
            content=payload,
            content_type=media_type,
            original_name=None,
            source_key=view.storage_key,
        )
        outcome: CrossCheckOutcome | None = None
        try:
            if isinstance(self._recognizer, CrossCheckRecognizer):
                outcome = self._recognizer.cross_check(request)
                result = outcome.result
                models = tuple(self._recognizer.models)
            else:
                result = self._recognizer.recognize(request)
                models = tuple(getattr(self._recognizer, "models", ()))
        except OcrServiceError as error:
            return self._failed(
                view,
                ERROR_OCR_FAILED,
                f"识别服务调用失败（{error.code}）：{error}。"
                "原件与这个批次都没有变化，稍后重试即可。",
            )

        notes = list(extra_notes) + list(result.warnings)
        if outcome is not None:
            notes.append(
                f"多模型并行读：{len(models)} 个模型，{outcome.disagreement_count} "
                "个字段有分歧 → 分歧的字段不产生候选，等人看原件填。"
                "⚠️ 一致也不等于对。"
            )
        elif not result.is_fake and len(models) < 2:
            notes.append(
                "⚠️ 当前只配了 1 个模型，**没有交叉验证这道防线**；"
                "读出来的商家与日期更要当候选看。"
            )
        if result.is_fake:
            notes.append(
                "⚠️ 本次候选由 **Fake 识别器**产生，不是真实识别结果，"
                "不能用来宣称任何准确率。"
            )
        return self._record_page(
            view,
            result=result,
            outcome_detail=outcome,
            provider=result.provider,
            models=models,
            prompt_sha12=None if result.is_fake else CONTENT_PROMPT_SHA12,
            rounds=("content",),
            # 交叉裁决是**每个模型一次**请求（并行发），单模型适配器是一次。
            # 这个数要等于真的发出去的请求数，否则「没有重复收费」就验不了。
            calls=len(models) if outcome is not None else 1,
            notes=tuple(notes),
            page_facts=page_facts,
            page_no=page_no,
        )

    def _record_page(
        self,
        view: IntakeItemView,
        *,
        result: RecognitionResult,
        outcome_detail: CrossCheckOutcome | None,
        provider: str,
        models: Sequence[str],
        prompt_sha12: str | None,
        rounds: tuple[str, ...],
        calls: int,
        notes: tuple[str, ...],
        page_facts: dict[str, Any],
        page_no: int | None = None,
    ) -> RecognizedItem:
        """把一页的识别产出变成候选 + payload，落库。"""
        date_order, order_from, confirmed = self._date_order_for(view)
        supplier_candidates, supplier_note = _supplier_candidates(result)
        date_candidates, date_payload = _date_candidates(
            result, date_order=date_order
        )
        all_notes = list(notes)
        if supplier_note:
            all_notes.append(supplier_note)
        if date_payload.get("ambiguous"):
            assumed = date_payload.get("assumed_order")
            if assumed:
                habit = "月/日/年" if assumed == DATE_ORDER_ASSUMED else "日/月/年"
                all_notes.append(
                    f"日期的日/月顺序**文本本身定不死**，按默认的{habit}读。"
                    "原文留着：确认这张卡片的商家、并在这家的路由上设 date_order "
                    "之后可以重新解析——**不必再识别一次**。"
                )
            else:
                all_notes.append(
                    "日期的日/月顺序**文本本身定不死**，两种读法都留着当候选。"
                    "确认这张卡片的商家、并在这家的路由上设 date_order 之后，"
                    "可以用保存下来的原文重新解析——**不必再识别一次**。"
                )
        if date_order == DATE_ORDER_STRICT and order_from == DATE_ORDER_FROM_DEFAULT:
            all_notes.append(
                "这张卡片的商家**还没人工确认**，所以不套用任何一家的日/月顺序："
                "拿相似度猜出来的商家去定日历日，错了也不会报错。歧义写法按本项目"
                "默认的月/日/年读，原文仍留着可重新解析。"
            )

        payload = {
            "payload_version": PAYLOAD_VERSION,
            "purpose": PAYLOAD_PURPOSE,
            "provider": provider,
            "models": list(models),
            "prompt_sha12": prompt_sha12,
            "is_fake": result.is_fake,
            "rounds": list(rounds),
            "source": {
                "kind": INTAKE_ITEM_KIND_PAGE,
                "page_no": page_no if page_no is not None else view.page_no,
                "original_name": view.original_name,
                "sha256": view.sha256,
                **page_facts,
            },
            "date_order": {
                "value": date_order,
                "from": order_from,
                "confirmed_source": confirmed,
            },
            "fields": _document_fields_payload(result, outcome_detail),
            "date_readings": date_payload,
            # 行项目一起存下来，M08 据此建草稿而不再为同一页付一次钱。
            # ⚠️ 看 `purpose` / `rounds`：这是候选那一轮，没跑结构提问与行容量。
            "lines": _lines_payload(result, outcome_detail),
            "raw_response": result.raw_response,
            "notes": all_notes,
        }
        item = self._intake.record_recognition(
            view.id,
            payload=payload,
            candidates={
                INTAKE_CANDIDATE_FIELD_PURCHASE_DATE: date_candidates,
                INTAKE_CANDIDATE_FIELD_SOURCE_NAME: supplier_candidates,
            },
            basis=INTAKE_CANDIDATE_BASIS_PAPER,
        )
        return RecognizedItem(
            item=item,
            outcome=OUTCOME_RECOGNIZED,
            provider=provider,
            calls=calls,
            notes=tuple(all_notes),
        )

    # --- 商家确认之后重新解析日期 -------------------------------------------

    def reparse_date(self, item_id: int) -> RecognizedItem:
        """用**保存下来的日期原文**配上这家商家的 `date_order` 重新解析。

        **一次模型调用都不会发生**（`calls` 恒为 0）。这是 M05 验收条件里那一条：
        「商家未知且日期歧义时两项可独立待定；随后确认商家并设 `date_order` 可重新
        得到日期」。

        两个前提，缺一个就报错而不是悄悄猜：

        1. **商家必须是人工确认过的**（`decided_source_name`）。候选再像也不算——
           这正是「不能仅凭相似度应用该商家的 OCR 路由」。
        2. **必须有保存下来的原文**。没有就说明这一页还没识别过（或者识别失败），
           该做的是跑识别，不是在这里编一个日期。

        ⚠️ 它改的是**候选**，不是 `decided_purchase_date`：候选永远不会自己变成
        决定（M02 规则二）。歧义消解之后那一维只剩一条候选，人点一下即可。
        """
        view = self._intake.get_item(item_id)
        if not view.decided_source_name_normalized:
            raise ValidationError(
                "这张卡片的商家还没人工确认，不能套用某一家的日/月顺序。"
                "拿还没确认的商家候选去定日历日，猜错不会报错——它只是把这笔采购"
                "算进了错误的月份。",
                details={"item_id": view.id, "field": "decided_source_name"},
            )
        payload = dict(view.recognition_payload or {})
        if not payload:
            raise ConflictError(
                "这张卡片还没有保存下来的识别原文，没有可重新解析的日期。"
                "先跑一次识别（或重试失败的那一次）。",
                details={
                    "item_id": view.id,
                    "recognition_status": view.recognition_status,
                },
            )

        date_order, order_from, confirmed = self._date_order_for(view)
        raw_text = _saved_date_raw_text(payload)
        if raw_text is None:
            # 纸面上本来就没有日期：不是错误，也没什么可重新解析的。如实记一条。
            readings = DateReadings(problem="保存下来的识别结果里没有日期原文")
            candidates: list[CandidateInput] = []
        else:
            readings = date_text_readings(
                raw_text,
                date_order=date_order,
                allow_short_year=True,
                assume_order=DATE_ORDER_ASSUMED,
            )
            candidates = [
                CandidateInput(
                    value=reading.text,
                    evidence={
                        "basis_detail": "reparsed_from_saved_text",
                        "raw_text": raw_text,
                        "reading": reading.order,
                        "note": reading.note,
                        "ambiguous": readings.ambiguous,
                        "assumed_order": readings.assumed_order,
                        "date_order": date_order,
                        "date_order_from": order_from,
                        "confirmed_source": confirmed,
                        "provider": payload.get("provider"),
                        "page": (payload.get("source") or {}).get("page_no"),
                        "recognized_at": (
                            view.recognized_at.isoformat()
                            if view.recognized_at
                            else None
                        ),
                    },
                )
                for reading in readings.readings
            ]

        payload["date_order"] = {
            "value": date_order,
            "from": order_from,
            "confirmed_source": confirmed,
        }
        payload["date_readings"] = {
            "raw_text": raw_text,
            "ambiguous": readings.ambiguous,
            "assumed_order": readings.assumed_order,
            "problem": readings.problem,
            "readings": [
                {"value": item.text, "order": item.order, "note": item.note}
                for item in readings.readings
            ],
        }
        notes = list(payload.get("notes") or [])
        notes.append(
            f"按已确认商家「{view.decided_source_name}」的 date_order="
            f"{date_order} 用保存的原文重新解析日期，**没有重新调用模型**。"
        )
        payload["notes"] = notes

        item = self._intake.record_recognition(
            view.id,
            payload=payload,
            candidates={INTAKE_CANDIDATE_FIELD_PURCHASE_DATE: candidates},
            basis=INTAKE_CANDIDATE_BASIS_PAPER,
            # 这一次没有再读一遍原件：保留原来的识别时刻，不谎报一个新的。
            read_at=view.recognized_at,
        )
        logger.info(
            "intake date reparsed item=%s order=%s candidates=%s",
            item.id,
            date_order,
            len(candidates),
        )
        return RecognizedItem(
            item=item,
            outcome=OUTCOME_RECOGNIZED,
            provider=payload.get("provider"),
            calls=0,
            notes=tuple(notes[-1:]),
        )

    # --- 内部 ---------------------------------------------------------------

    def _date_order_for(
        self, view: IntakeItemView
    ) -> tuple[str, str, str | None]:
        """这一页的日/月顺序该按什么读。返回（取值, 依据, 已确认的商家名）。

        ⚠️ **只认 `decided_source_name_normalized`**，也就是人工确认过的商家。
        候选里那个商家名一个字都不参与——这是 docs/multifiles.md M05 的第二条子项：
        「没有人工确认商家时，不能仅凭相似度应用该商家的 OCR 路由」。
        """
        normalized = view.decided_source_name_normalized
        if not normalized:
            return DATE_ORDER_STRICT, DATE_ORDER_FROM_DEFAULT, None
        with self._database.unit_of_work() as session:
            route = OcrRouteRepository(session).get(normalized)
            order = (route.date_order if route else None) or DATE_ORDER_STRICT
        return order, DATE_ORDER_FROM_CONFIRMED_SOURCE, normalized

    def _failed(
        self, view: IntakeItemView, code: str, message: str
    ) -> RecognizedItem:
        """把失败写在这**一个内容**上，原件与同批次其它内容一概不动。"""
        item = self._intake.fail_recognition(
            view.id, error_code=code, error_message=message
        )
        return RecognizedItem(item=item, outcome=OUTCOME_FAILED, calls=0)


# --- 候选的提取 -------------------------------------------------------------


def _supplier_candidates(
    result: RecognitionResult,
) -> tuple[list[CandidateInput], str | None]:
    """从识别结果里取商家候选。

    ⚠️ **只接受已识别出来的纸面读法**（`RECOGNIZED`）。`UNREADABLE` 是「这里有
    东西但认不出」、`MISSING` 是「纸上没有」，两者都不产生候选——来源不明就是不明，
    不会被任何建议悄悄填上一个编造的名字（M02 规则三）。

    展示值取 M01 已经定下的那一个：纸面有英文取英文，否则取纸面中文
    （`integrations/dashscope._supplier_from_names` 与 `crosscheck` 的裁决）。
    两种写法的原文都进证据，不丢。
    """
    chosen = result.document_field(FIELD_SUPPLIER)
    english = result.document_field(FIELD_SUPPLIER_ENGLISH)
    chinese = result.document_field(FIELD_SUPPLIER_CHINESE)
    paper = result.document_field(FIELD_SUPPLIER_EVIDENCE)

    if chosen is None or chosen.status is not FieldStatus.RECOGNIZED:
        if chosen is not None and chosen.status is FieldStatus.UNREADABLE:
            return [], (
                "纸面上像是有商家但读不出来（或两个模型读法不一致），"
                "所以**不给商家候选**：这张卡片留在「商家待定」，等人看原件。"
            )
        if result.provider == TEXT_LAYER_PROVIDER:
            # 实测（`samples/real/sample_grocery_supplier_invoices.pdf` 5 页）：
            # 卖方名就是页首一行**没有标签**的标题，而页上唯一带标签的一方是
            # `Bill to:` 的**买方**。所以「挑一个带标签的名字」或「取最上面那行」
            # 都会把买方当成商家——§3.1 明确排除买方。宁可留空让人命名一次，
            # 之后由 M07 的商家记忆与同文件参考接着用。
            return [], (
                "这一页有文字层，但纸面上**没有卖方标签**（Vendor / Sold by / "
                "供应商……），所以不给商家候选。⚠️ 不会退而取页首标题或某个带标签的"
                "名字——单据上带标签的那一方往往是**买方**（`Bill to:`），认错了"
                "不报错。人确认一次来源之后，后续批次由商家记忆接着建议。"
            )
        return [], None

    evidence: dict[str, Any] = {
        "basis_detail": "paper_reading",
        "raw_text": chosen.raw_text,
        "page": chosen.page,
        "supplier_english": _field_text(english),
        "supplier_chinese": _field_text(chinese),
        "paper_evidence": _field_text(paper),
        "provider": result.provider,
        "model": result.model,
        "is_fake": result.is_fake,
    }
    return [
        CandidateInput(
            value=chosen.raw_text or "",
            confidence=chosen.confidence,
            evidence=evidence,
        )
    ], None


def _date_candidates(
    result: RecognitionResult, *, date_order: str
) -> tuple[list[CandidateInput], dict[str, Any]]:
    """从识别结果里取日期候选，歧义时**两条都留**。

    这里刻意不碰「上传日期」与「系统地区」：两者都不是纸面事实。读不出来就是
    读不出来，那张卡片的日期待定，用户可以在整理页上直接定一个日期
    （M02 的 `classify_item`）。
    """
    found = result.document_field(FIELD_INVOICE_DATE)
    if found is None or found.status is not FieldStatus.RECOGNIZED:
        return [], {
            "raw_text": None,
            "ambiguous": False,
            "problem": (
                "纸面上认不出日期" if found is not None else "识别结果里没有日期字段"
            ),
            "readings": [],
        }

    raw_text = found.raw_text or ""
    readings = date_text_readings(
        raw_text,
        date_order=date_order,
        allow_short_year=True,
        # 商家路由说了话时这个参数轮不到（`date_text_readings` 自己会让路）。
        assume_order=DATE_ORDER_ASSUMED,
    )
    payload = {
        "raw_text": raw_text,
        "ambiguous": readings.ambiguous,
        "assumed_order": readings.assumed_order,
        "problem": readings.problem,
        "date_order": date_order,
        "readings": [
            {"value": item.text, "order": item.order, "note": item.note}
            for item in readings.readings
        ],
    }
    candidates = [
        CandidateInput(
            value=reading.text,
            confidence=found.confidence,
            evidence={
                "basis_detail": "paper_reading",
                "raw_text": raw_text,
                "reading": reading.order,
                "note": reading.note,
                "ambiguous": readings.ambiguous,
                "assumed_order": readings.assumed_order,
                "date_order": date_order,
                "page": found.page,
                "provider": result.provider,
                "model": result.model,
                "is_fake": result.is_fake,
            },
        )
        for reading in readings.readings
    ]
    return candidates, payload


def _document_fields_payload(
    result: RecognitionResult, outcome: CrossCheckOutcome | None
) -> dict[str, Any]:
    """抬头字段的原文与出处。与 `invoices` 那边的 `raw_document_fields` 同一个形状，
    这样 M08 把它搬进草稿时不用再翻译一遍。"""
    fields: dict[str, Any] = {}
    for name in DOCUMENT_FIELDS:
        found = result.document_field(name)
        if found is None:
            continue
        verdict = (
            outcome.agreements.get(name) if outcome is not None else None
        )
        fields[name] = {
            "status": str(found.status),
            "raw_text": found.raw_text,
            "confidence": found.confidence,
            "page": found.page,
            "model_values": list(verdict.values) if verdict is not None else [],
            "agreement": verdict.note if verdict is not None else None,
        }
    return fields


def _lines_payload(
    result: RecognitionResult, outcome: CrossCheckOutcome | None
) -> list[dict[str, Any]]:
    """行项目的原文，供 M08 复用而不重复收费。

    ⚠️ 这里**不做任何预填判断**：哪一格能填、哪一行能确认属于审核那一层
    （「识别层不做业务判断」）。本模块只负责把读到的原文与跨模型读法原样留下。
    """
    lines: list[dict[str, Any]] = []
    for index, line in enumerate(result.lines):
        verdicts = (
            outcome.line_agreements[index]
            if outcome is not None and index < len(outcome.line_agreements)
            else {}
        )
        lines.append(
            {
                "index": line.index,
                "page": line.page,
                "raw_text": line.raw_text,
                "fields": {
                    name: {
                        "status": str(found.status),
                        "raw_text": found.raw_text,
                        "confidence": found.confidence,
                        "model_values": list(verdicts[name].values)
                        if name in verdicts
                        else [],
                        "agreement": verdicts[name].note if name in verdicts else None,
                    }
                    for name in LINE_FIELDS
                    if (found := line.get(name)) is not None
                },
            }
        )
    return lines


def _saved_date_raw_text(payload: dict[str, Any]) -> str | None:
    """从保存下来的 payload 里找日期原文。

    两处都看：页内容记在 `fields.invoice_date.raw_text`，表格行记在同一个位置
    （`_recognize_row` 刻意用了同一个键），所以重新解析不必关心这张卡片是页还是行。
    `date_readings.raw_text` 是上一次重新解析写下的，作为兜底。
    """
    fields = payload.get("fields") or {}
    date_field = fields.get(FIELD_INVOICE_DATE) or {}
    raw = _text_or_none(date_field.get("raw_text"))
    if raw is not None:
        return raw
    readings = payload.get("date_readings") or {}
    return _text_or_none(readings.get("raw_text"))


def _field_text(found: Any) -> str | None:
    return found.raw_text if found is not None else None


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _cell_ref(cell: dict[str, Any], row_no: int | None) -> str | None:
    column = cell.get("column")
    if not column or row_no is None:
        return None
    return f"{column}{row_no}"


def _row_evidence(
    view: IntakeItemView,
    row_data: dict[str, Any],
    cell: dict[str, Any],
    *,
    raw_text: str | None,
    reading: str | None = None,
    note: str = "",
    ambiguous: bool = False,
) -> dict[str, Any]:
    """表格行候选的证据：**原单元格**在哪、写着什么。

    「原行号可回查」是 M04 的验收条件之一，这里把同一组坐标带进候选，于是整理页上
    点一条建议就能指回那一格。
    """
    raw_fields = row_data.get("raw_fields") or {}
    return {
        "basis_detail": "spreadsheet_cell",
        "source": PROVIDER_SPREADSHEET,
        "sheet": raw_fields.get("sheet") or view.sheet_name,
        "row": view.row_no,
        "column": cell.get("column"),
        "cell": cell.get("cell") or _cell_ref(cell, view.row_no),
        "header": cell.get("header"),
        "raw_text": raw_text,
        "cell_status": cell.get("status"),
        "reading": reading,
        "ambiguous": ambiguous,
        "note": note,
        "original_name": view.original_name,
    }


# --- 接到 M03 的摄入钩子上 ---------------------------------------------------


class IntakeRecognitionProcessor(IntakeItemProcessor):
    """M03 留的处理器接口在 M05 的实现：**把刚切出来的页排进识别队列**。

    ⚠️ 它刻意**不在这里直接跑识别**。两个理由：

    1. 切页跑在摄入那一条线程上（上传请求之外），而识别**每页要花钱、扫描件一页
       约 5—15 秒**。把它压进切页那一步，十几份文件就会串成一条几分钟的链，而且
       中间失败一次，后面的页连排队的痕迹都没有。
    2. **状态就是队列**（与 M03 的切页同一条）。排好队之后，谁来驱动、一次驱动几个
       由调度器决定：`POST /api/intake-batches/<id>/recognize` 是恢复入口，
       重复驱动安全且免费。

    排队本身是一次便宜的落库写入，不访问网络。
    """

    def __init__(self, intake: IntakeService) -> None:
        self._intake = intake

    def process(self, pages: Sequence[PageSource]) -> None:
        for page in pages:
            self._intake.mark_recognition_pending(page.item_id)
        logger.info("intake recognition queued pages=%s", len(pages))


__all__ = [
    "DATE_ORDER_FROM_CONFIRMED_SOURCE",
    "DATE_ORDER_FROM_DEFAULT",
    "DATE_ORDER_FROM_SPREADSHEET",
    "ERROR_BYTES_MISSING",
    "ERROR_CONTENT_UNSUPPORTED",
    "ERROR_IMAGE_UNREADABLE",
    "ERROR_OCR_FAILED",
    "ERROR_PAGE_IMAGE_MISSING",
    "ERROR_PDF_UNREADABLE",
    "ERROR_ROW_DATA_MISSING",
    "OUTCOME_ALREADY_RECOGNIZED",
    "OUTCOME_FAILED",
    "OUTCOME_RECOGNIZED",
    "PAYLOAD_PURPOSE",
    "PAYLOAD_VERSION",
    "PROVIDER_SPREADSHEET",
    "PROVIDER_TEXT_LAYER",
    "IntakeRecognitionProcessor",
    "IntakeRecognitionService",
    "RecognizedItem",
]
