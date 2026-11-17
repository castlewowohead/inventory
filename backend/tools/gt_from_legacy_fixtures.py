"""步骤 07.10：把旧项目的英文合成样本改写成本项目自己的回归样本。

来源：`kitchen-ledger/tests/fixtures/invoice-layouts.json`（6 个版式）与
`freshfields-ocr.txt`（一份 OCR 输出原文）。**只在开发时读一次，转成本项目自己的
文件；运行时、测试时都不碰旧目录**（总纲第 2 节的独立性要求）。

### 这批样本能证明什么、不能证明什么

- ✅ 能当**英文印刷体文本解析**的回归样本：列序颠倒、无 SKU、描述换行、小数点用
  逗号、多档税、按重量计价——这几类边界恰好是步骤 08「数字 PDF 直接抽文字层」
  那条路径要处理的，而那条路径本项目只有 1 份真实样本（5 页）。
- ❌ **不能当中文手写 OCR 的性能证据**。它们是英文、是合成的、而且是**文本**不是
  图像，连 OCR 都不经过。
- ❌ **不把旧的 Tesseract 英文方案设为默认**。本步骤的选型结论只针对阿里云
  Model Studio 那三个可用型号，与这批样本无关。

### 改写而不是复制

旧 fixture 的 `expected` 用的是旧项目的字段名（`billingUnit`、`baseQuantity`、
`extractedSupplierName`）和 JS 的 `number` 类型（浮点）。两样都不要：字段名换成
`src/contracts.py` 的标准名，数值一律转成字符串按 GT 口径存。`baseQuantity`
（换算后的基础数量）直接丢弃——换算是步骤 09 的事，GT 里不存派生值。

落地在 `samples/synthetic/`（随项目交付），不是 `samples/real/`（含客户数据，
`.gitignore` 挡着）。

用法：python tools/gt_from_legacy_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

from gt_schema import GroundTruth, build_document, build_line, check_arithmetic

BACKEND = Path(__file__).resolve().parent.parent
LEGACY = BACKEND.parent / "kitchen-ledger" / "tests" / "fixtures"
OUT_DIR = BACKEND / "samples" / "synthetic" / "text_layouts"

ANNOTATOR = "dev（改写自 kitchen-ledger 的英文合成 fixture）"
ANNOTATED_ON = "2026-11-10"


def slug(name: str) -> str:
    return name.replace(" ", "_").replace("/", "_")


def convert(entry: dict) -> tuple[GroundTruth, str]:
    expected = entry["expected"]
    lines = []
    for index, item in enumerate(expected.get("items", []), 1):
        lines.append(
            build_line(
                index,
                item_description=item.get("description"),
                quantity=_text(item.get("quantity")),
                unit=item.get("billingUnit"),
                unit_price=_text(item.get("price")),
                line_amount=_text(item.get("amount")),
                pack_spec=None,
                note=(
                    f"supplierCode={item['supplierCode']}"
                    if item.get("supplierCode")
                    else None
                ),
            )
        )
    gt = GroundTruth(
        image=f"{slug(entry['name'])}.txt",
        doc_class="digital_pdf",
        annotator=ANNOTATOR,
        annotated_on=ANNOTATED_ON,
        document=build_document(
            supplier=expected.get("extractedSupplierName"),
            invoice_date=expected.get("date"),
            invoice_number=expected.get("number"),
            invoice_total=_text(expected.get("total")),
            currency=None,
        ),
        lines=lines,
        notes=[
            "改写自 kitchen-ledger/tests/fixtures/invoice-layouts.json。",
            "英文合成文本，**不是图像**，不经过 OCR：用于步骤 08"
            "「数字 PDF 直接抽文字层」那条路径的解析回归，不作为中文手写性能证据。",
            "旧 fixture 的 baseQuantity（换算后的基础数量）已丢弃——"
            "换算属步骤 09，GT 不存派生值。",
        ],
    )
    return gt, entry["text"]


def _text(value) -> str | None:
    """旧 fixture 的数值是 JS 浮点。转成字符串，去掉 `25.0` 这类多余的小数位。"""
    if value is None:
        return None
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def main() -> int:
    source = LEGACY / "invoice-layouts.json"
    if not source.exists():
        print(f"找不到 {source}；旧项目不在预期位置，跳过（不影响其它步骤）")
        return 0

    entries = json.loads(source.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        gt, text = convert(entry)
        stem = Path(gt.image).stem
        (OUT_DIR / f"{stem}.txt").write_text(text + "\n", encoding="utf-8")
        gt.save(OUT_DIR / f"{stem}.json")
        issues = check_arithmetic(gt.to_json())
        print(f"  {stem}  {len(gt.lines)} 行" + ("  算术提示 " + str(len(issues)) if issues else ""))
        for issue in issues:
            print(f"      · {issue}")

    ocr_text = LEGACY / "freshfields-ocr.txt"
    if ocr_text.exists():
        (OUT_DIR / "freshfields_ocr_output.txt").write_text(
            ocr_text.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print("  freshfields_ocr_output.txt（旧 OCR 输出原文，留作解析回归输入）")

    (OUT_DIR / "README.md").write_text(
        """# 英文合成文本样本（步骤 07.10）

改写自 `kitchen-ledger/tests/fixtures/`，由 `tools/gt_from_legacy_fixtures.py` 生成。
**运行时与测试时都不读旧目录**，这里的文件是本项目自己的副本。

| 文件 | 内容 |
| --- | --- |
| `<版式>.txt` | 发票文本原文 |
| `<版式>.json` | 对应的 Ground Truth，字段名对齐 `src/contracts.py` |
| `freshfields_ocr_output.txt` | 旧项目一份 OCR 输出原文，留作解析回归输入 |

覆盖的边界：列序颠倒、无 SKU 与货币符号、描述换行、小数点用逗号、多档税、
按重量计价。

## 这批样本不能用来做什么

- **不是中文手写 OCR 的性能证据**：英文、合成、而且是文本不是图像。
- **不把旧的 Tesseract 英文方案设为默认**：步骤 07 的选型结论只针对阿里云
  Model Studio 的可用型号，与这批样本无关。
- `baseQuantity` 等换算后的派生值已丢弃，换算属步骤 09。
""",
        encoding="utf-8",
    )
    print(f"\n已写入 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
