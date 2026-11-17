"""本地文档处理：格式嗅探、HEIC 解码、PDF 文字层与扫描页、表格几何。

**为什么不放在 `integrations/`**：这里一行代码都不访问外部服务。`integrations/`
的职责是「封装外部服务与厂商差异」（见 docs/struct.md），而本包只用 Pillow 与
pypdf 做本地计算——把它放进 `integrations/` 会让「哪些模块会发网络请求」这件事
变得说不清。换 OCR 厂商时本包不用改，这正是它独立存在的理由。

本包提供四件事，按步骤 08 的分层编号对应：

- `files`：上传文件的真实格式判定、HEIC 解码、EXIF 方向校正、data URL 媒体类型。
  ⚠️ 媒体类型按**嗅探出来的真实格式**给，不统一写死 `image/jpeg`——步骤 07 实测
  写死会让 PNG 被按错格式解码。
- `pdf`：区分「带文字层的数字 PDF」与「扫描成图的 PDF」，前者直接抽文字层
  （08 分流第一条路径，根本不调 OCR），后者取出每页内嵌的图像。
- `textlayer`：把定位好的文本片段还原成行列结构与统一契约。⚠️ **文字层不是白捡**
  ——步骤 07 实测字符准确但行列归位仍要写代码（发票号 `MP-2026-0611-219` 被
  拆成 `MP-2026-0611-21` 和 `9` 两段）。
- `grid`：去斜、找印刷横线、切行带与列带。去斜是**前置不是优化**：
  步骤 07 实测不去斜时 74 页里有 37 页一条横线都找不到。
- `anchor`（批次 2）：「行带 ↔ 印刷行号」「列带 ↔ 字段」的对应关系，以及守着它的
  本地校验（长度 / 连号 / 连续 / 两模型一致）。⚠️ 这里的模块**不调模型**，只判读数
  合不合法——读数由 `integrations/ocr.py` 取回来。
- `ink`（批次 2）：第 0 层墨迹闸门。同一列的数据格是同一个印刷方框，逐像素取中位数
  得到「空格子模板」再相减。⚠️ 它依赖 `anchor`：不知道哪几条行带是数据行，模板就
  会混进表头带。

本包**不做**业务判断：不决定某个值能不能预填、不决定某行能不能确认，
那些属于 `services/extraction.py` 与步骤 05 的审核规则。
"""

from __future__ import annotations

from src.documents.files import (
    DocumentFormat,
    DecodedImage,
    FormatError,
    decode_image,
    media_type_for,
    sniff_format,
)
from src.documents.anchor import ColumnMap, RowAnchor, column_map_from, row_anchor_from
from src.documents.grid import (
    ColumnBand,
    GridError,
    PageGrid,
    RowBand,
    detect_grid,
    header_tiles,
    row_number_tiles,
)
from src.documents.ink import InkError, InkGate, InkVerdict, gate_column, gate_rows
from src.documents.pdf import (
    PdfError,
    PdfKind,
    PdfPageImage,
    TextFragment,
    extract_page_images,
    inspect_pdf,
    read_text_fragments,
)
from src.documents.textlayer import TextLayerParse, parse_text_layout, parse_text_rows

__all__ = [
    "ColumnBand",
    "ColumnMap",
    "DecodedImage",
    "DocumentFormat",
    "FormatError",
    "GridError",
    "InkError",
    "InkGate",
    "InkVerdict",
    "PageGrid",
    "PdfError",
    "PdfKind",
    "PdfPageImage",
    "RowAnchor",
    "RowBand",
    "TextFragment",
    "TextLayerParse",
    "column_map_from",
    "decode_image",
    "detect_grid",
    "extract_page_images",
    "gate_column",
    "gate_rows",
    "header_tiles",
    "inspect_pdf",
    "media_type_for",
    "parse_text_layout",
    "parse_text_rows",
    "read_text_fragments",
    "row_anchor_from",
    "row_number_tiles",
    "sniff_format",
]
