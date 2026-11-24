"""整理批次的日期/商家候选与日期解析（多文件计划 M05）：Service 层。

本模块验证的是**候选**，不是决定：读出来的日期与商家一个字都不会落进
`intake_items.decided_*`。四条 M05 验收条件各自有用例：

1. 商家未知且日期歧义时**两项可独立待定**（`6/11/2026` 留两条读法、商家 0 条候选）。
2. 随后**确认商家并设 `date_order`** 可重新得到一个确定的日期，**不再调模型**。
3. **重新打开批次不重复调用模型**（已经 `done` 的内容一次调用都不发生）。
4. **提取失败不丢原件**（失败只写在这一个内容上，字节与别的内容照常）。

样本说明：PDF 由本模块手写字节当场造出来（`text_pdf` / `scan_pdf`）、图片由 Pillow
画出来、CSV 由字符串拼出来，**全是合成样本**；商家名一律是 `Costco Wholesale`、
`Hua Feng Market` 这类合成或公开字符串，没有引用 `samples/real/` 的客户原件
（那个目录被 Git 忽略，测试不能依赖它存在）。

⚠️ 识别内容由**测试替身**给出：真实模型的产出每次都可能不同，拿它当断言依据等于
把测试绑在一次调用的运气上。真实 OCR 的实跑结果另见 `docs/multifiles.md` M05 的
验证记录——那里才有「新配置在真实原件上读出了什么」。
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Sequence

import pytest
from PIL import Image

from src.contracts import (
    FIELD_INVOICE_DATE,
    FIELD_SUPPLIER,
    FIELD_SUPPLIER_CHINESE,
    FIELD_SUPPLIER_ENGLISH,
    FIELD_SUPPLIER_EVIDENCE,
    FieldEvidence,
    FieldStatus,
    RecognitionRequest,
    RecognitionResult,
    RecognizedField,
    RecognizedLine,
    RecognizedPage,
)
from src.errors import ConflictError, NotFoundError, ValidationError
from src.integrations.dashscope import OcrServiceError
from src.integrations.file_storage import LocalFileStorage
from src.models import (
    INTAKE_CANDIDATE_BASIS_PAPER,
    INTAKE_CANDIDATE_FIELD_PURCHASE_DATE,
    INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
    INTAKE_RECOGNITION_STATUS_DONE,
    INTAKE_RECOGNITION_STATUS_FAILED,
    INTAKE_RECOGNITION_STATUS_PENDING,
)
from src.services.extraction import ExtractionService
from src.services.intake import IntakeService
from src.services.intake_ingest import IntakeIngestService
from src.services.intake_recognition import (
    DATE_ORDER_FROM_CONFIRMED_SOURCE,
    DATE_ORDER_FROM_DEFAULT,
    ERROR_BYTES_MISSING,
    ERROR_CONTENT_UNSUPPORTED,
    ERROR_OCR_FAILED,
    ERROR_PAGE_IMAGE_MISSING,
    OUTCOME_ALREADY_RECOGNIZED,
    OUTCOME_FAILED,
    OUTCOME_RECOGNIZED,
    PROVIDER_SPREADSHEET,
    PROVIDER_TEXT_LAYER,
    IntakeRecognitionProcessor,
    IntakeRecognitionService,
)
from src.services.invoices import UploadedFile

# --- 合成样本 ---------------------------------------------------------------


def text_pdf(
    pages: Sequence[Sequence[tuple[float, float, str]]],
    *,
    width: int = 612,
    height: int = 792,
) -> bytes:
    """造一份**带真实文字层**的 PDF，每个文本项自带坐标。

    为什么手写 PDF 字节而不是用一个排版库：本仓库的依赖里没有 PDF 生成器
    （只有 pypdf，它造不出文字层），而「有文字层的 PDF 根本不调 OCR」这条分流
    必须有一份真的带字符的 PDF 才验得了。

    ⚠️ 每个文本项各占一个 `BT ... ET`：挤在同一个 `BT` 里时 pypdf 会把一行合成
    一个片段，于是 `textlayer` 分不出格子（实测过）。
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    content_ids: list[int] = []
    for items in pages:
        parts: list[bytes] = []
        for x, y, text in items:
            escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            parts.append(
                f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET".encode("utf-8")
            )
        stream = b"\n".join(parts)
        content_ids.append(
            add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        )
    pages_id = len(objects) + len(pages) + 1
    page_ids = [
        add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, width, height, font, content_id)
        )
        for content_id in content_ids
    ]
    kids = b" ".join(b"%d 0 R" % page_id for page_id in page_ids)
    assert add(b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_ids), kids)) == (
        pages_id
    )
    root = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number)
        out.write(body)
        out.write(b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, root, xref)
    )
    return out.getvalue()


INVOICE_PAGE = [
    (72, 740, "Vendor: Costco Wholesale"),
    (72, 715, "Date: 6/11/2026"),
    (72, 640, "Description"),
    (260, 640, "Qty"),
    (320, 640, "Unit"),
    (380, 640, "Unit Price"),
    (470, 640, "Amount"),
    (72, 620, "Ground Beef"),
    (260, 620, "10"),
    (320, 620, "lb"),
    (380, 620, "6.00"),
    (470, 620, "60.00"),
]
"""一页合成的数字 PDF：商家与**歧义日期**都印在纸上，下面是一张单行的明细表。"""

SECOND_PAGE = [
    (72, 740, "Vendor: Hua Feng Market"),
    (72, 715, "Date: 2026-11-09"),
]
"""同一份 PDF 的第二页写着**另一家商家、另一个日期**——一份文件的不同页落进不同
组，正是现有 `invoice_files` 表达不了的那件事。"""


def scan_pdf(pages: int = 1) -> bytes:
    """造一份**没有文字层**的 PDF（空白页）：扫描件在结构上就是这样。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def jpeg_bytes(width: int = 120, height: int = 160) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (250, 248, 230)).save(buffer, format="JPEG")
    return buffer.getvalue()


def upload_of(name: str, content: bytes, content_type: str | None = None) -> UploadedFile:
    return UploadedFile(content=content, original_name=name, content_type=content_type)


# --- 识别的测试替身 ---------------------------------------------------------


class StubRecognizer:
    """按用例要求返回固定抬头的识别器，并**数清自己被调了几次**。

    数调用次数是本模块最要紧的一件事：「不对同一页重复收费」只有在能断言
    `calls == 0` 的时候才算验过，否则就只是一句承诺。
    """

    provider = "stub-provider"
    is_fake = False
    models = ("stub-model-a", "stub-model-b")

    def __init__(
        self,
        *,
        supplier: tuple[FieldStatus, str | None] = (
            FieldStatus.RECOGNIZED,
            "Costco Wholesale",
        ),
        supplier_english: str | None = "Costco Wholesale",
        supplier_chinese: str | None = "好市多",
        date: tuple[FieldStatus, str | None] = (FieldStatus.RECOGNIZED, "6/11/2026"),
        error: OcrServiceError | None = None,
        lines: int = 1,
    ) -> None:
        self.supplier = supplier
        self.supplier_english = supplier_english
        self.supplier_chinese = supplier_chinese
        self.date = date
        self.error = error
        self.lines = lines
        self.calls: list[RecognitionRequest] = []

    @property
    def model(self) -> str:
        return self.models[0]

    def recognize(self, request: RecognitionRequest) -> RecognitionResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return RecognitionResult(
            provider=self.provider,
            is_fake=False,
            model=self.model,
            recognized_at=datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc),
            pages=(RecognizedPage(number=1, text="stub page", width=1700, height=2200),),
            document_fields=self._document_fields(),
            lines=self._lines(),
            raw_response='{"stub": true}',
            source_key=request.source_key,
        )

    def _document_fields(self) -> tuple[RecognizedField, ...]:
        evidence = FieldEvidence(page=1)
        fields = [_field(FIELD_SUPPLIER, self.supplier, evidence)]
        fields.append(
            _field(
                FIELD_SUPPLIER_ENGLISH,
                (
                    (FieldStatus.RECOGNIZED, self.supplier_english)
                    if self.supplier_english
                    else (FieldStatus.MISSING, None)
                ),
                evidence,
            )
        )
        fields.append(
            _field(
                FIELD_SUPPLIER_CHINESE,
                (
                    (FieldStatus.RECOGNIZED, self.supplier_chinese)
                    if self.supplier_chinese
                    else (FieldStatus.MISSING, None)
                ),
                evidence,
            )
        )
        fields.append(
            _field(
                FIELD_SUPPLIER_EVIDENCE,
                (FieldStatus.RECOGNIZED, "Sold by: Costco Wholesale"),
                evidence,
            )
        )
        fields.append(_field(FIELD_INVOICE_DATE, self.date, evidence))
        return tuple(fields)

    def _lines(self) -> tuple[RecognizedLine, ...]:
        return tuple(
            RecognizedLine(
                index=index,
                page=1,
                raw_text=f"Ground Beef {index} lb 6.00",
                fields=(
                    RecognizedField.recognized("item_description", "Ground Beef"),
                    RecognizedField.recognized("quantity", str(index)),
                    RecognizedField.recognized("unit", "lb"),
                    RecognizedField.recognized("unit_price", "6.00"),
                    RecognizedField.unreadable("line_amount"),
                ),
            )
            for index in range(1, self.lines + 1)
        )


def _field(
    name: str, value: tuple[FieldStatus, str | None], evidence: FieldEvidence
) -> RecognizedField:
    status, text = value
    if status is FieldStatus.RECOGNIZED and text:
        return RecognizedField.recognized(name, text, evidence=evidence)
    if status is FieldStatus.UNREADABLE:
        return RecognizedField.unreadable(name, evidence=evidence)
    return RecognizedField.missing(name)


# --- 夹具 -------------------------------------------------------------------


@pytest.fixture
def stub() -> StubRecognizer:
    return StubRecognizer()


@pytest.fixture
def stub_recognition(
    intake_service: IntakeService,
    migrated_database,
    storage: LocalFileStorage,
    stub: StubRecognizer,
) -> IntakeRecognitionService:
    """用测试替身当识别器的识别服务。调用次数可断言。"""
    return IntakeRecognitionService(
        intake_service,
        database=migrated_database,
        file_storage=storage,
        recognizer=stub,
    )


@pytest.fixture
def extraction_service(
    migrated_database, storage: LocalFileStorage, invoice_service, config
) -> ExtractionService:
    """只为了能在测试里给某家商家设 `date_order`（`ocr_routes` 的 owner 在这里）。"""
    from src.integrations.ocr import FakeRecognizer

    return ExtractionService(
        migrated_database,
        file_storage=storage,
        recognizer=FakeRecognizer(),
        invoice_service=invoice_service,
        config=config,
    )


@pytest.fixture
def intake_spreadsheet_service(migrated_database, storage: LocalFileStorage):
    """M04 的表格摄入服务。与识别服务共用同一个库和存储。

    M04 自己的用例走 HTTP（`tests/test_intake_spreadsheets.py`），这里只需要
    「把合成 CSV 变成批次里的行」这一步，所以直接装一个 Service。
    """
    from src.services.intake_spreadsheets import IntakeSpreadsheetService

    return IntakeSpreadsheetService(migrated_database, storage, max_bytes=5_000_000)


def ingest(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    name: str,
    content: bytes,
) -> tuple[int, list[int]]:
    """建一个批次、传一个文件、切好页。返回（批次 id, 页内容 id 列表）。"""
    batch = intake_service.create_batch()
    outcome = intake_ingest_service.upload(batch.id, upload_of(name, content))
    assert outcome.ingested is not None
    return batch.id, [item.id for item in outcome.ingested.items]


# --- 数字 PDF：文字层，零成本 -----------------------------------------------


def test_a_digital_pdf_page_is_read_from_the_text_layer_without_calling_any_model(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """有文字层就**绝不**调 OCR：字符来自文件本身，比任何模型都准，而且零成本。"""
    batch_id, items = ingest(
        intake_ingest_service, intake_service, "invoice.pdf", text_pdf([INVOICE_PAGE])
    )

    result = stub_recognition.recognize_item(items[0])

    assert result.outcome == OUTCOME_RECOGNIZED
    assert result.provider == PROVIDER_TEXT_LAYER
    assert result.calls == 0
    assert stub.calls == []  # 一次网络调用都没有发生
    assert result.source_candidates == ("Costco Wholesale",)
    payload = result.item.recognition_payload or {}
    assert payload["source"]["has_text_layer"] is True
    assert payload["rounds"] == ["text_layer"]
    assert payload["prompt_sha12"] is None


def test_two_pages_of_one_pdf_get_their_own_supplier_and_date_candidates(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """一份 PDF 的两页写着两家商家、两个日期 → 两页各自拿到自己的候选。

    这正是现有 `invoice_files`（一份文件只属一张单据）表达不了的那件事。
    """
    batch_id, items = ingest(
        intake_ingest_service,
        intake_service,
        "mixed.pdf",
        text_pdf([INVOICE_PAGE, SECOND_PAGE]),
    )
    assert len(items) == 2

    first = stub_recognition.recognize_item(items[0])
    second = stub_recognition.recognize_item(items[1])

    assert first.source_candidates == ("Costco Wholesale",)
    assert second.source_candidates == ("Hua Feng Market",)
    # 第 2 页的日期是 ISO 写法，没有歧义 → 恰好一条候选。
    assert second.date_candidates == ("2026-11-09",)
    assert (first.calls, second.calls) == (0, 0)


def test_the_line_items_from_the_text_layer_are_kept_for_the_review_step(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """明细行一起存下来：M08 据此建草稿，而**不是**把同一页再读一遍。

    ⚠️ payload 自报 `purpose` / `rounds`，所以 M08 看得出这是候选那一轮的产出，
    不会把它当成已经过步骤 08 那几层校验的结果。
    """
    _, items = ingest(
        intake_ingest_service, intake_service, "invoice.pdf", text_pdf([INVOICE_PAGE])
    )

    result = stub_recognition.recognize_item(items[0])

    payload = result.item.recognition_payload or {}
    assert payload["purpose"] == "intake_grouping"
    assert len(payload["lines"]) == 1
    first_line = payload["lines"][0]
    assert first_line["fields"]["item_description"]["raw_text"] == "Ground Beef"
    assert first_line["fields"]["line_amount"]["raw_text"] == "60.00"


# --- 验收条件 1：商家未知且日期歧义时两项独立待定 ---------------------------


def test_an_ambiguous_date_reads_as_month_first_and_an_unknown_supplier_stays_unknown(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """`6/11/2026` 按默认的月/日/年读成 **6 月 11 日**；商家读不出就是 0 条候选。

    第 15 节：本项目服务的是美国餐厅，纸上的 `6/11` 就是 6 月 11 日。原文与
    「这是默认读法」都留在证据里，商家确认后仍可重新解析成另一种。
    """
    stub.supplier = (FieldStatus.UNREADABLE, None)
    stub.supplier_english = None
    stub.supplier_chinese = None
    _, items = ingest(
        intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes()
    )

    result = stub_recognition.recognize_item(items[0])

    assert result.date_candidates == ("2026-06-11",)
    assert result.source_candidates == ()
    payload = result.item.recognition_payload or {}
    # 文本确实定不死，这件事仍然如实记着——只是本项目已经替它定了默认读法。
    assert payload["date_readings"]["ambiguous"] is True
    assert payload["date_readings"]["assumed_order"] == "mdy"
    assert payload["date_readings"]["raw_text"] == "6/11/2026"
    assert payload["date_order"]["from"] == DATE_ORDER_FROM_DEFAULT
    assert payload["date_order"]["value"] == "strict"
    # 读不出商家时要说得出「纸面上像是有但读不出」，而不是一句泛泛的「未知」。
    assert any("读不出来" in note for note in result.notes)


def test_candidates_never_become_decisions(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """识别完之后 `decided_*` 仍然全空，整页还在「待归类」里。

    docs/multifiles.md 第 4 节：系统建议与人工确认必须显著分开。再有把握的候选也
    要人点一下。
    """
    batch_id, items = ingest(
        intake_ingest_service, intake_service, "invoice.pdf", text_pdf([INVOICE_PAGE])
    )

    stub_recognition.recognize_item(items[0])

    item = intake_service.get_item(items[0])
    assert item.decided_purchase_date is None
    assert item.decided_source_name is None
    groups = intake_service.list_groups(batch_id)
    assert [(group.purchase_date, group.source_name) for group in groups] == [
        (None, None)
    ]


def test_both_paper_spellings_of_the_supplier_survive_in_the_candidate_evidence(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """展示名取纸面英文（M01 的口径），但**两种写法的原文都留着**。"""
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())

    result = stub_recognition.recognize_item(items[0])

    candidate = next(
        item
        for item in result.item.candidates
        if item.field == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
    )
    assert candidate.value == "Costco Wholesale"
    assert candidate.basis == INTAKE_CANDIDATE_BASIS_PAPER
    assert candidate.evidence is not None
    assert candidate.evidence["supplier_english"] == "Costco Wholesale"
    assert candidate.evidence["supplier_chinese"] == "好市多"
    assert candidate.evidence["paper_evidence"] == "Sold by: Costco Wholesale"


def test_a_missing_supplier_produces_no_candidate_and_no_invented_name(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """纸上没有商家 → 0 条候选。**不会被任何建议悄悄填上一个编造的名字。**"""
    stub.supplier = (FieldStatus.MISSING, None)
    stub.supplier_english = None
    stub.supplier_chinese = None
    _, items = ingest(intake_ingest_service, intake_service, "receipt.jpg", jpeg_bytes())

    result = stub_recognition.recognize_item(items[0])

    assert result.source_candidates == ()
    assert result.item.recognition_status == INTAKE_RECOGNITION_STATUS_DONE


def test_a_page_with_nothing_readable_is_done_not_pending(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """读完发现纸上什么都没有 → `done` + 0 条候选，**不是** `pending`。

    这两件事必须分得开，否则重新打开批次时只能把所有没候选的页再送一遍模型——
    那就是对同一页重复收费。
    """
    stub.supplier = (FieldStatus.MISSING, None)
    stub.supplier_english = None
    stub.supplier_chinese = None
    stub.date = (FieldStatus.MISSING, None)
    _, items = ingest(intake_ingest_service, intake_service, "blank.jpg", jpeg_bytes())

    result = stub_recognition.recognize_item(items[0])

    assert result.item.recognition_status == INTAKE_RECOGNITION_STATUS_DONE
    assert result.item.candidates == ()
    assert stub_recognition.recognize_item(items[0]).outcome == (
        OUTCOME_ALREADY_RECOGNIZED
    )
    assert len(stub.calls) == 1  # 第二次一个请求都没发


# --- 验收条件 2：确认商家 + date_order → 重新得到日期，不再调模型 -----------


def test_an_unconfirmed_supplier_candidate_never_applies_that_suppliers_date_order(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    extraction_service: ExtractionService,
) -> None:
    """这家的 `date_order=dmy` 已经设好了，但**商家还没人工确认** → 不套用它。

    M05 第二条子项：没有人工确认商家时，不能仅凭相似度应用该商家的 OCR 路由。
    拿一个猜出来的商家去定日历日，猜错不报错——它只是把这笔采购算进错误的月份。

    ⚠️ 这家故意设成 `dmy`：如果路由被错误地套上去，`6/11/2026` 会变成 11 月 6 日；
    没套上去时它落到项目默认的月/日/年，仍是 6 月 11 日（第 15 节）。两者不同，
    这条断言才分得出真假。
    """
    extraction_service.set_route(
        source_name="Costco Wholesale", doc_class="printed_scan", date_order="dmy"
    )
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())

    result = stub_recognition.recognize_item(items[0])

    # 候选里明明有 `Costco Wholesale`，但它没有被用来解歧义。
    assert result.source_candidates == ("Costco Wholesale",)
    assert result.date_candidates == ("2026-06-11",)
    payload = result.item.recognition_payload or {}
    assert payload["date_order"]["from"] == DATE_ORDER_FROM_DEFAULT
    assert payload["date_order"]["confirmed_source"] is None


def test_confirming_the_supplier_then_reparsing_resolves_the_date_without_new_calls(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
    extraction_service: ExtractionService,
) -> None:
    """**M05 的核心验收**：确认商家并设 `date_order` 之后，用**保存下来的原文**
    重新解析，歧义消失，而且**一次模型调用都没有再发生**。"""
    extraction_service.set_route(
        source_name="Costco Wholesale", doc_class="printed_scan", date_order="mdy"
    )
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    first = stub_recognition.recognize_item(items[0])
    assert len(stub.calls) == 1  # 一轮内容识别，这个替身是单模型适配器
    read_at = first.item.recognized_at

    # 人在整理页上把这张卡片拖进 Costco 的组里（M02 的分类命令）。
    intake_service.classify_item(items[0], source_name="Costco Wholesale")

    result = stub_recognition.reparse_date(items[0])

    assert result.calls == 0
    assert len(stub.calls) == 1  # 没有第二次调用
    assert result.date_candidates == ("2026-06-11",)  # 6 月 11 日，不再是两条
    payload = result.item.recognition_payload or {}
    assert payload["date_order"]["value"] == "mdy"
    assert payload["date_order"]["from"] == DATE_ORDER_FROM_CONFIRMED_SOURCE
    assert payload["date_order"]["confirmed_source"] == "costco wholesale"
    assert payload["date_readings"]["ambiguous"] is False
    # 这一次没有重新读原件，所以识别时刻保持原样——不谎报一个新的。
    assert result.item.recognized_at == read_at
    assert any("没有重新调用模型" in note for note in result.notes)


def test_reparsing_keeps_the_supplier_candidate_and_the_manual_decision(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    extraction_service: ExtractionService,
) -> None:
    """重新解析只替换日期候选：商家候选与人工决定都不动。"""
    extraction_service.set_route(
        source_name="Costco Wholesale", doc_class="printed_scan", date_order="dmy"
    )
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    stub_recognition.recognize_item(items[0])
    intake_service.classify_item(items[0], source_name="Costco Wholesale")

    result = stub_recognition.reparse_date(items[0])

    assert result.date_candidates == ("2026-11-06",)  # dmy → 11 月 6 日
    assert result.source_candidates == ("Costco Wholesale",)
    assert result.item.decided_source_name == "Costco Wholesale"
    # ⚠️ 候选没有变成决定：日期那一列仍然空着，等人点一下。
    assert result.item.decided_purchase_date is None


def test_reparsing_without_a_confirmed_supplier_is_refused(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """商家还没人工确认就想按某家的习惯读日期 → 报错，而不是悄悄猜一个。"""
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    stub_recognition.recognize_item(items[0])

    with pytest.raises(ValidationError, match="商家还没人工确认"):
        stub_recognition.reparse_date(items[0])


def test_reparsing_a_page_that_was_never_recognised_is_a_conflict(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """没有保存下来的原文就没有可重新解析的东西：该做的是跑识别，不是编一个日期。"""
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    intake_service.classify_item(items[0], source_name="Costco Wholesale")

    with pytest.raises(ConflictError, match="还没有保存下来的识别原文"):
        stub_recognition.reparse_date(items[0])


def test_a_confirmed_supplier_without_a_date_order_falls_back_to_the_project_default(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """确认了商家、但这家没说过日/月顺序 → 落回项目默认的月/日/年，不是两条读法。

    确认商家本身不代表知道这家怎么写日期，所以这里**没有**第二条证据可用；
    第 15 节定下的默认读法正是为这种情形准备的，而不是去猜一个。原文仍留着：
    哪天这家被设成 `dmy`，重新解析就会改口。
    """
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    stub_recognition.recognize_item(items[0])
    intake_service.classify_item(items[0], source_name="Brand New Vendor")

    result = stub_recognition.reparse_date(items[0])

    assert result.date_candidates == ("2026-06-11",)
    payload = result.item.recognition_payload or {}
    assert payload["date_order"]["value"] == "strict"
    assert payload["date_order"]["from"] == DATE_ORDER_FROM_CONFIRMED_SOURCE
    assert payload["date_readings"]["assumed_order"] == "mdy"


# --- 验收条件 3：重新打开批次不重复调用模型 ---------------------------------


def test_draining_the_queue_twice_costs_nothing_the_second_time(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """**M05 的核心验收**：重新打开批次、再驱动一次队列 → 一个请求都不会发出去。

    ⚠️ 第二次队列直接是**空的**，而不是「取到两个、然后各自说早就读过了」：
    `recognition_status` 就是队列，`done` 的内容根本不在队列里。读到 0 条比读到
    两条再跳过更省——连那两次读库都不必发生。
    """
    batch = intake_service.create_batch()
    first = intake_ingest_service.upload(
        batch.id, upload_of("a.jpg", jpeg_bytes(120, 160))
    )
    second = intake_ingest_service.upload(
        batch.id, upload_of("b.jpg", jpeg_bytes(130, 170))
    )
    assert first.ingested and second.ingested

    one = stub_recognition.recognize_waiting(batch_id=batch.id)
    calls_after_first_pass = len(stub.calls)
    two = stub_recognition.recognize_waiting(batch_id=batch.id)

    assert [item.outcome for item in one] == [OUTCOME_RECOGNIZED] * 2
    assert sum(item.calls for item in one) == calls_after_first_pass == 2
    assert two == ()
    assert len(stub.calls) == calls_after_first_pass  # 一次都没有多调


def test_recognising_one_item_twice_reports_already_recognised(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """两个调度器同时驱动队列时，后到的那个如实说「早就读过了」，不重读。"""
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    stub_recognition.recognize_item(items[0])

    again = stub_recognition.recognize_item(items[0])

    assert again.outcome == OUTCOME_ALREADY_RECOGNIZED
    assert again.calls == 0
    assert len(stub.calls) == 1  # 只有第一次那一轮


def test_the_ingest_hook_queues_pages_without_calling_any_model(
    intake_service: IntakeService,
    storage: LocalFileStorage,
    stub: StubRecognizer,
) -> None:
    """M03 留的处理器钩子在 M05 的实现**只排队**，不在切页那一步跑识别。

    切页跑在摄入那条线程上，而识别每页要花钱、扫描件一页约 5—15 秒：压进切页就会
    把十几份文件串成一条几分钟的链。
    """
    ingest_service = IntakeIngestService(
        intake_service,
        file_storage=storage,
        upload_max_bytes=5_000_000,
        item_processor=IntakeRecognitionProcessor(intake_service),
    )
    batch = intake_service.create_batch()

    outcome = ingest_service.upload(batch.id, upload_of("scan.jpg", jpeg_bytes()))

    assert outcome.ingested is not None
    item = intake_service.get_item(outcome.ingested.items[0].id)
    assert item.recognition_status == INTAKE_RECOGNITION_STATUS_PENDING
    assert stub.calls == []  # 排队不调模型


def test_a_queued_page_is_picked_up_by_the_queue(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """排进队列（`pending`）与还没排到（NULL）都会被队列取到。"""
    batch_id, items = ingest(
        intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes()
    )
    intake_service.mark_recognition_pending(items[0])

    results = stub_recognition.recognize_waiting(batch_id=batch_id)

    assert [item.item.id for item in results] == items


# --- 验收条件 4：提取失败不丢原件 -------------------------------------------


def test_a_failed_recognition_keeps_the_original_and_can_be_retried(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """**M05 的核心验收**：模型调用失败 → 内容标 `failed` + 原因，**原件照常可回看**，
    重试一次就好了。"""
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    stub.error = OcrServiceError("限流了", code="rate_limited", retryable=True)

    failed = stub_recognition.recognize_item(items[0])

    assert failed.outcome == OUTCOME_FAILED
    assert failed.item.recognition_status == INTAKE_RECOGNITION_STATUS_FAILED
    assert failed.item.recognition_error_code == ERROR_OCR_FAILED
    assert "rate_limited" in (failed.item.recognition_error_message or "")
    # 原件引用一个字节都没动：页还在、还能回看。
    item, stream = intake_ingest_service.open_item(items[0])
    with stream:
        assert stream.read()[:3] == b"\xff\xd8\xff"  # 还是那张 JPEG
    assert item.page_no == 1

    stub.error = None
    retried = stub_recognition.recognize_waiting(
        batch_id=failed.item.batch_id, retry_failed=True
    )

    assert [entry.outcome for entry in retried] == [OUTCOME_RECOGNIZED]
    assert retried[0].source_candidates == ("Costco Wholesale",)


def test_a_failed_item_is_skipped_unless_the_retry_is_asked_for(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """失败的内容不会在每次驱动队列时都被重试一遍——那会把钱花在同一个坏页上。"""
    batch_id, items = ingest(
        intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes()
    )
    stub.error = OcrServiceError("挂了", code="upstream", retryable=False)
    stub_recognition.recognize_item(items[0])
    calls = len(stub.calls)

    assert stub_recognition.recognize_waiting(batch_id=batch_id) == ()
    assert len(stub.calls) == calls


def test_a_failure_on_one_page_does_not_touch_the_other_pages(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """一页读不出来，同一份 PDF 的别的页照常——坏一页不该让人把整批重传。

    这里用一份**没有文字层的空白 PDF**：它取不出整页图，所以每一页都只能如实报
    「读不出日期与商家」，而页引用与原件都还在。
    """
    batch_id, items = ingest(
        intake_ingest_service, intake_service, "blank.pdf", scan_pdf(3)
    )

    results = stub_recognition.recognize_waiting(batch_id=batch_id)

    assert len(results) == 3
    assert {entry.outcome for entry in results} == {OUTCOME_FAILED}
    assert {entry.error_code for entry in results} == {ERROR_PAGE_IMAGE_MISSING}
    # 三页都还在，原件也还在：失败写在内容上，不动字节。
    assert intake_service.list_items(batch_id).total == 3
    ref, stream = intake_ingest_service.open_file(
        intake_service.get_item(items[0]).batch_file_id
    )
    with stream:
        assert stream.read()[:5] == b"%PDF-"


def test_missing_bytes_fail_the_item_and_say_what_to_do(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    storage: LocalFileStorage,
) -> None:
    """字节不见了（盘被清过）→ `bytes_missing` + 一句能照着做的话，不是 500。"""
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    storage.delete(intake_service.get_item(items[0]).storage_key)

    result = stub_recognition.recognize_item(items[0])

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_BYTES_MISSING
    assert "重新上传" in (result.item.recognition_error_message or "")


def test_content_that_is_neither_image_nor_pdf_fails_with_a_readable_reason(
    intake_service: IntakeService,
    storage: LocalFileStorage,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """文件名说是 jpg、字节不是图也不是 PDF → 说清楚，别让人猜。"""
    stored = storage.save(b"not an image at all", original_name="scan.jpg")
    batch = intake_service.create_batch()
    registered = intake_service.register_file(
        batch.id,
        storage_key=stored.key,
        sha256=stored.sha256,
        size=stored.size,
        original_name="scan.jpg",
        content_type="image/jpeg",
    )
    item = intake_service.record_items(registered.ref.id, pages=(1,))[0]

    result = stub_recognition.recognize_item(item.id)

    assert result.error_code == ERROR_CONTENT_UNSUPPORTED


def test_a_removed_file_is_not_recognised_behind_the_users_back(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """用户把文件移出批次了 → 不再为它的页花钱。

    第 16 节起移除是真删，所以这里是「找不到这一页」而不是「它被撤掉了」。
    要守住的那一条没变：**一次模型调用都不许发生**。
    """
    _, items = ingest(intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes())
    intake_service.discard_file(intake_service.get_item(items[0]).batch_file_id)

    with pytest.raises(NotFoundError):
        stub_recognition.recognize_item(items[0])
    assert stub.calls == []


# --- M04 的表格行候选 -------------------------------------------------------


CSV_TWO_DAYS_TWO_SOURCES = (
    "description,quantity,unit,unit_price,date,supplier\n"
    "Ground Beef,10,lb,6.00,2026-11-08,Costco Wholesale\n"
    "Beef Tendon,4,lb,7.50,2026-11-09,Hua Feng Market\n"
    "Napa Cabbage,20,lb,1.20,2026-11-09,\n"
).encode("utf-8")
"""⚠️ **明确标为合成**的 CSV：两个日期、两家商家、一行缺来源。"""

CSV_AMBIGUOUS_DATE = (
    "description,quantity,unit,unit_price,date,supplier\n"
    "Ground Beef,10,lb,6.00,6/11/2026,Costco Wholesale\n"
).encode("utf-8")
"""合成 CSV：日期列写着 `6/11/2026`，而导入时没有选日/月顺序 → 这一行日期待定。"""


def upload_table(
    intake_spreadsheet_service, intake_service: IntakeService, name: str, content: bytes
) -> tuple[int, list[dict]]:
    batch = intake_service.create_batch()
    intake_spreadsheet_service.upload(batch.id, upload_of(name, content), {})
    rows = intake_spreadsheet_service.list_rows(batch.id)
    return batch.id, rows["items"]


def test_a_spreadsheet_row_uses_its_own_cell_as_the_evidence_and_calls_no_model(
    intake_spreadsheet_service,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """表格里写明的日期/商家优先用**原单元格证据**，一行都不经图像 OCR。"""
    batch_id, rows = upload_table(
        intake_spreadsheet_service,
        intake_service,
        "nov.csv",
        CSV_TWO_DAYS_TWO_SOURCES,
    )

    results = stub_recognition.recognize_waiting(batch_id=batch_id)

    assert len(results) == 3
    assert stub.calls == []
    assert all(entry.calls == 0 for entry in results)
    assert all(entry.provider == PROVIDER_SPREADSHEET for entry in results)
    assert [entry.date_candidates for entry in results] == [
        ("2026-11-08",),
        ("2026-11-09",),
        ("2026-11-09",),
    ]
    # 第三行的来源格是空的 → 这一行只有日期候选，来源待定。
    assert [entry.source_candidates for entry in results] == [
        ("Costco Wholesale",),
        ("Hua Feng Market",),
        (),
    ]
    first = next(
        item
        for item in results[0].item.candidates
        if item.field == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
    )
    assert first.evidence is not None
    assert first.evidence["row"] == 2  # 原表格行号可回查
    assert first.evidence["cell"] == "F2"
    assert first.evidence["header"] == "supplier"


def test_an_ambiguous_date_in_a_spreadsheet_cell_reads_as_month_first(
    intake_spreadsheet_service,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
) -> None:
    """表格那一格解析不出来时回到原文，按项目默认的月/日/年读（第 15 节）。

    ⚠️ 默认读法与「系统地区」无关：它是写在代码里的一条产品决定，换一台机器、
    换一个时区都不会变。
    """
    batch_id, rows = upload_table(
        intake_spreadsheet_service, intake_service, "amb.csv", CSV_AMBIGUOUS_DATE
    )

    results = stub_recognition.recognize_waiting(batch_id=batch_id)

    assert len(results) == 1
    assert results[0].date_candidates == ("2026-06-11",)
    payload = results[0].item.recognition_payload or {}
    assert payload["fields"][FIELD_INVOICE_DATE]["ambiguous"] is True
    assert payload["fields"][FIELD_INVOICE_DATE]["raw_text"] == "6/11/2026"
    assert payload["fields"][FIELD_INVOICE_DATE]["cell"] == "E2"


def test_a_row_without_saved_cells_fails_only_that_row(
    intake_spreadsheet_service,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    migrated_database,
) -> None:
    """一行的单元格数据丢了 → 只有这一行 `failed`，别的行照常出候选。

    M05 第三条子项：识别失败不影响其他行。
    """
    batch_id, rows = upload_table(
        intake_spreadsheet_service,
        intake_service,
        "nov.csv",
        CSV_TWO_DAYS_TWO_SOURCES,
    )
    from src.models import IntakeItem

    with migrated_database.unit_of_work() as session:
        session.get(IntakeItem, rows[1]["id"]).row_data = None

    results = stub_recognition.recognize_waiting(batch_id=batch_id)

    by_id = {entry.item.id: entry for entry in results}
    assert by_id[rows[1]["id"]].outcome == OUTCOME_FAILED
    assert by_id[rows[0]["id"]].source_candidates == ("Costco Wholesale",)
    assert by_id[rows[2]["id"]].date_candidates == ("2026-11-09",)


def test_spreadsheet_rows_also_support_reparsing_after_the_supplier_is_confirmed(
    intake_spreadsheet_service,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    extraction_service: ExtractionService,
) -> None:
    """表格行与页走**同一条**重新解析路径：原文存在同一个位置。"""
    extraction_service.set_route(
        source_name="Costco Wholesale", doc_class="digital_pdf", date_order="mdy"
    )
    batch_id, rows = upload_table(
        intake_spreadsheet_service, intake_service, "amb.csv", CSV_AMBIGUOUS_DATE
    )
    stub_recognition.recognize_waiting(batch_id=batch_id)
    intake_service.classify_item(rows[0]["id"], source_name="Costco Wholesale")

    result = stub_recognition.reparse_date(rows[0]["id"])

    assert result.calls == 0
    assert result.date_candidates == ("2026-06-11",)


# --- 重算候选是整组替换，不叠加 ---------------------------------------------


def test_re_recognising_after_a_retry_replaces_candidates_instead_of_piling_them_up(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    stub_recognition: IntakeRecognitionService,
    stub: StubRecognizer,
) -> None:
    """重试一次之后同一层依据上只剩最新结果——叠加会让人看到三个旧值，分不清哪个
    还算数。"""
    batch_id, items = ingest(
        intake_ingest_service, intake_service, "scan.jpg", jpeg_bytes()
    )
    stub_recognition.recognize_item(items[0])
    stub.supplier = (FieldStatus.RECOGNIZED, "Restaurant Depot")
    stub.supplier_english = "Restaurant Depot"
    stub.supplier_chinese = None
    stub.date = (FieldStatus.RECOGNIZED, "2026-11-10")
    intake_service.fail_recognition(
        items[0], error_code="manual_retry", error_message="人工要求重读一次。"
    )

    results = stub_recognition.recognize_waiting(batch_id=batch_id, retry_failed=True)

    assert results[0].source_candidates == ("Restaurant Depot",)
    assert results[0].date_candidates == ("2026-11-10",)
    assert len(results[0].item.candidates) == 2  # 不是 4 条
