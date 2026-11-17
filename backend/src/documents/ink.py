"""第 0 层墨迹闸门：**页内逐像素配准**的空 / 非空判定（步骤 08.3a，批次 2）。

### 这与步骤 07 否决掉的那几条不是同一件事

`impl_record.md` 步骤 07 第 4 节否决的是**按密度标量比较**的三条基准（固定阈值数暗点、
页内行带密度中位数、跨页同序号行带密度中位数），以及另外两条（跨页
`ImageChops.lighter` 合成空白模板、形态学腐蚀去噪）。它们共同的毛病是**没有配准**：
拿一个标量去比，或者拿对不齐的两页去相减。

这里做的是另一件事：**同一页里，同一列的 M 个数据格子是同一个印刷方框**——同一张
纸、同一次扫描、同一个去斜角，天生逐像素对齐，不需要跨页配准。把它们缩放到同一
尺寸后**逐像素取中位数**，得到这一列的「空格子模板」；写了字的行是少数，所以中位数
落在空白上。再拿每个格子与模板相减，剩下的就是手写。

⚠️ **这条路是由「行号列锚定」解锁的**（见 `grid.row_number_strip`）。没有锚定就不
知道哪几条行带是数据行，把表头带混进来，模板立刻被污染——这正是步骤 07 那条
「跨页同序号行带中位数」失败的根因之一（不同页检测到的行带数是 21 / 24 / 25 三种，
序号根本对不上）。

### 2026-11-11 实测（人工核对子集 3 页，口径与 `impl_record.md` 步骤 07 4.3 一致）

| 页 | 纸面写了 | 写了字的行得分 | 空行得分 |
| --- | --- | --- | --- |
| TangFa p01 | 1 行 | 0.0708 | 0.0055 — 0.0206 |
| TangFa p07 | 1 行 | 0.0776 | 0.0108 — 0.0195 |
| ChangShun p01 | 4 行 | 0.0873 — 0.1249 | 0.0017 — 0.0306 |

⚠️ ChangShun p01 那三个 0.028—0.031 的「空行」**纸上确实有墨**——第 5—7 行有一条
划掉用的斜线，第 10—12 行压着签名。闸门判它们「有墨」是**对的**：闸门管的是
「这一格是不是空的」，不是「这一行是不是商品行」。

### 阈值往哪边偏

判错的两个方向代价**不对称**：把写了字的格子判成空，会**丢掉真实数据**；把空格子
判成有字，最多是退回批次 1 的行为（不丢弃模型报的值）。所以阈值往「宁可算有墨」
偏，`INK_THRESHOLD` 卡在实测空行上限的约 2 倍、写字行下限的约 2/3 处。

### 诚实失败

数据行太少（中位数模板不成立）、或者**过半行都判成有墨**（模板被污染）时，返回
`determined=False`，调用方退回批次 1 的行为，而不是给一个猜出来的结论。
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

from PIL import Image

from src.documents.grid import PageGrid, RowBand

logger = logging.getLogger(__name__)

INK_DELTA = 45
"""比**模板**暗多少算墨。与 `grid.DARK_MARGIN`（比纸张底色暗多少）不是一个口径：
那一个是「比纸暗」，这一个是「比同一列其它格子的同一个像素暗」。"""

DARK_MARGIN_ABSOLUTE = 40
"""比纸张底色暗多少算暗点。**只用于查模板有没有被污染**，不用于判空。"""

INK_THRESHOLD = 0.040
"""判为「有墨」的得分下限。

2026-11-11 在人工核对子集上量出来的区间（生产参数，`WORK_CELL_WIDTH=480`）：

| | 最小 | 最大 |
| --- | --- | --- |
| 写了字的行 | **0.0513**（TangFa p01，淡铅笔复写纸） | 0.1418 |
| 真正空白的行 | 0.0001 | **0.0206** |
| 有墨但不是商品行（划线、签名） | 0.0256 | 0.0305 |

阈值卡在 0.040：离写字行下限 1.28 倍，离空行上限 1.94 倍。

⚠️ **留白只有 1.28 倍，而且只在 3 页上标定过。** 淡铅笔那一侧是窄的那一侧——
TangFa 的复写纸铅笔印最淡，而全量实跑证明这个留白**不够**：TangFa p14 第 3 行
纸上写了 `4 x30 / 8.99 / 1079.00`，得分只有 **0.0233**，会被判成空行。

**所以这个闸门不当行数上限用**（否则那一行真实采购就被悄悄删掉了），只作证据、
以及支撑「行↔商品行」的对应。阈值按**对应**这个用途定：漏判只会让对应拒绝建立
（判据自验），而误判成有墨才可能把对应整体错开一行——所以宁可高一点。"""

CELL_INSET = 0.15
"""上下左右各往里缩多少，然后才比较。

⚠️ **这一条是成败所在。** 不缩进时印刷格线会落在格子边缘，而相邻两格的线位置差
1—2 像素，缩放再相减就留下一圈残影：实测 TangFa p01 的空格子得分因此是
0.000—0.039，与写了字的 0.060 几乎挨上。缩进 15% 之后同一批空格子得分变成
**精确的 0.000**，写字格 0.060。手写压线的那部分会被切掉一点，但判「有没有墨」
不需要完整字形——人工审核时可以查看整页原件。"""

ROW_INSET = 0.15
"""行级闸门的上下缩进。横向只缩 2%：行级比较的是整条数据区，左右两端是表格外框，
位置比列线稳。"""

MIN_TEMPLATE_ROWS = 6
"""少于这么多数据行就不做中位数模板。中位数要「多数是空的」才站得住，
行数太少时一两行手写就能把它带偏。"""

WORK_CELL_WIDTH = 480
"""配准比较的工作宽度。原图一格 1260 像素宽，逐像素取中位数在 Python 里太慢；
实测缩到 480 宽后**分离度不变**（见模块文档的表），而一页快一个数量级。"""


class InkError(ValueError):
    """墨迹判定失败。调用方应退回「本页没有墨迹结论」，而不是改用猜测。"""


@dataclass(frozen=True)
class InkVerdict:
    """一个格子 / 一行的判定。得分一起带出来，于是事后能复核判断对不对。"""

    index: int
    score: float
    has_ink: bool

    def summary(self) -> dict[str, object]:
        return {"index": self.index, "score": round(self.score, 4), "has_ink": self.has_ink}


@dataclass(frozen=True)
class InkGate:
    """一页（或一列）的墨迹闸门结果。

    `determined=False` 时 `verdicts` 是空的，`reason` 说明为什么判不出来。
    """

    determined: bool
    reason: str = ""
    threshold: float = INK_THRESHOLD
    verdicts: tuple[InkVerdict, ...] = field(default_factory=tuple)

    @property
    def inked(self) -> tuple[int, ...]:
        """判为有墨的序号。"""
        return tuple(item.index for item in self.verdicts if item.has_ink)

    @property
    def blank(self) -> tuple[int, ...]:
        return tuple(item.index for item in self.verdicts if not item.has_ink)

    def verdict(self, index: int) -> InkVerdict | None:
        for item in self.verdicts:
            if item.index == index:
                return item
        return None

    def summary(self) -> dict[str, object]:
        return {
            "determined": self.determined,
            "reason": self.reason,
            "threshold": self.threshold,
            "inked": list(self.inked),
            "scores": [item.summary() for item in self.verdicts],
        }


def _deskewed(image: Image.Image, grid: PageGrid) -> Image.Image:
    if not grid.skew_degrees:
        return image
    return image.rotate(grid.skew_degrees, resample=Image.BICUBIC, fillcolor=255)


def _crop_ratio(
    source: Image.Image, left: float, right: float, top: float, bottom: float
) -> Image.Image:
    width, height = source.size
    box = (
        max(0, int(round(left * width))),
        max(0, int(round(top * height))),
        min(width, int(round(right * width))),
        min(height, int(round(bottom * height))),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        raise InkError(f"判定区域落在图外或宽高为 0：{box}。")
    return source.crop(box).convert("L")


CONTAMINATION_MARGIN = 0.010
"""模板比**最干净的那个格子**多出这么多暗点，就判定模板被写上的字污染了。

⚠️ **这一条堵的是中位数模板的翻转失效。** 过半格子写了字时，逐像素中位数落在
**写了字**的那一侧：于是写了字的行与模板一致、得分接近 0，反而是空行与模板差异大、
得分高——结论整个反过来，而且「有墨的格子超过一半」这个计数守卫**根本不会触发**
（被判有墨的是那几个空行，是少数）。

判据：空格子模板应当和**全页最干净的那个格子**几乎一样暗（都只剩印刷格线）。
两者差出 1% 的像素，就说明模板里混进了手写。"""


def _dark_ratio(frame: bytes, cutoff: int) -> float:
    return sum(1 for value in frame if value < cutoff) / len(frame)


def _scores(cells: list[Image.Image]) -> tuple[list[float], str | None]:
    """逐像素中位数模板 + 相减。

    返回（每个格子的墨迹得分, 模板被污染时的说明）。得分是「比模板暗」的像素占比。
    """
    width = min(WORK_CELL_WIDTH, max(cell.width for cell in cells))
    height = max(1, int(statistics.median(cell.height for cell in cells)))
    if width >= WORK_CELL_WIDTH:
        height = max(1, round(height * WORK_CELL_WIDTH / max(cell.width for cell in cells)))
    if width < 6 or height < 6:
        raise InkError(f"格子缩放后只有 {width}×{height} 像素，判不出墨迹。")

    frames = [cell.resize((width, height), Image.BILINEAR).tobytes() for cell in cells]
    count = len(frames)
    middle = count // 2
    area = width * height
    template = bytes(
        sorted(frame[offset] for frame in frames)[middle] for offset in range(area)
    )

    # 绝对暗点率只用来**查模板有没有被污染**，不用来判单个格子空不空——
    # 步骤 07 已实测绝对密度分不开空行与写字行（三种基准全部重叠）。
    sample = sorted(template[::7])
    background = sample[len(sample) // 2]
    cutoff = max(1, background - DARK_MARGIN_ABSOLUTE)
    cleanest = min(_dark_ratio(frame, cutoff) for frame in frames)
    template_dark = _dark_ratio(template, cutoff)
    note = None
    if template_dark > cleanest + CONTAMINATION_MARGIN:
        note = (
            f"空格子模板本身的暗点率 {template_dark:.4f} 比最干净的那个格子"
            f"（{cleanest:.4f}）高出 {template_dark - cleanest:.4f}——**模板被写上的字"
            "污染了**。逐像素中位数要「多数格子是空的」才成立；过半写满时结论会整个"
            "反过来（写了字的反而得分低）。这种页诚实返回未判定，不给结论。"
        )

    cutoffs = bytes(max(0, value - INK_DELTA) for value in template)
    scores = [
        sum(1 for offset in range(area) if frame[offset] < cutoffs[offset]) / area
        for frame in frames
    ]
    return scores, note


def _gate(cells: list[Image.Image], indexes: list[int], *, what: str) -> InkGate:
    if len(cells) < MIN_TEMPLATE_ROWS:
        return InkGate(
            determined=False,
            reason=(
                f"{what}只有 {len(cells)} 个格子（需要 {MIN_TEMPLATE_ROWS} 个）："
                "中位数模板要「多数是空的」才站得住，格子太少时一两行手写就能把它带偏。"
            ),
        )
    try:
        scores, contaminated = _scores(cells)
    except InkError as error:
        return InkGate(determined=False, reason=str(error))
    if contaminated is not None:
        return InkGate(determined=False, reason=f"{what}：{contaminated}")

    verdicts = tuple(
        InkVerdict(index=index, score=score, has_ink=score >= INK_THRESHOLD)
        for index, score in zip(indexes, scores)
    )
    inked = sum(1 for item in verdicts if item.has_ink)
    if inked > len(verdicts) // 2:
        return InkGate(
            determined=False,
            reason=(
                f"{what}有 {inked}/{len(verdicts)} 个格子判成有墨，**过半**——"
                "空格子模板取的是中位数，写满的页会让模板本身带上墨，结论就反过来了。"
                "这种页诚实返回未判定，不给结论。"
            ),
        )
    gate = InkGate(determined=True, verdicts=verdicts)
    logger.info("ink gate %s %s", what, gate.summary())
    return gate


def gate_rows(
    image: Image.Image, grid: PageGrid, data_bands: tuple[RowBand, ...]
) -> InkGate:
    """行级闸门：这几条**数据行带**里，哪几行写了东西。

    判定区是「行号列右边 → 表格右边界」：行号列本身印着**逐行不同**的数字，
    把它放进中位数模板只会让每一行都显得有墨（实测 TangFa p01 的行号列得分
    0.040—0.109，没有任何分离度）。它是锚定用的，不是判墨用的。

    这是**我们自己算出来的事实**，不是模型自报的状态——第 1 层
    `STRUCTURE_PROMPT` 问出来的 `written_rows` 只是旁证，两者取严。
    """
    if not grid.determined:
        raise InkError("格线未判定，没有行带可判。")
    if not data_bands:
        raise InkError("没有数据行带（行号锚定尚未建立）。")
    source = _deskewed(image, grid)
    _, strip_right = grid.row_number_strip
    span = grid.table_right - strip_right
    if span <= 0:
        raise InkError(
            f"行号列右边没有剩下可判的宽度（表格 [{grid.table_left:.3f}, "
            f"{grid.table_right:.3f}]）。"
        )
    margin = span * 0.02
    cells = []
    for band in data_bands:
        inset = band.height * ROW_INSET
        cells.append(
            _crop_ratio(
                source,
                strip_right + margin,
                grid.table_right - margin,
                band.top + inset,
                band.bottom - inset,
            )
        )
    return _gate(cells, [band.index for band in data_bands], what="行级")


def gate_column(
    image: Image.Image,
    grid: PageGrid,
    data_bands: tuple[RowBand, ...],
    column_index: int,
) -> InkGate:
    """逐格闸门：某一列里，哪几格写了东西。

    用法是**丢弃**：这一格判成空的，模型在它上面报出来的任何值一律不进草稿。
    这是整条链路上唯一不依赖模型诚实度的硬闸门。
    """
    if not grid.determined:
        raise InkError("格线未判定，没有格子可判。")
    column = next((item for item in grid.columns if item.index == column_index), None)
    if column is None:
        raise InkError(f"这一页没有第 {column_index} 条列带。")
    source = _deskewed(image, grid)
    dx = (column.right - column.left) * CELL_INSET
    cells = []
    for band in data_bands:
        dy = band.height * CELL_INSET
        cells.append(
            _crop_ratio(
                source,
                column.left + dx,
                column.right - dx,
                band.top + dy,
                band.bottom - dy,
            )
        )
    return _gate(cells, [band.index for band in data_bands], what=f"第 {column_index} 列")


__all__ = [
    "CELL_INSET",
    "CONTAMINATION_MARGIN",
    "DARK_MARGIN_ABSOLUTE",
    "INK_DELTA",
    "INK_THRESHOLD",
    "InkError",
    "InkGate",
    "InkVerdict",
    "MIN_TEMPLATE_ROWS",
    "WORK_CELL_WIDTH",
    "gate_column",
    "gate_rows",
]
