"""图片 / PDF 的批次摄入：字节落盘 + 按页切成可归类内容（多文件计划 M03）。

本模块是 M02 与 `documents/` 之间的那一层：

- **M02（`services/intake.py`）只接受「字节已经存好了」这个事实**，它不读磁盘、
  不认识 PDF。
- **`documents/`** 只做本地计算（嗅探格式、解码图片、数一份 PDF 有几页），
  一行代码都不访问网络。
- 本模块把两边接起来：存字节 → 登记进批次 → 数出确定性的页数 → 把页登记成内容。

### 四条贯穿本模块的规则

1. **页数是数出来的，不是猜的。** 一张图片恒等于一页；一份 PDF 的页数来自它的
   页树（`documents.pdf.inspect_pdf`）。⚠️ **多页 PDF 不等于一张发票**——这正是
   现有 `POST /api/invoices` 表达不了的那件事（那边的多文件含义是"同一张单据的
   多页"）。这里只产生页，页归哪个日期/商家组由人决定（M06）。
2. **不决定商家，也不决定日期。** 本步骤连模型都不调。日期与商家候选是 M05 的事，
   本模块只把「这一页在哪个文件的第几页、原件怎么取、本地能看出什么事实」交给
   可注入的 `IntakeItemProcessor`。默认实现什么都不做。
3. **状态就是队列。** `intake_batch_files.status` 的 `pending` 含义是「字节存好了、
   还没切」。没有另一张任务表（理由见 `repositories/intake.py` 的 `list_waiting`）。
   上传请求**只存字节、只登记**，切页交给调度器；浏览器关掉不影响已经收下的字节，
   重启后 `pending` 的文件还在，再驱动一次队列就接着切。
4. **不报假进度。** 对外只有离散状态（`pending` / `ready` / `failed` /
   `discarded`）与「切出了几个内容」。没有百分比：本地切页没有可信的中间进度，
   编一个出来只会让人以为卡在 73%。

### 一份字节只存一次是怎么做到的

`LocalFileStorage.save()` 必须先把字节写下来才算得出 `sha256`，所以判重只能发生在
写完之后。于是本模块的顺序是**先写、再登记、登记告诉我们这份字节早就有了就把刚写的
那份删掉**（`register_file` 返回 `reused_bytes`）。刻意不用「先算摘要再查库再写」：
那是 check-then-act，两个请求同时传同一个文件时会双写。

⚠️ 删之前要确认**库里记着的那个 key 真的还在**。不在的话保留刚写的这一份并留一条
警告——宁可多一份孤儿字节，也不能把唯一一份原件删掉。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field as dataclass_field
from typing import BinaryIO, Callable, Final, Protocol, Sequence

from src.documents.files import (
    DocumentFormat,
    FormatError,
    decode_image,
    heic_decoder_available,
    sniff_format,
)
from src.documents.pdf import PdfError, PdfInspection, inspect_pdf
from src.errors import ConflictError, NotFoundError, ValidationError
from src.integrations.file_storage import LocalFileStorage
from src.models import (
    INTAKE_FILE_STATUS_DISCARDED,
    INTAKE_FILE_STATUS_READY,
)
from src.services.intake import (
    IntakeFileRefView,
    IntakeItemView,
    IntakeService,
    RegisteredFile,
)
from src.services.invoices import (
    ALLOWED_UPLOAD_EXTENSIONS,
    UploadedFile,
    upload_extension,
)

logger = logging.getLogger(__name__)

# --- 口径常量 ---------------------------------------------------------------

# 本步骤受理的扩展名。**与单张上传同一份白名单**（`services/invoices.py`）：
# 两边各写一份就会出现「单张上传拦住、批次上传放过」。
# ⚠️ CSV / XLSX 不在这里：表格按行切，是 M04 的事，它会带自己的受理集合进来。
DOCUMENT_EXTENSIONS: Final[tuple[str, ...]] = ALLOWED_UPLOAD_EXTENSIONS

# 一张图片恒等于一页。写成常量是为了让「为什么图片也有页号」有个指得到的地方：
# 页号是**回看原件的坐标**，图片的坐标就是第 1 页（见 models.IntakeItem）。
SINGLE_IMAGE_PAGE: Final[int] = 1

# 切页结果。`already_ingested` 不是错误：两个调度器同时驱动队列时，后到的那个看到
# 文件已经 `ready`，如实这么说，而不是把已有的页重切一遍（页的 id 要稳定）。
OUTCOME_INGESTED: Final[str] = "ingested"
OUTCOME_FAILED: Final[str] = "failed"
OUTCOME_ALREADY_INGESTED: Final[str] = "already_ingested"

# 失败原因。都是**用户或环境**的问题，不是程序错误，所以落在文件状态上让人看见并
# 能重试，而不是抛成 500。
ERROR_BYTES_MISSING: Final[str] = "bytes_missing"
ERROR_PDF_UNREADABLE: Final[str] = "pdf_unreadable"
ERROR_IMAGE_UNREADABLE: Final[str] = "image_unreadable"
ERROR_CONTENT_UNSUPPORTED: Final[str] = "content_unsupported"

_IMAGE_FORMATS: Final[frozenset[DocumentFormat]] = frozenset(
    {DocumentFormat.JPEG, DocumentFormat.PNG, DocumentFormat.HEIC}
)


# --- 交给 M05 的入参 ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageSource:
    """一页的原件坐标，加上**本地**能看出来的事实。

    ⚠️ 这里一个字都不涉及商家与日期。M05 拿到它去跑日期/商家提取，本步骤只负责
    「这一页是谁的第几页、字节怎么取、它是扫描页还是数字 PDF 页」。

    `has_text_layer` / `has_full_page_image` 只对 PDF 页有值（图片是 None）：
    这两条正是 M05 分流「抽文字层还是送 OCR」的依据，而**事实在摄入时已经算过**，
    不必为此把 PDF 再解析一遍。
    """

    item_id: int
    batch_id: int
    batch_file_id: int
    page_no: int
    storage_key: str
    sha256: str
    original_name: str
    document_format: str
    width: float | None = None
    height: float | None = None
    has_text_layer: bool | None = None
    has_full_page_image: bool | None = None


class IntakeItemProcessor(Protocol):
    """切完页之后的后续处理（M05 的日期/商家提取挂在这里）。

    为什么是注入的接口而不是直接调用：本步骤**不能**依赖识别流程，否则 M03 的测试
    就得先有 OCR。注入之后，M03 交付的是「页切好了、原件引用齐了」，M05 换掉这一个
    实现即可，摄入代码一行不用改。

    实现方的异常由本模块吞掉并记日志：页已经**提交**了，后续处理失败不该让已经
    收下的页丢掉（与「一条提取任务失败不动草稿」同一条）。
    """

    def process(self, pages: Sequence[PageSource]) -> None: ...


class NoFurtherProcessing:
    """默认处理器：什么都不做，只记一条日志。

    M03 刻意不在这里放任何「顺手做一点」的逻辑：日期与商家一旦在摄入阶段被猜出来，
    就会以系统建议的身份出现在组里，而本步骤没有任何依据可言。
    """

    def process(self, pages: Sequence[PageSource]) -> None:
        logger.debug(
            "intake ingest: no further processing configured pages=%s", len(pages)
        )


# --- 调度 -------------------------------------------------------------------


class IngestScheduler(Protocol):
    """切页在哪里跑。返回 True 表示「交给后台了」，False 表示「已经当场跑完」。"""

    def submit(self, job: Callable[[], None]) -> bool: ...


class InlineIngest:
    """当场跑完。测试与脚本用它：结果确定，不需要等线程。"""

    def submit(self, job: Callable[[], None]) -> bool:
        job()
        return False


class BackgroundIngest:
    """后台线程池里跑。

    上传请求因此只做「存字节 + 登记」就返回，切页在请求之外进行——浏览器关掉、
    网络断掉都不影响已经收下的字节。与 `ExtractionWorker` 是同一条理由，但**刻意
    不复用它**：那边是一张落库的任务表 + 轮询 + 卡住任务恢复，因为一次调用要花钱、
    有 `running` 中间态；本地切页没有这些代价，`pending` 状态本身就够当队列。

    ⚠️ 线程池里的异常一律吞掉并记日志：线程死掉会让后续上传全部没人切，而文件状态
    上看不出任何异常。真正的失败由 `fail_file` 写在文件上，这里兜的是预料之外的。
    """

    def __init__(self, *, workers: int = 1) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="intake-ingest"
        )

    def submit(self, job: Callable[[], None]) -> bool:
        self._pool.submit(self._guarded, job)
        return True

    def shutdown(self, *, wait: bool = True) -> None:
        """等正在切的那一个切完。切到一半被掐断会留下 `pending`，下次照样能接着切。"""
        self._pool.shutdown(wait=wait)

    @staticmethod
    def _guarded(job: Callable[[], None]) -> None:
        try:
            job()
        except Exception:  # noqa: BLE001 - 后台线程不能因为一个文件而死掉
            logger.exception("intake ingest job failed in background")


# --- 结果视图 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestedFile:
    """一次切页的结果。

    `page_count` 是**文件实际有几页**，`items` 是**登记成内容的页**。失败时
    两者都可能是 0，原因在 `error_code` / `error_message` 上，而不是一句泛泛的
    「处理失败」。
    """

    ref: IntakeFileRefView
    outcome: str
    items: tuple[IntakeItemView, ...] = ()
    document_format: str | None = None
    page_count: int = 0
    notes: tuple[str, ...] = dataclass_field(default_factory=tuple)

    @property
    def error_code(self) -> str | None:
        return self.ref.error_code

    @property
    def error_message(self) -> str | None:
        return self.ref.error_message


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """一次上传的结果。

    `queued` 为 True 表示切页交给后台了，此时 `ingested` 是 None——**不假装已经
    切完**。前端据此去轮询批次状态。
    """

    registered: RegisteredFile
    queued: bool
    ingested: IngestedFile | None = None

    @property
    def ref(self) -> IntakeFileRefView:
        """这个文件**现在**的样子。

        ⚠️ 不能直接用 `registered.ref`：那是**登记那一刻**的快照，状态还是
        `pending`。当场切完时报一个已经过时的状态，等于告诉前端"还在等"，而它其实
        已经 ready 了。切页交给后台时才用登记快照——那时 `pending` 就是真相。
        """
        return self.registered.ref if self.ingested is None else self.ingested.ref


# --- 服务 -------------------------------------------------------------------


class IntakeIngestService:
    """把上传的图片 / PDF 变成批次里的可归类内容。

    事务边界仍在 M02 的 `IntakeService` 上：本模块的每一步落库都是对它的一次调用
    （`register_file` / `record_items` / `fail_file`），自己不开 Session。于是
    「切到一半失败不留半套关系」那条保证继续由 M02 的一个事务提供。
    """

    def __init__(
        self,
        intake: IntakeService,
        *,
        file_storage: LocalFileStorage,
        upload_max_bytes: int,
        item_processor: IntakeItemProcessor | None = None,
        scheduler: IngestScheduler | None = None,
        allowed_extensions: tuple[str, ...] = DOCUMENT_EXTENSIONS,
    ) -> None:
        self._intake = intake
        self._storage = file_storage
        self._upload_max_bytes = upload_max_bytes
        self._processor = item_processor or NoFurtherProcessing()
        self._scheduler = scheduler or InlineIngest()
        self._allowed_extensions = allowed_extensions

    # --- 上传 ---------------------------------------------------------------

    def upload(self, batch_id: int, upload: UploadedFile) -> UploadOutcome:
        """收一个文件：校验 → 存字节 → 登记进批次 → 安排切页。

        **一次一个文件**，不是一次一批：十几份混合文件里有一份太大或格式不对时，
        整批一起失败就得全部重传。逐文件上传让第 2 个失败不影响第 1 个
        （docs/multifiles.md M03 的验收条件之一）。
        """
        self._check_upload(upload)
        stored = self._storage.save(
            upload.content,
            original_name=upload.original_name,
            content_type=upload.content_type,
        )
        try:
            registered = self._intake.register_file(
                batch_id,
                storage_key=stored.key,
                sha256=stored.sha256,
                size=stored.size,
                original_name=stored.original_name,
                content_type=stored.content_type,
            )
        except Exception:
            # 登记没成（批次不存在、同批次重复、批次已收口……）：刚写下的这一份
            # 字节没有任何行引用它，留着就是孤儿。
            self._storage.delete(stored.key)
            raise

        if registered.reused_bytes:
            self._drop_duplicate_bytes(stored.key, registered.ref)

        ref = registered.ref
        if ref.status == INTAKE_FILE_STATUS_READY:
            # 撤掉之后又传回来、而且页还在：不重切，页的 id 与已有分类一起留着。
            return UploadOutcome(registered=registered, queued=False)

        batch_file_id = ref.id
        ingested: list[IngestedFile] = []
        queued = self._scheduler.submit(
            lambda: ingested.append(self.ingest_file(batch_file_id))
        )
        return UploadOutcome(
            registered=registered,
            queued=queued,
            ingested=None if queued or not ingested else ingested[0],
        )

    def _drop_duplicate_bytes(self, fresh_key: str, ref: IntakeFileRefView) -> None:
        """这份字节库里早就有了，把刚写下的那一份删掉（原件字节只存一次）。

        先确认库里记着的那个 key 真的还在：不在就保留刚写的这一份并警告——宁可多一份
        孤儿字节，也不能把唯一一份原件删掉。
        """
        if fresh_key == ref.storage_key:
            return
        if not self._storage.exists(ref.storage_key):
            logger.warning(
                "intake ingest kept freshly stored bytes: recorded key %s is missing "
                "(batch_file=%s)",
                ref.storage_key,
                ref.id,
            )
            return
        self._storage.delete(fresh_key)
        logger.info(
            "intake ingest reused existing bytes sha=%s; dropped duplicate copy",
            ref.sha256[:12],
        )

    def _check_upload(self, upload: UploadedFile) -> str:
        """类型与大小白名单。与单张上传同一套口径，返回小写扩展名。

        ⚠️ 按**扩展名**拦是入口校验（步骤 05 定下的口径）；真正按什么格式处理由
        字节说话（`sniff_format`，见 `documents/files.py` 第 1 条）。两者分工不同，
        不是重复。
        """
        if not upload.original_name or not upload.original_name.strip():
            raise ValidationError("上传文件缺少文件名。", details={"field": "file"})
        extension = upload_extension(upload.original_name)
        if extension not in self._allowed_extensions:
            raise ValidationError(
                f"不支持的文件类型 {extension or upload.original_name!r}。"
                f"可以上传：{', '.join(self._allowed_extensions)}。",
                details={
                    "field": "file",
                    "allowed_extensions": list(self._allowed_extensions),
                },
            )
        if not upload.content:
            raise ValidationError(
                f"文件 {upload.original_name} 是空的。", details={"field": "file"}
            )
        if len(upload.content) > self._upload_max_bytes:
            raise ValidationError(
                f"文件 {upload.original_name} 有 {len(upload.content)} 字节，"
                f"超过了单个文件 {self._upload_max_bytes} 字节的上限。",
                details={
                    "field": "file",
                    "max_bytes": self._upload_max_bytes,
                    "size": len(upload.content),
                },
            )
        return extension

    # --- 切页 ---------------------------------------------------------------

    def ingest_waiting(
        self,
        *,
        batch_id: int | None = None,
        retry_failed: bool = False,
        limit: int | None = None,
    ) -> tuple[IngestedFile, ...]:
        """把还没切的文件切完，按上传顺序。

        这是**恢复入口**：进程重启、后台线程没跑成、用户关掉浏览器又回来，再驱动
        一次队列就接着切。已经 `ready` 的文件不会被重切，所以重复调用是安全的。

        `retry_failed=True` 连失败的也重试一遍——解析失败常常是环境问题
        （缺 HEIC 解码器、字节还没同步完），装好依赖之后不该逼人重传。
        """
        waiting = self._intake.waiting_files(
            batch_id=batch_id,
            include_failed=retry_failed,
            **({} if limit is None else {"limit": limit}),
        )
        results: list[IngestedFile] = []
        for ref in waiting:
            try:
                results.append(self.ingest_file(ref.id))
            except ConflictError:
                # 另一个调度器抢先切完了（或批次刚被收口）。如实记一条，不重切。
                results.append(
                    IngestedFile(
                        ref=self._intake.get_file(ref.id),
                        outcome=OUTCOME_ALREADY_INGESTED,
                    )
                )
        logger.info(
            "intake ingest drained batch=%s files=%s", batch_id, len(results)
        )
        return tuple(results)

    def ingest_file(self, batch_file_id: int) -> IngestedFile:
        """读一个文件的字节，数出页数，把页登记成可归类内容。

        四种结局都落在**文件状态**上，调用方不必解释异常：

        - 切成了 → `ready`，`item_count` 是页数。
        - 已经切过 → 原样返回，页的 id 不变（`already_ingested`）。失败之后又被
          重试到的、但页其实早就切好的文件也走这一条。
        - 字节取不到、PDF 打不开、图片坏了、格式不受理 → `failed` + 原因，可重试。
        - 文件已从批次撤掉 → 冲突（撤掉的东西不该被偷偷切回来）。
        """
        ref = self._intake.get_file(batch_file_id)
        if ref.status == INTAKE_FILE_STATUS_DISCARDED:
            raise ConflictError(
                f"文件 {ref.original_name} 已经从批次里撤掉了，不再切页。",
                details={"batch_file_id": ref.id},
            )
        if ref.status == INTAKE_FILE_STATUS_READY or ref.item_count is not None:
            # `item_count` 有值就说明页已经切出来过了——哪怕这个文件之后被标成了
            # 失败（M02 的 `fail_file` 对任何未回收的文件都允许）。**不重切**：
            # 页的 id 要稳定，而重切会撞上 `(batch_file_id, page_no)` 的唯一索引，
            # 表现成一个 500 而不是一句说得清的话。
            return IngestedFile(
                ref=ref,
                outcome=OUTCOME_ALREADY_INGESTED,
                page_count=ref.item_count or 0,
            )

        try:
            content = self._storage.read(ref.storage_key)
        except (NotFoundError, ValidationError, OSError) as error:
            return self._failed(
                ref,
                ERROR_BYTES_MISSING,
                f"原件读不出来（{ref.original_name}）：{error}。"
                "请重新上传这个文件。",
            )

        fmt = sniff_format(content)
        if fmt is DocumentFormat.PDF:
            return self._ingest_pdf(ref, content)
        if fmt in _IMAGE_FORMATS:
            return self._ingest_image(ref, content, fmt)
        return self._failed(
            ref,
            ERROR_CONTENT_UNSUPPORTED,
            f"{ref.original_name} 的内容既不是图片也不是 PDF（文件头认不出来）。"
            "文件名的后缀可能与真实内容不一致，请确认后重新上传。",
        )

    def _ingest_pdf(self, ref: IntakeFileRefView, content: bytes) -> IngestedFile:
        """一份 PDF → 每页一个内容。

        ⚠️ **页数来自页树，不来自"这份 PDF 看起来像几张发票"**。一份五页 PDF 产生
        五个页内容，各自回指自己那一页；它们可以落到五个不同的日期/商家组，也可以
        全在一组——那是人的决定（M06），摄入阶段不替他决定。
        """
        try:
            inspection = inspect_pdf(content)
        except PdfError as error:
            return self._failed(ref, ERROR_PDF_UNREADABLE, f"{error}")

        pages = tuple(page.number for page in inspection.pages)
        items = self._intake.record_items(ref.id, pages=pages)
        notes = tuple(inspection.notes)
        logger.info(
            "intake ingest pdf batch_file=%s kind=%s pages=%s",
            ref.id,
            inspection.kind,
            len(pages),
        )
        return self._ingested(
            ref,
            items,
            document_format=str(DocumentFormat.PDF),
            page_count=len(pages),
            notes=notes,
            inspection=inspection,
        )

    def _ingest_image(
        self, ref: IntakeFileRefView, content: bytes, fmt: DocumentFormat
    ) -> IngestedFile:
        """一张图片 → 恰好一个内容（第 1 页）。

        解码只用来**核对文件没坏**并量出尺寸，解出来的像素不保存——原件是证据，
        转码出来的字节只在识别阶段用（见 `documents/files.py` 第 2 条）。
        """
        notes: list[str] = []
        width: float | None = None
        height: float | None = None
        try:
            decoded = decode_image(content, original_name=ref.original_name)
        except FormatError as error:
            if fmt is DocumentFormat.HEIC and not heic_decoder_available():
                # 文件没问题，是本机缺解码器。页照样登记：原件存着、能回看，
                # 装上 pillow-heif 之后识别阶段自然能用。重传一张一模一样的
                # HEIC 解决不了任何问题。
                notes.append(
                    "本机没有 HEIC 解码器（pillow-heif），这张图在识别阶段才需要它；"
                    "原件已保存，页已登记。"
                )
            else:
                return self._failed(ref, ERROR_IMAGE_UNREADABLE, f"{error}")
        else:
            with decoded.image:
                width = float(decoded.image.width)
                height = float(decoded.image.height)
            notes.extend(decoded.notes)

        items = self._intake.record_items(ref.id, pages=(SINGLE_IMAGE_PAGE,))
        logger.info(
            "intake ingest image batch_file=%s format=%s size=%sx%s",
            ref.id,
            fmt,
            width,
            height,
        )
        return self._ingested(
            ref,
            items,
            document_format=str(fmt),
            page_count=1,
            notes=tuple(notes),
            width=width,
            height=height,
        )

    def _ingested(
        self,
        ref: IntakeFileRefView,
        items: tuple[IntakeItemView, ...],
        *,
        document_format: str,
        page_count: int,
        notes: tuple[str, ...],
        inspection: PdfInspection | None = None,
        width: float | None = None,
        height: float | None = None,
    ) -> IngestedFile:
        """页已提交，交给后续处理器，然后汇报结果。

        处理器的异常在这里吞掉：页已经**落库**了，后续处理失败不该把已经收下的页
        一起丢掉。
        """
        pages = tuple(
            PageSource(
                item_id=item.id,
                batch_id=item.batch_id,
                batch_file_id=item.batch_file_id,
                page_no=item.page_no or SINGLE_IMAGE_PAGE,
                storage_key=item.storage_key,
                sha256=item.sha256,
                original_name=item.original_name,
                document_format=document_format,
                width=_page_width(inspection, item.page_no) if inspection else width,
                height=_page_height(inspection, item.page_no) if inspection else height,
                has_text_layer=_page_has_text(inspection, item.page_no),
                has_full_page_image=_page_has_image(inspection, item.page_no),
            )
            for item in items
        )
        if pages:
            try:
                self._processor.process(pages)
            except Exception:  # noqa: BLE001 - 页已落库，后续处理失败不能连累它
                logger.exception(
                    "intake item processor failed batch_file=%s items=%s",
                    ref.id,
                    [item.id for item in items],
                )
        return IngestedFile(
            ref=self._intake.get_file(ref.id),
            outcome=OUTCOME_INGESTED,
            items=items,
            document_format=document_format,
            page_count=page_count,
            notes=notes,
        )

    def _failed(
        self, ref: IntakeFileRefView, code: str, message: str
    ) -> IngestedFile:
        """把失败写在这一个文件上。

        **只动这一条**：同批次其它文件与它们已经切好的页一概不动。坏掉一个文件不该
        让人把十几份重传一遍。
        """
        logger.warning(
            "intake ingest failed batch_file=%s code=%s", ref.id, code
        )
        failed = self._intake.fail_file(
            ref.id, error_code=code, error_message=message
        )
        return IngestedFile(ref=failed, outcome=OUTCOME_FAILED)

    # --- 回看原件 -----------------------------------------------------------

    def open_item(self, item_id: int) -> tuple[IntakeItemView, BinaryIO]:
        """回看某一页的原件。

        ⚠️ **返回的是整份原件的字节**，页号在 `item.page_no` 上。本项目没有 PDF
        栅格化依赖（只有 pypdf，不读像素），所以数字 PDF 的"第 3 页那张图"在后端
        根本生成不出来——由前端的 PDF 查看器跳到那一页。宁可这样，也不能返回一张
        占位图冒充原件：原件是人工核对的证据。
        """
        item = self._intake.get_item(item_id)
        return item, self._storage.open(item.storage_key)

    def open_file(self, batch_file_id: int) -> tuple[IntakeFileRefView, BinaryIO]:
        """回看整份原件（原样返回存下来的字节，不压缩、不转格式）。"""
        ref = self._intake.get_file(batch_file_id)
        return ref, self._storage.open(ref.storage_key)


# --- 内部辅助 ---------------------------------------------------------------


def _page_facts(inspection: PdfInspection | None, page_no: int | None):  # noqa: ANN202
    if inspection is None or page_no is None:
        return None
    return inspection.page(page_no)


def _page_width(inspection: PdfInspection | None, page_no: int | None) -> float | None:
    facts = _page_facts(inspection, page_no)
    return None if facts is None else facts.width


def _page_height(inspection: PdfInspection | None, page_no: int | None) -> float | None:
    facts = _page_facts(inspection, page_no)
    return None if facts is None else facts.height


def _page_has_text(
    inspection: PdfInspection | None, page_no: int | None
) -> bool | None:
    facts = _page_facts(inspection, page_no)
    return None if facts is None else facts.has_text


def _page_has_image(
    inspection: PdfInspection | None, page_no: int | None
) -> bool | None:
    facts = _page_facts(inspection, page_no)
    return None if facts is None else facts.image_count > 0


__all__ = [
    "DOCUMENT_EXTENSIONS",
    "ERROR_BYTES_MISSING",
    "ERROR_CONTENT_UNSUPPORTED",
    "ERROR_IMAGE_UNREADABLE",
    "ERROR_PDF_UNREADABLE",
    "OUTCOME_ALREADY_INGESTED",
    "OUTCOME_FAILED",
    "OUTCOME_INGESTED",
    "SINGLE_IMAGE_PAGE",
    "BackgroundIngest",
    "IngestScheduler",
    "IngestedFile",
    "InlineIngest",
    "IntakeIngestService",
    "IntakeItemProcessor",
    "NoFurtherProcessing",
    "PageSource",
    "UploadOutcome",
]
