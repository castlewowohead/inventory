"""表格行结构检测：数清一张预印单据**有多少个数据行格子**。本地计算，不调模型。

**本工具实际做到了什么，先说清楚**（2026-11-10 实测，步骤 07.8a）：

| 能力 | 状态 |
| --- | --- |
| 去斜 + 找出印刷横线 + 切出行带 | ✅ 可靠，74 页全部成功 |
| 每张表有几个数据行格子（行容量） | ✅ 可靠 |
| 逐行判定「这一行写了字 / 是空的」 | ❌ **没做到**，原因见下 |

所以它为步骤 07.8a 提供的是**行容量**这一个客观事实——模型报出的行数超过表格的
物理行数，就是确证的编造；而「哪几行写了字」这个更细的判断，本轮由人工核对子集
给出（见 `tools/eval_ocr.py` 的 `VERIFIED_WRITTEN_ROWS`），不由本工具冒充。

逐行空/非空判定失败的原因与数据留在下面「三个坑」里，步骤 08 的 08.3a 要接着做
（那条清单项本来就把「墨迹判定的像素阈值」列为待定），结论是：**只靠阈值不够，
必须先做图像配准**。

### 为什么需要它

步骤 07 实测到的最危险失败模式是「自洽的编造」：模型在空格子里报出一个值，而且
这个值与其它数字算得平（`8 × 117 = 936`）。步骤 04 的 `_check_amount()` 对它完全
无效——它校验的恰恰是模型保证会做对的那部分。模型自报的置信度也靠不住：提示词
要求「认不出填 UNREADABLE」它不照做，接口接受 `logprobs` 却不返回。

能客观判对错的只剩一件事：**这个格子里到底有没有墨**。不需要知道正确答案是什么，
只需要知道「这里什么都没有」。墨迹判空 + 模型在该处报出了值 = **可证伪的编造**。

### 为什么按「行」而不按「格」

文档原本设想按格子判定。但五家供应商版式各不相同，手写还经常跨格、斜着写，按格子
判要给每家硬编一套坐标，拆错一格会静默错位。按行判不需要任何 per-供应商配置：
表格横线自己就把行分好了，而「这张单上有 N 行写了东西」已经足够支撑步骤 07 要的
那个决策——模型输出 M 行而纸上只写了 N 行时，多出来的就是确证的编造。

格子级判定留给步骤 08：那时有选定的模型和真实的审核界面，为裁剪原图做模板才划算，
而且那时错了人眼当场能发现。

### 实际踩到的三个坑（阈值就是这么定出来的）

1. **白边稀释**：扫描件四周有大片白边，表格只占图宽约 69%。按整幅图宽算横线覆盖率，
   印刷横线会被稀释到 0.43—0.58，一条都找不到。→ 一切比例按**内容区宽度**算。

2. **倾斜**：扫描有 1°—2° 的旋转，倾斜的横线不会落在同一行像素上。不去斜时，
   74 张里有 37 张**一条横线都找不到**（TangFa 12/18、ChangShun 15/32、FengYuan 10/18）。
   去斜后同样这些页能找到 26—42 条。→ 必须先在 ±2° 内搜一遍。

3. **逐像素比对的底噪压不下去——这一条没能解决，是本工具的能力边界。**
   试过三种基准，都分不开空行与写了字的行：

   | 基准 | 空行带密度 | 写了字的行带 | 能否分开 |
   | --- | --- | --- | --- |
   | 固定阈值数暗点 | 4.1%—9.3% | 7.5% | ❌ 完全重叠 |
   | 页内行带中位数 | 0.6%—3.0% | 2.7% | ❌ 重叠 |
   | 跨页同序号行带中位数 | 1.9%—4.4% | 4.1% | ❌ 重叠 |

   两个原因叠在一起：① 复写纸是淡铅笔/淡蓝墨，笔画本身的墨量就接近纸张纹理噪声；
   ② 印刷线只要错开 1 个像素，两张图在那条线上就产生大片差异，而**不同页之间
   检测到的行带数并不相同**（Tang Fa 18 页里有 21 / 24 / 25 三种），序号对不上，
   跨页中位数因此没有真正对齐。

   结论：**阈值法不够，要做图像配准**（按格线做仿射对齐，而不是按行带序号凑）。
   这属于步骤 08 的 08.3a，那里本来就把像素阈值列为待定；本步骤把失败数据留下，
   免得步骤 08 再试一遍同样的三条路。

### 诚实失败

找不到足够多的横线（版式不是预印表格、或去斜后仍然对不齐）时，本工具返回
`determined=False` 并说明原因，**不返回一个猜出来的行数**。与 `split_scanned_pdf.py`
同一条原则：拆错页 / 判错行都不会报错，是最难发现的那种错。

用法：
    python tools/ink_rows.py samples/real/pages --out samples/real/ink/rows.json
    python tools/ink_rows.py <单张图> --debug
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

WORK_WIDTH = 640
"""统计用的工作宽度。墨迹判定只看面积占比，不需要原始 2479px；缩图让 74 张的
整批处理从几分钟降到十几秒，也顺带平滑掉一部分扫描噪点。"""

DARK_MARGIN = 40
"""比背景亮度中位数暗多少算「暗点」。背景取中位数而不是假设为白——复写纸底色偏黄。"""

SKEW_RANGE = 2.0
SKEW_STEP = 0.25
"""去斜搜索范围与步长（度）。实测这批扫描件的倾斜在 ±2° 内，1.75°、2.00°、
−1.50°、1.00° 都出现过。"""

RULE_COVERAGE = 0.55
"""一行像素里暗点占**内容区**宽度多少算「印刷横线」。实测印刷横线为 0.62—0.84
（被竖线间空白打断，到不了 1.0），手写整行写满也难以覆盖 55%。"""

MIN_RULES = 8
"""少于这么多条横线就判定「这不是预印表格」，返回未判定而不是硬算。"""

MIN_BAND_HEIGHT = 14
MAX_BAND_HEIGHT = 80
"""行带高度的合理范围（工作宽度 640 下）。过矮是两条挨着的印刷线，过高是漏检了
中间的横线，两者都不当作数据行。"""

BAND_CANVAS = (640, 40)
"""每个行带归一到这个尺寸再做逐像素比对，消掉行高的细微差别。"""

LABEL_STRIP = 0.04
"""最左侧这个比例是预印行号栏。跨页中位数本来就能消掉行号，这里再切掉一点，
是为了让「行号写歪了」之类的差异不进统计。"""

INK_DELTA = 45
"""某像素比同序号行带的跨页中位数暗这么多，才算「这里多了墨」。"""

INK_THRESHOLD = 0.012
"""行带内多出来的墨占比超过这个值就标记 `has_ink`。

⚠️ **这个标记不可信，不要拿它当「这一行写了字」的判据。** 实测空行带与写了字的
行带在任何阈值下都重叠（模块文档「三个坑」第 3 条有三组数据）。保留这个字段与
`ink_ratio` 是作为步骤 08 继续调参的原始数据，不是本步骤的结论。

本工具对步骤 07 的有效产出是 `band_count`（行容量），那个是可靠的。"""

MIN_PAGES_FOR_TEMPLATE = 6
"""建立跨页基准至少需要这么多页。不足时退回页内中位数，并在结果里标明基准来源
——两种基准的可信度不一样，不能混在一起解读。"""


@dataclass
class RowBand:
    index: int
    ink_ratio: float
    has_ink: bool


@dataclass
class PageInk:
    image: str
    determined: bool
    reason: str = ""
    skew_degrees: float = 0.0
    rule_count: int = 0
    baseline: str = ""
    bands: list[RowBand] = field(default_factory=list)

    @property
    def band_count(self) -> int:
        """表格的行容量：切出来的数据行格子数。**这是本工具可靠的那个产出。**"""
        return len(self.bands)

    @property
    def flagged_rows(self) -> list[int]:
        """`has_ink` 为真的行带。⚠️ 不可信，见 INK_THRESHOLD 的说明。"""
        return [b.index for b in self.bands if b.has_ink]


def _grey(path: Path) -> Image.Image:
    with Image.open(path) as source:
        grey = source.convert("L")
        return grey.resize(
            (WORK_WIDTH, max(1, int(grey.height * WORK_WIDTH / grey.width))),
            Image.BILINEAR,
        )


def _binary(grey: Image.Image) -> Image.Image:
    pixels = grey.tobytes()  # mode "L"：一字节一像素
    background = sorted(pixels)[len(pixels) // 2]
    cutoff = background - DARK_MARGIN
    return grey.point(lambda v, c=cutoff: 255 if v < c else 0)


def _content_span(binary: Image.Image) -> tuple[int, int] | None:
    width, height = binary.size
    columns = binary.transpose(Image.Transpose.ROTATE_90).tobytes()
    floor = height * 0.02
    inked = [
        x for x in range(width)
        if columns.count(255, x * height, (x + 1) * height) > floor
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


def _deskew(grey: Image.Image) -> tuple[Image.Image, float, int]:
    """在 ±SKEW_RANGE 内找让强横线最多的角度。返回 (转正的图, 角度, 横线行数)。"""
    best = (0, 0.0, grey)
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


def _merge_adjacent(values: list[int], gap: int = 4) -> list[tuple[int, int]]:
    """把挨着的横线行合并成一条线（印刷线有 2—4 像素厚）。"""
    if not values:
        return []
    groups: list[list[int]] = [[values[0], values[0]]]
    for v in values[1:]:
        if v - groups[-1][1] <= gap:
            groups[-1][1] = v
        else:
            groups.append([v, v])
    return [(a, b) for a, b in groups]


def extract_bands(path: Path) -> tuple[PageInk, list[bytes]]:
    """切出行带并归一化。返回 (页结果骨架, 各行带的像素字节)。"""
    grey = _grey(path)
    straight, angle, rule_count = _deskew(grey)
    if rule_count < MIN_RULES:
        return (
            PageInk(
                image=path.name,
                determined=False,
                reason=f"去斜后仍只找到 {rule_count} 条横线（需要 {MIN_RULES} 条），"
                "该页可能不是预印表格，或版式不符合预期",
                skew_degrees=angle,
                rule_count=rule_count,
            ),
            [],
        )

    binary = _binary(straight)
    span = _content_span(binary)
    assert span is not None  # rule_count > 0 已保证有内容
    left, right = span
    rules = _merge_adjacent(_rule_rows(binary, left, right))

    crops: list[bytes] = []
    for (_, prev_bottom), (next_top, _) in zip(rules, rules[1:]):
        top, bottom = prev_bottom + 1, next_top - 1
        height = bottom - top + 1
        if height < MIN_BAND_HEIGHT or height > MAX_BAND_HEIGHT:
            continue
        crops.append(
            straight.crop((left, top, right + 1, bottom + 1))
            .resize(BAND_CANVAS, Image.BILINEAR)
            .tobytes()
        )

    page = PageInk(
        image=path.name,
        determined=True,
        skew_degrees=angle,
        rule_count=len(rules),
    )
    return page, crops


def _median_bytes(stack: list[bytes]) -> bytes:
    size = len(stack[0])
    middle = len(stack) // 2
    return bytes(sorted(s[i] for s in stack)[middle] for i in range(size))


def _excess_ink(band: bytes, baseline: bytes) -> float:
    width, height = BAND_CANVAS
    label_cut = int(width * LABEL_STRIP)
    counted = height * (width - label_cut)
    extra = 0
    for y in range(height):
        row = y * width
        for x in range(label_cut, width):
            if baseline[row + x] - band[row + x] > INK_DELTA:
                extra += 1
    return extra / counted


def supplier_of(name: str) -> str:
    """文件名形如 `05_TangFa_Wholesale_18p__p07.jpg`，供应商是 `__p` 之前那段。"""
    return re.split(r"__p\d+", name)[0]


def analyse_group(paths: list[Path]) -> list[PageInk]:
    """对同一供应商的一组页做墨迹判定。

    基准优先用**跨页同序号行带的中位数**：行号、格线与噪声特性都对得上，只剩手写
    是多出来的。页数不足时退回页内中位数，并在结果里标明——两种基准可信度不同。
    """
    extracted = [extract_bands(p) for p in paths]
    usable = [(page, crops) for page, crops in extracted if page.determined and crops]

    band_counts = [len(crops) for _, crops in usable]
    common = min(band_counts) if band_counts else 0

    cross_page: list[bytes] = []
    if len(usable) >= MIN_PAGES_FOR_TEMPLATE and common >= MIN_RULES:
        for index in range(common):
            cross_page.append(_median_bytes([crops[index] for _, crops in usable]))

    results: list[PageInk] = []
    for page, crops in extracted:
        if not page.determined or not crops:
            results.append(page)
            continue
        # 页内中位数始终算出来：跨页基准只覆盖前 common 个行带位置，
        # 超出的那些（这一页比别页多切出来的行带）仍需要一个基准。
        within = _median_bytes(crops)
        if cross_page:
            page.baseline = f"跨页同序号行带中位数（{len(usable)} 页）"
        else:
            page.baseline = "页内行带中位数（同供应商页数不足，可信度较低）"
        for index, band in enumerate(crops, 1):
            baseline = (
                cross_page[index - 1]
                if cross_page and index <= len(cross_page)
                else within
            )
            ratio = _excess_ink(band, baseline)
            page.bands.append(
                RowBand(
                    index=index,
                    ink_ratio=round(ratio, 5),
                    has_ink=ratio > INK_THRESHOLD,
                )
            )
        results.append(page)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数一张预印表格上有几行写了东西")
    parser.add_argument("target", type=Path, help="单张图，或含图的目录")
    parser.add_argument("--out", type=Path, help="结果 JSON 输出路径")
    parser.add_argument("--debug", action="store_true", help="打印每个行带的密度")
    args = parser.parse_args(argv)

    if args.target.is_dir():
        images = sorted(
            p for p in args.target.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
    else:
        images = [args.target]

    groups: dict[str, list[Path]] = defaultdict(list)
    for path in images:
        groups[supplier_of(path.name)].append(path)

    results: list[PageInk] = []
    for supplier, paths in groups.items():
        print(f"\n{supplier}（{len(paths)} 页）")
        for page in analyse_group(paths):
            results.append(page)
            if not page.determined:
                print(f"  {page.image}: 未判定 — {page.reason}")
                continue
            print(
                f"  {page.image}: 去斜 {page.skew_degrees:+.2f}°，"
                f"行容量 {page.band_count}"
            )
            if args.debug:
                for band in page.bands:
                    flag = "标记" if band.has_ink else "    "
                    print(f"      行带 {band.index:>3}  {flag}  {band.ink_ratio:.5f}")

    decided = sum(1 for r in results if r.determined)
    print(f"\n共 {len(results)} 页，判定成功 {decided}，未判定 {len(results) - decided}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "thresholds": {
                        "work_width": WORK_WIDTH,
                        "dark_margin": DARK_MARGIN,
                        "skew_range": SKEW_RANGE,
                        "rule_coverage": RULE_COVERAGE,
                        "ink_delta": INK_DELTA,
                        "ink_threshold": INK_THRESHOLD,
                    },
                    "pages": [asdict(page) for page in results],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
