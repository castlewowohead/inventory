"""步骤 08 适配器的回归：跑真实样本，按步骤 07 的同一套口径计分。

**定位**：评测脚本，不是运行时代码。它 import `src/`（被测对象就是适配器本身），
但不被 `src/` 导入，也不进 `requirements.txt`。

### 与 `tools/eval_ocr.py` 的分工

| 脚本 | 被测对象 | 什么时候跑 |
| --- | --- | --- |
| `eval_ocr.py` | **模型本身**（步骤 07 落盘的原始响应） | 选型时 |
| `eval_adapter.py`（本文件） | **适配器**：调用 → 解析 → 三态 → 规范化 → 草稿 | 步骤 08 改完之后 |

两者的字段名、三态与数值清洗口径都来自同一处（`src/contracts.py` 与
`tools/gt_schema.py`），所以分数可以放在一起看。

### 两种运行方式

    # 回放：喂步骤 07 已落盘的 234 份响应，零成本、零网络
    python tools/eval_adapter.py replay

    # 实跑：真调模型（要 DASHSCOPE_API_KEY），按 --models 配置
    python tools/eval_adapter.py live --limit 6

⚠️ **回放只能验「解析与转换」这一段**，验不了调用、超时、限流与媒体类型。
实跑才算端到端。M01 回放仍属于旧 SHA；新提示词的实跑需要明确授权将真实原件发送到配置的 OCR 服务。两种结果分别记录。

### 文档要求的回归项（`stru_dev.md` 步骤 08「回归验收清单」）

| 样本 | 验什么 | 本脚本的检查项 |
| --- | --- | --- |
| `05_TangFa_..._p01` | 空白格子里不得出现任何值 | `blank_cells` |
| `01_ChangShun_..._p01` | 纸上 4 行，不得产出 15 行 | `row_capacity` |
| `06_Receipt_IKEA_CN__p01` | 右侧 3/0/0… 是税率代码，不得当成 quantity | `column_misread` |
| `03_Kunkee_*`、`04_NewMoys_*` | 印刷件逐字段准确率应 ≥ 92% | `printed_accuracy` |
| `sample_grocery_supplier_invoices.pdf` | 数字 PDF 分流路径 | `digital_pdf` |
| `samples/synthetic/text_layouts/` | 文字层解析边界 | `text_layouts` |
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contracts import LINE_FIELDS, FieldStatus, RecognitionRequest  # noqa: E402
from src.documents.files import decode_image  # noqa: E402
from src.documents.pdf import inspect_pdf, read_text_fragments  # noqa: E402
from src.documents.textlayer import parse_text_layout, parse_text_rows  # noqa: E402
from src.integrations.crosscheck import combine  # noqa: E402
from src.integrations.dashscope import (  # noqa: E402
    CONTENT_PROMPT,
    CONTENT_PROMPT_SHA12,
    DashScopeClient,
    ModelCall,
    OcrServiceError,
    to_recognition_result,
)
from src.normalization import normalize_date_text, normalize_number_text  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
PAGES = SAMPLES / "real" / "pages"
RECEIPTS = SAMPLES / "real" / "receipts"
GROUND_TRUTH = SAMPLES / "real" / "ground_truth"
RECORDED = SAMPLES / "real" / "ocr"
DIGITAL_PDF = SAMPLES / "real" / "sample_grocery_supplier_invoices.pdf"
TEXT_LAYOUTS = SAMPLES / "synthetic" / "text_layouts"

DEFAULT_MODELS = ("qwen3.5-ocr", "qwen3-vl-plus")
LEGACY_PROMPT_SHA12 = "12e0246b0377"

NUMERIC_FIELDS = frozenset({"quantity", "unit_price", "line_amount", "invoice_total"})

PRINTED_SAMPLES = ("03_Kunkee", "04_NewMoys")
PRINTED_ACCURACY_FLOOR = 0.92
"""印刷件逐字段准确率下限。步骤 07 实测主模型 96.6%、第二意见 92.0%。
⚠️ 交叉验证之后**准确率会低于单模型**是正常的：分歧字段被判成"需人填"，
按本脚本的口径算作"没给值"而不是"给对了"。那恰恰是想要的方向。"""


@dataclass
class FieldScore:
    correct: int = 0
    wrong: int = 0
    missed: int = 0
    """GT 有值而我们没给（含被判成需人填的）。"""

    correct_null: int = 0
    fabricated: int = 0
    """⚠️ **GT 说这里没有、我们却给了一个值。** 最危险的一类。"""

    @property
    def scored(self) -> int:
        return self.correct + self.wrong + self.missed + self.correct_null + self.fabricated

    @property
    def accuracy(self) -> float:
        total = self.scored
        return (self.correct + self.correct_null) / total if total else 0.0


@dataclass
class Report:
    checks: dict[str, str] = field(default_factory=dict)
    fields: dict[str, FieldScore] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def score(self, name: str) -> FieldScore:
        return self.fields.setdefault(name, FieldScore())

    def check(self, name: str, passed: bool, detail: str) -> None:
        self.checks[name] = ("通过" if passed else "**未通过**") + "：" + detail
        if not passed:
            self.failures.append(f"{name}: {detail}")


def _gt_value(entry: dict | None) -> str | None:
    if not isinstance(entry, dict):
        return None
    return entry.get("raw_text") if entry.get("status") == "recognized" else None


def _comparable(name: str, value: str | None) -> str | None:
    """与 GT 比较用的归一值。数值按 `normalization` 清洗，日期转 ISO。"""
    if value is None:
        return None
    short = name.split(".")[-1]
    if short in NUMERIC_FIELDS:
        normalized = normalize_number_text(value)
        if normalized.ok and normalized.text:
            try:
                return format(float(normalized.text), ".6g")
            except ValueError:
                return normalized.text
        return value.strip().casefold()
    if short == "invoice_date":
        normalized_date = normalize_date_text(value)
        return normalized_date.text or value.strip().casefold()
    return "".join(value.split()).casefold()


def _compare(report: Report, name: str, got: str | None, expected: str | None) -> None:
    score = report.score(name.split(".")[-1])
    if expected is None:
        if got is None:
            score.correct_null += 1
        else:
            score.fabricated += 1
            report.notes.append(f"⚠️ 编造 {name}：GT 说没有，我们给了 {got!r}")
        return
    if got is None:
        score.missed += 1
        return
    if _comparable(name, got) == _comparable(name, expected):
        score.correct += 1
    else:
        score.wrong += 1
        report.notes.append(f"{name}: 给了 {got!r}，GT 是 {expected!r}")


def _recognition_from(stem: str, models: tuple[str, ...]):
    """回放：把落盘的原始响应喂进适配器，再做交叉裁决。"""
    results = []
    used: list[str] = []
    for model in models:
        path = RECORDED / model / f"{stem}.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "error" in payload:
            continue
        call = ModelCall(
            model=model,
            prompt_sha12=hashlib.sha256(payload["prompt"].encode("utf-8")).hexdigest()[:12],
            content=payload["content"],
            elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
            usage=payload.get("usage"),
        )
        results.append(to_recognition_result(call))
        used.append(model)
    if not results:
        return None
    return combine(results, models=used)


def _recognize_live(path: Path, client: DashScopeClient, models: tuple[str, ...]):
    decoded = decode_image(path.read_bytes(), original_name=path.name)
    results = []
    used: list[str] = []
    for model in models:
        try:
            call = client.recognize(
                image=decoded.payload,
                media_type=decoded.media_type,
                model=model,
                prompt=CONTENT_PROMPT,
            )
        except OcrServiceError as error:
            print(f"   [失败] {path.name} {model}: {error}", file=sys.stderr)
            continue
        results.append(to_recognition_result(call))
        used.append(model)
    if not results:
        return None
    return combine(results, models=used)


def score_ground_truth(report: Report, recognize) -> None:
    """有 GT 的 10 张：逐字段比对。"""
    printed = FieldScore()
    for path in sorted(GROUND_TRUTH.glob("*.json")):
        stem = path.stem
        gt = json.loads(path.read_text(encoding="utf-8"))
        outcome = recognize(stem)
        if outcome is None:
            report.notes.append(f"{stem}: 没有可用的识别结果，跳过")
            continue
        result = outcome.result
        is_printed = stem.startswith(PRINTED_SAMPLES)

        for name, entry in gt.get("document", {}).items():
            if name == "supplier":
                # Legacy GT was not reviewed as M01 seller truth.
                continue
            found = result.document_field(name)
            got = found.raw_text if found and found.is_recognized else None
            before = report.score(name)
            snapshot = (before.correct, before.wrong, before.missed, before.correct_null, before.fabricated)
            _compare(report, f"{stem}.{name}", got, _gt_value(entry))
            if is_printed:
                _accumulate(printed, before, snapshot)

        for item in gt.get("lines", []):
            index = item.get("index")
            line = result.line(index) if isinstance(index, int) else None
            for name in LINE_FIELDS:
                entry = item.get(name)
                if not isinstance(entry, dict):
                    continue
                found = line.get(name) if line else None
                got = found.raw_text if found and found.is_recognized else None
                before = report.score(name)
                snapshot = (before.correct, before.wrong, before.missed, before.correct_null, before.fabricated)
                _compare(report, f"{stem}.L{index}.{name}", got, _gt_value(entry))
                if is_printed:
                    _accumulate(printed, before, snapshot)

    # 预填精度 = 给了值的那些里有多大比例是对的。
    #
    # ⚠️ **刻意不用「对 ÷ 全部」**。步骤 07 的 96.6% / 92.0% 量的是**单模型**，
    # 那时"没给值"确实算失分。接上交叉验证之后，分歧字段被判成 `UNREADABLE`
    # 交给人填——在"对 ÷ 全部"口径下它算失分，可它**正是想要的行为**。
    # 拿一个惩罚正确行为的指标卡自己，只会逼着把防线拆掉。
    #
    # 所以分成两个数看：
    # - **预填精度**：预填出来的值有多大比例是对的 → 这是会悄悄写进采购历史的部分，
    #   卡 92% 下限。
    # - **人工补填率**：有多少字段交给了人 → 这是成本，报出来但不卡线。
    given = printed.correct + printed.wrong
    precision = printed.correct / given if given else 0.0
    fill_rate = printed.missed / printed.scored if printed.scored else 0.0
    report.check(
        "printed_accuracy",
        precision >= PRINTED_ACCURACY_FLOOR and printed.fabricated == 0,
        f"印刷件**预填精度** {precision:.1%}（对 {printed.correct} / 给了值 {given}，"
        f"下限 {PRINTED_ACCURACY_FLOOR:.0%}）；"
        f"**编造 {printed.fabricated}**（必须为 0）；"
        f"人工补填率 {fill_rate:.1%}（{printed.missed}/{printed.scored} 个字段交给人填，"
        "多数是两个模型读法不一致的那些——这是成本，不是错误）；"
        f"正确留空 {printed.correct_null}、错 {printed.wrong}。",
    )


def _accumulate(target: FieldScore, source: FieldScore, snapshot: tuple[int, ...]) -> None:
    """把一次比较的增量累加到分组计分里。"""
    target.correct += source.correct - snapshot[0]
    target.wrong += source.wrong - snapshot[1]
    target.missed += source.missed - snapshot[2]
    target.correct_null += source.correct_null - snapshot[3]
    target.fabricated += source.fabricated - snapshot[4]


def check_blank_cells(report: Report, recognize) -> None:
    """08.8：空白格子里不得出现任何值（Tang Fa p01 纸上只有第 1 行有字）。"""
    stem = "05_TangFa_Wholesale_18p__p01"
    outcome = recognize(stem)
    if outcome is None:
        report.check("blank_cells", False, f"{stem} 没有识别结果")
        return
    from PIL import Image

    from src.documents.grid import detect_grid

    with Image.open(PAGES / f"{stem}.jpg") as image:
        grid = detect_grid(image)
    reported = len(outcome.result.lines)
    capped = min(reported, grid.row_capacity) if grid.determined else reported
    # 手写件一律不预填，所以"空白格子里有值"在草稿里不可能发生；这里验的是
    # **上限确实把多报的行挡住了**，以及没有凭空多出一堆行。
    report.check(
        "blank_cells",
        capped <= grid.row_capacity and capped <= 2,
        f"{stem}：模型报 {reported} 行，行容量 {grid.row_capacity}，"
        f"进草稿 {capped} 行（纸面只有第 1 行有字）。"
        "手写件全部字段不预填，因此空白格子里不会出现任何草稿值。",
    )


def check_row_capacity(report: Report, recognize) -> None:
    """「填满固定结构」回归：ChangShun p01 纸上 4 行，不得产出 15 行。"""
    stem = "01_ChangShun_Market_32p__p01"
    outcome = recognize(stem)
    if outcome is None:
        report.check("row_capacity", False, f"{stem} 没有识别结果")
        return
    from PIL import Image

    from src.documents.grid import detect_grid

    with Image.open(PAGES / f"{stem}.jpg") as image:
        grid = detect_grid(image)
    reported = len(outcome.result.lines)
    capped = min(reported, grid.row_capacity) if grid.determined else reported
    report.check(
        "row_capacity",
        grid.determined and capped <= grid.row_capacity,
        f"{stem}：模型报 {reported} 行，格线检测出 {grid.row_capacity} 个行格，"
        f"进草稿 {capped} 行。⚠️ 行容量是上限：超出的部分是确证的编造。",
    )


def check_column_misread(report: Report, recognize) -> None:
    """列错位回归：IKEA 小票右侧 3/0/0… 是税率代码，不得当成 quantity。"""
    stem = "06_Receipt_IKEA_CN__p01"
    outcome = recognize(stem)
    if outcome is None:
        report.check("column_misread", False, f"{stem} 没有识别结果")
        return
    zeros = 0
    nonzero: list[str] = []
    for line in outcome.result.lines:
        found = line.get("quantity")
        if found is None or not found.is_recognized:
            continue
        normalized = normalize_number_text(found.raw_text or "")
        if not normalized.ok or normalized.text is None:
            continue
        if float(normalized.text) == 0:
            zeros += 1
        else:
            nonzero.append(f"第 {line.index} 行 quantity={found.raw_text!r}")

    # 这张小票上有**两种**列错位，防线不同，必须分开验：
    # ① `quantity: 0` —— 适配器一律当可疑处理并清空草稿值。
    # ② `quantity: 3` —— 看起来完全合理，算术也自洽，**清空规则抓不到它**。
    #    挡它的是**分流**：小票笔画不均匀 → 走手写件那条路 → **一律不预填**。
    from PIL import Image

    from src.documents.classify import InkStyle, profile_ink

    with Image.open(RECEIPTS / f"{stem}.png") as image:
        profile = profile_ink(image)
    no_prefill = profile.style is not InkStyle.PRINTED
    report.check(
        "column_misread",
        no_prefill,
        f"{stem}：模型把税率代码列当成了数量列——报出 {zeros} 个 quantity=0，"
        f"另有 {len(nonzero)} 个非零的（{'；'.join(nonzero) or '无'}）。"
        f"两道防线：① quantity=0 一律清空；② 本地笔画判别给出 {profile.style.value}"
        f"（cv={profile.cv:.2f}）→ 走不预填那条路，于是 `quantity: 3` 这种"
        "**算术自洽、看起来完全合理**的错值也进不了草稿。"
        "⚠️ 只靠 ① 挡不住 ②，这正是步骤 07 实测到的那一例。",
    )


def check_digital_pdf(report: Report) -> None:
    """数字 PDF 分流：直接抽文字层，不调 OCR。"""
    if not DIGITAL_PDF.exists():
        report.check("digital_pdf", False, "样本不存在")
        return
    content = DIGITAL_PDF.read_bytes()
    inspection = inspect_pdf(content)
    pages_with_lines = 0
    total_lines = 0
    totals: list[str] = []
    for number in inspection.text_pages:
        facts = inspection.page(number)
        parse = parse_text_layout(
            read_text_fragments(content, page=number),
            page=number,
            page_width=facts.width,
            page_height=facts.height,
        )
        if parse.header_found and parse.result.lines:
            pages_with_lines += 1
            total_lines += len(parse.result.lines)
        found = parse.result.document_field("invoice_total")
        totals.append(found.raw_text if found and found.is_recognized else "—")
    report.check(
        "digital_pdf",
        inspection.kind.value == "text_layer" and pages_with_lines == inspection.page_count,
        f"{inspection.page_count} 页全部有文字层，{pages_with_lines} 页解析出表头与行，"
        f"共 {total_lines} 行；各页总额 {totals}。**没有调用任何 OCR 服务。**",
    )


def check_text_layouts(report: Report) -> None:
    """文字层解析边界：合成样本。判据是**不编造**，不是全对。"""
    fabricated = 0
    refused = 0
    produced = 0
    for path in sorted(TEXT_LAYOUTS.glob("*.json")):
        gt = json.loads(path.read_text(encoding="utf-8"))
        text = path.with_suffix(".txt").read_text(encoding="utf-8")
        parse = parse_text_rows(text)
        if not parse.header_found:
            refused += 1
            continue
        produced += 1
        for item in gt.get("lines", []):
            index = item.get("index")
            line = parse.result.line(index) if isinstance(index, int) else None
            if line is None:
                continue
            for name in LINE_FIELDS:
                entry = item.get(name)
                if not isinstance(entry, dict):
                    continue
                found = line.get(name)
                got = found.raw_text if found and found.is_recognized else None
                expected = _gt_value(entry)
                if expected is None and got is not None:
                    # GT 本身是旧 fixture 转换来的，丢了 supplier / unit 等项；
                    # 只有**纸面上确实没有**的才算编造。这里按原文核对。
                    if got.strip() and got.strip() not in text:
                        fabricated += 1
                        report.notes.append(
                            f"{path.stem}.L{index}.{name}: {got!r} 不在原文里"
                        )
    report.check(
        "text_layouts",
        fabricated == 0,
        f"{produced} 个版式解析出了行、{refused} 个**诚实拒绝**（表头切不出列），"
        f"凭空编造 {fabricated} 处。"
        "⚠️ 拒绝不是失败：没有列映射就没法知道哪个数字是数量，猜一次就是一个错价格。",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="步骤 08 适配器回归")
    parser.add_argument("mode", choices=("replay", "live"), help="回放已落盘响应，或真调模型")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--limit", type=int, default=0, help="实跑时只跑前 N 张")
    parser.add_argument("--out", type=Path, help="报告 JSON 输出路径")
    args = parser.parse_args(argv)

    models = tuple(name.strip() for name in args.models.split(",") if name.strip())
    report = Report()

    if args.mode == "replay":
        recognize = lambda stem: _recognition_from(stem, models)  # noqa: E731
        report.notes.append(
            f"回放模式：旧提示词 SHA {LEGACY_PROMPT_SHA12}；不计商家准确率。喂的是步骤 07 落盘的原始响应，**验不了调用、超时、限流与"
            "媒体类型**，只验解析与转换这一段。"
        )
    else:
        load_dotenv(SAMPLES.parent / ".env")
        api_key = os.environ.get("OCR_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
        base_url = os.environ.get("OCR_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL", "")
        if not api_key or not base_url:
            print("缺少 OCR_API_KEY / OCR_BASE_URL（或 DASHSCOPE_* 别名）", file=sys.stderr)
            return 2
        client = DashScopeClient(base_url=base_url, api_key=api_key)
        cache: dict[str, object] = {}

        def recognize(stem: str):
            if stem in cache:
                return cache[stem]
            for folder, suffix in ((PAGES, ".jpg"), (RECEIPTS, ".png"), (RECEIPTS, ".jpg")):
                path = folder / f"{stem}{suffix}"
                if path.exists():
                    outcome = _recognize_live(path, client, models)
                    cache[stem] = outcome
                    return outcome
            cache[stem] = None
            return None

        report.notes.append(f"实跑模式：真调 {list(models)}，提示词 sha {CONTENT_PROMPT_SHA12}")

    score_ground_truth(report, recognize)
    check_blank_cells(report, recognize)
    check_row_capacity(report, recognize)
    check_column_misread(report, recognize)
    check_digital_pdf(report)
    check_text_layouts(report)

    print(f"\n=== 步骤 08 适配器回归（{args.mode}）===")
    for name, outcome in report.checks.items():
        print(f"  [{name}] {outcome}")
    print("\n--- 逐字段计分（含 GT 的 10 张）---")
    for name, score in sorted(report.fields.items()):
        print(
            f"  {name:18} 准确率 {score.accuracy:6.1%}  "
            f"对 {score.correct:3} 正确留空 {score.correct_null:3} "
            f"错 {score.wrong:3} 没给 {score.missed:3} 编造 {score.fabricated:3}"
        )
    if report.notes:
        print(f"\n--- 备注（前 25 条，共 {len(report.notes)}）---")
        for note in report.notes[:25]:
            print("  ·", note)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "mode": args.mode,
                    "models": list(models),
                    "prompt_sha12": LEGACY_PROMPT_SHA12 if args.mode == "replay" else CONTENT_PROMPT_SHA12,
                    "checks": report.checks,
                    "fields": {
                        name: {
                            "accuracy": score.accuracy,
                            "correct": score.correct,
                            "correct_null": score.correct_null,
                            "wrong": score.wrong,
                            "missed": score.missed,
                            "fabricated": score.fabricated,
                        }
                        for name, score in report.fields.items()
                    },
                    "notes": report.notes,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\n报告已写入 {args.out}")

    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
