"""印刷 / 手写的本地判别：新供应商首次出现时的兜底（步骤 08.3f）。

### 它在分流里的位置

分流以**按供应商路由**为主（供应商是固定的，这家一直寄印刷体 PDF、那家一直是
手写复写纸）。本模块只负责**一家供应商第一次出现、还没有路由记录**的那一次，
而且判不准时**倒向保守那条路**（当手写件处理：不预填）。

⚠️ 它**不看识别出来的内容**，只看像素。步骤 08 已明确「分流依据必须与识别出的
内容无关」，而且「禁止用各数字算得平当分流开关」——步骤 07 的
`8 × 117 = 936` 证明算术自洽的编造完全存在。

### 判据是量出来的，不是想出来的

原计划写的是「笔画粗细是否均匀」。第一次实现按 900px 工作宽度量，**三个特征全部
重叠**（墨迹占比、对比度、笔画中位宽），因为在那个分辨率下笔画只有 2—3 像素，
等于量到了分辨率下限：印刷件 contrast 119.6 与手写件 122.5 分不开。

改到**原图分辨率**（2479px）之后，笔画有 5—6 像素，分布就量得出来了。
2026-11-11 在步骤 07 的全部真实样本上实测（74 页扫描单页 + 4 张零售小票）：

| 分组 | 张数 | `iqr/med` 区间 | `cv` 区间 |
| --- | --- | --- | --- |
| 印刷体供应商发票 | 6 | 0.00 — **0.20** | 0.54 — **0.66** |
| 中文手写复写纸 | 68 | **0.20** — 1.00 | **0.68** — 1.12 |
| 零售小票 | 4 | 0.50 — 2.00 | 0.82 — 1.83 |

两条结论：

1. **`iqr/med` 单独用不行**——手写件最小值 0.20 与印刷件最大值 0.20 重合
   （ChangShun p16）。那一页的 `cv` 是 0.78，远在印刷件之上。
2. **两个特征合起来在这批样本上完全分开**：印刷 6/6、手写 0/68、小票 0/4。

⚠️ **但 `cv` 的间隔只有 0.66 对 0.68（3%）**，而且印刷件样本只有 **6 张、
2 家供应商**，样本里**没有清晰的英文印刷体发票**（步骤 07 已记录这个缺口）。
所以：

- 阈值卡在 `cv ≤ 0.67`，严格落在手写件最小值之下；
- 两个特征之间的中间带判为 `UNKNOWN`，**走保守那条路**；
- 这个判别器**只是兜底**，真正可靠的是供应商路由表。

### 小票被判成「非印刷」是对的，不是误判

四张零售小票的 `cv` 都在 0.82 以上，于是会走手写件那条路（不预填）。这看起来像
误判，其实**正合实测**：步骤 07 的同一个模型在印刷发票上字段准确率 96.6%，
在小票上只有 32.1%。小票上不预填是对的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from PIL import Image

logger = logging.getLogger(__name__)

# 判为印刷体的上限。两条都要满足。
PRINTED_MAX_IQR_RATIO = 0.25
PRINTED_MAX_CV = 0.67

# 判为「非均匀笔画」（手写、小票）的下限。任一满足即可。
NON_UNIFORM_MIN_IQR_RATIO = 0.40
NON_UNIFORM_MIN_CV = 0.70

DARK_MARGIN = 40
"""比背景亮度中位数暗多少算墨。与 documents.grid 同一个口径。"""

ROW_STRIDE = 5
"""每隔几行取一行做笔画测量。原图 3230 行取约 430 行，够稳也够快。"""

MAX_STROKE_RUN = 40
"""超过这么宽的连续暗像素不算笔画——那是印刷表格线、涂黑块或阴影。
⚠️ 不排掉它们的话，预印表格的横线会把两类单据的分布一起拉平。"""

MIN_STROKE_SAMPLES = 200
"""笔画样本少于这么多就判不出来（近乎空白页）。诚实返回 UNKNOWN。"""

CONTENT_BAND = (1 / 6, 5 / 6)
"""只量页面中间这一段。页眉的大号标题与页脚的细字号都会歪曲笔画宽度分布。"""


class InkStyle(StrEnum):
    """笔画风格。"""

    PRINTED = "printed"
    """笔画宽度均匀：印刷体。"""

    NON_UNIFORM = "non_uniform"
    """笔画宽度不均匀：手写，或热敏小票那种糊掉的印刷。"""

    UNKNOWN = "unknown"
    """落在中间带，或样本不足。**按保守那条路处理。**"""


@dataclass(frozen=True)
class InkProfile:
    """一页的笔画测量结果。数字一起带出来，于是事后能复核判断对不对。"""

    style: InkStyle
    stroke_samples: int = 0
    iqr_ratio: float | None = None
    cv: float | None = None
    median_width: int | None = None
    reason: str = ""

    @property
    def is_printed(self) -> bool:
        return self.style is InkStyle.PRINTED

    def summary(self) -> dict[str, object]:
        return {
            "style": self.style.value,
            "stroke_samples": self.stroke_samples,
            "iqr_ratio": None if self.iqr_ratio is None else round(self.iqr_ratio, 3),
            "cv": None if self.cv is None else round(self.cv, 3),
            "median_width": self.median_width,
            "reason": self.reason,
        }


def profile_ink(image: Image.Image) -> InkProfile:
    """量一页的笔画宽度分布，给出印刷 / 非均匀 / 判不出。

    ⚠️ 必须传**原图分辨率**的图像。缩过的图量不出来——这是实测踩过的坑，
    900px 下三个特征全部重叠。
    """
    runs = _stroke_runs(image)
    if len(runs) < MIN_STROKE_SAMPLES:
        return InkProfile(
            style=InkStyle.UNKNOWN,
            stroke_samples=len(runs),
            reason=(
                f"笔画样本只有 {len(runs)} 个（需要 {MIN_STROKE_SAMPLES} 个）："
                "这一页近乎空白，或者整页都是大块色块。按保守路径处理。"
            ),
        )

    runs.sort()
    count = len(runs)
    p25 = runs[count // 4]
    p50 = runs[count // 2]
    p75 = runs[3 * count // 4]
    mean = sum(runs) / count
    variance = sum((value - mean) ** 2 for value in runs) / count
    cv = (variance**0.5) / mean if mean else 0.0
    iqr_ratio = (p75 - p25) / max(1, p50)

    if iqr_ratio <= PRINTED_MAX_IQR_RATIO and cv <= PRINTED_MAX_CV:
        style = InkStyle.PRINTED
        reason = (
            f"笔画宽度均匀（iqr/med={iqr_ratio:.2f} ≤ {PRINTED_MAX_IQR_RATIO}、"
            f"cv={cv:.2f} ≤ {PRINTED_MAX_CV}）：按印刷体处理。"
        )
    elif iqr_ratio >= NON_UNIFORM_MIN_IQR_RATIO or cv >= NON_UNIFORM_MIN_CV:
        style = InkStyle.NON_UNIFORM
        reason = (
            f"笔画宽度不均匀（iqr/med={iqr_ratio:.2f}、cv={cv:.2f}）："
            "按手写件处理（所有字段不预填）。"
        )
    else:
        style = InkStyle.UNKNOWN
        reason = (
            f"笔画指标落在中间带（iqr/med={iqr_ratio:.2f}、cv={cv:.2f}）——"
            f"印刷件要求 ≤{PRINTED_MAX_IQR_RATIO}/≤{PRINTED_MAX_CV}，"
            f"手写件要求 ≥{NON_UNIFORM_MIN_IQR_RATIO} 或 ≥{NON_UNIFORM_MIN_CV}。"
            "判不出来就按保守路径处理，不赌。"
        )

    profile = InkProfile(
        style=style,
        stroke_samples=count,
        iqr_ratio=iqr_ratio,
        cv=cv,
        median_width=p50,
        reason=reason,
    )
    logger.info("ink profiled %s", profile.summary())
    return profile


def _stroke_runs(image: Image.Image) -> list[int]:
    """逐行量连续暗像素的长度，即笔画的水平截面宽度。"""
    try:
        grey = image.convert("L")
    except (OSError, ValueError) as error:
        logger.warning("ink profiling failed to convert image: %s", error)
        return []
    pixels = grey.tobytes()
    width, height = grey.size
    if width == 0 or height == 0:
        return []

    # 背景亮度取中位数（复写纸底色偏黄，不能假设为白）。抽样算，不扫全图。
    sample = pixels[::7]
    background = sorted(sample)[len(sample) // 2]
    cutoff = background - DARK_MARGIN
    if cutoff <= 0:
        return []

    start = int(height * CONTENT_BAND[0])
    stop = int(height * CONTENT_BAND[1])
    runs: list[int] = []
    for y in range(start, stop, ROW_STRIDE):
        row = pixels[y * width : (y + 1) * width]
        run = 0
        for value in row:
            if value < cutoff:
                run += 1
            elif run:
                if 1 <= run <= MAX_STROKE_RUN:
                    runs.append(run)
                run = 0
        if 1 <= run <= MAX_STROKE_RUN:
            runs.append(run)
    return runs


__all__ = [
    "CONTENT_BAND",
    "DARK_MARGIN",
    "InkProfile",
    "InkStyle",
    "MAX_STROKE_RUN",
    "MIN_STROKE_SAMPLES",
    "NON_UNIFORM_MIN_CV",
    "NON_UNIFORM_MIN_IQR_RATIO",
    "PRINTED_MAX_CV",
    "PRINTED_MAX_IQR_RATIO",
    "profile_ink",
]
