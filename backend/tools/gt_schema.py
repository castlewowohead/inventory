"""步骤 07.4 的 Ground Truth 格式：结构、校验、读写与数值规范化。

**定位**：评测工具，不是运行时代码。不被 `src/` 导入，不进 `requirements.txt`。

### 为什么字段名照搬 `src/contracts.py`

GT 的字段名与三态直接取自 `src/contracts.py` 的 `LINE_FIELDS` / `DOCUMENT_FIELDS`
与 `FieldStatus`。步骤 08 的适配器产出的就是那套结构；GT 另发明一套名字，到步骤 08
就要把两边来回翻译两遍，每翻一遍都是一次出错机会。

### 比契约多出来的一个字段：`pack_spec`

`LINE_FIELDS` 里没有「包装规格」。但真实单据上它到处都是——New Moy's 的 `Cases`
列写 `6X30`，Kun Kee 把 `1000g x 10` 直接写进描述，New Moy's 还有把 `6X44.10`
糊在中文品名后面的（`Beef Stomach - 牛肚6X44.10`）。步骤 09 的 case/pack 换算
恰恰要靠这个。现在不标，到步骤 09 就得把 74 张单据重标一遍。

所以 GT 行里多一个 `pack_spec`，**只记原文、不解析**。它不参与步骤 07 的模型计分
（契约里没有对应字段，模型没被要求输出它），只作为步骤 09 的输入存着。

### 「纸上印着的」与「我们算出来的」必须分开

三态本身就是为这件事准备的：单据上没印总额 → `invoice_total` 就是 `MISSING`，
**不许填各行相加的结果**。这不是洁癖——步骤 07 实测到 `qwen3-vl-plus` 会把
`936 ÷ 8 = 117` 反推出来的单价当成「读到的值」报出来，而且自带「验证：8 × 117 = 936
→ 一致」。如果 GT 自己也把推算值当事实，评测就会反过来奖励这种编造。

用户提交的四份小票 GT 里就有实例：Lowe's 那张的 `Total Discount` 小票上**印着**
`90.99`，GT 里却存成 `90.98999999999999`（75.80+7.60+7.59 的浮点和）；IKEA 那张的
「折扣前合计 298.10」纸上根本没有。转换器会把这类推算值挪进不计分的 `notes`。

### 数值一律字符串，且规范化要留痕

`DecimalAsInteger` 的教训（步骤 04）和 openpyxl 读小数的教训（步骤 06）在 GT 文件
里又出现了一次：用户的工作簿里存着 `0.6899999999999999`、`1.0900000000000001`、
`-2.4700000000000002`。直接拿去比对，模型读对了 `0.69` 反而会被判错。

因此：GT 里所有数值都是**字符串**，由 `normalize_number()` 统一清洗，而且每一次
清洗都记进 `notes`——与步骤 08「数值规范化要显式且可见」是同一条原则，**不静默改数**。
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

# 与 src/contracts.py 保持同源：从契约模块导入，而不是在这里抄一份字符串常量。
# 抄一份的话，契约改名时这里不会报错，只会静默比错字段。
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from src.contracts import (  # noqa: E402
    DOCUMENT_FIELDS,
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_TOTAL,
    FIELD_ITEM_DESCRIPTION,
    FIELD_LINE_AMOUNT,
    FIELD_QUANTITY,
    FIELD_UNIT,
    FIELD_UNIT_PRICE,
    LINE_FIELDS,
    FieldStatus,
)

SCHEMA_VERSION = "gt/1"

FIELD_PACK_SPEC = "pack_spec"
"""包装规格原文（`6X30`、`1000g x 10`、`2X60.1`）。契约里没有，步骤 09 要用。
只记原文不解析；不参与模型计分。"""

GT_LINE_FIELDS: tuple[str, ...] = LINE_FIELDS + (FIELD_PACK_SPEC,)

SCORED_LINE_FIELDS: tuple[str, ...] = LINE_FIELDS
"""参与计分的行字段。`pack_spec` 不在内——模型没被要求输出它。"""

SCORED_DOCUMENT_FIELDS: tuple[str, ...] = (
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_TOTAL,
)
"""参与计分的单据字段。`supplier` 与 `currency` 不计分：提示词没有要求模型输出
这两项（见 tools/ocr_batch.py 的 DEFAULT_PROMPT），拿没要求过的东西扣分不公平。"""

NUMERIC_FIELDS: frozenset[str] = frozenset(
    {FIELD_QUANTITY, FIELD_UNIT_PRICE, FIELD_LINE_AMOUNT, FIELD_INVOICE_TOTAL}
)
"""按数值比较的字段。其余按文本比较。"""


class GroundTruthError(ValueError):
    """GT 文件本身不符合格式。标注写错了，不是模型的问题。"""


# --------------------------------------------------------------------------
# 标注约定（步骤 07.4）：四种情况的写法
# --------------------------------------------------------------------------

MARK_UNREADABLE = "?"
"""看得见但认不出 → `?` → FieldStatus.UNREADABLE"""

MARK_MISSING = "无"
"""单据上根本没有这一项 → `无` → FieldStatus.MISSING"""

MARK_ROW_ABSENT = "该行不存在"
"""模型凭空多出来的行。GT 里不会出现这个标记——GT 只记纸上真实存在的行；
评测脚本把「模型给了、GT 没有」的行按多报（false row）计分。保留这个常量是为了
让标注人看到约定的完整四种情况。"""


def mark_to_field(text: str | None) -> dict[str, Any]:
    """把标注人写的一格文本转成 GT 字段。

    `?` → unreadable，`无` → missing，空 → missing，其余 → recognized + 原文。
    """
    if text is None:
        return {"status": FieldStatus.MISSING.value, "raw_text": None}
    stripped = text.strip()
    if not stripped or stripped == MARK_MISSING:
        return {"status": FieldStatus.MISSING.value, "raw_text": None}
    if stripped == MARK_UNREADABLE:
        return {"status": FieldStatus.UNREADABLE.value, "raw_text": None}
    return {"status": FieldStatus.RECOGNIZED.value, "raw_text": text}


# --------------------------------------------------------------------------
# 数值规范化
# --------------------------------------------------------------------------

_CURRENCY_AND_SPACE = re.compile(r"[\s$￥¥,，]")
_TRAILING_UNIT = re.compile(r"(?i)\s*(lbs?\.?|磅|kg|克|g|ea\.?|each|件|个)\s*$")


def normalize_number(raw: str) -> tuple[Decimal | None, list[str]]:
    """把 GT / 模型输出里的一个数值原文转成 Decimal，并把每一步改动记下来。

    返回 `(值, 改动说明列表)`。无法解析时返回 `(None, [...])`——**不猜、不填 0**。

    改动说明列表就是「规范化要显式可见」的落实：调用方把它写进报告，人能看到
    `1，040.00` 是怎么变成 `1040.00` 的，而不是只看到一个干净的数字。
    """
    notes: list[str] = []
    text = raw.strip()
    if not text:
        return None, ["空字符串"]

    folded = unicodedata.normalize("NFKC", text)
    if folded != text:
        notes.append(f"全角转半角：{text!r} → {folded!r}")
    text = folded

    without_unit = _TRAILING_UNIT.sub("", text)
    if without_unit != text:
        notes.append(f"去掉尾部单位：{text!r} → {without_unit!r}")
    text = without_unit

    cleaned = _CURRENCY_AND_SPACE.sub("", text)
    if cleaned != text:
        notes.append(f"去掉货币符号/千分位/空白：{text!r} → {cleaned!r}")
    text = cleaned

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
        notes.append("括号记负")

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, notes + [f"无法解析为数值：{raw!r}"]
    if negative:
        value = -value

    tidy = _drop_float_noise(value)
    if tidy != value:
        notes.append(f"去掉浮点尾噪：{value} → {tidy}")
        value = tidy
    return value, notes


def _drop_float_noise(value: Decimal) -> Decimal:
    """把 `0.6899999999999999` 这种浮点往返残留还原成 `0.69`。

    规则：如果在 0—4 位小数里存在一个 d，使四舍五入到 d 位后与原值相差小于 1e-9，
    就取最短的那个 d。4 位是步骤 04 给数量和单价定的 scale，真实单据不会比这更细，
    所以超出 4 位的尾巴必定是浮点噪声而不是数据。

    差值阈值卡在 1e-9：`0.6899999999999999` 与 `0.69` 差 1e-16，会被还原；
    真实的 `0.68999`（五位有效数字的数据）与任何更短的写法差 >= 1e-5，不会被动。
    """
    if -value.as_tuple().exponent <= 4:
        return value
    for places in range(5):
        candidate = value.quantize(Decimal(1).scaleb(-places))
        if abs(value - candidate) < Decimal("1e-9"):
            return candidate
    return value


def normalize_text(raw: str) -> str:
    """文本字段的比较口径：NFKC + 折叠空白 + casefold。

    与步骤 03 的 `normalize_name()` 同一套口径。这里不直接复用那个函数，因为它是
    业务层的食材名规范化（将来可能加业务规则，比如去掉规格后缀）；评测要的是一个
    不会随业务改动的固定口径。**两者今天行为一致，但不应该绑在一起。**
    """
    folded = unicodedata.normalize("NFKC", raw)
    return " ".join(folded.split()).casefold()


# --------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------

DOC_CLASSES = ("printed", "handwritten", "receipt", "digital_pdf")
"""单据类别。切分口径见 stru_dev.md 步骤 07：印刷件与手写件分开计分、分开结论。
`receipt` 是后来并入的小票样本，`digital_pdf` 带文字层、不走 OCR。"""


@dataclass
class GroundTruth:
    image: str
    doc_class: str
    annotator: str
    annotated_on: str
    document: dict[str, dict[str, Any]]
    lines: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "image": self.image,
            "doc_class": self.doc_class,
            "annotator": self.annotator,
            "annotated_on": self.annotated_on,
            "notes": self.notes,
            "document": self.document,
            "lines": self.lines,
        }

    def save(self, path: Path) -> None:
        validate(self.to_json())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _validate_field(where: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise GroundTruthError(f"{where} 不是对象：{value!r}")
    status = value.get("status")
    if status not in {s.value for s in FieldStatus}:
        raise GroundTruthError(f"{where} 的 status 非法：{status!r}")
    raw = value.get("raw_text")
    if status == FieldStatus.RECOGNIZED.value:
        if not isinstance(raw, str) or not raw.strip():
            raise GroundTruthError(
                f"{where} 标为 recognized 却没有原文；认不出用 '?'，纸上没有用 '无'"
            )
    elif raw is not None:
        # 与 contracts.py 的 RecognizedField.__post_init__ 同一条规则。
        raise GroundTruthError(f"{where} 状态为 {status}，不应带原文 {raw!r}")


def validate(data: dict[str, Any]) -> None:
    if data.get("schema") != SCHEMA_VERSION:
        raise GroundTruthError(f"schema 不是 {SCHEMA_VERSION}：{data.get('schema')!r}")
    if data.get("doc_class") not in DOC_CLASSES:
        raise GroundTruthError(f"doc_class 非法：{data.get('doc_class')!r}")
    if not data.get("image"):
        raise GroundTruthError("缺少 image")

    document = data.get("document") or {}
    for name in DOCUMENT_FIELDS:
        if name in document:
            _validate_field(f"document.{name}", document[name])
    for name in document:
        if name not in DOCUMENT_FIELDS:
            raise GroundTruthError(
                f"document 里有契约外的字段 {name!r}；"
                f"合法字段：{', '.join(DOCUMENT_FIELDS)}"
            )

    lines = data.get("lines")
    if not isinstance(lines, list):
        raise GroundTruthError("lines 必须是数组")
    for i, line in enumerate(lines, 1):
        if line.get("index") != i:
            raise GroundTruthError(f"第 {i} 行的 index 是 {line.get('index')!r}，应为 {i}")
        for name in GT_LINE_FIELDS:
            if name in line:
                _validate_field(f"lines[{i}].{name}", line[name])
        for name in line:
            if name not in GT_LINE_FIELDS and name not in {"index", "note"}:
                raise GroundTruthError(f"lines[{i}] 有未知字段 {name!r}")


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate(data)
    return data


def load_all(directory: Path) -> dict[str, dict[str, Any]]:
    """读一个目录下的全部 GT，按图片文件名索引。"""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        data = load(path)
        out[data["image"]] = data
    return out


def build_line(
    index: int,
    item_description: str | None = None,
    quantity: str | None = None,
    unit: str | None = None,
    unit_price: str | None = None,
    line_amount: str | None = None,
    pack_spec: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """按标注约定造一行。每个参数传的是标注人写的原文（或 `?` / `无` / None）。"""
    line: dict[str, Any] = {
        "index": index,
        FIELD_ITEM_DESCRIPTION: mark_to_field(item_description),
        FIELD_QUANTITY: mark_to_field(quantity),
        FIELD_UNIT: mark_to_field(unit),
        FIELD_UNIT_PRICE: mark_to_field(unit_price),
        FIELD_LINE_AMOUNT: mark_to_field(line_amount),
        FIELD_PACK_SPEC: mark_to_field(pack_spec),
    }
    if note:
        line["note"] = note
    return line


def build_document(
    supplier: str | None = None,
    invoice_date: str | None = None,
    invoice_number: str | None = None,
    invoice_total: str | None = None,
    currency: str | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        "supplier": mark_to_field(supplier),
        FIELD_INVOICE_DATE: mark_to_field(invoice_date),
        FIELD_INVOICE_NUMBER: mark_to_field(invoice_number),
        FIELD_INVOICE_TOTAL: mark_to_field(invoice_total),
        "currency": mark_to_field(currency),
    }


def check_arithmetic(data: dict[str, Any], tolerance: str = "0.05") -> list[str]:
    """对 GT 自身做算术体检：行内 数量×单价≈行金额，以及各行之和≈单据总额。

    **这是给标注找错的，不是给模型计分的。** 口径与步骤 04 的 `_check_amount()`
    一致（容差 $0.05），但方向相反：步骤 04 用它拦用户输入，这里用它查标注手误。

    算得不平不一定是错——真实单据上就有算不平的（四舍五入、手写笔误），所以返回
    的是提示清单不是异常，由人看过再决定。
    """
    issues: list[str] = []
    limit = Decimal(tolerance)
    total_of_lines = Decimal(0)
    have_all_amounts = True

    for line in data["lines"]:
        idx = line["index"]
        qty, _ = _value_of(line, FIELD_QUANTITY)
        price, _ = _value_of(line, FIELD_UNIT_PRICE)
        amount, _ = _value_of(line, FIELD_LINE_AMOUNT)
        if amount is None:
            have_all_amounts = False
        else:
            total_of_lines += amount
        if qty is not None and price is not None and amount is not None:
            gap = abs(qty * price - amount)
            if gap > limit:
                issues.append(
                    f"第 {idx} 行：{qty} × {price} = {qty * price}，"
                    f"但行金额写的是 {amount}（差 {gap}）"
                )

    doc_total, _ = _value_of(data["document"], FIELD_INVOICE_TOTAL)
    if doc_total is not None and have_all_amounts and data["lines"]:
        gap = abs(total_of_lines - doc_total)
        if gap > limit:
            issues.append(
                f"各行相加 {total_of_lines}，单据总额写的是 {doc_total}（差 {gap}）"
            )
    return issues


def _value_of(holder: dict[str, Any], name: str) -> tuple[Decimal | None, list[str]]:
    field_data = holder.get(name)
    if not field_data or field_data.get("status") != FieldStatus.RECOGNIZED.value:
        return None, []
    return normalize_number(field_data["raw_text"])


def iter_recognized(holder: dict[str, Any], names: Iterable[str]):
    for name in names:
        data = holder.get(name)
        if data and data.get("status") == FieldStatus.RECOGNIZED.value:
            yield name, data["raw_text"]
