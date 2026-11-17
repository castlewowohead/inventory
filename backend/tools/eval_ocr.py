"""步骤 07.8 / 07.8a 的评测脚本：给候选 OCR 模型打分。

分成完全不同的两半，**结论不能互相外推**：

### A. 有 Ground Truth 的（印刷体供应商发票 6 张 + 小票 4 张）

逐字段比对，口径按 `imp_notes.md` 4.9。每个字段判成六种之一：

| 判定 | 含义 |
| --- | --- |
| `correct` | GT 有值，模型给了同一个值 |
| `wrong` | GT 有值，模型给了别的值 |
| `missed` | GT 有值，模型给了 null / UNREADABLE（漏，但没造假） |
| `fabricated` | **GT 说纸上没有，模型却报出了一个值** |
| `fabricated_unreadable` | **GT 说人都认不出，模型却报出了一个值** |
| `correct_null` | GT 没有 / 认不出，模型也报了 null，正确 |

最后两类之外，**`fabricated*` 就是 `imp_notes.md` 4.9 的 False Acceptance**，
也是本轮最危险的失败模式——它不会报错、不会被察觉，还常常算术自洽。

### B. 没有 Ground Truth 的（68 张中文手写复写纸）

手写件拿不到 GT（连母语者都认不出这些连笔速记），所以**不排名次**，只出三个
不需要任何人读懂手写的客观指标：

1. **虚报率**：模型报的行数超过纸面实际写了字的行数 → 多出来的是确证编造。
   判据有两级：
   - **行容量**（`ink_rows.py` 给出，74 页全自动）：报的行数超过表格的物理行格数，
     无可辩驳。这是下界里的下界。
   - **人工核对子集**（`VERIFIED_WRITTEN_ROWS`）：逐张看图数出真正写了字的行数。
     数「哪几行有墨」不需要读懂字，所以这件事人能做、而且能做准。
2. **跨模型分歧率**：三个模型给三个答案时，至少两个是错的——错误率的**下界**。
3. **算术不自洽率**：|数量 × 单价 − 行金额| > $0.05。同样是下界，
   **自洽的编造抓不到**（`8 × 117 = 936` 即是）。

**数字与中文商品名分开计分**（stru_dev.md 步骤 07 的切分口径）：手写数字有算术关系
和单据总额两道交叉验证，手写中文商品名没有任何可验证来源，两类的结论完全不同。

用法：
    python tools/eval_ocr.py --out samples/real/eval/report.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import gt_schema
from gt_schema import (
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_TOTAL,
    FIELD_ITEM_DESCRIPTION,
    FIELD_LINE_AMOUNT,
    FIELD_QUANTITY,
    FIELD_UNIT,
    FIELD_UNIT_PRICE,
    NUMERIC_FIELDS,
    SCORED_DOCUMENT_FIELDS,
    SCORED_LINE_FIELDS,
    normalize_number,
    normalize_text,
)

BACKEND = Path(__file__).resolve().parent.parent
OCR_DIR = BACKEND / "samples" / "real" / "ocr"
GT_DIR = BACKEND / "samples" / "real" / "ground_truth"
INK_FILE = BACKEND / "samples" / "real" / "ink" / "rows.json"

AMOUNT_TOLERANCE = Decimal("0.05")
"""与步骤 04 的 `AMOUNT_TOLERANCE` 同值。这里用于判「算术自洽」，
不是用于拦截输入——方向相反，但口径必须一致，否则两处对同一张单据的结论会打架。"""

TEXT_FIELDS = (FIELD_ITEM_DESCRIPTION, FIELD_UNIT, FIELD_INVOICE_DATE, FIELD_INVOICE_NUMBER)
"""按文本比较的字段。其余在 NUMERIC_FIELDS 里，按数值比较。"""

NULL_MARKERS = {"", "null", "none", "n/a", "na", "unreadable", "未识别", "无"}
"""模型用来表示「没有」的各种写法。提示词要求 null，但实测它经常返回空字符串
或 "UNREADABLE" 字面量，所以这里一并认。"""


# --------------------------------------------------------------------------
# 人工核对子集：逐张看图数出真正写了字的商品行数。
#
# **数「哪几行有墨」不需要读懂写的是什么。** 手写件拿不到逐字段 GT，但「这一行
# 是不是空的」人眼一看就知道，所以虚报率这一个指标是可以有真裁判的。
#
# 只收录**无歧义**的页：商品行与空行一眼可分。像 FengYuan p01 那种铅笔写了又擦、
# 擦了又写、还划掉一行的，人工也数不准，**不收**——宁可样本少，不要一个含糊的分母。
# --------------------------------------------------------------------------
VERIFIED_WRITTEN_ROWS: dict[str, dict[str, Any]] = {
    "05_TangFa_Wholesale_18p__p01.jpg": {
        "item_rows": 1,
        "row_capacity": 21,
        "note": "只有第 1 行写了（8 × 20，936.00）；第 2—21 行全空。"
        "總數列下方有一处跨行的大字潦草笔迹，是总额/签名，不是商品行。",
    },
    "05_TangFa_Wholesale_18p__p07.jpg": {
        "item_rows": 1,
        "row_capacity": 21,
        "note": "同上版式，只有第 1 行写了（8 × 20，936.00）；第 2—21 行全空。"
        "发票号 14265、日期 6/11/2026，与 p01（13975）是不同的两单。",
    },
    "01_ChangShun_Market_32p__p01.jpg": {
        "item_rows": 4,
        "row_capacity": 15,
        "note": "第 1—4 行写了商品；第 5—15 行空。第 6 行金额列有手写 142，"
        "是合计（24+10+40+68=142），不是商品行；另有一条斜划线表示后面不再有商品。",
    },
}


# --------------------------------------------------------------------------
# 模型输出解析
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_model_output(content: str) -> dict[str, Any] | None:
    """把模型返回的文本解析成结构。解析不出来就返回 None，**不猜**。"""
    text = (content or "").strip()
    if not text:
        return None
    match = _FENCE.search(text)
    if match:
        text = match.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def model_value(holder: Any, name: str) -> str | None:
    """取模型给的某个字段值；各种「没有」的写法一律归成 None。"""
    if not isinstance(holder, dict):
        return None
    raw = holder.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in NULL_MARKERS:
        return None
    return text


def model_lines(data: dict[str, Any]) -> list[dict[str, Any]]:
    lines = data.get("lines")
    return [line for line in lines if isinstance(line, dict)] if isinstance(lines, list) else []


# --------------------------------------------------------------------------
# 比对
# --------------------------------------------------------------------------

VERDICTS = (
    "correct",
    "wrong",
    "missed",
    "fabricated",
    "fabricated_unreadable",
    "correct_null",
)


def compare(gt_field: dict[str, Any] | None, given: str | None, name: str) -> str:
    """一个字段的判定。见模块文档的六种判定表。"""
    status = (gt_field or {}).get("status", "missing")

    if status == "recognized":
        expected = gt_field["raw_text"]
        if given is None:
            return "missed"
        if name in NUMERIC_FIELDS:
            want, _ = normalize_number(expected)
            got, _ = normalize_number(given)
            if want is None or got is None:
                return "wrong"
            return "correct" if want == got else "wrong"
        return "correct" if normalize_text(expected) == normalize_text(given) else "wrong"

    # GT 说这里没有值：模型也该说没有。报出任何值都是编造。
    if given is None:
        return "correct_null"
    return "fabricated" if status == "missing" else "fabricated_unreadable"


@dataclass
class FieldTally:
    counts: Counter = field(default_factory=Counter)

    def add(self, verdict: str) -> None:
        self.counts[verdict] += 1

    @property
    def scored(self) -> int:
        """GT 有值的那些（分母用于准确率）。"""
        return self.counts["correct"] + self.counts["wrong"] + self.counts["missed"]

    @property
    def accuracy(self) -> float | None:
        return self.counts["correct"] / self.scored if self.scored else None

    @property
    def fabricated(self) -> int:
        return self.counts["fabricated"] + self.counts["fabricated_unreadable"]

    @property
    def null_chances(self) -> int:
        return self.fabricated + self.counts["correct_null"]


def evaluate_with_gt(
    gts: dict[str, dict[str, Any]], outputs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """A 部分：对有 GT 的页做逐字段比对。

    ⚠️ **按 doc_class 分开统计，不要只看合并数字。** 实测发现把 6 张供应商发票和
    4 张零售小票混在一起会得出错误结论：`qwen3.5-ocr` 在小票上「多报 24 行」，
    看合并数字像是严重编造；但那 24 行全部来自小票的折扣行、礼品卡余额行、类别
    标题行——GT 把它们归进备注，模型把它们当成了商品行。**这是行切分口径的分歧，
    不是无中生有**；同一个模型在 6 张供应商发票上多报为 0。

    供应商发票才是本产品真正要处理的单据，小票是为覆盖 `imp_notes.md` 4.9 点名的
    版式类型补进来的。两者的结论不能互相外推。
    """
    per_field: dict[str, FieldTally] = defaultdict(FieldTally)
    row_stats = Counter()
    page_notes: list[dict[str, Any]] = []
    by_class: dict[str, dict[str, Any]] = {}
    for doc_class in sorted({gt["doc_class"] for gt in gts.values()}):
        subset = {i: g for i, g in gts.items() if g["doc_class"] == doc_class}
        if len(subset) != len(gts):  # 避免只有一类时无谓地再算一遍
            by_class[doc_class] = evaluate_with_gt(subset, outputs)

    for image, gt in sorted(gts.items()):
        result = outputs.get(image)
        if result is None:
            row_stats["无结果"] += 1
            continue
        data = parse_model_output(result.get("content", ""))
        if data is None:
            row_stats["输出无法解析"] += 1
            page_notes.append({"image": image, "problem": "输出不是可解析的 JSON"})
            continue

        for name in SCORED_DOCUMENT_FIELDS:
            verdict = compare(gt["document"].get(name), model_value(data, name), name)
            per_field[name].add(verdict)

        gt_lines = gt["lines"]
        got_lines = model_lines(data)
        row_stats["GT 行数"] += len(gt_lines)
        row_stats["模型行数"] += len(got_lines)
        if len(got_lines) > len(gt_lines):
            row_stats["多报行"] += len(got_lines) - len(gt_lines)
            page_notes.append(
                {
                    "image": image,
                    "problem": f"多报 {len(got_lines) - len(gt_lines)} 行"
                    f"（纸上 {len(gt_lines)} 行，模型给了 {len(got_lines)} 行）",
                }
            )
        elif len(got_lines) < len(gt_lines):
            row_stats["漏报行"] += len(gt_lines) - len(got_lines)
            page_notes.append(
                {
                    "image": image,
                    "problem": f"漏报 {len(gt_lines) - len(got_lines)} 行",
                }
            )
        else:
            row_stats["行数一致的页"] += 1

        for position, gt_line in enumerate(gt_lines):
            got = got_lines[position] if position < len(got_lines) else {}
            for name in SCORED_LINE_FIELDS:
                verdict = compare(gt_line.get(name), model_value(got, name), name)
                per_field[name].add(verdict)

    return {
        "fields": {
            name: {
                "accuracy": tally.accuracy,
                "scored": tally.scored,
                **dict(tally.counts),
            }
            for name, tally in sorted(per_field.items())
        },
        "rows": dict(row_stats),
        "problems": page_notes,
        "summary": summarise_gt(per_field, row_stats),
        "by_doc_class": by_class,
    }


def summarise_gt(
    per_field: dict[str, FieldTally], row_stats: Counter
) -> dict[str, Any]:
    """`imp_notes.md` 4.9 点名的几个总指标。"""
    total_scored = sum(t.scored for t in per_field.values())
    total_correct = sum(t.counts["correct"] for t in per_field.values())
    total_fabricated = sum(t.fabricated for t in per_field.values())
    total_null_chances = sum(t.null_chances for t in per_field.values())
    # 人工修正率：GT 有值但模型给错或漏，加上模型凭空多出来的——都要人动手。
    needs_fix = (
        sum(t.counts["wrong"] + t.counts["missed"] for t in per_field.values())
        + total_fabricated
    )
    denominator = total_scored + total_null_chances
    gt_rows = row_stats.get("GT 行数", 0)

    # FAR 要分开看：`unit` 一项就能主导整体数字。小票各行不印单位，GT 因此一律
    # MISSING，模型顺手补一个「件」就记一次编造——按提示词的规则这确实是编造，
    # 但它和「凭空报出一个单价」的危害完全不是一回事：前者人一眼看出来，
    # 后者会算得平、会被当成事实存进价格历史。**选型要看后者。**
    numeric_fabricated = sum(
        t.fabricated for name, t in per_field.items() if name in NUMERIC_FIELDS
    )
    numeric_chances = sum(
        t.null_chances for name, t in per_field.items() if name in NUMERIC_FIELDS
    )
    return {
        "字段准确率": total_correct / total_scored if total_scored else None,
        "False Acceptance Rate": (
            total_fabricated / total_null_chances if total_null_chances else None
        ),
        "FAR（仅数值字段）": (
            numeric_fabricated / numeric_chances if numeric_chances else None
        ),
        "数值字段编造次数": numeric_fabricated,
        "人工修正率": needs_fix / denominator if denominator else None,
        "行匹配准确率": (
            (gt_rows - row_stats.get("多报行", 0) - row_stats.get("漏报行", 0)) / gt_rows
            if gt_rows
            else None
        ),
    }


# --------------------------------------------------------------------------
# B 部分：无 GT 的客观指标
# --------------------------------------------------------------------------


def arithmetic_check(line: dict[str, Any]) -> bool | None:
    """这一行的 数量 × 单价 是否对得上 行金额。三项不全时返回 None（查不了）。"""
    qty = model_value(line, FIELD_QUANTITY)
    price = model_value(line, FIELD_UNIT_PRICE)
    amount = model_value(line, FIELD_LINE_AMOUNT)
    if not (qty and price and amount):
        return None
    q, _ = normalize_number(qty)
    p, _ = normalize_number(price)
    a, _ = normalize_number(amount)
    if q is None or p is None or a is None:
        return None
    return abs(q * p - a) <= AMOUNT_TOLERANCE


def evaluate_without_gt(
    images: list[str],
    outputs_by_model: dict[str, dict[str, dict[str, Any]]],
    capacities: dict[str, int],
) -> dict[str, Any]:
    models = sorted(outputs_by_model)
    parsed: dict[str, dict[str, dict[str, Any] | None]] = {
        model: {
            image: parse_model_output(
                (outputs_by_model[model].get(image) or {}).get("content", "")
            )
            for image in images
        }
        for model in models
    }

    # --- 1. 虚报率 ---
    over_capacity: list[dict[str, Any]] = []
    per_model_over = Counter()
    for image in images:
        capacity = capacities.get(image)
        if capacity is None:
            continue
        for model in models:
            data = parsed[model][image]
            if data is None:
                continue
            count = len(model_lines(data))
            if count > capacity:
                per_model_over[model] += 1
                over_capacity.append(
                    {"image": image, "model": model, "reported": count, "capacity": capacity}
                )

    verified: list[dict[str, Any]] = []
    per_model_verified_extra = Counter()
    per_model_verified_pages = Counter()
    for image, truth in VERIFIED_WRITTEN_ROWS.items():
        for model in models:
            data = parsed[model].get(image)
            if data is None:
                continue
            count = len(model_lines(data))
            extra = count - truth["item_rows"]
            per_model_verified_pages[model] += 1
            if extra > 0:
                per_model_verified_extra[model] += extra
            verified.append(
                {
                    "image": image,
                    "model": model,
                    "reported": count,
                    "actual": truth["item_rows"],
                    "fabricated_rows": max(0, extra),
                }
            )

    # --- 2. 跨模型分歧率 ---
    disagreement = {"数字": Counter(), "中文商品名": Counter()}
    disagreement_examples: list[dict[str, Any]] = []
    for image in images:
        datas = {m: parsed[m][image] for m in models if parsed[m][image] is not None}
        if len(datas) < 2:
            continue
        line_counts = {m: len(model_lines(d)) for m, d in datas.items()}
        if len(set(line_counts.values())) > 1:
            disagreement["数字"]["行数分歧的页"] += 1
        depth = min(line_counts.values())
        for position in range(depth):
            for name in SCORED_LINE_FIELDS:
                values = []
                for model, data in datas.items():
                    raw = model_value(model_lines(data)[position], name)
                    if raw is None:
                        continue
                    if name in NUMERIC_FIELDS:
                        value, _ = normalize_number(raw)
                        values.append(str(value) if value is not None else raw)
                    else:
                        values.append(normalize_text(raw))
                if len(values) < 2:
                    continue
                bucket = "中文商品名" if name == FIELD_ITEM_DESCRIPTION else "数字"
                disagreement[bucket]["可比字段"] += 1
                if len(set(values)) > 1:
                    disagreement[bucket]["分歧"] += 1
                    if len(disagreement_examples) < 25 and name != FIELD_UNIT:
                        disagreement_examples.append(
                            {
                                "image": image,
                                "line": position + 1,
                                "field": name,
                                "values": values,
                            }
                        )

    # --- 3. 算术不自洽率 ---
    arithmetic = {model: Counter() for model in models}
    for image in images:
        for model in models:
            data = parsed[model][image]
            if data is None:
                continue
            for line in model_lines(data):
                verdict = arithmetic_check(line)
                if verdict is None:
                    arithmetic[model]["三项不全"] += 1
                elif verdict:
                    arithmetic[model]["自洽"] += 1
                else:
                    arithmetic[model]["不自洽"] += 1

    return {
        "虚报率": {
            "超过表格行容量": {
                "说明": "模型报的行数 > 表格的物理行格数，无可辩驳的编造",
                "按模型": dict(per_model_over),
                "实例": over_capacity[:20],
                "可判定页数": sum(1 for i in images if i in capacities),
            },
            "人工核对子集": {
                "说明": "逐张看图数出真正写了字的商品行数；数墨迹不需要读懂字",
                "页数": len(VERIFIED_WRITTEN_ROWS),
                "按模型多报的行数": dict(per_model_verified_extra),
                "按模型核对页数": dict(per_model_verified_pages),
                "明细": verified,
            },
        },
        "跨模型分歧率": {
            bucket: {
                "可比字段": counts["可比字段"],
                "分歧": counts["分歧"],
                "分歧率": (
                    counts["分歧"] / counts["可比字段"] if counts["可比字段"] else None
                ),
                **({"行数分歧的页": counts["行数分歧的页"]} if "行数分歧的页" in counts else {}),
            }
            for bucket, counts in disagreement.items()
        },
        "分歧实例": disagreement_examples,
        "算术不自洽率": {
            model: {
                **dict(counts),
                "不自洽率": (
                    counts["不自洽"] / (counts["不自洽"] + counts["自洽"])
                    if (counts["不自洽"] + counts["自洽"])
                    else None
                ),
            }
            for model, counts in arithmetic.items()
        },
    }


# --------------------------------------------------------------------------
# 装载
# --------------------------------------------------------------------------


def load_outputs() -> dict[str, dict[str, dict[str, Any]]]:
    """读各模型的识别结果。全是失败记录的模型会被跳过并报出原因。"""
    outputs: dict[str, dict[str, dict[str, Any]]] = {}
    blocked: dict[str, str] = {}
    for directory in sorted(OCR_DIR.iterdir()):
        if not directory.is_dir() or directory.name == "smoke":
            continue
        results: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if "error" in data:
                errors.append(data["error"])
                continue
            results[data["image"]] = data
        if not results:
            blocked[directory.name] = errors[0] if errors else "无结果"
            continue
        outputs[directory.name] = results
    if blocked:
        print("以下候选未产出任何结果（标记为阻塞，不参与评分）：")
        for model, reason in blocked.items():
            print(f"  {model}: {reason[:160]}")
        print()
    return outputs


def load_capacities() -> dict[str, int]:
    if not INK_FILE.exists():
        return {}
    data = json.loads(INK_FILE.read_text(encoding="utf-8"))
    return {
        page["image"]: len(page["bands"])
        for page in data["pages"]
        if page.get("determined") and page.get("bands")
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="步骤 07 的 OCR 候选评测")
    parser.add_argument("--out", type=Path, help="报告 JSON 输出路径")
    args = parser.parse_args(argv)

    gts = gt_schema.load_all(GT_DIR)
    outputs = load_outputs()
    capacities = load_capacities()

    all_images = sorted({image for results in outputs.values() for image in results})
    handwritten = [i for i in all_images if i not in gts]

    print(f"GT 页 {len(gts)} 张，手写无 GT 页 {len(handwritten)} 张，"
          f"参评模型 {len(outputs)} 个\n")

    report: dict[str, Any] = {
        "有 GT（印刷件与小票）": {},
        "无 GT（手写件）": {},
    }

    print("=" * 72)
    print("A. 有 Ground Truth 的页：逐字段比对")
    print("=" * 72)
    class_names = {
        "printed": "印刷体供应商发票（6 张，本产品的真实目标单据）",
        "receipt": "零售小票（4 张，为覆盖 imp_notes 4.9 的版式补入）",
    }
    scored_all = {
        model: evaluate_with_gt(gts, results) for model, results in sorted(outputs.items())
    }
    report["有 GT（印刷件与小票）"] = scored_all

    for doc_class, label in class_names.items():
        print(f"\n--- {label} ---")
        print(
            f"  {'模型':<16}{'字段准确率':>10}{'FAR 全部':>11}{'FAR 数值':>11}"
            f"{'人工修正率':>11}{'行匹配':>9}"
        )
        for model, scored in scored_all.items():
            part = scored["by_doc_class"].get(doc_class)
            if part is None:
                continue
            s = part["summary"]
            print(
                f"  {model:<16}{_pct(s['字段准确率']):>12}"
                f"{_pct(s['False Acceptance Rate']):>12}"
                f"{_pct(s['FAR（仅数值字段）']):>12}"
                f"{_pct(s['人工修正率']):>13}{_pct(s['行匹配准确率']):>11}"
            )

    print("\n--- 逐字段明细（10 张合计）---")
    for model, scored in scored_all.items():
        print(f"\n  {model}")
        for name, stats in scored["fields"].items():
            print(
                f"      {name:<18} 准确 {_pct(stats['accuracy']):>7}"
                f"  (对 {stats.get('correct', 0)} / 错 {stats.get('wrong', 0)}"
                f" / 漏 {stats.get('missed', 0)}"
                f" / 编造 {stats.get('fabricated', 0) + stats.get('fabricated_unreadable', 0)})"
            )

    print("\n" + "=" * 72)
    print("B. 手写件（无 GT）：三个不需要读懂手写的客观指标")
    print("=" * 72)
    without = evaluate_without_gt(handwritten, outputs, capacities)
    report["无 GT（手写件）"] = without

    over = without["虚报率"]["超过表格行容量"]
    print(f"\n  1. 虚报率")
    print(f"     a) 超过表格物理行容量（{over['可判定页数']} 页可判定）：")
    if over["按模型"]:
        for model, count in sorted(over["按模型"].items()):
            print(f"        {model:<16} {count} 页")
    else:
        print("        三个模型都没有报出超过行容量的行数")
    sub = without["虚报率"]["人工核对子集"]
    print(f"     b) 人工核对子集（{sub['页数']} 页，逐张看图数过）：")
    for entry in sub["明细"]:
        mark = "  ← 编造" if entry["fabricated_rows"] else ""
        print(
            f"        {entry['image'][:34]:<34} {entry['model']:<14}"
            f" 报 {entry['reported']} 行 / 实际 {entry['actual']} 行{mark}"
        )

    print(f"\n  2. 跨模型分歧率（三个模型给了不同答案 = 至少两个是错的）")
    for bucket, stats in without["跨模型分歧率"].items():
        print(
            f"     {bucket:<8} {_pct(stats['分歧率']):>7}"
            f"   ({stats['分歧']} / {stats['可比字段']} 个可比字段)"
        )

    print(f"\n  3. 算术不自洽率（|数量 × 单价 − 行金额| > ${AMOUNT_TOLERANCE}）")
    for model, stats in sorted(without["算术不自洽率"].items()):
        print(
            f"     {model:<16} {_pct(stats['不自洽率']):>7}"
            f"   (不自洽 {stats.get('不自洽', 0)} / 自洽 {stats.get('自洽', 0)}"
            f" / 三项不全 {stats.get('三项不全', 0)})"
        )

    print("\n  分歧实例（前几条）：")
    for example in without["分歧实例"][:8]:
        print(
            f"     {example['image'][:30]:<30} 行{example['line']} "
            f"{example['field']:<16} {example['values']}"
        )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n完整报告已写入 {args.out}")
    return 0


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
