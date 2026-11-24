"""图片 / PDF 的批次摄入（多文件计划 M03）：Service 层。

本模块验证的是**字节与页**，不是分类：一份文件进来变成几个可归类内容、每个内容能
不能回指自己那一页、失败之后能不能接着干。日期与商家一个字都不碰——摄入阶段没有
任何依据可言，那是 M05 / M06。

样本说明：这里的 PDF 由 `pypdf.PdfWriter` 当场造出来、图片由 Pillow 当场画出来，
**全是合成样本**，用途是把代码路径与边界验到底。真实客户原件（`samples/real/`）
不进测试：那个目录被 Git 忽略，测试不能依赖它存在。真实五页 PDF 的实跑结果另见
`docs/multifiles.md` M03 的验证记录。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

import pytest
from PIL import Image
from pypdf import PdfWriter

from src.documents.pdf import MAX_PDF_PAGES
from src.errors import ConflictError, NotFoundError, ValidationError
from src.integrations.file_storage import LocalFileStorage
from src.models import (
    INTAKE_FILE_STATUS_FAILED,
    INTAKE_FILE_STATUS_PENDING,
    INTAKE_FILE_STATUS_READY,
    INTAKE_ITEM_KIND_PAGE,
)
from src.services.intake import IntakeService
from src.services.intake_ingest import (
    ERROR_BYTES_MISSING,
    ERROR_CONTENT_UNSUPPORTED,
    ERROR_IMAGE_UNREADABLE,
    ERROR_PDF_UNREADABLE,
    OUTCOME_ALREADY_INGESTED,
    OUTCOME_FAILED,
    OUTCOME_INGESTED,
    BackgroundIngest,
    IntakeIngestService,
    PageSource,
)
from src.services.invoices import InvoiceService, UploadedFile


# --- 合成样本 ---------------------------------------------------------------


def pdf_bytes(pages: int, *, width: int = 612, height: int = 792) -> bytes:
    """造一份有确定页数的 PDF（空白页，没有文字层）。"""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def png_bytes(width: int = 120, height: int = 90) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (240, 240, 230)).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int = 100, height: int = 140) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (250, 248, 230)).save(buffer, format="JPEG")
    return buffer.getvalue()


def upload_of(name: str, content: bytes, content_type: str | None = None) -> UploadedFile:
    return UploadedFile(content=content, original_name=name, content_type=content_type)


def stored_files(storage: LocalFileStorage) -> list[Path]:
    """存储目录下现在有几份字节。「原件字节只存一次」靠数它来验。"""
    root = storage.root
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


class RecordingProcessor:
    """记下收到了哪些页。M05 真正的实现换掉的就是它。"""

    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[tuple[PageSource, ...]] = []
        self._explode = explode

    def process(self, pages: Sequence[PageSource]) -> None:
        self.calls.append(tuple(pages))
        if self._explode:
            raise RuntimeError("假装 M05 的处理器炸了")

    @property
    def pages(self) -> tuple[PageSource, ...]:
        return tuple(page for call in self.calls for page in call)


@pytest.fixture
def batch_id(intake_service: IntakeService) -> int:
    return intake_service.create_batch().id


# --- 页数是数出来的 ---------------------------------------------------------


def test_a_five_page_pdf_becomes_five_pages_each_pointing_at_its_own_page(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """M03 验收第一条：一份五页 PDF 产生五个可回看的页引用。

    ⚠️ 顺带守住「多页 PDF 不等于一张发票」：摄入只产生页，没有任何单据被建出来。
    """
    outcome = intake_ingest_service.upload(batch_id, upload_of("five.pdf", pdf_bytes(5)))

    assert outcome.queued is False  # InlineIngest：当场切完
    assert outcome.ingested is not None
    assert outcome.ingested.outcome == OUTCOME_INGESTED
    assert outcome.ingested.page_count == 5
    assert outcome.ingested.document_format == "pdf"
    assert outcome.ingested.ref.status == INTAKE_FILE_STATUS_READY
    assert outcome.ingested.ref.item_count == 5

    items = intake_service.list_items(batch_id).items
    assert [item.page_no for item in items] == [1, 2, 3, 4, 5]
    assert {item.kind for item in items} == {INTAKE_ITEM_KIND_PAGE}
    assert {item.row_no for item in items} == {None}
    # 五个内容各自回指同一份原件的不同页，而且 id 互不相同（分组的最小单位）。
    assert len({item.id for item in items}) == 5
    assert {item.original_name for item in items} == {"five.pdf"}
    assert len({item.sha256 for item in items}) == 1
    # 一个字都没有决定：日期与商家留给人。
    assert {item.decided_purchase_date for item in items} == {None}
    assert {item.decided_source_name for item in items} == {None}
    assert all(item.candidates == () for item in items)


def test_an_image_is_exactly_one_page(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """一张图片恒等于一页，页号是 1——那是回看原件的坐标，不是"第几张发票"。"""
    outcome = intake_ingest_service.upload(batch_id, upload_of("shop.png", png_bytes()))

    assert outcome.ingested is not None
    assert outcome.ingested.page_count == 1
    assert outcome.ingested.document_format == "png"
    items = intake_service.list_items(batch_id).items
    assert [(item.kind, item.page_no, item.row_no) for item in items] == [
        (INTAKE_ITEM_KIND_PAGE, 1, None)
    ]


def test_the_real_format_decides_not_the_extension(
    intake_ingest_service: IntakeIngestService, batch_id: int
) -> None:
    """`.jpg` 里装着一份三页 PDF：按**内容**切出三页。

    与 `documents/files.py` 第 1 条同一口径。按扩展名处理会把这份文件切成一页，
    后面两页的原件就再也没人看得到了。
    """
    outcome = intake_ingest_service.upload(
        batch_id, upload_of("photo.jpg", pdf_bytes(3))
    )

    assert outcome.ingested is not None
    assert outcome.ingested.document_format == "pdf"
    assert outcome.ingested.page_count == 3


def test_a_pdf_over_the_page_cap_fails_instead_of_being_half_ingested(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """超过一次处理上限的 PDF 整份失败，不留下半套页。"""
    outcome = intake_ingest_service.upload(
        batch_id, upload_of("ledger.pdf", pdf_bytes(MAX_PDF_PAGES + 1))
    )

    assert outcome.ingested is not None
    assert outcome.ingested.outcome == OUTCOME_FAILED
    assert outcome.ingested.error_code == ERROR_PDF_UNREADABLE
    assert str(MAX_PDF_PAGES) in (outcome.ingested.error_message or "")
    assert intake_service.list_items(batch_id).total == 0


# --- 一份字节只存一次 -------------------------------------------------------


def test_the_same_bytes_in_two_batches_are_stored_once(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    storage: LocalFileStorage,
    batch_id: int,
) -> None:
    """同一份 PDF 传进两个批次：盘上只有一份字节，两边各有自己的页。"""
    content = pdf_bytes(2)
    first = intake_ingest_service.upload(batch_id, upload_of("inv.pdf", content))
    other_batch = intake_service.create_batch().id
    second = intake_ingest_service.upload(other_batch, upload_of("inv.pdf", content))

    assert first.registered.reused_bytes is False
    assert second.registered.reused_bytes is True
    assert second.registered.shared_with_batch_ids == (batch_id,)
    assert len(stored_files(storage)) == 1, "重复的那一份字节应该被删掉"
    assert intake_service.list_items(batch_id).total == 2
    assert intake_service.list_items(other_batch).total == 2


def test_the_same_file_twice_in_one_batch_is_refused_and_leaves_no_orphan(
    intake_ingest_service: IntakeIngestService,
    storage: LocalFileStorage,
    batch_id: int,
) -> None:
    """同一批次里同一份文件只能进来一次；被拒的那次不留下孤儿字节。"""
    content = jpeg_bytes()
    intake_ingest_service.upload(batch_id, upload_of("a.jpg", content))

    with pytest.raises(ConflictError) as error:
        intake_ingest_service.upload(batch_id, upload_of("again.jpg", content))

    assert "已经在本批次里" in str(error.value)
    assert len(stored_files(storage)) == 1


def test_a_rejected_upload_leaves_no_bytes_behind(
    intake_ingest_service: IntakeIngestService,
    storage: LocalFileStorage,
    batch_id: int,
) -> None:
    """校验不过的上传一个字节都不留（白名单、空文件、超限、批次不存在）。"""
    with pytest.raises(ValidationError, match="不支持的文件类型"):
        intake_ingest_service.upload(batch_id, upload_of("notes.txt", b"hello"))
    with pytest.raises(ValidationError, match="是空的"):
        intake_ingest_service.upload(batch_id, upload_of("empty.png", b""))
    with pytest.raises(ValidationError, match="上限"):
        intake_ingest_service.upload(
            batch_id, upload_of("huge.png", b"\x89PNG\r\n\x1a\n" + b"x" * 200_000)
        )
    with pytest.raises(NotFoundError):
        intake_ingest_service.upload(9999, upload_of("ok.png", png_bytes()))

    assert stored_files(storage) == []


def test_csv_is_not_this_step(
    intake_ingest_service: IntakeIngestService, batch_id: int
) -> None:
    """CSV / XLSX 不走这条通路（按行切是 M04），白名单里就没有它们。"""
    with pytest.raises(ValidationError) as error:
        intake_ingest_service.upload(batch_id, upload_of("rows.csv", b"a,b\n1,2\n"))

    assert error.value.details["allowed_extensions"] == ["jpg", "jpeg", "png", "pdf", "heic"]


# --- 一个坏文件不连累别的 ---------------------------------------------------


def test_the_second_file_failing_leaves_the_first_one_and_the_batch_usable(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """M03 验收第二条：第 2 个文件上传失败时，第 1 个文件和批次仍可恢复。

    这正是「一次一个文件」的理由：整批一起提交的话，一份太大就得全部重传。
    """
    first = intake_ingest_service.upload(batch_id, upload_of("one.pdf", pdf_bytes(2)))

    with pytest.raises(ValidationError, match="上限"):
        intake_ingest_service.upload(
            batch_id, upload_of("two.pdf", b"%PDF-1.4" + b"x" * 200_000)
        )

    batch = intake_service.get_batch(batch_id)
    assert batch.status == "open"
    assert [ref.id for ref in batch.files] == [first.ref.id]
    assert batch.files[0].status == INTAKE_FILE_STATUS_READY
    assert intake_service.list_items(batch_id).total == 2
    # 批次照常接着收第三个文件。
    third = intake_ingest_service.upload(batch_id, upload_of("three.png", png_bytes()))
    assert third.ingested is not None
    assert third.ingested.outcome == OUTCOME_INGESTED


def test_a_broken_pdf_fails_only_itself(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    good = intake_ingest_service.upload(batch_id, upload_of("good.pdf", pdf_bytes(3)))
    bad = intake_ingest_service.upload(
        batch_id, upload_of("bad.pdf", b"%PDF-1.4\nnot really a pdf")
    )

    assert bad.ingested is not None
    assert bad.ingested.outcome == OUTCOME_FAILED
    assert bad.ingested.ref.status == INTAKE_FILE_STATUS_FAILED
    assert bad.ingested.error_code == ERROR_PDF_UNREADABLE
    assert bad.ingested.ref.item_count is None

    statuses = {
        ref.original_name: ref.status for ref in intake_service.get_batch(batch_id).files
    }
    assert statuses == {"good.pdf": INTAKE_FILE_STATUS_READY, "bad.pdf": INTAKE_FILE_STATUS_FAILED}
    assert intake_service.list_items(batch_id).total == 3
    assert good.ingested is not None and good.ingested.page_count == 3


def test_a_broken_image_fails_with_a_reason(
    intake_ingest_service: IntakeIngestService, batch_id: int
) -> None:
    outcome = intake_ingest_service.upload(
        batch_id, upload_of("torn.png", b"\x89PNG\r\n\x1a\n" + b"garbage")
    )

    assert outcome.ingested is not None
    assert outcome.ingested.outcome == OUTCOME_FAILED
    assert outcome.ingested.error_code == ERROR_IMAGE_UNREADABLE


def test_content_that_is_neither_image_nor_pdf_fails_and_says_so(
    intake_ingest_service: IntakeIngestService, batch_id: int
) -> None:
    """后缀是 `.png`、内容是别的东西：按内容拒，错误消息指出后缀与内容不一致。"""
    outcome = intake_ingest_service.upload(
        batch_id, upload_of("fake.png", b"PK\x03\x04 this is a zip")
    )

    assert outcome.ingested is not None
    assert outcome.ingested.error_code == ERROR_CONTENT_UNSUPPORTED
    assert "后缀" in (outcome.ingested.error_message or "")


# --- 可恢复 -----------------------------------------------------------------


def test_an_interrupted_upload_is_recoverable_and_the_retry_really_splits_pages(
    intake_service: IntakeService,
    storage: LocalFileStorage,
    batch_id: int,
) -> None:
    """字节取不到不是 500，是一个**写在文件上**的状态；字节回来之后重试就切成。

    模拟的是「上传中断」：引用登记了、切页还没跑，而字节没落全。第一次驱动队列
    失败且说得清，字节回来之后再驱动一次就真的切出了页——重试不是"把状态改一下"。
    """
    service = IntakeIngestService(
        intake_service,
        file_storage=storage,
        upload_max_bytes=64 * 1024,
        scheduler=_NeverRuns(),
    )
    content = pdf_bytes(4)
    outcome = service.upload(batch_id, upload_of("scan.pdf", content))
    file_id = outcome.ref.id
    key = intake_service.get_file(file_id).storage_key
    storage.delete(key)  # 字节没落全

    failed = service.ingest_waiting(batch_id=batch_id)
    assert [item.outcome for item in failed] == [OUTCOME_FAILED]
    assert failed[0].error_code == ERROR_BYTES_MISSING
    assert "重新上传" in (failed[0].error_message or "")
    assert intake_service.list_items(batch_id).total == 0

    # 字节回到原来的 key 上（重传同一份文件的效果），再驱动一次队列。
    path = storage.root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    results = service.ingest_waiting(batch_id=batch_id, retry_failed=True)

    assert [item.outcome for item in results] == [OUTCOME_INGESTED]
    assert results[0].page_count == 4
    assert results[0].ref.error_code is None
    assert intake_service.list_items(batch_id).total == 4


def test_a_file_marked_failed_after_its_pages_were_split_is_not_split_twice(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """页切好之后才被标成失败的文件，重试时不重切。

    重切会撞上 `(batch_file_id, page_no)` 的唯一索引——那会变成一个 500，而且页的
    id 本该是稳定的（整理页上的卡片正按 id 记着人的分类）。
    """
    outcome = intake_ingest_service.upload(batch_id, upload_of("five.pdf", pdf_bytes(5)))
    before = [item.id for item in intake_service.list_items(batch_id).items]
    intake_service.fail_file(
        outcome.ref.id, error_code="external", error_message="别处把它标成了失败"
    )

    results = intake_ingest_service.ingest_waiting(batch_id=batch_id, retry_failed=True)

    assert [item.outcome for item in results] == [OUTCOME_ALREADY_INGESTED]
    assert results[0].page_count == 5
    assert [item.id for item in intake_service.list_items(batch_id).items] == before


def test_draining_the_queue_twice_does_not_re_split_pages(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """重复驱动队列是安全的：已经切好的文件不重切，页的 id 稳定。

    两个调度器同时驱动（入口的后台线程 + 用户点"继续处理"）就是这个情形。
    """
    intake_ingest_service.upload(batch_id, upload_of("a.pdf", pdf_bytes(2)))
    before = [item.id for item in intake_service.list_items(batch_id).items]

    again = intake_ingest_service.ingest_waiting(batch_id=batch_id)
    once_more = intake_ingest_service.ingest_waiting(
        batch_id=batch_id, retry_failed=True
    )

    assert again == ()  # 没有 pending 的了
    assert [item.outcome for item in once_more] == []
    after = [item.id for item in intake_service.list_items(batch_id).items]
    assert after == before


def test_an_already_ingested_file_says_so_instead_of_splitting_again(
    intake_ingest_service: IntakeIngestService, batch_id: int
) -> None:
    outcome = intake_ingest_service.upload(batch_id, upload_of("a.png", png_bytes()))
    assert outcome.ingested is not None

    repeat = intake_ingest_service.ingest_file(outcome.ref.id)

    assert repeat.outcome == OUTCOME_ALREADY_INGESTED
    assert repeat.page_count == 1
    assert repeat.items == ()


def test_a_removed_file_is_not_silently_split_again(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """移除掉的文件不该被偷偷切回来——它在这个批次里已经不存在了（第 16 节）。

    2026-11-24 起移除是**真删**：那条引用没了，所以再去切它是「找不到这个文件」，
    而不是「这个文件被撤掉了」。队列里也不该剩下它。
    """
    outcome = intake_ingest_service.upload(batch_id, upload_of("gone.pdf", pdf_bytes(2)))
    removed = intake_service.discard_file(outcome.ref.id)
    assert removed.bytes_still_referenced is False
    assert removed.removed_item_count == 2

    with pytest.raises(NotFoundError):
        intake_ingest_service.ingest_file(outcome.ref.id)

    assert intake_ingest_service.ingest_waiting(batch_id=batch_id) == ()


# --- 后台跑 -----------------------------------------------------------------


def test_the_background_scheduler_takes_the_splitting_out_of_the_upload_call(
    intake_service: IntakeService, storage: LocalFileStorage, batch_id: int
) -> None:
    """装上后台调度器之后：上传只做「存字节 + 登记」，切页在调用之外完成。

    `queued=True` 时响应里**不带**切页结果——不假装已经切完（没有假百分比，也没有
    假的"已完成"）。等线程池收工之后，状态自己变成 ready。
    """
    scheduler = BackgroundIngest()
    service = IntakeIngestService(
        intake_service,
        file_storage=storage,
        upload_max_bytes=64 * 1024,
        scheduler=scheduler,
    )

    outcome = service.upload(batch_id, upload_of("bg.pdf", pdf_bytes(3)))

    assert outcome.queued is True
    assert outcome.ingested is None
    assert outcome.ref.status == INTAKE_FILE_STATUS_PENDING
    assert outcome.ref.item_count is None

    scheduler.shutdown(wait=True)

    assert intake_service.get_file(outcome.ref.id).status == INTAKE_FILE_STATUS_READY
    assert [item.page_no for item in intake_service.list_items(batch_id).items] == [1, 2, 3]


def test_waiting_files_是队列本身(
    intake_service: IntakeService, storage: LocalFileStorage, batch_id: int
) -> None:
    """没人驱动队列时，文件就停在 `pending`——它可被查到，重启之后也还在。"""
    service = IntakeIngestService(
        intake_service,
        file_storage=storage,
        upload_max_bytes=64 * 1024,
        scheduler=_NeverRuns(),
    )
    service.upload(batch_id, upload_of("later.pdf", pdf_bytes(2)))

    waiting = intake_service.waiting_files(batch_id=batch_id)
    assert [ref.status for ref in waiting] == [INTAKE_FILE_STATUS_PENDING]
    assert intake_service.list_items(batch_id).total == 0

    results = service.ingest_waiting(batch_id=batch_id)
    assert [item.page_count for item in results] == [2]
    assert intake_service.waiting_files(batch_id=batch_id) == ()


class _NeverRuns:
    """假装"交给后台了"但从不执行：模拟进程在切页前就被 kill。"""

    def submit(self, job) -> bool:  # noqa: ANN001
        return True


# --- 交给 M05 的入参 --------------------------------------------------------


def test_the_processor_gets_page_coordinates_and_local_facts_only(
    intake_service: IntakeService, storage: LocalFileStorage, batch_id: int
) -> None:
    """可注入处理器拿到的是**页坐标 + 本地事实**，不含任何日期或商家。

    `has_text_layer` / `has_full_page_image` 是摄入时已经算过的事实，M05 据此分流
    「抽文字层还是送 OCR」，不必把同一份 PDF 再解析一遍。
    """
    processor = RecordingProcessor()
    service = IntakeIngestService(
        intake_service,
        file_storage=storage,
        upload_max_bytes=64 * 1024,
        item_processor=processor,
    )

    service.upload(batch_id, upload_of("blank.pdf", pdf_bytes(2, width=600, height=800)))

    assert len(processor.calls) == 1
    pages = processor.pages
    assert [page.page_no for page in pages] == [1, 2]
    assert {page.batch_id for page in pages} == {batch_id}
    assert all(page.document_format == "pdf" for page in pages)
    assert all(page.storage_key and page.sha256 for page in pages)
    assert [page.width for page in pages] == [600.0, 600.0]
    assert [page.height for page in pages] == [800.0, 800.0]
    # 空白页没有文字层、也没有整页图——如实这么说，不猜。
    assert {page.has_text_layer for page in pages} == {False}
    assert {page.has_full_page_image for page in pages} == {False}
    # PageSource 上没有任何商家/日期字段：摄入阶段不决定它们。
    assert not [name for name in vars(PageSource).get("__annotations__", {})
                if "source" in name or "date" in name]


def test_an_image_page_reports_its_pixel_size_and_no_pdf_facts(
    intake_service: IntakeService, storage: LocalFileStorage, batch_id: int
) -> None:
    processor = RecordingProcessor()
    service = IntakeIngestService(
        intake_service,
        file_storage=storage,
        upload_max_bytes=64 * 1024,
        item_processor=processor,
    )

    service.upload(batch_id, upload_of("note.jpg", jpeg_bytes(100, 140)))

    page = processor.pages[0]
    assert (page.width, page.height) == (100.0, 140.0)
    assert page.has_text_layer is None  # 图片谈不上文字层
    assert page.has_full_page_image is None


def test_a_failing_processor_does_not_lose_the_pages_that_were_recorded(
    intake_service: IntakeService, storage: LocalFileStorage, batch_id: int
) -> None:
    """后续处理器炸了，页照样留着。

    页已经**提交**了；让它连带回滚的话，一个 M05 的 bug 就会把老板已经传上来的
    原件页全部弄丢（与「一条提取任务失败不动草稿」同一条）。
    """
    service = IntakeIngestService(
        intake_service,
        file_storage=storage,
        upload_max_bytes=64 * 1024,
        item_processor=RecordingProcessor(explode=True),
    )

    outcome = service.upload(batch_id, upload_of("boom.pdf", pdf_bytes(3)))

    assert outcome.ingested is not None
    assert outcome.ingested.outcome == OUTCOME_INGESTED
    assert intake_service.get_file(outcome.ref.id).status == INTAKE_FILE_STATUS_READY
    assert intake_service.list_items(batch_id).total == 3


def test_the_default_processor_decides_nothing(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """默认不挂处理器：页切好之后没有任何候选、没有任何决定。"""
    intake_ingest_service.upload(batch_id, upload_of("plain.pdf", pdf_bytes(2)))

    items = intake_service.list_items(batch_id).items
    assert all(item.candidates == () for item in items)
    assert all(item.decided_at is None for item in items)
    groups = intake_service.list_groups(batch_id)
    # 两页都在"待归类"那一个组里（两个键都空），没有人替它们选过日期或商家。
    assert [(group.purchase_date, group.source_name, group.item_count) for group in groups] == [
        (None, None, 2)
    ]


# --- 回看原件 ---------------------------------------------------------------


def test_opening_a_page_gives_back_the_original_bytes_untouched(
    intake_ingest_service: IntakeIngestService,
    intake_service: IntakeService,
    batch_id: int,
) -> None:
    """回看原件返回的是存下来的字节本身——它是核对用的证据，不压缩、不转格式。

    PDF 的第 3 页返回的是**整份 PDF**，页号在 `item.page_no` 上：本项目没有栅格化
    依赖，后端生成不出单页图，返回占位图冒充原件是不能接受的。
    """
    content = pdf_bytes(5)
    intake_ingest_service.upload(batch_id, upload_of("five.pdf", content))
    third = intake_service.list_items(batch_id).items[2]

    item, stream = intake_ingest_service.open_item(third.id)
    with stream:
        assert stream.read() == content
    assert item.page_no == 3
    assert item.original_name == "five.pdf"


def test_opening_a_whole_file_works_for_a_failed_one_too(
    intake_ingest_service: IntakeIngestService, batch_id: int
) -> None:
    """解析失败的文件照样能回看：字节收下了，人要能看见到底传了什么上来。"""
    broken = b"%PDF-1.4\nbroken on purpose"
    outcome = intake_ingest_service.upload(batch_id, upload_of("bad.pdf", broken))
    assert outcome.ingested is not None and outcome.ingested.outcome == OUTCOME_FAILED

    ref, stream = intake_ingest_service.open_file(outcome.ref.id)
    with stream:
        assert stream.read() == broken
    assert ref.status == INTAKE_FILE_STATUS_FAILED


# --- 与旧单张上传路径并存 ---------------------------------------------------


def test_the_same_bytes_on_the_legacy_path_are_only_a_hint(
    intake_ingest_service: IntakeIngestService,
    invoice_service: InvoiceService,
    batch_id: int,
) -> None:
    """同一份字节在旧单张上传路径上已有单据时：**提示**，不阻断。

    两条路径的判重表是分开的（见 `models.IntakeFile`），放宽旧索引会让步骤 05 的
    重复拦截失效。所以这里报出那张旧单据的 id，由人决定怎么办。
    """
    content = jpeg_bytes()
    invoice = invoice_service.create_invoice(
        [upload_of("legacy.jpg", content)], recognize=False
    )

    outcome = intake_ingest_service.upload(batch_id, upload_of("same.jpg", content))

    assert outcome.registered.same_bytes_invoice_ids == (invoice.id,)
    assert outcome.ingested is not None
    assert outcome.ingested.outcome == OUTCOME_INGESTED  # 没被阻断
