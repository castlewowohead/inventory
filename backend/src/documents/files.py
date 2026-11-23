"""上传文件的真实格式判定、解码与媒体类型。

三条口径：

1. **按内容嗅探，不信扩展名。** 手机相册导出、微信转发、重命名都会让扩展名和内容
   对不上。步骤 05 的白名单按扩展名拦（那是入口校验，没问题），但要把字节交给
   解码器或 OCR 服务时，必须按**真实格式**给媒体类型——步骤 07 实测统一写死
   `image/jpeg` 会让 PNG 被按错格式解码。

2. **HEIC 要解码成 JPEG 再送出去。** 步骤 05 收下 `.heic` 但只存不识别，解码留到
   本步骤。阿里云的 data URL 只吃 jpeg / png，所以 HEIC 进来先转 JPEG。
   **转码是一次显式的、留痕的转换**：原始 HEIC 永远保持不动（它是单据证据），
   转出来的 JPEG 只在内存里用于识别与表格分析，不落库、不入存储。

3. **EXIF 方向要先摆正。** iPhone 竖拍的照片像素其实是横的，靠 EXIF 的
   `Orientation` 告诉浏览器怎么转。不摆正的话去斜找横线会全部失败（找的是横线，
   而图是躺着的）。摆正同样留痕。

**不做的事**：不缩图、不增强对比度、不去噪。步骤 07 实测 8 MP 原图单页
3,000—8,300 tokens、5—7 秒，**成本与延迟不构成约束**，为省钱缩图只会丢掉笔画
细节——而笔画细节正是这批复写纸手写件唯一的指望。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from enum import StrEnum

from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Pillow 的解压炸弹上限。显式写出来而不是用默认值：这批扫描件是 2479×3230（8 MP），
# 手机原图可到 12 MP（4032×3024），留到 80 MP 足够宽裕，又挡得住刻意构造的巨图。
MAX_IMAGE_PIXELS = 80_000_000

# HEIC 转 JPEG 的质量。95 几乎看不出损失；再高只会让体积翻倍而笔画细节不再增加。
HEIC_JPEG_QUALITY = 95


class DocumentFormat(StrEnum):
    """嗅探出来的真实格式。"""

    JPEG = "jpeg"
    PNG = "png"
    HEIC = "heic"
    PDF = "pdf"
    UNKNOWN = "unknown"


# 各格式送给 OCR 服务时 data URL 用的媒体类型。
# HEIC 不在这里：它必须先转成 JPEG，转完按 JPEG 给。
_MEDIA_TYPES: dict[DocumentFormat, str] = {
    DocumentFormat.JPEG: "image/jpeg",
    DocumentFormat.PNG: "image/png",
    DocumentFormat.PDF: "application/pdf",
}

# 魔数。只认我们真正处理的这几种，其它一律 UNKNOWN，由调用方报错而不是猜。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF-"
_JPEG_MAGIC = b"\xff\xd8\xff"

# HEIF / HEIC 是 ISO-BMFF 容器：前 4 字节是 box 长度，紧跟 `ftyp`，再跟品牌。
# 这些品牌是 Apple 的 HEIC 与通用 HEIF 实际会出现的取值。
_HEIF_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1"}
)


class FormatError(ValueError):
    """文件格式不受支持，或内容坏到解不开。

    这是**用户输入问题**（传错文件、文件损坏），由 API 层映射成 400；
    不是程序错误，也不应该让提取任务无限重试。
    """


@dataclass(frozen=True)
class DecodedImage:
    """一张解好码、方向已摆正的图，连同送出去时该用的字节与媒体类型。

    `notes` 是**人看得见的转换记录**：转了码、转了方向都写在这里，最终落进
    识别结果的 `warnings` 与任务记录里。与步骤 08.3e「数值规范化要显式且可见」
    同一条原则——任何对原件的改动都要留痕，不静默发生。
    """

    image: Image.Image
    """已摆正方向的 Pillow 图像对象。调用方负责 close（或用 with）。"""

    payload: bytes
    """送给 OCR 服务的字节。JPEG / PNG 原样透传，HEIC 是转出来的 JPEG。"""

    media_type: str
    """`payload` 对应的媒体类型，用于 data URL。**不写死 image/jpeg。**"""

    source_format: DocumentFormat
    """原始文件的真实格式（转码前）。"""

    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height


def sniff_format(content: bytes) -> DocumentFormat:
    """按魔数判断真实格式。认不出就是 UNKNOWN，**不按扩展名补**。"""
    if content.startswith(_PNG_MAGIC):
        return DocumentFormat.PNG
    if content.startswith(_PDF_MAGIC):
        return DocumentFormat.PDF
    if content.startswith(_JPEG_MAGIC):
        return DocumentFormat.JPEG
    if len(content) >= 12 and content[4:8] == b"ftyp":
        if content[8:12].lower() in _HEIF_BRANDS:
            return DocumentFormat.HEIC
    return DocumentFormat.UNKNOWN


def media_type_for(content: bytes) -> str | None:
    """`content` 送给 OCR 服务时该用的媒体类型；HEIC 与未知格式返回 None。

    HEIC 返回 None 是故意的：它必须先经 `decode_image()` 转成 JPEG，
    这里不给出一个「看起来能用」的媒体类型。
    """
    return _MEDIA_TYPES.get(sniff_format(content))


def _is_heic_supported() -> bool:
    """HEIC 解码器在不在。

    `pillow-heif` 是本步骤新加的运行依赖。把「装没装」做成可查询的事实，
    是为了在缺它的时候能报一句**说得清的话**，而不是抛一个 Pillow 内部异常。
    """
    try:
        import pillow_heif  # noqa: PLC0415  (按需导入：缺它时也要能启动)
    except ImportError:
        return False
    pillow_heif.register_heif_opener()
    return True


def heic_decoder_available() -> bool:
    """本机装了 HEIC 解码器吗。

    给**摄入阶段**区分两种解不开：文件本身坏了（用户要重传），还是本机缺解码器
    （文件没问题，是环境缺依赖）。两者都报「解析失败」会让人去重传一张好照片，
    传几次都一样。
    """
    return _is_heic_supported()


def decode_image(content: bytes, *, original_name: str | None = None) -> DecodedImage:
    """解码一张图：摆正方向、必要时转码，返回可直接送出去的字节与媒体类型。

    PDF 不走这里（PDF 要先经 `documents.pdf` 判断是文字层还是扫描图）。
    """
    fmt = sniff_format(content)
    if fmt is DocumentFormat.PDF:
        raise FormatError(
            f"{original_name or '文件'} 是 PDF，要先判断它是带文字层的数字 PDF "
            "还是扫描成图的 PDF（见 documents.pdf），不能直接当图片解码。"
        )
    if fmt is DocumentFormat.UNKNOWN:
        raise FormatError(
            f"认不出 {original_name or '文件'} 的格式（内容的前几个字节既不是 "
            "JPEG / PNG / HEIC 也不是 PDF）。文件可能损坏或被改了扩展名。"
        )

    notes: list[str] = []
    if fmt is DocumentFormat.HEIC and not _is_heic_supported():
        raise FormatError(
            "这是 HEIC（iPhone 原图格式），但本机没有 HEIC 解码器。"
            "请安装 requirements.txt 里的 pillow-heif，或在手机上改用「兼容模式」"
            "拍照（输出 JPG）。原始文件已经保存，不会丢。"
        )

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(content)) as opened:
            opened.load()
            # EXIF 方向：竖拍照片的像素其实是横的，靠 Orientation 标签告诉看图程序
            # 怎么转。不摆正的话去斜找横线会全部失败（找的是横线，而图是躺着的）。
            #
            # ⚠️ 判断「到底转没转」要看**标签值**，不能看 `exif_transpose` 有没有
            # 返回新对象——它**总是**返回副本，即使没有方向标签。按对象身份判断会
            # 让每一张图都挂上一条「已按 EXIF 方向摆正」，而那是假的；
            # 诊断信息说错话比不说话更糟。
            orientation = _exif_orientation(opened)
            oriented = ImageOps.exif_transpose(opened) or opened
            if orientation not in (None, 1):
                notes.append(
                    f"按 EXIF 方向标签 {orientation} 摆正："
                    f"{opened.width}×{opened.height} → "
                    f"{oriented.width}×{oriented.height}"
                )
            image = oriented if oriented is not opened else oriented.copy()
    except FormatError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise FormatError(
            f"{original_name or '文件'} 解码失败：{error}。"
            "文件可能在传输中损坏；原始文件已经保存，可以重新上传一次。"
        ) from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    if fmt is DocumentFormat.HEIC:
        payload, media_type = _to_jpeg(image)
        notes.append(
            f"HEIC 转 JPEG（质量 {HEIC_JPEG_QUALITY}，**不缩图**）："
            f"{len(content)} → {len(payload)} 字节。原始 HEIC 保持不动。"
        )
    elif image.mode not in ("RGB", "L") and fmt is DocumentFormat.JPEG:
        # 极少见：CMYK 或带 alpha 的 JPEG。转一遍 RGB 再送出去，并留痕。
        payload, media_type = _to_jpeg(image)
        notes.append(f"JPEG 色彩模式 {image.mode} 转 RGB 后重新编码")
    else:
        payload = content
        media_type = _MEDIA_TYPES[fmt]

    logger.info(
        "image decoded format=%s size=%sx%s payload=%s media_type=%s notes=%s",
        fmt,
        image.width,
        image.height,
        len(payload),
        media_type,
        len(notes),
    )
    return DecodedImage(
        image=image,
        payload=payload,
        media_type=media_type,
        source_format=fmt,
        notes=tuple(notes),
    )


_EXIF_ORIENTATION_TAG = 274
"""EXIF 的 Orientation 标签号。1 = 正常，其余是各种旋转与镜像。"""


def _exif_orientation(image: Image.Image) -> int | None:
    """读 EXIF 方向标签。没有 EXIF、或读不出来时返回 None（不猜成 1）。"""
    try:
        exif = image.getexif()
    except (OSError, ValueError, AttributeError):
        return None
    if not exif:
        return None
    value = exif.get(_EXIF_ORIENTATION_TAG)
    return int(value) if isinstance(value, int) else None


def _to_jpeg(image: Image.Image) -> tuple[bytes, str]:
    """把一张图编码成 JPEG 字节。**不缩图**，只做必要的色彩模式转换。"""
    source = image
    if source.mode in ("RGBA", "LA", "P"):
        # 带透明通道的图没法直接存 JPEG。白底合成，而不是丢掉 alpha——
        # 丢 alpha 会把透明区域变成黑色，扫描件上那是一大片黑。
        source = source.convert("RGBA")
        canvas = Image.new("RGB", source.size, (255, 255, 255))
        canvas.paste(source, mask=source.split()[-1])
        source = canvas
    elif source.mode not in ("RGB", "L"):
        source = source.convert("RGB")
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", quality=HEIC_JPEG_QUALITY, subsampling=0)
    return buffer.getvalue(), _MEDIA_TYPES[DocumentFormat.JPEG]


__all__ = [
    "DecodedImage",
    "DocumentFormat",
    "FormatError",
    "HEIC_JPEG_QUALITY",
    "MAX_IMAGE_PIXELS",
    "decode_image",
    "heic_decoder_available",
    "media_type_for",
    "sniff_format",
]
