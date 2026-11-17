"""PDF：区分「带文字层的数字 PDF」与「扫描成图的 PDF」，并取出各自需要的东西。

这是步骤 08 分流的第一道岔口。两条路径的代价差一个数量级：数字 PDF 直接抽文字层
**根本不调 OCR**（近 0 耗时、零成本），扫描页要逐页调模型（5—15 秒）。

### 分流依据：解析得出来，不是文本够长

⚠️ **刻意不用「文本超过固定长度就无需 OCR」这条旧阈值**（docs/stru_dev.md 步骤 08
的「不移植」明确点了这一条）。原因：一张扫描件 PDF 也可能带着页眉水印、扫描仪型号、
页码这些零散文字，长度轻松过阈值，而单据正文一个字都没有。反过来，一张只有一行
商品的数字 PDF 文本很短，却完全可以精确解析。

本模块因此**只报事实**（有没有文字算子、有几个文本片段、有几张内嵌图），
**由解析结果决定走哪条路**：`documents.textlayer` 能从文字层还原出表头与至少一行
商品，就走文字层；还原不出来，就按扫描页送 OCR。决定权交给「真的解析成功了吗」，
而不是交给一个拍出来的数字。

### 文字层不是白捡

步骤 07 实测：`sample_grocery_supplier_invoices.pdf` 的字符准确、5 张发票行金额之和
与印着的 Total 完全吻合，**但行列归位仍然要写代码**——第 2 页的发票号
`MP-2026-0611-219` 在文字层里是 `MP-2026-0611-21` 和 `9` 两个片段，因为渲染时换了行。
所以本模块给出的是**带绝对坐标的片段**，拼装交给 `textlayer`；直接用
`extract_text()` 的换行结果会把那个单号拆成两段存进去。

坐标要自己算：pypdf 的 `visitor_text` 给的 `tm` 是文本矩阵，`cm` 是当前变换矩阵，
绝对位置是两者相乘。只读 `tm[4]` / `tm[5]` 会得到一堆 0（实测如此）。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from enum import StrEnum

from pypdf import PdfReader
from pypdf.errors import PdfReadError, PyPdfError

from src.documents.files import DocumentFormat, sniff_format

logger = logging.getLogger(__name__)

# 一张内嵌图要覆盖页面多大比例，才算「这一页就是这张扫描图」。
# 0.80 的来历：扫描件的图总是铺满整页（实测这批 PDF 的图与 mediabox 等比），
# 而 logo、签章、二维码都远小于此。够不到这个比例时**不猜**，见 extract_page_images。
FULL_PAGE_AREA_RATIO = 0.80

# 一份 PDF 最多处理多少页。多页发票常见十几页，这批样本最多 32 页；
# 给 200 页是为了挡住"把整年账本当一张发票传上来"，那种情况应该拆开传。
MAX_PDF_PAGES = 200


class PdfError(ValueError):
    """PDF 坏了、加密了，或结构不符合预期。

    用户输入问题（传错文件、文件损坏、带密码），映射成 400，不重试。
    """


class PdfKind(StrEnum):
    """一份 PDF 的整体类型。按页统计后得出。"""

    TEXT_LAYER = "text_layer"
    """每一页都有可抽取的文本片段。走文字层路径，不调 OCR。"""

    SCANNED = "scanned"
    """没有文本片段，但每页有整页图像。按扫描页送 OCR。"""

    MIXED = "mixed"
    """有的页有文字层、有的页只有图。**按页分别处理**，不给整份文件下一个结论。"""

    EMPTY = "empty"
    """既没有文本片段也没有可用的整页图像。诚实失败，不猜。"""


@dataclass(frozen=True)
class TextFragment:
    """文字层里的一个文本片段，带页面绝对坐标。

    坐标用 PDF 用户空间：原点在**左下角**，y 向上增长。保留这个原生口径，
    不在这里翻转成「屏幕坐标」——翻转一次就要在每个调用点记得翻回来。
    需要比例坐标时由 `textlayer` 用 `page_width` / `page_height` 换算。
    """

    text: str
    x: float
    y: float
    font_size: float
    page: int

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True)
class PdfPageFacts:
    """一页的客观事实。**不含判断**，判断由调用方按解析结果做。"""

    number: int
    width: float
    height: float
    fragment_count: int
    text_characters: int
    image_count: int

    @property
    def has_text(self) -> bool:
        """有没有抽到任何非空白文本片段。注意这**不是**「可以不用 OCR」。"""
        return self.fragment_count > 0


@dataclass(frozen=True)
class PdfInspection:
    """一份 PDF 的逐页事实与整体类型。"""

    kind: PdfKind
    pages: tuple[PdfPageFacts, ...]
    producer: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, number: int) -> PdfPageFacts | None:
        for item in self.pages:
            if item.number == number:
                return item
        return None

    @property
    def text_pages(self) -> tuple[int, ...]:
        return tuple(page.number for page in self.pages if page.has_text)

    @property
    def image_pages(self) -> tuple[int, ...]:
        return tuple(page.number for page in self.pages if page.image_count > 0)


@dataclass(frozen=True)
class PdfPageImage:
    """从扫描页里取出来的整页图像。"""

    page: int
    payload: bytes
    media_type: str
    width: int
    height: int
    notes: tuple[str, ...] = field(default_factory=tuple)


def _open(content: bytes) -> PdfReader:
    if sniff_format(content) is not DocumentFormat.PDF:
        raise PdfError("这不是 PDF（文件头不是 %PDF-）。")
    try:
        reader = PdfReader(io.BytesIO(content))
    except (PdfReadError, PyPdfError, OSError, ValueError) as error:
        raise PdfError(f"PDF 打不开：{error}") from None
    if reader.is_encrypted:
        # 空密码能解开的加密 PDF 很常见（只设了权限密码）。试一次，不行就说清楚。
        try:
            if reader.decrypt("") == 0:
                raise PdfError(
                    "这份 PDF 有打开密码。请先去掉密码再上传——"
                    "系统不保存也不尝试猜测密码。"
                )
        except PdfError:
            raise
        except (PyPdfError, NotImplementedError) as error:
            raise PdfError(f"这份 PDF 的加密方式无法处理：{error}") from None
    page_count = len(reader.pages)
    if page_count == 0:
        raise PdfError("这份 PDF 一页都没有。")
    if page_count > MAX_PDF_PAGES:
        raise PdfError(
            f"这份 PDF 有 {page_count} 页，超过一次处理 {MAX_PDF_PAGES} 页的上限。"
            "请按单据拆成几份再传——一次传整年账本的话，审核界面也没法用。"
        )
    return reader


def read_text_fragments(content: bytes, *, page: int) -> list[TextFragment]:
    """抽一页的文本片段，带页面绝对坐标。

    页码从 1 开始，与人翻单据时说的「第几页」一致（同 `contracts.FieldEvidence`）。
    """
    reader = _open(content)
    if not 1 <= page <= len(reader.pages):
        raise PdfError(f"第 {page} 页不存在（这份 PDF 共 {len(reader.pages)} 页）。")
    return _fragments_of(reader, page)


def _fragments_of(reader: PdfReader, number: int) -> list[TextFragment]:
    target = reader.pages[number - 1]
    collected: list[TextFragment] = []

    def visit(text: str, cm, tm, _font_dict, font_size) -> None:  # noqa: ANN001
        if not text or not text.strip():
            return
        # 绝对位置 = 文本矩阵 × 当前变换矩阵。只看 tm[4] / tm[5] 会得到一堆 0。
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        collected.append(
            TextFragment(
                text=text.strip(),
                x=float(x),
                y=float(y),
                font_size=float(font_size or 0.0),
                page=number,
            )
        )

    try:
        target.extract_text(visitor_text=visit)
    except (PyPdfError, OSError, ValueError, KeyError) as error:
        # 单页解析失败不该让整份文件失败：其它页可能是好的。
        logger.warning("pdf page %s text extraction failed: %s", number, error)
        return []
    return collected


def inspect_pdf(content: bytes) -> PdfInspection:
    """逐页统计客观事实，并给出整体类型。

    ⚠️ 这里**不判断**「要不要调 OCR」。那个决定由「文字层到底解析出东西了没有」
    给出，见 `documents.textlayer` 与 `services/extraction.py`。
    """
    reader = _open(content)
    notes: list[str] = []
    facts: list[PdfPageFacts] = []
    for number in range(1, len(reader.pages) + 1):
        page = reader.pages[number - 1]
        fragments = _fragments_of(reader, number)
        try:
            image_count = len(page.images)
        except (PyPdfError, OSError, ValueError, KeyError) as error:
            logger.warning("pdf page %s image listing failed: %s", number, error)
            notes.append(f"第 {number} 页的内嵌图像列不出来：{error}")
            image_count = 0
        box = page.mediabox
        facts.append(
            PdfPageFacts(
                number=number,
                width=float(box.width),
                height=float(box.height),
                fragment_count=len(fragments),
                text_characters=sum(len(item.text) for item in fragments),
                image_count=image_count,
            )
        )

    with_text = [item for item in facts if item.has_text]
    with_image = [item for item in facts if item.image_count > 0]
    if with_text and len(with_text) == len(facts):
        kind = PdfKind.TEXT_LAYER
    elif not with_text and with_image:
        kind = PdfKind.SCANNED
    elif with_text or with_image:
        kind = PdfKind.MIXED
        notes.append(
            f"{len(with_text)} 页有文字层、{len(with_image)} 页有整页图、"
            f"共 {len(facts)} 页：按页分别处理。"
        )
    else:
        kind = PdfKind.EMPTY
        notes.append(
            "这份 PDF 既抽不到文字，也没有整页图像——可能是空白扫描，"
            "或者用了本项目处理不了的图像编码。"
        )

    producer = None
    try:
        info = reader.metadata
        producer = str(info.producer) if info and info.producer else None
    except (PyPdfError, OSError, ValueError, KeyError):
        producer = None

    logger.info(
        "pdf inspected kind=%s pages=%s text_pages=%s image_pages=%s producer=%s",
        kind,
        len(facts),
        len(with_text),
        len(with_image),
        producer,
    )
    return PdfInspection(
        kind=kind,
        pages=tuple(facts),
        producer=producer,
        notes=tuple(notes),
    )


def extract_page_images(content: bytes, *, pages: tuple[int, ...] | None = None) -> list[PdfPageImage]:
    """取出指定页（默认全部页）的整页图像，用于送 OCR。

    **一页取一张整页图**。一页里有多张图时，只在其中一张覆盖了整页（≥
    `FULL_PAGE_AREA_RATIO`）时才用它并留一条提示；没有一张覆盖整页就**跳过这一页
    并说明原因**，不挑一张"看起来最大的"充数——那可能是把拼版扫描的半张单据
    当成了整张，而少掉的那半张不会报错，只能靠人在收口确认时发现。
    """
    reader = _open(content)
    total = len(reader.pages)
    wanted = tuple(range(1, total + 1)) if pages is None else pages
    for number in wanted:
        if not 1 <= number <= total:
            raise PdfError(f"第 {number} 页不存在（这份 PDF 共 {total} 页）。")

    results: list[PdfPageImage] = []
    for number in wanted:
        page = reader.pages[number - 1]
        try:
            images = list(page.images)
        except (PyPdfError, OSError, ValueError, KeyError) as error:
            raise PdfError(f"第 {number} 页的内嵌图像取不出来：{error}") from None
        if not images:
            continue

        box = page.mediabox
        page_area = float(box.width) * float(box.height)
        notes: list[str] = []
        chosen = images[0]
        if len(images) > 1:
            # 按像素面积排序，取最大的那张，但**必须**它自己覆盖整页才采用。
            ranked = sorted(
                images,
                key=lambda item: item.image.width * item.image.height,
                reverse=True,
            )
            chosen = ranked[0]
            covered = _covers_page(chosen, page_area)
            if not covered:
                logger.warning(
                    "pdf page %s has %s images, none covers the page; skipped",
                    number,
                    len(images),
                )
                results.append(
                    PdfPageImage(
                        page=number,
                        payload=b"",
                        media_type="",
                        width=0,
                        height=0,
                        notes=(
                            f"这一页有 {len(images)} 张图，没有一张覆盖整页，"
                            "无法确定哪张是单据本身——这一页没有送识别。"
                            "请把这一页单独拍一张整页照片再传。",
                        ),
                    )
                )
                continue
            notes.append(
                f"这一页有 {len(images)} 张图，用了覆盖整页的那张"
                f"（{chosen.image.width}×{chosen.image.height}）。"
            )

        payload, media_type, extra = _image_payload(chosen)
        notes.extend(extra)
        results.append(
            PdfPageImage(
                page=number,
                payload=payload,
                media_type=media_type,
                width=chosen.image.width,
                height=chosen.image.height,
                notes=tuple(notes),
            )
        )
    return results


def _covers_page(image, page_area: float) -> bool:  # noqa: ANN001
    """这张图的像素面积是否足以认为它铺满了整页。

    用像素面积与页面点面积的比，而不是去解析 `/Matrix` 放置矩阵：扫描件的整页图
    分辨率总是远高于 72 dpi，比值会远大于 1；真正的小图（logo、签章）比值很小。
    阈值按比例而非绝对值，于是与扫描 dpi 无关。
    """
    if page_area <= 0:
        return False
    pixels = image.image.width * image.image.height
    # 72 dpi 下 1 点 = 1 像素。150 dpi 扫描的整页图像素面积约为点面积的 4.3 倍，
    # 所以这里比的是"至少达到页面点面积的 FULL_PAGE_AREA_RATIO"。
    return pixels / page_area >= FULL_PAGE_AREA_RATIO


def _image_payload(image) -> tuple[bytes, str, list[str]]:  # noqa: ANN001
    """把 pypdf 的内嵌图像变成可送出去的字节与媒体类型。

    `image.data` 对 JPEG（DCTDecode）就是原始 JPEG 字节，直接透传最好——
    不重新编码就不会二次损失。其它编码（FlateDecode 的位图、CCITT 传真组、JPX）
    的 `data` 不是一个独立图片文件，必须经 Pillow 重新编码成 PNG。
    """
    raw = bytes(image.data)
    fmt = sniff_format(raw)
    if fmt is DocumentFormat.JPEG:
        return raw, "image/jpeg", []
    if fmt is DocumentFormat.PNG:
        return raw, "image/png", []
    buffer = io.BytesIO()
    source = image.image
    if source.mode not in ("RGB", "L", "RGBA"):
        source = source.convert("RGB")
    source.save(buffer, format="PNG")
    return (
        buffer.getvalue(),
        "image/png",
        [
            f"内嵌图像不是 JPEG / PNG（{image.name}），已按原始像素重新编码成 PNG "
            "后送识别；原始 PDF 保持不动。"
        ],
    )


__all__ = [
    "FULL_PAGE_AREA_RATIO",
    "MAX_PDF_PAGES",
    "PdfError",
    "PdfInspection",
    "PdfKind",
    "PdfPageFacts",
    "PdfPageImage",
    "TextFragment",
    "extract_page_images",
    "inspect_pdf",
    "read_text_fragments",
]
