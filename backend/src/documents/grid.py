"""预印表格的几何：去斜、找印刷格线、切行带与列带。

本模块是步骤 08 行号锚定与墨迹检测的前置。
它搬的是 `tools/ink_rows.py` 里**实测可靠的那部分**（去斜 + 找横线 + 切行带，
74 页中 68 页成功），不搬那个**实测不可靠**的部分（逐行判定"写了字 / 空的"——
空行与写字行的像素密度在三种基准下全部重叠，三条失败的路见
`docs/impl_record.md` 步骤 07 第 4 节）。

格线几何供行号锚定与墨迹分析使用。识别厂商没有提供的字段坐标不写入草稿。

### 去斜是前置，不是优化

步骤 07 实测：不去斜时 74 页里有 **37 页一条横线都找不到**；在 ±2° 内以 0.25°
搜一遍后，同样这些页能找到 26—42 条。实际倾斜角出现过 +1.75°、+2.00°、
−1.50°、+1.00°。

### 比例一律按内容区算，不按整幅图宽

扫描件四周有大片白边，表格只占图宽约 69%。按整幅图宽算横线覆盖率，印刷横线会被
稀释到 0.43—0.58，**一条都找不到**（步骤 07 踩过的第一个坑）。

### 诚实失败

找不到足够多的格线就返回 `determined=False` 并说明原因，**不返回一个猜出来的
行数**。拆错行不会报错，是最难发现的那种错。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from src.contracts import BoundingBox, CoordinateSpace

logger = logging.getLogger(__name__)

WORK_WIDTH = 640
"""几何检测的工作宽度。格线位置只需要相对精度，不需要原始 2479px；缩图让整批处理
快一个数量级，也顺带平滑掉一部分扫描噪点。行号与墨迹分析仍然用原图。"""

DARK_MARGIN = 40
"""比背景亮度中位数暗多少算「暗点」。背景取中位数而不是假设为白——复写纸底色偏黄。"""

SKEW_RANGE = 2.0
SKEW_STEP = 0.25
"""去斜搜索范围与步长（度）。实测这批扫描件的倾斜在 ±2° 内。"""

RULE_COVERAGE = 0.55
"""一行像素里暗点占**内容区**宽度多少算「印刷横线」。实测印刷横线为 0.62—0.84
（被竖线间空白打断，到不了 1.0），手写整行写满也难以覆盖 55%。"""

COLUMN_COVERAGE = 0.55
"""一列像素里暗点占**内容区**高度多少算「印刷竖线」。与横线同一个口径；
竖线更容易被手写跨格书写打断，所以达不到这个覆盖率的列直接不算。"""

MIN_RULES = 8
"""少于这么多条横线就判定「这不是预印表格」，返回未判定而不是硬算。"""

MIN_COLUMNS = 3
"""少于这么多条竖线就不给列切分（只给行带）。三条竖线才能围出两个格子，
少于此谈不上"按列裁格子"。"""

MIN_BAND_HEIGHT_RATIO = 14 / 640
MAX_BAND_HEIGHT_RATIO = 80 / 640
"""行带高度的合理范围，按**工作宽度的比例**给（原文是 640 下的 14—80 像素）。
过矮是两条挨着的印刷线，过高是漏检了中间的横线，两者都不当作数据行。"""

MIN_CELL_WIDTH_RATIO = 12 / 640
"""列带宽度下限，同样按工作宽度的比例。太窄的是双线或竖线本身的厚度。"""

RULE_MERGE_GAP = 4
"""挨得这么近的格线行/列合并成一条（印刷线有 2—4 像素厚）。"""

ROW_NUMBER_COLUMN_FRACTION = 0.12
"""行号列占**表格宽度**的比例。批次 2 实测：TangFa 的行号列占表宽 4.7%、
ChangShun 占 4.5%，取 12% 是为了把整个数字连同它右侧的格线都圈进来，宁可多带一点
邻列也不要把数字切掉——这一条只喂给模型读印刷行号，多带一点不影响。

⚠️ 不用 `columns[0]` 来定它：实测列带检测在这批样本上**不可靠**（74 页里 7 页一条
竖线都找不到，TangFa 的行号列分隔线因为只贯穿数据行而经常漏检，FengYuan 的
`columns[0]` 甚至落在表格左边界之外）。所以这里按**横线自身的左右端点**定表格边界，
那是逐像素可复核的事实。"""


class GridError(ValueError):
    """图像处理失败。调用方应当退回「这一页没有可用几何」，而不是改用猜测。"""


@dataclass(frozen=True)
class RowBand:
    """一个数据行格子带。坐标是**相对整页的比例**，与图像分辨率无关。"""

    index: int
    top: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def box(self) -> BoundingBox:
        return BoundingBox(
            left=0.0, top=self.top, width=1.0, height=self.height,
            space=CoordinateSpace.RATIO,
        )


@dataclass(frozen=True)
class ColumnBand:
    """一个列带。坐标同样是相对整页的比例。"""

    index: int
    left: float
    right: float

    @property
    def width(self) -> float:
        return self.right - self.left


@dataclass(frozen=True)
class PageGrid:
    """一页的表格几何。

    `determined=False` 时 `rows` / `columns` 都是空的，`reason` 说明为什么判不出来。
    调用方此时应当**不给坐标**（`evidence.box` 留 None），而不是编一个。
    """

    determined: bool
    reason: str = ""
    skew_degrees: float = 0.0
    rule_count: int = 0
    column_count: int = 0
    content_left: float = 0.0
    content_right: float = 1.0
    table_left: float = 0.0
    table_right: float = 1.0
    rows: tuple[RowBand, ...] = field(default_factory=tuple)
    columns: tuple[ColumnBand, ...] = field(default_factory=tuple)

    @property
    def row_number_strip(self) -> tuple[float, float]:
        """最左侧「行号列」的左右比例坐标。

        这批预印单据的最左列印着行号（1、2、3…）。那是**印刷数字**——步骤 07 实测
        模型在印刷体上的字段准确率 96.6%，是它最准的那一类。批次 2 按这一列建立
        「行带 ↔ 商品行」的锚定，比按像素阈值猜表头可靠得多。
        """
        width = self.table_right - self.table_left
        return self.table_left, self.table_left + width * ROW_NUMBER_COLUMN_FRACTION

    @property
    def row_capacity(self) -> int:
        """表格的**行容量**：切出来的数据行格子数。

        这是 `tools/ink_rows.py` 里唯一实测可靠的那个产出。它的用法是**上限**：
        模型报出的商品行数超过行容量，多出来的就是确证的编造
        （步骤 07 实例：ChangShun p01 纸上只写了 4 行，`qwen-vl-ocr` 报出 15 行，
        而那张表恰好印着 15 个行格）。
        """
        return len(self.rows)

    def row(self, index: int) -> RowBand | None:
        for item in self.rows:
            if item.index == index:
                return item
        return None

    def cell_box(self, *, row: int, column: int) -> BoundingBox | None:
        """某一格的比例坐标。行或列不存在时返回 None，**不夹到边界**。"""
        band = self.row(row)
        if band is None:
            return None
        for item in self.columns:
            if item.index == column:
                return BoundingBox(
                    left=item.left,
                    top=band.top,
                    width=item.width,
                    height=band.height,
                    space=CoordinateSpace.RATIO,
                )
        return None


# --- 像素层面的基础操作 -----------------------------------------------------


def _work_image(image: Image.Image) -> Image.Image:
    grey = image.convert("L")
    if grey.width == WORK_WIDTH:
        return grey
    height = max(1, round(grey.height * WORK_WIDTH / grey.width))
    return grey.resize((WORK_WIDTH, height), Image.BILINEAR)


def _binary(grey: Image.Image) -> Image.Image:
    pixels = grey.tobytes()  # mode "L"：一字节一像素
    background = sorted(pixels)[len(pixels) // 2]
    cutoff = background - DARK_MARGIN
    return grey.point(lambda v, c=cutoff: 255 if v < c else 0)


def _content_span(binary: Image.Image) -> tuple[int, int] | None:
    """内容区的左右边界（列）。一切覆盖率按它算，不按整幅图宽。

    ⚠️ **与 `_rule_columns` 是同一个左右镜像错误，一起修的。** `ROTATE_90` 转后
    第 r 行是原图第 `width-1-r` 列，所以直接拿 r 当列号会把内容区整个镜像过去
    （人造图上验证：内容只在 x=5…15 时，批次 1 的实现返回 84…94）。

    实际影响比 `_rule_columns` 小，因为内容区通常接近左右对称，镜像之后仍然盖得住
    表格——这也是它在批次 1 没被发现的原因。但它决定横线覆盖率的统计窗口，而
    `content_left` / `content_right` 还会原样记进 `raw_fields.grid`。
    """
    width, height = binary.size
    columns = binary.transpose(Image.Transpose.ROTATE_90).tobytes()
    floor = height * 0.02
    inked = [
        x
        for x in range(width)
        if columns.count(255, (width - 1 - x) * height, (width - x) * height) > floor
    ]
    if not inked:
        return None
    return inked[0], inked[-1]


def _content_rows(binary: Image.Image) -> tuple[int, int] | None:
    """内容区的上下边界（行）。竖线覆盖率按它算。"""
    width, height = binary.size
    raw = binary.tobytes()
    floor = width * 0.02
    inked = [
        y for y in range(height) if raw.count(255, y * width, (y + 1) * width) > floor
    ]
    if not inked:
        return None
    return inked[0], inked[-1]


def _rule_rows(binary: Image.Image, left: int, right: int) -> list[int]:
    width, height = binary.size
    raw = binary.tobytes()
    need = int((right - left + 1) * RULE_COVERAGE)
    return [
        y
        for y in range(height)
        if raw[y * width + left : y * width + right + 1].count(255) >= need
    ]


def _rule_columns(binary: Image.Image, top: int, bottom: int) -> list[int]:
    """印刷竖线所在的列。按内容区高度算覆盖率。

    ⚠️ **这里修掉了批次 1 的一个左右镜像错误。** PIL 的 `ROTATE_90` 是**逆时针**转：
    转完之后第 r 行对应的是原图的第 `width-1-r` 列，**不是**第 r 列。批次 1 按
    `found.append(x)` 直接记下来，于是每一条竖线都被记成了它关于页面中线的镜像位置
    （实测：人造图上 x=10 的竖线被报成 x=89）。两个后果：

    1. `crop(row, column)` 裁的一直是**镜像那一列**——行对、列错，而且不报错；
    2. `impl_record.md` 步骤 08 5.2 的第 4 条失败路径（「按竖线只在数据行里存在认表头」）
       是在镜像坐标上量的，难怪「表头带与数据带的逐列覆盖率都是 0.04」——那是在
       表格外的空白处采样。那条路**不是本来就走不通，是量错了地方**。

    ⚠️ 修法**不是**换成 `ROTATE_270`。顺时针转会把 x 对上，却把**每行内的 y 顺序
    倒过来**（原 `(x, y)` → 新 `(H-1-y, x)`），于是 `[top:bottom+1]` 这个窗口取到的
    是页面上下翻转后的那一段。实测 ChangShun p01 的内容区是 y∈[2,559]，翻转后读的
    是 y∈[274,831]——那是收据下方的空白，一条竖线都剩不下。**两个镜像各修一半，
    合起来错得更隐蔽。**

    正确的修法是留着 `ROTATE_90`（行内 y 的顺序是对的），只把行号映射过来：
    原图第 x 列 = 转后第 `width-1-x` 行。
    """
    width, height = binary.size
    rotated = binary.transpose(Image.Transpose.ROTATE_90).tobytes()
    # ROTATE_90（逆时针）后尺寸变成 (height, width)：
    # 转后第 r 行 = 原图第 width-1-r 列，行内位置 = 原图的 y（不倒序）。
    need = int((bottom - top + 1) * COLUMN_COVERAGE)
    found: list[int] = []
    for x in range(width):
        start = (width - 1 - x) * height
        if rotated[start + top : start + bottom + 1].count(255) >= need:
            found.append(x)
    return found


def _deskew(grey: Image.Image) -> tuple[Image.Image, float, int]:
    """在 ±SKEW_RANGE 内找让强横线最多的角度。返回 (转正的图, 角度, 横线行数)。"""
    best: tuple[int, float, Image.Image] = (0, 0.0, grey)
    steps = int(SKEW_RANGE / SKEW_STEP)
    for i in range(-steps, steps + 1):
        angle = i * SKEW_STEP
        candidate = (
            grey
            if angle == 0
            else grey.rotate(angle, resample=Image.BILINEAR, fillcolor=255)
        )
        binary = _binary(candidate)
        span = _content_span(binary)
        if span is None:
            continue
        count = len(_rule_rows(binary, *span))
        if count > best[0]:
            best = (count, angle, candidate)
    return best[2], best[1], best[0]


def _rule_extent(
    binary: Image.Image, rules: list[tuple[int, int]]
) -> tuple[int, int] | None:
    """印刷横线自身的左右端点中位数，也就是**表格的左右边界**。

    ⚠️ 这与 `_content_span` 不是一回事。内容区包含页眉大标题、纸张边缘的暗影、
    页脚的 "Thank You!"——实测 TangFa p01 的内容区左边界是 0.170，而表格真正的
    左边界在 0.225；差的这 5.5% 刚好是一整个行号列的宽度，按内容区去裁会把行号
    裁丢。横线是表格自己的边框，取它的端点才是表格边界。

    取中位数而不是最小/最大值：少数横线（NAME / ADDRESS 那几条）比表体窄或宽，
    用中位数不会被它们带偏。
    """
    width, _ = binary.size
    raw = binary.tobytes()
    lefts: list[int] = []
    rights: list[int] = []
    for low, high in rules:
        y = (low + high) // 2
        row = raw[y * width : (y + 1) * width]
        first = row.find(255)
        if first < 0:
            continue
        lefts.append(first)
        rights.append(width - 1 - row[::-1].find(255))
    if not lefts:
        return None
    lefts.sort()
    rights.sort()
    return lefts[len(lefts) // 2], rights[len(rights) // 2]


def _merge_adjacent(values: list[int], gap: int = RULE_MERGE_GAP) -> list[tuple[int, int]]:
    """把挨着的格线合并成一条（印刷线有 2—4 像素厚）。"""
    if not values:
        return []
    groups: list[list[int]] = [[values[0], values[0]]]
    for value in values[1:]:
        if value - groups[-1][1] <= gap:
            groups[-1][1] = value
        else:
            groups.append([value, value])
    return [(low, high) for low, high in groups]


# --- 对外接口 ---------------------------------------------------------------


def detect_grid(image: Image.Image) -> PageGrid:
    """检测一页的表格几何。判不出来时返回 `determined=False` 并说明原因。"""
    try:
        grey = _work_image(image)
    except (OSError, ValueError) as error:
        raise GridError(f"图像无法转灰度：{error}") from None

    straight, angle, rule_count = _deskew(grey)
    if rule_count < MIN_RULES:
        return PageGrid(
            determined=False,
            reason=(
                f"去斜后仍只找到 {rule_count} 条横线（需要 {MIN_RULES} 条）："
                "这一页可能不是预印表格（小票、纯文字单据），或版式不符合预期。"
            ),
            skew_degrees=angle,
            rule_count=rule_count,
        )

    binary = _binary(straight)
    span = _content_span(binary)
    rows_span = _content_rows(binary)
    if span is None or rows_span is None:
        return PageGrid(
            determined=False,
            reason="去斜后找不到内容区（整页近乎空白）。",
            skew_degrees=angle,
            rule_count=rule_count,
        )
    left, right = span
    top, bottom = rows_span
    page_width, page_height = straight.size

    rules = _merge_adjacent(_rule_rows(binary, left, right))
    extent = _rule_extent(binary, rules)
    table_left, table_right = (
        (extent[0] / page_width, (extent[1] + 1) / page_width)
        if extent is not None
        else (left / page_width, (right + 1) / page_width)
    )
    min_height = MIN_BAND_HEIGHT_RATIO * page_width
    max_height = MAX_BAND_HEIGHT_RATIO * page_width

    bands: list[RowBand] = []
    for (_, prev_bottom), (next_top, _) in zip(rules, rules[1:]):
        band_top, band_bottom = prev_bottom + 1, next_top - 1
        height = band_bottom - band_top + 1
        if height < min_height or height > max_height:
            continue
        bands.append(
            RowBand(
                index=len(bands) + 1,
                top=band_top / page_height,
                bottom=(band_bottom + 1) / page_height,
            )
        )

    verticals = _merge_adjacent(_rule_columns(binary, top, bottom))
    columns: list[ColumnBand] = []
    if len(verticals) >= MIN_COLUMNS:
        min_width = MIN_CELL_WIDTH_RATIO * page_width
        for (_, prev_right), (next_left, _) in zip(verticals, verticals[1:]):
            cell_left, cell_right = prev_right + 1, next_left - 1
            if cell_right - cell_left + 1 < min_width:
                continue
            columns.append(
                ColumnBand(
                    index=len(columns) + 1,
                    left=cell_left / page_width,
                    right=(cell_right + 1) / page_width,
                )
            )

    logger.info(
        "grid detected skew=%+.2f rules=%s rows=%s verticals=%s columns=%s table=[%.3f,%.3f]",
        angle,
        len(rules),
        len(bands),
        len(verticals),
        len(columns),
        table_left,
        table_right,
    )
    return PageGrid(
        determined=True,
        skew_degrees=angle,
        rule_count=len(rules),
        column_count=len(verticals),
        content_left=left / page_width,
        content_right=(right + 1) / page_width,
        table_left=table_left,
        table_right=table_right,
        rows=tuple(bands),
        columns=tuple(columns),
    )


TILE_GAP = 14
"""拼接行号格子时相邻两格之间留的空白像素。"""


def row_number_tiles(image: Image.Image, grid: PageGrid) -> Image.Image:
    """把**每一条行带**最左侧的行号格子裁下来，按从上到下的顺序竖着拼成一条。

    ### 为什么要拼，而不是把整条左列原样发给模型

    模型不会报坐标（步骤 07 的提示词没要求，它也没给），所以「模型读到的第 3 个
    数字」落在**哪一条行带**里无从对应。拼接图把这件事变成构造上的事实：
    **第 k 个格子就是第 k 条行带**，因为那是我们自己裁、自己摆的。模型只需要
    逐格回答「这一格印着几」，顺序由图本身保证。

    ### 边框与空白是给模型数格子用的

    每格四周留空白并描一个淡框。实测（2026-11-11，74 页）两种画法都试过：
    淡框 + 不告诉模型格子数 → 35 页锚定成立；换成粗黑框 + 在提示词里写明
    「图里正好有 N 个格子」之后，`qwen3-vl-plus` 开始**整体平移一格去凑够 N**
    （ChangShun p01：一个模型答 `[null,1,2,…,15]`，另一个答 `[null,null,1,…,14]`，
    两份各自都是连号、都能过本地校验，却差了一行）。**把期望长度写进提示词会诱发
    凑长度**，所以这里不写格子数，长度对不对由本地校验说了算。

    ⚠️ 这是一个**新增**的提示词配置，不改 `CONTENT_PROMPT`（它绑着步骤 07 的选型
    结论，sha 由测试守着）。
    """
    if not grid.determined or not grid.rows:
        raise GridError("格线未判定，拼不出行号列。")
    straight = (
        image.rotate(grid.skew_degrees, resample=Image.BICUBIC, fillcolor=255)
        if grid.skew_degrees
        else image
    )
    width, height = straight.size
    left_ratio, right_ratio = grid.row_number_strip
    left = max(0, int(round(left_ratio * width)))
    right = min(width, int(round(right_ratio * width)))
    if right - left < 4:
        raise GridError(
            f"行号列宽度只有 {right - left} 像素，裁不出可读的格子"
            f"（表格边界 [{grid.table_left:.3f}, {grid.table_right:.3f}]）。"
        )

    tiles = [
        straight.crop(
            (
                left,
                max(0, int(round(band.top * height))),
                right,
                min(height, int(round(band.bottom * height))),
            )
        ).convert("L")
        for band in grid.rows
    ]
    tiles = [tile for tile in tiles if tile.width > 0 and tile.height > 0]
    if len(tiles) != len(grid.rows):
        raise GridError("有行带裁出来是空的，拼不出行号列。")

    tile_width = max(tile.width for tile in tiles)
    total = sum(tile.height for tile in tiles) + TILE_GAP * (len(tiles) + 1)
    canvas = Image.new("L", (tile_width + 2 * TILE_GAP, total), 255)
    pen = ImageDraw.Draw(canvas)
    offset = TILE_GAP
    for tile in tiles:
        canvas.paste(tile, (TILE_GAP, offset))
        pen.rectangle(
            [TILE_GAP - 1, offset - 1, TILE_GAP + tile.width, offset + tile.height],
            outline=160,
        )
        offset += tile.height + TILE_GAP
    return canvas


def header_tiles(image: Image.Image, grid: PageGrid, band_index: int) -> Image.Image:
    """把**表头那一行**的每个列格子裁下来，按从左到右的顺序竖着拼成一条。

    与 `row_number_tiles` 是同一个套路、同一个理由：第 k 个格子就是第 k 条列带，
    这是我们自己裁、自己摆的构造性事实，模型只需要逐格回答「这一格印的是哪一类」。

    竖着拼而不是横着拼：表头字是横写的，竖着堆每一格都保持原来的长宽比，
    不会被挤扁。
    """
    if not grid.determined or not grid.columns:
        raise GridError("这一页没有列带，拼不出表头。")
    band = grid.row(band_index)
    if band is None:
        raise GridError(f"这一页没有第 {band_index} 条行带，拼不出表头。")
    straight = (
        image.rotate(grid.skew_degrees, resample=Image.BICUBIC, fillcolor=255)
        if grid.skew_degrees
        else image
    )
    width, height = straight.size
    top = max(0, int(round(band.top * height)))
    bottom = min(height, int(round(band.bottom * height)))
    tiles = []
    for column in grid.columns:
        left = max(0, int(round(column.left * width)))
        right = min(width, int(round(column.right * width)))
        if right - left < 4 or bottom - top < 4:
            raise GridError(
                f"第 {column.index} 条列带在表头行上只有 {right - left}×{bottom - top} "
                "像素，裁不出可读的格子。"
            )
        tiles.append(straight.crop((left, top, right, bottom)).convert("L"))

    tile_width = max(tile.width for tile in tiles)
    total = sum(tile.height for tile in tiles) + TILE_GAP * (len(tiles) + 1)
    canvas = Image.new("L", (tile_width + 2 * TILE_GAP, total), 255)
    pen = ImageDraw.Draw(canvas)
    offset = TILE_GAP
    for tile in tiles:
        canvas.paste(tile, (TILE_GAP, offset))
        pen.rectangle(
            [TILE_GAP - 1, offset - 1, TILE_GAP + tile.width, offset + tile.height],
            outline=160,
        )
        offset += tile.height + TILE_GAP
    return canvas


__all__ = [
    "COLUMN_COVERAGE",
    "ColumnBand",
    "GridError",
    "MIN_COLUMNS",
    "MIN_RULES",
    "PageGrid",
    "ROW_NUMBER_COLUMN_FRACTION",
    "RULE_COVERAGE",
    "RowBand",
    "SKEW_RANGE",
    "TILE_GAP",
    "WORK_WIDTH",
    "detect_grid",
    "header_tiles",
    "row_number_tiles",
]
