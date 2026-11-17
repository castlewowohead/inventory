"""把用户提交的 4 份小票 GT 工作簿转成步骤 07.4 的标准格式。

来源：`samples/real/print_GT/receipt_{1..4}.xlsx`，用户按自己的表头标注
（商品编码 / 商品名称 / 单价 / 数量 / 金额 / 折扣 / 备注，外加「交易汇总」和
「识别备注」两张表）。本脚本把它翻译成 gt_schema 的契约字段，**不要求用户返工**。

转换时处理的四件事，每一件都在输出的 notes 里留痕：

1. **直接读 OOXML 的 `<v>` 文本，不走 openpyxl 常规 reader。**
   口径与步骤 06 的导入一致。常规 reader 会把十进制单元格先转成 float，正是步骤
   04 刚解决掉的精度问题。

2. **去掉浮点尾噪。** 工作簿里存着 `0.6899999999999999`（receipt_4 西红柿数量）、
   `1.0900000000000001`、`-2.4700000000000002`、`90.98999999999999`。这些是写文件
   时的浮点往返残留。不清洗的话，模型读对了 `0.69` 反而会被判成错。

3. **推算值不进 GT。** 用户的「交易汇总」表里有几个纸面上并不存在的数：
   receipt_1 的「折扣前合计 298.10」「促销折扣合计 -28.90」是各行相加推出来的；
   receipt_2 的 Total Discount 存成 `90.98999999999999`，而小票上**印着** `90.99`。
   契约的 DOCUMENT_FIELDS 里本来就没有这些字段，于是它们自然落进不计分的 notes。
   这不是形式主义：步骤 07 实测到模型会把反推值当成「读到的值」报出来
   （`936 ÷ 8 = 117`），GT 自己若也把推算当事实，评测就会反过来奖励编造。

4. **两处与原图不符，按原图订正**（2026-11-10 与用户确认，以读图为准）：
   - receipt_2 `Sales#`：工作簿写 `50567MG1`，原图是 `S0567MG1`（字母 S；
     小票下方另有一行 `STORE: 0567` 佐证）。该字段不参与计分，仅订正留档。
   - receipt_3 `MP SALSA` 行的商品编码：工作簿写 `212269175`，原图是 `212261975`
     （相邻两行为 212020540 / 212080513）。商品编码也不参与计分。

用法：python tools/gt_from_xlsx.py
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from gt_schema import (
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_TOTAL,
    GroundTruth,
    build_line,
    check_arithmetic,
    mark_to_field,
    normalize_number,
)

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

BACKEND = Path(__file__).resolve().parent.parent
SRC_DIR = BACKEND / "samples" / "real" / "print_GT"
OUT_DIR = BACKEND / "samples" / "real" / "ground_truth"

ANNOTATOR = "user（人工标注）+ dev（格式转换与两处按原图订正）"
ANNOTATED_ON = "2026-11-10"


# 每份工作簿对应哪张图，以及单据级字段怎么取。
#
# 明细行的表头是规整的，可以自动识别；**单据级信息是自由文本**，比如 receipt_1 的
# 商家写的是「照片顶部店名被遮挡/模糊，无法可靠辨识」，receipt_3 的交易日期写的是
# 「照片未显示/不可辨识」。靠关键词猜这类句子该算 UNREADABLE 还是 MISSING 很脆，
# 猜错就是一次假的扣分。只有 4 份文件，这里**显式声明**比写启发式规则诚实。
#
# 三态的判断依据：照片上有这个东西但看不清 → '?'（UNREADABLE）；
# 照片根本没拍到 / 单据上就没有这一项 → '无'（MISSING）。
WORKBOOKS: dict[str, dict[str, Any]] = {
    "receipt_1.xlsx": {
        "image": "06_Receipt_IKEA_CN__p01.png",
        "doc_class": "receipt",
        "document": {
            "supplier": "?",  # 店名被马赛克遮挡——人为不可读，不算模型失败
            FIELD_INVOICE_DATE: "21.12.26 20:37:43",
            FIELD_INVOICE_NUMBER: "590",  # 小票底部「交易号 590」
            FIELD_INVOICE_TOTAL: "269.20",  # 纸面「合计: 269.20」
            "currency": "RMB",
        },
        "notes": [
            "店名被马赛克打码，supplier 标 UNREADABLE；这是人为处理，不是模型失败。",
            "右侧那一列数字（三文鱼行是 3、其余是 0）是**税率代码**，不是数量。"
            "证据：含税表印着代码 0 → 13.0% 净值 140.97 税 18.33，代码 3 → 9.0% "
            "净值 100.83 税 9.07；而 109.90 ÷ 1.09 = 100.83，"
            "269.20 − 109.90 = 159.30 = 140.97 × 1.13。"
            "即三文鱼是唯一的 9% 商品。模型若把 3 当数量报出，应判错。",
            "用户工作簿「交易汇总」里的『折扣前合计 298.10』『促销折扣合计 -28.90』"
            "纸面上没有印，是各行推算值，不进 GT。",
            "纸面只印了合计 269.20，各商品行没有数量列，因此 quantity 多为 MISSING。",
        ],
    },
    "receipt_2.xlsx": {
        "image": "07_Receipt_Lowes_EN__p01.jpg",
        "doc_class": "receipt",
        "document": {
            "supplier": "LOWE'S HOME CENTERS",
            FIELD_INVOICE_DATE: "09-02-22",  # 原文照抄，日月顺序未确认
            FIELD_INVOICE_NUMBER: "32756",  # 纸面「INVOICE 32756」
            FIELD_INVOICE_TOTAL: "385.81",  # 纸面「TOTAL: 385.81」
            "currency": "USD",
        },
        "notes": [
            "日期按纸面原文 `09-02-22` 记录，**不转成 2022-09-02**："
            "日/月顺序在单据上无从确认，步骤 04/06 的口径是不明就拒绝而不是猜。",
            "`Sales#` 按原图订正为 `S0567MG1`（工作簿原写 50567MG1）；该字段不计分。",
            "Total Discount 纸面印着 90.99；工作簿存成 90.98999999999999"
            "（75.80+7.60+7.59 的浮点和）。该项不是契约字段，不进 GT。",
            "商品行的『单价』取自 DISCOUNT EACH 行上印的原价，"
            "『金额』取自商品行右侧的折后净额，因此 数量×单价 ≠ 金额 属于正常。",
            "最后一行 LCC SYSTEM USE ONLY 金额 0.00，是用途标记不是商品。",
        ],
    },
    "receipt_3.xlsx": {
        "image": "08_Receipt_Target_EN__p01.jpg",
        "doc_class": "receipt",
        "document": {
            "supplier": "?",  # 照片未拍到店头 logo
            FIELD_INVOICE_DATE: "无",  # 日期在小票下半段，照片没拍到
            FIELD_INVOICE_NUMBER: "无",
            FIELD_INVOICE_TOTAL: "84.37",  # 纸面「TOTAL $84.37」
            "currency": "USD",
        },
        "notes": [
            "照片只拍到小票中段：店名、日期、小票号都不在画面内，按 MISSING 记。"
            "模型按提示词应返回 null，报出任何值都是编造。",
            "`MP SALSA` 行商品编码按原图订正为 `212261975`"
            "（工作簿原写 212269175）；商品编码不计分。",
            "两张礼品卡（$25.00 / $10.00）按商品行记录，它们不是食材采购数据。",
            "IRISH SPRING 行：原价 $4.59、优惠 $1.09、实付 $3.50，"
            "单价取原价、金额取实付，故 数量×单价 ≠ 金额 属正常。",
        ],
    },
    "receipt_4.xlsx": {
        "image": "09_Receipt_VegMarket_CN__p01.jpg",
        "doc_class": "receipt",
        "document": {
            "supplier": "?",
            FIELD_INVOICE_DATE: "2017.11.22 09:14",
            FIELD_INVOICE_NUMBER: "3002 NO.030117/11220146",
            FIELD_INVOICE_TOTAL: "26.90",  # 纸面「合计: 26.90」
            "currency": "CNY",
        },
        "notes": [
            "原图只有 270×360 像素，比那 74 张扫描页（2479×3230）小两个数量级。"
            "本张上的结果更多反映照片质量，不单独作为模型能力证据。",
            "单位列：小票有『数量: 5.37 件数: 9.00』，数量实为重量 kg，"
            "各行未印单位，因此 unit 记 MISSING，不替纸面补 kg。",
            "用户已记录：小票显示总重 5.37，各行相加 5.38，差 0.01 为显示/进位差异。",
        ],
    },
}

# 工作簿明细表头 → 契约字段。表头文字在四份文件里一致。
COLUMN_MAP = {
    "商品名称": "item_description",
    "单价/原价": "unit_price",
    "数量": "quantity",
    "金额/净额": "line_amount",
}
NOTE_COLUMNS = ("商品编码", "折扣", "分类及备注")

# 按原图订正的单元格（工作簿文件名 → 单元格 → 订正后的值）。
CELL_CORRECTIONS = {
    "receipt_2.xlsx": {"B8": "S0567MG1"},
    "receipt_3.xlsx": {"A14": "212261975"},
}


def read_sheets(path: Path) -> list[dict[str, str]]:
    """读出每张工作表的 `单元格 → 文本`，直接取 OOXML 的 `<v>`，不经 float。"""
    sheets: list[dict[str, str]] = []
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(t.text or "" for t in item.iter(NS + "t")))
        names = sorted(
            n for n in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
        )
        for name in names:
            root = ET.fromstring(archive.read(name))
            cells: dict[str, str] = {}
            for cell in root.iter(NS + "c"):
                ref = cell.get("r")
                kind = cell.get("t")
                node = cell.find(NS + "v")
                if kind == "s" and node is not None:
                    text = shared[int(node.text)]
                elif kind == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter(NS + "t"))
                elif node is not None:
                    text = node.text or ""
                else:
                    continue
                if ref and text != "":
                    cells[ref] = text
            sheets.append(cells)
    return sheets


def cell_row(ref: str) -> int:
    return int(re.sub(r"[A-Z]", "", ref))


def cell_col(ref: str) -> str:
    return re.sub(r"[0-9]", "", ref)


def find_header_row(cells: dict[str, str]) -> int | None:
    """明细表头所在行：含有「商品名称」的那一行。"""
    for ref, text in cells.items():
        if text.strip() == "商品名称":
            return cell_row(ref)
    return None


def tidy(raw: str, trail: list[str], where: str) -> str:
    """数值单元格的清洗；每改动一次就往 trail 里记一条，不静默改数。"""
    value, changes = normalize_number(raw)
    if value is None:
        return raw
    text = format(value, "f")
    if text != raw:
        trail.append(f"{where}：{raw} → {text}（{'；'.join(changes) or '格式化'}）")
    return text


def convert(filename: str, spec: dict[str, Any]) -> GroundTruth:
    path = SRC_DIR / filename
    sheets = read_sheets(path)
    cells = dict(sheets[0])
    for ref, fixed in CELL_CORRECTIONS.get(filename, {}).items():
        cells[ref] = fixed

    header_row = find_header_row(cells)
    if header_row is None:
        raise SystemExit(f"{filename} 找不到「商品名称」表头，格式与预期不符，不猜")

    headers: dict[str, str] = {}
    for ref, text in cells.items():
        if cell_row(ref) == header_row:
            headers[cell_col(ref)] = text.strip()

    trail: list[str] = []
    lines: list[dict[str, Any]] = []
    rows = sorted({cell_row(r) for r in cells if cell_row(r) > header_row})
    for row in rows:
        row_cells = {
            headers.get(cell_col(ref), ""): cells[ref]
            for ref in cells
            if cell_row(ref) == row and cell_col(ref) in headers
        }
        if not row_cells.get("商品名称"):
            continue
        index = len(lines) + 1
        values: dict[str, str | None] = {}
        for header, field_name in COLUMN_MAP.items():
            raw = row_cells.get(header, "").strip()
            if not raw:
                values[field_name] = None
                continue
            if field_name == "item_description":
                values[field_name] = raw
            else:
                values[field_name] = tidy(raw, trail, f"第 {index} 行 {header}")
        extras = [
            f"{col}={row_cells[col]}"
            for col in NOTE_COLUMNS
            if row_cells.get(col) not in (None, "", "0")
        ]
        lines.append(
            build_line(
                index,
                item_description=values["item_description"],
                quantity=values["quantity"],
                unit=None,  # 小票各行未印单位；不替纸面补
                unit_price=values["unit_price"],
                line_amount=values["line_amount"],
                pack_spec=None,  # 小票没有箱规
                note="；".join(extras) or None,
            )
        )

    document = {
        name: mark_to_field(value) for name, value in spec["document"].items()
    }
    notes = list(spec["notes"])
    notes.insert(0, f"由 {filename} 转换（直接读 OOXML <v> 文本，不经 float）。")
    if trail:
        notes.append("数值规范化留痕：" + "｜".join(trail))

    return GroundTruth(
        image=spec["image"],
        doc_class=spec["doc_class"],
        annotator=ANNOTATOR,
        annotated_on=ANNOTATED_ON,
        document=document,
        lines=lines,
        notes=notes,
    )


def main() -> int:
    for filename, spec in WORKBOOKS.items():
        gt = convert(filename, spec)
        target = OUT_DIR / (Path(gt.image).stem + ".json")
        gt.save(target)
        issues = check_arithmetic(gt.to_json())
        print(f"  {filename} → {target.name}  {len(gt.lines)} 行")
        for issue in issues:
            print(f"      · 算术提示：{issue}")
    print(f"\n已写入 {OUT_DIR}")
    print("小票上的折扣价与原价分列，算术提示属预期，已在各自 notes 说明。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
