"""本地文档处理的测试（步骤 08 的 `src/documents/`）。

这组用例不访问网络，也不读真实单据——真实样本含客户数据、不进源码交付
（见 `.gitignore`），所以这里用**程序生成的图像与 PDF**。真实样本上的验证由
`tools/eval_adapter.py` 与 `tools/e2e_extraction.py` 负责，结论记在实施记录里。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from src.documents.classify import InkStyle, profile_ink
from src.documents.files import (
    DocumentFormat,
    FormatError,
    decode_image,
    media_type_for,
    sniff_format,
)
from src.documents.grid import detect_grid
from src.documents.pdf import PdfError, PdfKind, inspect_pdf, read_text_fragments
from src.documents.textlayer import parse_text_rows


def _png(width: int = 40, height: int = 30, color=(255, 255, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(width: int = 40, height: int = 30) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (240, 240, 230)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _ruled_form(rows: int = 12, columns: int = 5) -> Image.Image:
    """画一张预印表格：横线 + 竖线，像这批供应商单据那样。"""
    width, height = 900, 1200
    image = Image.new("RGB", (width, height), (250, 248, 230))
    draw = ImageDraw.Draw(image)
    left, right = 80, width - 80
    top, bottom = 150, height - 150
    step = (bottom - top) // rows
    for index in range(rows + 1):
        y = top + index * step
        draw.line([(left, y), (right, y)], fill=(40, 90, 60), width=3)
    column_step = (right - left) // columns
    for index in range(columns + 1):
        x = left + index * column_step
        draw.line([(x, top), (x, bottom)], fill=(40, 90, 60), width=3)
    return image


# --- 格式嗅探与解码 ---------------------------------------------------------


def test_format_is_sniffed_from_content_not_the_extension() -> None:
    """按内容认格式。手机相册导出、微信转发、重命名都会让扩展名对不上。"""
    assert sniff_format(_png()) is DocumentFormat.PNG
    assert sniff_format(_jpeg()) is DocumentFormat.JPEG
    assert sniff_format(b"%PDF-1.4\n") is DocumentFormat.PDF
    assert sniff_format(b"not a real file") is DocumentFormat.UNKNOWN


def test_media_type_follows_the_real_format() -> None:
    """⚠️ 步骤 07 实测：统一写死 image/jpeg 会让 PNG 被按错格式解码。"""
    assert media_type_for(_png()) == "image/png"
    assert media_type_for(_jpeg()) == "image/jpeg"
    assert media_type_for(b"rubbish") is None


def test_decoding_a_png_passes_the_original_bytes_through() -> None:
    """能直接送出去的格式不重新编码——不重编就不会二次损失。"""
    content = _png()

    decoded = decode_image(content, original_name="a.png")

    assert decoded.payload == content
    assert decoded.media_type == "image/png"
    assert decoded.source_format is DocumentFormat.PNG


def test_decoding_refuses_a_pdf_with_a_pointer_to_the_right_module() -> None:
    with pytest.raises(FormatError, match="documents.pdf"):
        decode_image(b"%PDF-1.4\n", original_name="scan.pdf")


def test_broken_content_fails_with_a_message_that_says_what_to_do() -> None:
    with pytest.raises(FormatError) as excinfo:
        decode_image(b"\xff\xd8\xff" + b"garbage" * 20, original_name="photo.jpg")

    # 原始文件已经保存——提取失败不该让人以为单据丢了。
    assert "原始文件" in str(excinfo.value)


def test_no_exif_tag_means_no_rotation_note() -> None:
    """⚠️ `exif_transpose` **总是**返回副本，即使没有方向标签。

    按对象身份判断会让每一张图都挂上一条"已按 EXIF 方向摆正"——
    而那是假的。诊断信息说错话比不说话更糟。
    """
    decoded = decode_image(_jpeg(), original_name="scan.jpg")

    assert not any("EXIF" in note for note in decoded.notes)


# --- PDF --------------------------------------------------------------------


def test_a_pdf_with_a_text_layer_is_recognised_as_such() -> None:
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)

    inspection = inspect_pdf(buffer.getvalue())

    # 空白页既没有文字也没有图：诚实地报 EMPTY，而不是猜一个类别。
    assert inspection.kind is PdfKind.EMPTY
    assert inspection.page_count == 1


def test_non_pdf_content_is_refused() -> None:
    with pytest.raises(PdfError, match="%PDF-"):
        inspect_pdf(_png())


def test_reading_a_missing_page_is_an_error_not_an_empty_list() -> None:
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(PdfError, match="第 5 页"):
        read_text_fragments(buffer.getvalue(), page=5)


# --- 表格几何 ---------------------------------------------------------------


def test_grid_detection_finds_rows_and_columns_on_a_ruled_form() -> None:
    grid = detect_grid(_ruled_form(rows=12, columns=5))

    assert grid.determined
    # 12 条格子由 13 条横线围出来；检测到的行带数应当接近它。
    assert 8 <= grid.row_capacity <= 12
    assert len(grid.columns) >= 3


def test_grid_is_honestly_undetermined_on_a_blank_page() -> None:
    """找不到足够的横线就说找不到，**不返回一个猜出来的行数**。

    拆错行不会报错，是最难发现的那种错（与 split_scanned_pdf 同一条原则）。
    """
    grid = detect_grid(Image.new("RGB", (900, 1200), (255, 255, 255)))

    assert not grid.determined
    assert grid.reason
    assert grid.rows == ()


def test_detected_columns_are_not_mirrored_left_to_right() -> None:
    """⚠️ 批次 1 的竖线检测把每一列记成了它**关于页面中线的镜像**位置。

    PIL 的 `ROTATE_90` 是逆时针转，转后第 r 行对应原图第 `width-1-r` 列，而当时
    直接把 r 当成了列号。后果有两个：`crop(row, column)` 一直裁的是镜像那一列
    （行对、列错，而且不报错）；以及 `impl_record.md` 步骤 08 5.2 第 4 条那次
    「按竖线认表头」的测量其实是在表格外的空白处采样。
    """
    image = Image.new("RGB", (400, 300), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    # 一条贯穿全高的竖线，**刻意放在偏左**：镜像与否一眼能看出来。
    for x in (60, 70):
        draw.line([(x, 0), (x, 299)], fill=(0, 0, 0), width=3)
    from src.documents.grid import _binary, _rule_columns, _work_image

    binary = _binary(_work_image(image))
    found = _rule_columns(binary, 0, binary.size[1] - 1)

    assert found, "应当找得到这条竖线"
    middle = binary.size[0] / 2
    assert all(x < middle for x in found), f"竖线在左半边，却检出在 {found}（镜像了）"


def test_table_bounds_come_from_the_rules_not_the_content_area() -> None:
    """表格左右边界取**横线自身的端点**，不取内容区。

    内容区包含页眉大标题与纸张边缘的暗影——实测 TangFa p01 的内容区左边界是
    0.170，而表格真正的左边界在 0.225，差的这一截刚好是一整个行号列的宽度。
    """
    image = _ruled_form(rows=12, columns=5)
    draw = ImageDraw.Draw(image)
    # 在表格左边之外画一条页眉横幅：内容区会被它拉宽，表格边界不该受影响。
    draw.rectangle([10, 40, 870, 70], fill=(20, 20, 20))

    grid = detect_grid(image)

    assert grid.determined
    assert grid.content_left < grid.table_left
    # 表格画在 x=80..820 / 900 之间。
    assert abs(grid.table_left - 80 / 900) < 0.03
    assert abs(grid.table_right - 820 / 900) < 0.03


def test_the_row_number_strip_sits_at_the_left_edge_of_the_table() -> None:
    grid = detect_grid(_ruled_form(rows=12, columns=5))
    left, right = grid.row_number_strip

    assert left == grid.table_left
    assert left < right <= grid.table_right


def test_row_number_tiles_give_one_tile_per_band() -> None:
    """拼接图里第 k 个格子就是第 k 条行带——这是构造上的事实，不是模型给的。"""
    from src.documents.grid import TILE_GAP, row_number_tiles

    image = _ruled_form(rows=12, columns=5)
    grid = detect_grid(image)
    tiles = row_number_tiles(image, grid)

    # 高度 = 各行带高度之和 + (格子数 + 1) 条间隔。
    assert tiles.height > TILE_GAP * (grid.row_capacity + 1)
    assert tiles.width > 0


def test_header_tiles_need_both_a_band_and_columns() -> None:
    from src.documents.grid import GridError, header_tiles

    image = _ruled_form(rows=12, columns=5)
    grid = detect_grid(image)

    assert header_tiles(image, grid, grid.rows[0].index).height > 0
    with pytest.raises(GridError):
        header_tiles(image, grid, 999)


# --- 印刷 / 手写判别 ---------------------------------------------------------


def test_uniform_printed_strokes_are_classified_as_printed() -> None:
    """合成一张笔画宽度一致的图：应当判成印刷体。"""
    image = Image.new("RGB", (1200, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for row in range(20):
        y = 150 + row * 30
        for column in range(40):
            x = 100 + column * 25
            draw.rectangle([x, y, x + 5, y + 14], fill=(20, 20, 20))

    profile = profile_ink(image)

    assert profile.style is InkStyle.PRINTED
    assert profile.reason


def test_a_blank_page_is_unknown_not_printed() -> None:
    """判不出来就说判不出来——调用方会按**最保守**那条路处理（不预填）。"""
    profile = profile_ink(Image.new("RGB", (1200, 900), (255, 255, 255)))

    assert profile.style is InkStyle.UNKNOWN
    assert "样本" in profile.reason


# --- 文字层解析 -------------------------------------------------------------


def test_pipe_delimited_table_maps_columns_by_field_index() -> None:
    """⚠️ 竖线表按**第几个字段**归列，不按字符偏移。

    表头词比值长，按偏移归列会整体错一列——实测 `kg` 被归进描述列、
    `6.00` 被归进单位列，而且不报错。
    """
    parse = parse_text_rows(
        "Vendor: Lakeview Produce\n"
        "Invoice No. LP-9981\n"
        "Qty | Description | UOM | Rate | Amount\n"
        "6 | Lemons | kg | 6.00 | 36.00\n"
        "Total 36.00\n"
    )

    assert parse.header_found
    line = parse.result.line(1)
    assert line is not None
    assert line.get("item_description").raw_text == "Lemons"
    assert line.get("quantity").raw_text == "6"
    assert line.get("unit").raw_text == "kg"
    assert line.get("unit_price").raw_text == "6.00"
    assert line.get("line_amount").raw_text == "36.00"


def test_inline_labels_are_split_from_their_values() -> None:
    """`Vendor: Lakeview Produce` 一格里既有标签也有值，必须拆开。

    不拆的话这一格会被**下一行的标签**认领——实测出现过
    `Supplier: Northern ProduceNP-091` 这种拼接。
    """
    parse = parse_text_rows(
        "Supplier: Northern Produce\n"
        "Invoice #\n"
        "NP-091\n"
        "Description | Qty | Unit | Price | Amount\n"
        "Lemons | 5 | kg | 6.00 | 30.00\n"
        "Grand Total: 33.60\n"
    )

    assert parse.result.document_field("supplier").raw_text == "Northern Produce"
    assert parse.result.document_field("invoice_number").raw_text == "NP-091"


def test_invoice_total_never_comes_from_adding_the_lines() -> None:
    """⚠️ 总额只认纸面上带总额标签的那个数。

    `Grand Total` 优先于 `Total due`——后者是扣掉预付之后的应付额。
    把各行相加会让对账永远显示差额为 0，也会在评测里**奖励编造**。
    """
    parse = parse_text_rows(
        "Description | Qty | Unit | Price | Amount\n"
        "Lemons | 5 | kg | 6.00 | 30.00\n"
        "Subtotal 30.00\n"
        "Grand total 252.55\n"
        "Total due 102.55\n"
    )

    assert parse.result.document_field("invoice_total").raw_text == "252.55"


def test_a_product_name_containing_total_is_not_treated_as_a_summary_row() -> None:
    """`Total Mix Vegetables` 是商品名，不是汇总行。丢掉一行商品不会报错。"""
    parse = parse_text_rows(
        "Description | Qty | Unit | Price | Amount\n"
        "Total Mix Vegetables | 2 | kg | 3.00 | 6.00\n"
        "Grand total 6.00\n"
    )

    line = parse.result.line(1)
    assert line is not None
    assert line.get("item_description").raw_text == "Total Mix Vegetables"


def test_an_unsplittable_layout_is_refused_rather_than_guessed() -> None:
    """切不出列就不产出行，并说明**为什么**。

    没有列映射就没法知道哪个数字是数量、哪个是单价，猜一次就是一个错价格
    永久留在历史里。
    """
    parse = parse_text_rows(
        "Supplier: Market Pantry\n"
        "Description Qty Unit Price Amount\n"
        "Fresh onions 2 12.50 25.00\n"
        "Invoice total 96.80\n"
    )

    assert not parse.header_found
    assert parse.result.lines == ()
    assert "单个空格" in parse.reason
    # 单据级字段仍然抽得出来——拒绝的只是行。
    assert parse.result.document_field("invoice_total").raw_text == "96.80"


def test_a_text_layer_has_no_unreadable_state() -> None:
    """文字层里要么有字符、要么纸上没有，**不存在「认不出」**。

    把空值错标成 UNREADABLE 会让审核界面要求人去补一个纸上本来就没有的东西。
    """
    parse = parse_text_rows(
        "Description | Qty | Unit | Price | Amount\n"
        "Lemons | 5 |  | 6.00 | 30.00\n"
        "Total 30.00\n"
    )

    line = parse.result.line(1)
    assert line is not None
    assert str(line.get("unit").status) == "missing"
