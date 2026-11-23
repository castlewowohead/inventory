"""版式指纹：把一页的**可解释版式特征**算成一个分组键（多文件计划 M07）。

与 `documents/` 其它模块同一条约定：**一行代码都不访问网络**，只用 pypdf 与
Pillow 做本地计算。实测一页约 0.3—0.6 秒（2479×3230 的扫描页），因此它是
「算一次存起来」的量级，不是每次请求都重算的量级。

### 它回答什么、不回答什么

本模块只回答一句话：**这两页的版式特征一样吗**。它回答不了「这两页是不是同一
家」——docs/multifiles.md 第 2 节引的两篇研究说的正是这件事：同一模板常共享布局，
而相似版式的细小差异会造成误配。所以指纹的用途只有两个：

1. 把**读不出商家**的扫描页聚成「相似的一堆」，让人一次看完、一次命名；
2. 当同一指纹下已经有人确认过商家时，给出一条**待确认的候选**。

⚠️ 它永远不够格把一堆内容自动确认到某一家（`services/intake_suggestions.py` 里
一个字都不会写进 `decided_*`）。

### 特征取不到时宁可说「不知道」

格线检测不出来（照片、空白页、歪得太厉害）时 `template_evidence` 是 False：
这一页的指纹里**没有模板信息**，它只是「同类纸张的一堆待办」。调用方据此**不**
拿它去套某一家商家——否则「两张都没有格线的歪照片」会被当成同一家单据的证据，
而那种错误不报错。

### 三条分流与各自的特征

| 内容 | 特征 | 模板证据 |
| --- | --- | --- |
| 有文字层的 PDF 页 | 页面尺寸（点）+ **字号集合** + producer | 字号 ≥ 3 种 |
| 没有文字层的 PDF 页（扫描件） | 笔迹风格 + 格线行数 + 有无竖线 + 长宽比 | 检测到格线 |
| 图片（JPG / PNG / HEIC） | 同上（按解码后的像素算） | 检测到格线 |

**为什么文字层用字号集合**：实测真实数字 PDF
（`samples/real/sample_grocery_supplier_invoices.pdf` 5 页）的字号集合
`7.0 / 8.5 / 9.0 / 9.5 / 11.0 / 17.0 / 19.0` **五页完全一致**，而表头列位置的
*个数* 会随内容多少变（13 / 14 / 15）。所以字号集合进指纹，列位置只进
`features` 供人核对——把会变的东西放进分组键，同一个模板会被切成三堆。

**为什么扫描页用格线行数而不是列数**：同样是实测
（`samples/real/` 的 5 份 PDF、共 74 个扫描页里抽 17 页）——行数在同一份单据里
稳定且在不同商家之间不同（16 / 17—18 / 25 / 2 / 4），而列数在同一份单据内就能
从 3 跳到 5。列数因此只保留「有没有竖线」这一位。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Final

from PIL import Image

from src.documents.classify import InkStyle, profile_ink
from src.documents.files import DocumentFormat, FormatError, decode_image, sniff_format
from src.documents.grid import GridError, detect_grid
from src.documents.pdf import PdfError, extract_page_images, inspect_pdf, read_text_fragments

logger = logging.getLogger(__name__)

# 指纹格式的版本。特征或分桶口径一改就加一，于是存下来的旧指纹不会与新指纹
# 悄悄混在一堆里（它们是用不同尺子量出来的）。
LAYOUT_PROFILE_VERSION: Final[int] = 1

# 一页属于哪一类原件。进指纹的第一段：扫描页与手机照片不混成一堆。
CLASS_PDF_TEXT: Final[str] = "pdf_text"
CLASS_PDF_SCAN: Final[str] = "pdf_scan"
CLASS_IMAGE: Final[str] = "image"

# 文字层页：表头带取页面上部多少。表头是模板，行项目是内容。
_HEADER_BAND: Final[float] = 0.6

# 文字层页：字号至少有几种才算一份「排版表」。一种字号的页（纯文本）谁都一样。
_MIN_FONT_SIZES: Final[int] = 3

# 长宽比保留一位小数。Letter 竖放是 1.3，照片常在 1.0—1.8 之间；再细就会把
# 同一台扫描仪的两页切开（实测同一份 PDF 的页是 1.303，一位小数稳定）。
_ASPECT_DIGITS: Final[int] = 1


@dataclass(frozen=True)
class LayoutProfile:
    """一页的版式特征。

    `fingerprint` 为 None 表示**算不出来**（字节读不出、页不存在、取不到整页图）：
    这样的页不参与任何堆，而不是落进一个叫「未知」的大堆里。
    """

    fingerprint: str | None
    content_class: str | None
    features: dict[str, Any] = dataclass_field(default_factory=dict)
    notes: tuple[str, ...] = ()
    problem: str | None = None

    @property
    def groupable(self) -> bool:
        return self.fingerprint is not None

    @property
    def template_evidence(self) -> bool:
        """指纹里有没有**模板信息**。

        False 的意思是「这一页的版式没量出来」：照片、空白页、格线检测失败。
        这样的堆只够让人扫视缩略图，不够给出「这堆是某一家」的候选。
        """
        return bool(self.features.get("template_evidence"))


def profile_page_layout(
    content: bytes, *, page_no: int = 1, original_name: str | None = None
) -> LayoutProfile:
    """算一页的版式指纹。**不访问网络**，失败一律返回带原因的 profile。

    不抛异常：一页算不出版式不该让整批建议失败（与「一页识别失败只落在这一个内容
    上」同一条）。原因写在 `problem` 里，调用方如实透出。
    """
    try:
        fmt = sniff_format(content)
    except FormatError as error:
        return LayoutProfile(None, None, problem=f"认不出文件格式：{error}")

    if fmt is DocumentFormat.PDF:
        return _profile_pdf_page(content, page_no=page_no)
    if fmt in (DocumentFormat.JPEG, DocumentFormat.PNG, DocumentFormat.HEIC):
        return _profile_image(content, fmt=fmt, original_name=original_name)
    return LayoutProfile(
        None, None, problem=f"{fmt} 既不是图片也不是 PDF，没有版式可量。"
    )


def _profile_pdf_page(content: bytes, *, page_no: int) -> LayoutProfile:
    try:
        inspection = inspect_pdf(content)
    except PdfError as error:
        return LayoutProfile(None, None, problem=f"PDF 打不开：{error}")
    facts = inspection.page(page_no)
    if facts is None:
        return LayoutProfile(
            None,
            None,
            problem=f"这份 PDF 里没有第 {page_no} 页（共 {inspection.page_count} 页）。",
        )

    if facts.has_text:
        return _profile_text_layer(content, page_no=page_no, inspection=inspection)

    try:
        images = extract_page_images(content, pages=(page_no,))
    except PdfError as error:
        return LayoutProfile(None, None, problem=f"取不出整页图：{error}")
    usable = next((item for item in images if item.payload), None)
    if usable is None:
        return LayoutProfile(
            None,
            None,
            problem=f"第 {page_no} 页既没有文字层，也取不出整页图，量不出版式。",
        )
    try:
        image = Image.open(io.BytesIO(usable.payload))
    except (OSError, ValueError) as error:
        return LayoutProfile(None, None, problem=f"整页图解不开：{error}")
    with image:
        return _profile_pixels(image, content_class=CLASS_PDF_SCAN)


def _profile_text_layer(content: bytes, *, page_no: int, inspection: Any) -> LayoutProfile:
    """文字层页：页面尺寸 + 字号集合进指纹，表头列位置只进 features。"""
    facts = inspection.page(page_no)
    try:
        fragments = read_text_fragments(content, page=page_no)
    except PdfError as error:
        return LayoutProfile(None, None, problem=f"文字层读不出来：{error}")

    width = facts.width or 1.0
    height = facts.height or 1.0
    fonts = sorted(
        {round(item.font_size, 1) for item in fragments if not item.is_blank}
    )
    header_columns = sorted(
        {
            round(item.x / width, 2)
            for item in fragments
            if not item.is_blank and item.y > height * _HEADER_BAND
        }
    )
    template_evidence = len(fonts) >= _MIN_FONT_SIZES
    features: dict[str, Any] = {
        "profile_version": LAYOUT_PROFILE_VERSION,
        "content_class": CLASS_PDF_TEXT,
        "page_width_points": round(width),
        "page_height_points": round(height),
        "font_sizes": fonts,
        "header_column_positions": header_columns,
        "fragment_count": facts.fragment_count,
        "producer": (inspection.producer or None),
        "template_evidence": template_evidence,
    }
    fingerprint = (
        f"v{LAYOUT_PROFILE_VERSION}|{CLASS_PDF_TEXT}"
        f"|{round(width)}x{round(height)}"
        f"|fonts={','.join(f'{value:g}' for value in fonts) or 'none'}"
    )
    notes = (
        "版式特征来自**文字层**：页面尺寸与字号集合，一次模型都没调。"
        "⚠️ 表头列位置的个数会随内容多少变（实测同一模板 13 / 14 / 15），"
        "所以它只进证据、不进分组键。",
    )
    if not template_evidence:
        notes += (
            f"字号只有 {len(fonts)} 种，算不上一份排版表——这一堆只够让人扫视，"
            "不能据此说它们是同一家。",
        )
    return LayoutProfile(fingerprint, CLASS_PDF_TEXT, features, notes)


def _profile_image(
    content: bytes, *, fmt: DocumentFormat, original_name: str | None
) -> LayoutProfile:
    try:
        decoded = decode_image(content, original_name=original_name or "")
    except FormatError as error:
        # HEIC 缺解码器也走这里：本机装不上解码器不是「这张照片有问题」，
        # 所以只是量不出版式，原件与这一页的其它建议都不受影响。
        return LayoutProfile(None, None, problem=f"图片解不开：{error}")
    # 用解码出来的**原分辨率**图像对象，不再从 payload 解一遍：`profile_ink`
    # 明确要求原分辨率，而 EXIF 方向已经由 `decode_image` 摆正。
    with decoded.image:
        profile = _profile_pixels(decoded.image, content_class=CLASS_IMAGE)
    features = dict(profile.features)
    features["source_format"] = str(fmt)
    return LayoutProfile(
        profile.fingerprint,
        profile.content_class,
        features,
        profile.notes + tuple(decoded.notes),
        profile.problem,
    )


def _profile_pixels(image: Image.Image, *, content_class: str) -> LayoutProfile:
    """像素页（扫描页 / 照片）：笔迹风格 + 格线行数 + 有无竖线 + 长宽比。

    ⚠️ `profile_ink` 要原分辨率的图（它自己的文档写明缩过的图量不出来），所以这里
    不缩图。
    """
    width = image.width or 1
    height = image.height or 1
    aspect = round(height / width, _ASPECT_DIGITS)
    ink = profile_ink(image)
    notes: tuple[str, ...] = ()
    ruled_rows: int | None = None
    ruled_columns: int | None = None
    skew: float | None = None
    try:
        grid = detect_grid(image)
    except GridError as error:
        notes += (f"格线检测没结果（{error}）。",)
    else:
        ruled_rows = len(grid.rows)
        ruled_columns = len(grid.columns)
        skew = round(grid.skew_degrees, 2)

    template_evidence = bool(ruled_rows)
    features: dict[str, Any] = {
        "profile_version": LAYOUT_PROFILE_VERSION,
        "content_class": content_class,
        "pixel_width": width,
        "pixel_height": height,
        "aspect": aspect,
        "ink_style": str(ink.style),
        "ink_stroke_samples": ink.stroke_samples,
        "ruled_rows": ruled_rows,
        "ruled_columns": ruled_columns,
        "skew_degrees": skew,
        "grid_detected": ruled_rows is not None,
        "template_evidence": template_evidence,
    }
    fingerprint = (
        f"v{LAYOUT_PROFILE_VERSION}|{content_class}"
        f"|ink={ink.style.value}"
        f"|rows={ruled_rows if ruled_rows is not None else 'none'}"
        f"|cols={'yes' if ruled_columns else 'no'}"
        f"|ar={aspect}"
    )
    if not template_evidence:
        notes += (
            "这一页**没量出格线**（照片、空白页或歪得太厉害）：指纹里没有模板"
            "信息，所以它只够形成一堆待人扫视的内容，不能据此说这堆是某一家。",
        )
    else:
        notes += (
            f"版式特征：{ink.style} 笔迹、{ruled_rows} 条格线行、"
            f"{'有' if ruled_columns else '没有'}竖线、长宽比 {aspect}。"
            "⚠️ 版式相同**不证明**商家相同。",
        )
    return LayoutProfile(fingerprint, content_class, features, notes)


__all__ = [
    "CLASS_IMAGE",
    "CLASS_PDF_SCAN",
    "CLASS_PDF_TEXT",
    "LAYOUT_PROFILE_VERSION",
    "LayoutProfile",
    "profile_page_layout",
]
