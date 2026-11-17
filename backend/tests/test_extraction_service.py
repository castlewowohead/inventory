"""提取服务的测试（步骤 08 的分流、分层、上限与任务状态）。

**不访问网络**：识别器用桩件注入，返回预置的识别结果。图像是程序生成的，
因为真实单据含客户数据、不进源码交付（见 `.gitignore`）。真实样本上的验证由
`tools/eval_adapter.py` 与 `tools/e2e_extraction.py` 负责。
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image, ImageDraw

from src.config import Config
from src.contracts import (
    FIELD_ITEM_DESCRIPTION,
    FIELD_LINE_AMOUNT,
    FIELD_QUANTITY,
    FIELD_UNIT,
    FIELD_UNIT_PRICE,
    RecognitionResult,
    RecognizedField,
    RecognizedLine,
    RecognizedPage,
)
from src.db import Database
from src.documents.grid import detect_grid
from src.errors import ConflictError, NotFoundError
from src.integrations.dashscope import OcrServiceError, StructureAnswer
from src.integrations.file_storage import LocalFileStorage
from src.models import (
    DOC_CLASS_HANDWRITTEN_SCAN,
    DOC_CLASS_PRINTED_SCAN,
    EXTRACTION_STATUS_FAILED,
    EXTRACTION_STATUS_SUCCEEDED,
)
from src.services.extraction import (
    PREFILL_CROSSCHECK,
    PREFILL_SUPPRESSED_HANDWRITTEN,
    ExtractionService,
)
from src.services.invoices import InvoiceService, UploadedFile
from tests.conftest import UPLOAD_MAX_BYTES_FOR_TESTS

from datetime import datetime, timezone


# --- 合成样本 ---------------------------------------------------------------


def _printed_page() -> bytes:
    """笔画宽度均匀的一页：本地判别应当判成印刷体。"""
    image = Image.new("RGB", (1400, 1000), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for row in range(22):
        y = 120 + row * 34
        for column in range(44):
            x = 90 + column * 27
            draw.rectangle([x, y, x + 5, y + 15], fill=(15, 15, 15))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _handwritten_page(rows: int = 10, written: tuple[int, ...] | None = None) -> bytes:
    """笔画宽度不均匀、带预印格线的一页：应当判成手写件。"""
    image = Image.new("RGB", (1400, 1800), (250, 248, 228))
    draw = ImageDraw.Draw(image)
    left, right, top = 100, 1300, 200
    step = 120
    for index in range(rows + 1):
        y = top + index * step
        draw.line([(left, y), (right, y)], fill=(40, 90, 60), width=4)
    for index in range(6):
        x = left + index * ((right - left) // 5)
        draw.line([(x, top), (x, top + rows * step)], fill=(40, 90, 60), width=4)
    # 粗细差别很大的笔画 —— 手写的特征。
    # `written` 给定时只往**那几行**里写，用来验第 0 层墨迹闸门；不给就铺满整页
    # （笔画判别需要足够的样本量，这是批次 1 的用法）。
    if written is None:
        for index in range(260):
            x = 140 + (index * 37) % 1100
            y = 240 + (index * 53) % (rows * step - 60)
            width = 2 + (index % 9)
            draw.line([(x, y), (x + 14 + index % 23, y + 7)], fill=(70, 70, 120), width=width)
    else:
        for row in written:
            base = top + (row - 1) * step + step // 2
            for index in range(34):
                x = left + 40 + index * 33
                draw.line(
                    [(x, base - 26), (x + 19, base + 28)],
                    fill=(70, 70, 120),
                    width=3 + index % 6,
                )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _line(index: int, **values: str) -> RecognizedLine:
    fields = []
    for name in (
        FIELD_ITEM_DESCRIPTION,
        FIELD_QUANTITY,
        FIELD_UNIT,
        FIELD_UNIT_PRICE,
        FIELD_LINE_AMOUNT,
    ):
        text = values.get(name)
        fields.append(
            RecognizedField.recognized(name, text)
            if text
            else RecognizedField.unreadable(name)
        )
    return RecognizedLine(index=index, page=1, raw_text=json.dumps(values), fields=fields)


class _StubRecognizer:
    """桩识别器：返回预置的行，不访问网络。"""

    provider = "dashscope"
    is_fake = False

    def __init__(self, lines, *, models=("qwen3.5-ocr", "qwen3-vl-plus"), structure=None, fail=None):
        self._lines = lines
        self.models = tuple(models)
        self._structure = structure
        self._fail = fail
        self.structure_calls = 0
        self.content_calls = 0

    def recognize(self, request) -> RecognitionResult:
        self.content_calls += 1
        if self._fail is not None:
            raise self._fail
        return RecognitionResult(
            provider=self.provider,
            is_fake=False,
            model="+".join(self.models),
            recognized_at=datetime.now(timezone.utc),
            pages=(RecognizedPage(number=1),),
            document_fields={},
            lines=tuple(self._lines),
            raw_response="{}",
            warnings=(),
            source_key=request.source_key,
        )

    def recognize_structure(self, request) -> StructureAnswer:
        self.structure_calls += 1
        return self._structure or StructureAnswer(parsed=False, note="桩件不答结构")


@pytest.fixture
def config() -> Config:
    return Config.from_env(
        {
            "APP_ENV": "testing",
            "SECRET_KEY": "test-only",
            "OCR_PROVIDER": "dashscope",
            "OCR_API_KEY": "sk-test",
            "OCR_BASE_URL": "https://example/v1",
            "OCR_WORKER_MODE": "inline",
        }
    )


def _services(database: Database, storage: LocalFileStorage, recognizer, config: Config):
    invoices = InvoiceService(
        database,
        file_storage=storage,
        recognizer=recognizer,
        upload_max_bytes=UPLOAD_MAX_BYTES_FOR_TESTS * 64,
    )
    extraction = ExtractionService(
        database,
        file_storage=storage,
        recognizer=recognizer,
        invoice_service=invoices,
        config=config,
    )
    invoices.attach_extraction(extraction)
    return invoices, extraction


def _upload(invoices: InvoiceService, content: bytes, name: str):
    return invoices.create_invoice(
        [UploadedFile(content=content, original_name=name, content_type=None)]
    )


# --- 分流 -------------------------------------------------------------------


def test_printed_pages_are_prefilled_from_agreeing_models(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    recognizer = _StubRecognizer(
        [_line(1, item_description="Rice", quantity="10", unit="lb",
               unit_price="6.00", line_amount="60.00")]
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _printed_page(), "printed.jpg")

    job = extraction.jobs_for(view.id)[0]
    assert job.status == EXTRACTION_STATUS_SUCCEEDED
    assert job.doc_class == DOC_CLASS_PRINTED_SCAN
    line = view.lines[0]
    assert line.description == "Rice"
    assert str(line.quantity) == "10.0000"
    assert str(line.unit_price) == "6.0000"


def test_handwritten_pages_are_never_prefilled(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """⚠️ 08.3h 的核心：手写件**所有字段**一律不预填。

    步骤 07 确认连中文母语使用者都认不出这些连笔速记，模型读出的任何值都没有
    可信来源——读对是运气，不是能力。模型的职责只剩定位格子、裁剪原图、排顺序。
    """
    recognizer = _StubRecognizer(
        [_line(1, item_description="牛柏叶", quantity="8", unit="x20",
               unit_price="5.85", line_amount="936.00")]
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _handwritten_page(), "hand.jpg")

    job = extraction.jobs_for(view.id)[0]
    assert job.doc_class == DOC_CLASS_HANDWRITTEN_SCAN
    line = view.lines[0]
    assert line.description is None
    assert line.quantity is None
    assert line.unit_price is None
    assert line.line_amount is None
    assert line.purchase_unit is None


def test_the_models_readings_are_still_preserved_on_handwriting(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """不预填 ≠ 丢原文。两个模型读到的东西要留着给人对照（契约第 1 条）。"""
    recognizer = _StubRecognizer([_line(1, quantity="8", line_amount="936.00")])
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _handwritten_page(), "hand.jpg")

    evidence = view.lines[0].raw_fields
    payload = evidence if isinstance(evidence, dict) else json.loads(evidence)
    assert payload["fields"]["quantity"]["raw_text"] == "8"
    assert payload["prompt_sha12"] == "12e0246b0377"


def test_handwriting_asks_for_structure_first(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """第 1 层：先问「哪几行写了东西」，再读内容（08.3b）。

    结构化提问正是诱发编造的那一步，所以先问判断题——步骤 07 实测判断题答得准。
    """
    recognizer = _StubRecognizer([_line(1, quantity="8")])
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    _upload(invoices, _handwritten_page(), "hand.jpg")

    assert recognizer.structure_calls == 1
    assert recognizer.content_calls == 1


def test_printed_pages_do_not_pay_for_the_structure_round(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """印刷件只跑内容这一轮——分流表的「1 轮 / 2 轮」就是这个意思。"""
    recognizer = _StubRecognizer([_line(1, quantity="1", unit_price="2.00")])
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    _upload(invoices, _printed_page(), "printed.jpg")

    assert recognizer.structure_calls == 0


# --- 上限 -------------------------------------------------------------------


def test_row_capacity_warns_instead_of_dropping_lines(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """⚠️ 行容量**只警告，不丢行**。

    原先它是硬上限，理由是「把固定结构填满」那个防线（ChangShun p01 纸上 4 行、
    `qwen-vl-ocr` 报 15 行）。全量实跑推翻了这个取舍：NEW MOY'S p01 纸上 5 行、
    两个模型都读出 5 行，格线检测只认出 3 个行格，于是两条真实采购行
    （$185.56、$167.40，合计 352.96）被当成「确证的编造」删掉，而界面上
    没有任何地方会说出这件事。

    编造的行进草稿是看得见的——纸上没有，老板跳过即可，何况每一行都要他点确认
    才入账；删掉的行是看不见的。所以这里只断言：行都在，而且有一条警告请人核对。
    """
    many = [_line(index, quantity=str(index)) for index in range(1, 61)]
    recognizer = _StubRecognizer(many)
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _handwritten_page(rows=10), "hand.jpg")

    job = extraction.jobs_for(view.id)[0]
    assert len(view.lines) == 60
    warning = next(note for note in job.warnings if "行格" in note)
    assert "⚠️" in warning and "手工加一行" in warning


def test_the_structure_answer_can_tighten_the_cap_further(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    recognizer = _StubRecognizer(
        [_line(index, quantity=str(index)) for index in range(1, 9)],
        structure=StructureAnswer(written_rows=(1, 2), parsed=True, printed_row_slots=10),
    )
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _handwritten_page(rows=10), "hand.jpg")

    assert len(view.lines) == 2


def test_zero_quantity_is_cleared_as_suspicious(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """⚠️ 步骤 07 的列错位实例。

    两个模型都把 IKEA 小票的**税率代码列**当成了数量列，报出 `quantity: 0`，
    并连带把行金额抹成 0 以保持算术自洽——**校验全过**。
    `quantity: 0` 不触发任何算术校验，却让这条采购记录的价格彻底失真。
    """
    recognizer = _StubRecognizer(
        [_line(1, item_description="Salmon", quantity="0",
               unit_price="109.90", line_amount="0")]
    )
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _printed_page(), "printed.jpg")

    line = view.lines[0]
    assert line.quantity is None
    # 未决问题用与步骤 06 导入通路相同的结构，前端只认一种形状。
    codes = {issue.get("code") for issue in line.review_issues}
    assert "suspicious_zero_quantity" in codes


# --- 任务状态 ---------------------------------------------------------------


def test_a_failed_extraction_keeps_the_invoice_and_records_why(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """⚠️ 08.5：超时、限流与失败可查询，且**不丢原文件与草稿**。"""
    recognizer = _StubRecognizer(
        [], fail=OcrServiceError("限流", code="http_429", retryable=True, status=429)
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _printed_page(), "printed.jpg")

    job = extraction.jobs_for(view.id)[0]
    assert job.status == EXTRACTION_STATUS_FAILED
    assert job.error_code == "http_429"
    # 单据与文件都还在——一次失败的提取不该让单据跟着消失。
    detail = invoices.get_invoice(view.id)
    assert len(detail.files) == 1


def test_a_second_extraction_is_refused_while_one_is_queued(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    recognizer = _StubRecognizer([_line(1, quantity="1")])
    invoices, extraction = _services(migrated_database, storage, recognizer, config)
    view = _upload(invoices, _printed_page(), "printed.jpg")

    # inline 模式下任务已经跑完，所以这里能再排一次；排完立刻再排才会撞。
    extraction.schedule(view.id)
    jobs = extraction.jobs_for(view.id)

    assert len(jobs) >= 2


def test_rerunning_after_a_line_was_confirmed_is_refused(
    migrated_database: Database,
    storage: LocalFileStorage,
    config: Config,
    food_item,
) -> None:
    """⚠️ 重新提取会替换草稿行，和已入账的行对不上——而那种错位不报错。"""
    recognizer = _StubRecognizer(
        [_line(1, item_description="Rice", quantity="10", unit="lb",
               unit_price="6.00", line_amount="60.00")]
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)
    view = _upload(invoices, _printed_page(), "printed.jpg")

    from src.services.invoices import ConfirmOption

    outcome = invoices.confirm_lines(
        view.id,
        options=(ConfirmOption(line_id=view.lines[0].id, food_item_id=food_item.id),),
    )
    assert outcome.confirmed_count == 1

    with pytest.raises(ConflictError, match="撤销确认"):
        extraction.schedule(view.id)


def test_rerunning_replaces_untouched_draft_lines_but_keeps_edited_ones(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """人改过的行（version > 1）不能被一次重试抹掉——那是人的劳动。"""
    recognizer = _StubRecognizer(
        [_line(1, item_description="Rice", quantity="10", unit="lb",
               unit_price="6.00", line_amount="60.00"),
         _line(2, item_description="Beans", quantity="5", unit="lb",
               unit_price="2.00", line_amount="10.00")]
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)
    view = _upload(invoices, _printed_page(), "printed.jpg")
    invoices.update_line(view.id, view.lines[0].id, description="老板改过的名字")

    extraction.schedule(view.id)

    detail = invoices.get_invoice(view.id)
    descriptions = [line.description for line in detail.lines]
    assert "老板改过的名字" in descriptions


# --- 供应商路由 -------------------------------------------------------------


def test_a_supplier_route_overrides_the_local_discriminator(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """按供应商路由是主依据——供应商是固定的，这比每次看像素可靠。"""
    recognizer = _StubRecognizer([_line(1, quantity="1", unit_price="2.00")])
    invoices, extraction = _services(migrated_database, storage, recognizer, config)
    extraction.set_route(source_name="Tang Fa", doc_class=DOC_CLASS_HANDWRITTEN_SCAN)

    view = _upload(invoices, _printed_page(), "printed.jpg")
    invoices.update_invoice(view.id, source_name="Tang Fa")
    extraction.schedule(view.id)

    job = extraction.jobs_for(view.id)[0]
    assert job.doc_class == DOC_CLASS_HANDWRITTEN_SCAN
    assert job.route_source == "supplier"


def test_date_order_is_settled_once_per_supplier(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """`6/11/2026` 文本本身定不死；在路由上说一次就不用每张单填一次。"""
    recognizer = _StubRecognizer([_line(1, quantity="1")])
    _, extraction = _services(migrated_database, storage, recognizer, config)

    route = extraction.set_route(
        source_name="Metro Prime", doc_class=DOC_CLASS_PRINTED_SCAN, date_order="mdy"
    )

    assert route.date_order == "mdy"
    assert route.source_name_normalized == "metro prime"


def test_an_invalid_doc_class_is_refused(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    from src.errors import ValidationError

    _, extraction = _services(migrated_database, storage, _StubRecognizer([]), config)

    with pytest.raises(ValidationError, match="doc_class"):
        extraction.set_route(source_name="X", doc_class="whatever")


# --- 批次 2：行号锚定与墨迹证据 ---------------------------------------------


class _AnchoringRecognizer(_StubRecognizer):
    """会读行号列与表头的桩识别器。

    `row_cells` / `header_cells` 直接给**两个模型都给出的**那份读数；要模拟两模型
    不一致，传 `row_cells_second`。
    """

    def __init__(self, lines, *, row_cells=None, header_cells=None,
                 row_cells_second=None, **kwargs):
        super().__init__(lines, **kwargs)
        self._row_cells = row_cells
        self._row_cells_second = row_cells_second
        self._header_cells = header_cells
        self.row_number_calls = 0
        self.header_calls = 0

    def read_row_numbers(self, request):
        self.row_number_calls += 1
        if self._row_cells is None:
            return [(name, None) for name in self.models]
        second = self._row_cells_second if self._row_cells_second is not None else self._row_cells
        return [(self.models[0], self._row_cells), (self.models[1], second)]

    def read_headers(self, request):
        self.header_calls += 1
        if self._header_cells is None:
            return [(name, None) for name in self.models]
        return [(name, self._header_cells) for name in self.models]


def _anchor_cells(bands: int, data_from: int, rows: int):
    """前 data_from-1 条行带没有行号，接下来 rows 条印着 1..rows，其余没有。"""
    cells = [None] * bands
    for offset in range(rows):
        cells[data_from - 1 + offset] = offset + 1
    return tuple(cells)


def test_an_anchored_page_keeps_row_evidence_without_crop_boxes(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """行号与墨迹证据保留，但不再为字段生成裁图坐标。"""
    page = _handwritten_page(rows=10, written=(1, 2))
    grid = detect_grid(Image.open(io.BytesIO(page)))
    cells = _anchor_cells(grid.row_capacity, 1, grid.row_capacity)
    recognizer = _AnchoringRecognizer(
        [_line(1, item_description="A"), _line(2, item_description="B")],
        row_cells=cells,
        header_cells=tuple(
            ["item_description", "quantity", "unit_price", "line_amount", "other"][: len(grid.columns)]
        ),
    )
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, page, "anchored.jpg")

    assert recognizer.row_number_calls == 1
    first = view.lines[0]
    assert first.raw_fields["grid"]["row_anchor"] == "row_numbers"
    assert first.raw_fields["printed_row"] == 1
    assert recognizer.header_calls == 0
    assert all(
        payload["box"] is None and payload["box_source"] is None
        for payload in first.raw_fields["fields"].values()
    )


def test_models_disagreeing_on_row_numbers_falls_back_to_batch_one(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """⚠️ 锚定差一行是**静默**错误，所以两个模型对不上就退回不给坐标。"""
    page = _handwritten_page(rows=10)
    grid = detect_grid(Image.open(io.BytesIO(page)))
    bands = grid.row_capacity
    recognizer = _AnchoringRecognizer(
        [_line(1, item_description="A")],
        row_cells=_anchor_cells(bands, 2, bands - 1),
        row_cells_second=_anchor_cells(bands, 3, bands - 2),
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, page, "disagree.jpg")

    first = view.lines[0]
    assert first.raw_fields["grid"]["row_anchor"] == "unestablished"
    assert "printed_row" not in first.raw_fields
    assert all(
        payload["box"] is None for payload in first.raw_fields["fields"].values()
    )


def test_a_page_without_printed_row_numbers_keeps_the_batch_one_behaviour(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """FengYuan 那一家纸上没印行号。读出来全是 null，锚定不成立——正确拒绝。"""
    page = _handwritten_page(rows=10)
    grid = detect_grid(Image.open(io.BytesIO(page)))
    recognizer = _AnchoringRecognizer(
        [_line(1, item_description="A")],
        row_cells=(None,) * grid.row_capacity,
    )
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, page, "nonumbers.jpg")

    assert view.lines[0].raw_fields["grid"]["row_anchor"] == "unestablished"


def test_the_ink_gate_never_deletes_rows_the_model_reported(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """⚠️ **墨迹闸门不当上限用。** 这是全量实跑抓到的代价换来的一条用例。

    一度的做法是：有墨行数比模型报的少就截断。TangFa p14 纸上第 1 行和第 3 行
    都写了字（第 3 行是 `4 x30 / 8.99 / 1079.00`），但那一行铅笔极淡、得分只有
    0.0233 低于阈值 0.040，于是**一条真实采购行被悄悄删掉**。删掉真实数据与填进
    一个错价格是同一类错误——老板都不会收到任何提示。

    闸门因此只做两件事：作为证据记下来，以及支撑行↔商品行的对应（那条判据自验，
    漏判时会拒绝建立对应而不是配错）。硬上限仍然只有行容量。
    """
    page = _handwritten_page(rows=10, written=(1, 2))
    recognizer = _AnchoringRecognizer(
        [_line(index, item_description=f"L{index}") for index in range(1, 6)],
        row_cells=None,
    )
    grid = detect_grid(Image.open(io.BytesIO(page)))
    recognizer._row_cells = _anchor_cells(grid.row_capacity, 1, grid.row_capacity)
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, page, "inkcap.jpg")

    job = extraction.jobs_for(view.id)[0]
    # 闸门只判出 2 行有墨，但模型报的 5 行一条都不许被它删掉。
    assert len(view.lines) == 5
    assert any("不用来截断" in note for note in job.warnings)
    # 行数对不上，于是行↔商品行的对应**拒绝建立**（不配错，也不硬凑）。
    assert all("printed_row" not in line.raw_fields for line in view.lines)


def test_the_anchor_is_not_attempted_on_a_table_with_too_few_bands(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """印刷件发票只切出 2—4 条行带，发请求是白花钱。"""
    recognizer = _AnchoringRecognizer([_line(1, item_description="A")], row_cells=None)
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    _upload(invoices, _printed_page(), "printed.jpg")

    assert recognizer.row_number_calls == 0


def test_extra_ink_below_the_product_rows_still_maps(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """作废斜线、合计、签名压在下面的空行上 —— 那几行**确实有墨**，但不是商品行。

    实测 ChangShun 那一叠几乎每页都这样（p08：纸面 5 行商品，第 6—7 行是斜线与
    合计）。判据因此是「最靠上的 n 行恰好是第 1…n 号」，而不是「有墨行数 == 草稿
    行数」——后者在 17 页里只有 1 页能建立。
    """
    page = _handwritten_page(rows=10, written=(1, 2, 6))
    grid = detect_grid(Image.open(io.BytesIO(page)))
    recognizer = _AnchoringRecognizer(
        [_line(1, item_description="A"), _line(2, item_description="B")],
        row_cells=_anchor_cells(grid.row_capacity, 1, grid.row_capacity),
    )
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, page, "extraink.jpg")

    assert [line.raw_fields["printed_row"] for line in view.lines] == [1, 2]


def test_a_gap_in_the_written_rows_refuses_the_mapping(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """跳着写（第 1、3 行有字）时判据自动不成立——硬凑就会整体错位一行。"""
    page = _handwritten_page(rows=10, written=(1, 3))
    grid = detect_grid(Image.open(io.BytesIO(page)))
    recognizer = _AnchoringRecognizer(
        [_line(1, item_description="A"), _line(2, item_description="B")],
        row_cells=_anchor_cells(grid.row_capacity, 1, grid.row_capacity),
    )
    invoices, _ = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, page, "gap.jpg")

    assert all("printed_row" not in line.raw_fields for line in view.lines)


# --- 关键词认食材（步骤 10.C）----------------------------------------------


def _line_of(invoices: InvoiceService, invoice_id: int, line_no: int = 1):
    view = invoices.get_invoice(invoice_id)
    return next(line for line in view.lines if line.line_no == line_no)


def test_a_printed_line_auto_creates_and_selects_a_food_item(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """印刷件：关键词剥出来、单位归一得到、分类由词表猜出来 → 自动建好并选中。

    描述与单位是 NewMoys 真实单据上的写法（`samples/real/ground_truth/`）：
    `Beef Stomach - 牛肚6X44.10` + `lbs.`。
    """
    recognizer = _StubRecognizer(
        [
            _line(
                1,
                item_description="Beef Stomach - 牛肚6X44.10",
                quantity="180",
                unit="lbs.",
                unit_price="2.50",
                line_amount="450.00",
            )
        ]
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _printed_page(), "printed.jpg")
    extraction.run_pending(limit=5)

    line = _line_of(invoices, view.id)
    assert line.food_item is not None
    assert line.food_item.name == "Beef Stomach"
    assert line.food_item.name_zh == "牛肚"
    # `lbs.` 归一成 `lb`——句点是排版习惯，不是单位的一部分。
    assert line.food_item.base_unit == "lb"
    assert line.food_item.category.name == "Meat & Poultry"
    # ⚠️ 标「待确认」：名字是剥出来的、分类是猜的，两样都可能不对。
    assert line.food_item.needs_review is True
    # ⚠️ **仍然没有替老板确认这一行**（与 05.6 同口径）。
    assert line.status == "pending"
    assert line.confirmed_at is None


def test_two_lines_with_the_same_description_share_one_auto_created_item(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """同一张单上重复的商品不该建出两条食材，也不该撞唯一约束。

    真实样本里单内重复是有的（Target 小票 14 行 13 个不同描述）。
    """
    row = dict(
        item_description="Beef Tripe - 牛柏叶",
        quantity="120",
        unit="lbs.",
        unit_price="3.00",
        line_amount="360.00",
    )
    recognizer = _StubRecognizer([_line(1, **row), _line(2, **row)])
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _printed_page(), "printed.jpg")
    extraction.run_pending(limit=5)

    first = _line_of(invoices, view.id, 1)
    second = _line_of(invoices, view.id, 2)
    assert first.food_item is not None
    assert second.food_item is not None
    assert first.food_item.id == second.food_item.id


def test_a_line_without_a_usable_unit_is_not_auto_created(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """⚠️ **绝不拿 `each` 兜底。**

    `samples/real/ground_truth/` 的 48 行里，34 行小票纸面**根本没印单位**、
    1 行是包装单位「箱」——占 73%。给它们随便填一个 `each`，和步骤 09
    「算不出来时标准化价格是 `null` 而不是 `0`」是同一类错误：把"不知道"写成一个
    具体的值，不报错、不可察觉，然后喂进换算。

    做的是**把表单预填好**，让老板点一下基础单位。
    """
    recognizer = _StubRecognizer(
        [
            _line(
                1,
                item_description="箱　麻薯粉 WHEAT FLOUR 1000g x 10",
                quantity="20",
                unit="箱",
                unit_price="52.00",
                line_amount="1040.00",
            )
        ]
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _printed_page(), "printed.jpg")
    extraction.run_pending(limit=5)

    line = _line_of(invoices, view.id)
    assert line.food_item is None
    # 猜测照样给出来，界面据此把新建表单预填好、光标落在基础单位那一格。
    assert line.food_item_match is not None
    assert line.food_item_match["reason"] == "needs_base_unit"
    assert line.food_item_match["suggested_name"] == "WHEAT FLOUR"
    assert line.food_item_match["suggested_name_zh"] == "麻薯粉"
    assert line.food_item_match["suggested_category_id"] is not None
    # 单位原文留着给老板看——他要据此判断该填什么。
    assert line.food_item_match["unit_raw"] == "箱"


def test_handwritten_pages_never_get_a_keyword_guess(
    migrated_database: Database, storage: LocalFileStorage, config: Config
) -> None:
    """⚠️ 闸门是**单据类别**，不是模型置信度——与 08.3h 同一条，不另开口子。

    步骤 07 实测中文商品名跨模型分歧率 **96.0%**：手写件上模型读出的任何名字几乎
    一定是错的，而给老板一个错名字比给他一张裁剪图更慢（他得先删掉错的再重填）。
    """
    recognizer = _StubRecognizer(
        [
            _line(
                1,
                item_description="Beef Stomach - 牛肚",
                quantity="180",
                unit="lbs.",
                unit_price="2.50",
                line_amount="450.00",
            )
        ]
    )
    invoices, extraction = _services(migrated_database, storage, recognizer, config)

    view = _upload(invoices, _handwritten_page(), "handwritten.jpg")
    extraction.run_pending(limit=5)

    line = _line_of(invoices, view.id)
    assert line.food_item is None
    # 手写件连数值都不预填，这一点没变。
    assert line.quantity is None
    assert line.unit_price is None
