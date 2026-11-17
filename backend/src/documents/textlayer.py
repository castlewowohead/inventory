"""把文字层还原成行列结构与统一契约（步骤 08 分流的第一条路径，不调 OCR）。

### 这条路径的价值与限度

数字 PDF 的字符是**准确**的——步骤 07 实测 `sample_grocery_supplier_invoices.pdf`
5 张发票的行金额之和与印着的 Total 完全吻合。所以这条路径近 0 耗时、零成本，
而且没有「认不出」这种状态：文字层里要么有字符，要么没有。

⚠️ **但文字层不是白捡**。同一次实测：第 2 页的发票号 `MP-2026-0611-219` 在文字层里
是 `MP-2026-0611-21` 和 `9` **两个片段**（渲染时换了行）。直接用
`extract_text()` 的换行结果会把这个单号拆成两段存进去，而且不报错。行列归位必须
自己写代码——这就是本模块。

### 两种输入，一套还原

| 输入 | 来源 | 怎么切成格 |
| --- | --- | --- |
| 带坐标的文本片段 | 数字 PDF（`documents.pdf.read_text_fragments`） | 按 y 聚成行、按 x 归到列 |
| 纯文本 | `samples/synthetic/text_layouts/`、旧 OCR 输出 | 按 `|` 或连续空格切列 |

两种输入都先变成「行 × 格」的中间结构，再走同一套列角色识别。于是合成样本能当
真实解析逻辑的回归输入，而不是测一条只有它自己走的分支。

### 不移植整套英文正则 parser

docs/stru_dev.md 步骤 08 的「不移植」点名了这一条。本模块的策略是
**表头驱动**：识别出表头行里哪一列是描述、数量、单价、金额，再按列取值。
识别不出表头就**诚实地不产出行**（返回 `header_found=False` 与原因），由调用方
决定退回 OCR 还是让人手工加行——**不靠一串针对特定版式的正则去猜**。

### 总额只能来自纸面，绝不相加

⚠️ 步骤 07 的教训：GT 里出现推算值会**反过来奖励编造**（模型自己算一个数报上来
会被判成对）。所以 `invoice_total` 只认纸面上带总额标签的那个数字，优先级
`Grand Total` > `Invoice Total` > `Total`，并且**永不采用** `Amount Due` /
`Total Due` / `Balance`——那是扣掉预付或信用之后的应付额，不是单据总额
（实测样本 `plain_table_without_SKU...` 里 `Invoice total 96.80` 与
`Amount due 46.80` 同时存在，差 50 元）。单据上没有总额就是 `MISSING`。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.contracts import (
    FIELD_CURRENCY,
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_TOTAL,
    FIELD_ITEM_DESCRIPTION,
    FIELD_LINE_AMOUNT,
    FIELD_QUANTITY,
    FIELD_SUPPLIER,
    FIELD_UNIT,
    FIELD_UNIT_PRICE,
    BoundingBox,
    CoordinateSpace,
    FieldEvidence,
    RecognitionResult,
    RecognizedField,
    RecognizedLine,
    RecognizedPage,
)
from src.documents.pdf import TextFragment

logger = logging.getLogger(__name__)

TEXT_LAYER_PROVIDER = "text-layer"
"""这条路径的 provider 标识。**不是厂商名**——它根本没调厂商，
所以结果里 `model` 为 None，`is_fake` 为 False（字符是真的，来自文件本身）。"""

# 额外字段：合同契约里没有，但真实单据上到处都是，丢了步骤 09 就得重新提取一遍。
# 存进 `raw_fields` 的 `extra_fields` 下，与契约字段分开放。
EXTRA_FIELD_PACK_SPEC = "pack_spec"
"""包装规格原文：`6X30`、`1000g x 10`、`2 cases`。步骤 07 的 GT 里也是额外一列，
**不参与契约**，步骤 09 的 case / pack 换算要用。"""

EXTRA_FIELD_SUPPLIER_CODE = "supplier_code"
"""供应商自己的货号 / SKU。步骤 10 的映射记忆会用到，本步骤只保留不解释。"""

# 列角色的表头别名。中英文都收，**刻意保持短**：靠的是表头匹配，不是穷举版式。
# 与 services/spreadsheet_parsing.ALIASES 同源（表格导入的列别名），
# 两边都认同一批写法；这里多了 `pack_spec` 与 `supplier_code` 两个额外列。
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    FIELD_ITEM_DESCRIPTION: (
        "description", "item", "itemname", "item name", "product", "particulars",
        "goods", "commodity", "商品", "品名", "名称", "商品名称", "商品描述",
        "货品", "品项", "摘要", "货名",
    ),
    FIELD_QUANTITY: (
        "qty", "quantity", "weight", "数量", "重量", "采购数量", "件数",
    ),
    FIELD_UNIT: (
        "uom", "unit", "units", "单位", "采购单位", "计价单位",
    ),
    FIELD_UNIT_PRICE: (
        "price", "unitprice", "unit price", "rate", "unit cost", "单价", "价格",
        "单位价格",
    ),
    FIELD_LINE_AMOUNT: (
        "amount", "linetotal", "line total", "total", "extended", "extendedprice",
        "extension", "subtotal", "金额", "行金额", "小计", "合计", "总额",
    ),
    EXTRA_FIELD_PACK_SPEC: (
        "cases", "case", "pack", "packsize", "pack size", "spec", "packing",
        "规格", "包装", "包装规格", "箱数",
    ),
    EXTRA_FIELD_SUPPLIER_CODE: (
        "sku", "code", "itemcode", "item code", "itemno", "item no", "product code",
        "货号", "编号", "商品编号", "产品编号",
    ),
}

# 列角色里哪些进契约、哪些只进 extra_fields。
_CONTRACT_COLUMNS = frozenset(
    {
        FIELD_ITEM_DESCRIPTION,
        FIELD_QUANTITY,
        FIELD_UNIT,
        FIELD_UNIT_PRICE,
        FIELD_LINE_AMOUNT,
    }
)

# 一行要算表头，至少要命中这么多个列角色，而且必须包含描述或金额里的一个。
MIN_HEADER_MATCHES = 2

# 汇总行的标签。命中这些就**不是商品行**，不进 `lines`。
# 税费 / 运费 / 折让在步骤 05 里是确认成 `adjustment` 行的，但那要由人决定；
# 识别层只负责「这不是一条商品采购」，不替人分类。
SUMMARY_LABELS: tuple[str, ...] = (
    "subtotal", "sub total", "net amount", "net total", "tax", "vat", "gst", "pst",
    "hst", "sales tax", "tax total", "freight", "shipping", "delivery", "deliverycharge",
    "delivery charge", "delivery fee", "fuel surcharge", "surcharge", "discount",
    "total discount", "rounding", "deposit", "credit", "balance", "amount due",
    "total due", "grand total", "invoice total", "total", "payment", "paid",
    "小计", "合计", "总计", "税", "税额", "增值税", "运费", "配送费", "折扣", "折让",
    "优惠", "押金", "应付", "应付金额", "实付", "已付", "本期应付", "合计金额",
)

# 单据总额标签的优先级：数字越小越优先。
# ⚠️ `amount due` / `total due` / `balance` **不在这里**，它们是扣除预付后的应付额。
TOTAL_LABEL_PRIORITY: tuple[tuple[str, int], ...] = (
    ("grand total", 1), ("总计", 1), ("价税合计", 1),
    ("invoice total", 2), ("发票总额", 2), ("单据总额", 2),
    ("total amount", 3), ("合计金额", 3),
    ("total", 4), ("合计", 4), ("金额合计", 4),
)

# 单据级字段的标签。值取标签右侧（或下一行）那一格。
DOCUMENT_LABELS: dict[str, tuple[str, ...]] = {
    FIELD_SUPPLIER: (
        "vendor", "supplier", "from", "sold by", "seller", "供应商", "销售方",
        "卖方", "商家",
    ),
    # ⚠️ 这里**不能**出现裸的 `invoice`。实测 `sample_grocery_supplier_invoices.pdf`
    # 右上角印着大号标题 `INVOICE`，如果 `invoice #` 归一化后变成 `invoice`，
    # 那个标题就会被当成发票号标签，于是把标题下面那行（供应商中文名）当成了单号。
    # 所以 `_key()` 刻意**保留 `#`**，别名里也必须带上 `#`。
    FIELD_INVOICE_NUMBER: (
        "invoice #", "invoice#", "invoice no", "invoice no.", "invoice number",
        "invoice id", "inv. no", "inv no", "inv #", "bill no", "bill #",
        "receipt no", "发票号", "发票号码", "单号", "单据号", "票号",
    ),
    FIELD_INVOICE_DATE: (
        "date", "invoice date", "date issued", "issue date", "issued",
        "日期", "开票日期", "单据日期", "出单日期",
    ),
    FIELD_CURRENCY: ("currency", "币种", "货币"),
}

# 币种代码白名单与步骤 04 一致（`services.purchases.CURRENCIES`）。
# 这里不导入那个常量是为了不让 documents 包依赖 services 层（分层方向：
# services → documents，不反向）；取值不一致会被测试抓到。
_CURRENCY_PATTERN = re.compile(r"\b(USD|CNY|RMB|EUR|GBP|CAD|AUD|JPY|HKD|TWD)\b", re.I)

_NUMERIC_HINT = re.compile(r"\d")
_PIPE_SPLIT = re.compile(r"\s*\|\s*")
_GAP_SPLIT = re.compile(r"\s{2,}")
# 汇总行里标签后面紧跟的值长什么样：数字、货币符号、括号负数、负号。
_VALUE_START = re.compile(r"^[\d(\-+$¥€£￥]")
# 一格文本里最后一个数（含货币符号、千分位、括号负数）。汇总行的值总在最右边。
_LAST_NUMBER = re.compile(r"[(\-+]?[$¥€£￥]?\s?\d[\d.,\s]*\)?(?!.*\d)")


def _key(text: str) -> str:
    """表头/标签匹配用的归一键：去空白、下划线、连字符与冒号，折大小写。

    ⚠️ **`#` 要保留**：去掉它会让 `Invoice #` 与大号标题 `INVOICE` 撞成同一个键，
    于是标题被当成发票号标签（实测踩过）。
    """
    return re.sub(r"[\s_\-:：]+", "", text).casefold()


def _label_match(key: str, label: str) -> bool:
    """`key` 是不是 `label` 这个标签。

    两种命中：**完全相等**，或者标签后面紧跟着一个值（纯文本单据常常把标签和值
    挤在一格：`Sales tax: 6.80` 切不开）。

    第二种必须卡住「后面是值」这个条件，否则 `Total Mix Vegetables` 这种**商品名**
    会被当成汇总行整行丢掉——而丢掉一行商品不会报错，只能靠人在收口确认时发现。
    """
    target = _key(label)
    if key == target:
        return True
    if not key.startswith(target):
        return False
    return bool(_VALUE_START.match(key[len(target) :]))


@dataclass(frozen=True)
class TextCell:
    """行内的一格。`left` / `right` 是该格的水平范围（PDF 点或字符偏移）。"""

    text: str
    left: float
    right: float

    @property
    def center(self) -> float:
        return (self.left + self.right) / 2


@dataclass(frozen=True)
class TextRow:
    """一行。`top` / `bottom` 是垂直范围（**屏幕方向**：越往下越大）。"""

    index: int
    cells: tuple[TextCell, ...]
    top: float
    bottom: float
    page: int
    delimited: bool = False
    """这一行的 `left`/`right` 是**列序号**还是**水平位置**。

    ⚠️ 这个区分是必须的。竖线分隔的纯文本表里，表头 `Qty | Description | UOM`
    与数据 `6 | Lemons | kg` 的**字符偏移完全对不上**（表头词比值长），按偏移归列
    会整体错一列——实测 `kg` 被归进描述列、`6.00` 被归进单位列，而且不报错。
    竖线表按**第几个字段**归列才对；定宽排版（PDF 坐标、空格对齐）才按位置归列。
    """

    @property
    def raw(self) -> str:
        return "  ".join(cell.text for cell in self.cells)

    @property
    def first_text(self) -> str:
        return self.cells[0].text if self.cells else ""


@dataclass(frozen=True)
class TextLayerParse:
    """一次文字层解析的结果。

    `header_found=False` 时 `result.lines` 一定是空的——**宁可不给行，也不给猜出来
    的行**。调用方据此决定退回 OCR 还是让人手工加行。
    """

    result: RecognitionResult
    header_found: bool
    reason: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)
    extra_fields: tuple[dict[str, str], ...] = field(default_factory=tuple)
    """契约之外的列，逐行与 `result.lines` 一一对应（包装规格、供应商货号）。
    契约里没有它们，但真实单据上到处都是——丢了步骤 09 的 case / pack 换算就得
    重新提取一遍。调用方把它写进 `invoice_lines.raw_fields` 的 `extra_fields` 下，
    与契约字段分开放。"""

    @property
    def has_lines(self) -> bool:
        return bool(self.result.lines)


# --- 输入 → 行 × 格 --------------------------------------------------------


def fragments_to_rows(
    fragments: list[TextFragment], *, page_height: float
) -> list[TextRow]:
    """把带坐标的片段聚成行。

    行高容差取**该页字号中位数的 60%**，不取一个固定点数：同一份 PDF 里标题
    20pt、正文 9pt，固定容差要么把两行并成一行，要么把一行拆成两行。

    y 坐标从 PDF 的「原点在左下」翻成「越往下越大」，这样行序就是阅读顺序。
    """
    usable = [item for item in fragments if not item.is_blank]
    if not usable:
        return []
    sizes = sorted(item.font_size for item in usable if item.font_size > 0)
    median_size = sizes[len(sizes) // 2] if sizes else 10.0
    tolerance = max(1.0, median_size * 0.6)

    # 翻成屏幕方向后按 y 升序、x 升序排。
    flipped = sorted(
        ((page_height - item.y, item.x, item) for item in usable),
        key=lambda entry: (entry[0], entry[1]),
    )

    rows: list[TextRow] = []
    bucket: list[tuple[float, float, TextFragment]] = []
    anchor = flipped[0][0]
    for entry in flipped:
        if entry[0] - anchor > tolerance:
            rows.append(_row_from_bucket(bucket, len(rows) + 1, median_size))
            bucket = []
            anchor = entry[0]
        bucket.append(entry)
    if bucket:
        rows.append(_row_from_bucket(bucket, len(rows) + 1, median_size))
    return rows


def _row_from_bucket(
    bucket: list[tuple[float, float, TextFragment]], index: int, median_size: float
) -> TextRow:
    tops = [entry[0] for entry in bucket]
    cells = tuple(
        TextCell(
            text=entry[2].text,
            left=entry[1],
            # 片段宽度未知（visitor 不给）。按字号估一个宽度：CJK 按 1 个字号宽、
            # 拉丁按 0.5 个字号宽。这个估计只用于列归属，
            # 偏差几个点不会改变原始 PDF 文字片段。
            right=entry[1] + _estimate_width(entry[2].text, entry[2].font_size or median_size),
        )
        for entry in bucket
    )
    return TextRow(
        index=index,
        cells=cells,
        top=min(tops),
        bottom=max(tops) + median_size,
        page=bucket[0][2].page,
    )


def _estimate_width(text: str, font_size: float) -> float:
    wide = sum(1 for char in text if ord(char) > 0x2E80)
    narrow = len(text) - wide
    return font_size * (wide + narrow * 0.5)


def text_to_rows(text: str, *, page: int = 1) -> list[TextRow]:
    """把纯文本切成行 × 格。

    优先按 `|` 切（合成样本与很多 OCR 输出用它），没有竖线时按**连续 2 个以上
    空格**切——单个空格在商品名里本来就有（`Beef Eye Round`），按它切会把名字拆散。
    `left` / `right` 用字符偏移，于是列归属的逻辑与 PDF 那条路完全一样。
    """
    rows: list[TextRow] = []
    for offset, raw in enumerate(text.splitlines()):
        if not raw.strip():
            continue
        cells, delimited = _split_line(raw)
        if not cells:
            continue
        rows.append(
            TextRow(
                index=len(rows) + 1,
                cells=cells,
                top=float(offset),
                bottom=float(offset) + 1.0,
                page=page,
                delimited=delimited,
            )
        )
    return rows


def _split_line(raw: str) -> tuple[tuple[TextCell, ...], bool]:
    """切一行，返回 (格子, 是否竖线分隔)。

    竖线分隔时 `left`/`right` 用**列序号**（第 i 个字段 → i..i+1）；
    按空格对齐时用**字符偏移**。见 `TextRow.delimited` 的说明。
    """
    if "|" in raw:
        pieces = [piece.strip() for piece in _PIPE_SPLIT.split(raw)]
        cells = tuple(
            TextCell(text=piece, left=float(index), right=float(index) + 1.0)
            for index, piece in enumerate(pieces)
            if piece
        )
        return cells, True

    cells_list: list[TextCell] = []
    position = 0
    for piece in _GAP_SPLIT.split(raw):
        found = raw.find(piece, position) if piece else -1
        start = found if found >= 0 else position
        if piece.strip():
            cells_list.append(
                TextCell(text=piece.strip(), left=float(start), right=float(start + len(piece)))
            )
        position = start + max(1, len(piece))
    return tuple(cells_list), False


# --- 行 × 格 → 契约 --------------------------------------------------------


@dataclass(frozen=True)
class _Column:
    role: str
    left: float
    right: float

    def overlaps(self, cell: TextCell) -> float:
        """这一格落在本列里的比例（0—1）。按**格子自身宽度**算。"""
        low = max(self.left, cell.left)
        high = min(self.right, cell.right)
        if high <= low:
            return 0.0
        width = max(cell.right - cell.left, 1e-6)
        return (high - low) / width


def _find_header(rows: list[TextRow]) -> tuple[int, tuple[_Column, ...]] | None:
    """找表头行，并把每个命中的表头格变成一列（左右边界取相邻表头的中点）。"""
    for position, row in enumerate(rows):
        matched: list[tuple[TextCell, str]] = []
        for cell in row.cells:
            role = _role_of(cell.text)
            if role is not None:
                matched.append((cell, role))
        roles = {role for _, role in matched}
        if len(matched) < MIN_HEADER_MATCHES:
            continue
        if not roles & {FIELD_ITEM_DESCRIPTION, FIELD_LINE_AMOUNT, FIELD_UNIT_PRICE}:
            continue
        if len(roles) != len(matched):
            # 同一个角色命中两列（`Total` 既是行金额又是汇总）。表头有歧义时
            # 不硬选一个——这正是「认不出就说认不出」在列映射上的样子。
            logger.info("text layer header row %s has duplicate roles; skipped", row.index)
            continue
        return position, _columns_from(matched)
    return None


def _role_of(text: str) -> str | None:
    key = _key(text)
    if not key:
        return None
    for role, aliases in COLUMN_ALIASES.items():
        if key in {_key(alias) for alias in aliases}:
            return role
    return None


def _columns_from(matched: list[tuple[TextCell, str]]) -> tuple[_Column, ...]:
    """表头格 → 列边界。相邻两个表头之间取中点分界，首尾放开到 ±无穷。"""
    ordered = sorted(matched, key=lambda entry: entry[0].left)
    columns: list[_Column] = []
    for index, (cell, role) in enumerate(ordered):
        left = float("-inf") if index == 0 else (ordered[index - 1][0].right + cell.left) / 2
        right = (
            float("inf")
            if index == len(ordered) - 1
            else (cell.right + ordered[index + 1][0].left) / 2
        )
        columns.append(_Column(role=role, left=left, right=right))
    return tuple(columns)


def _is_summary(row: TextRow) -> bool:
    """这一行是不是汇总行（小计 / 税 / 运费 / 总额 / 应付）。

    只看**第一格**：汇总标签总在最左边，而商品名里出现 `total` 这个词
    （`Total Mix Vegetables`）不该让整行被当成汇总。
    """
    first = _key(row.first_text)
    if not first:
        return False
    return any(_label_match(first, label) for label in SUMMARY_LABELS)


def _total_priority(row: TextRow) -> int | None:
    """这一行是不是**单据总额**行；是则返回优先级，否则 None。

    明细表在第一个总额行结束。总额之后印的是页脚、签收栏、「请随款注明单号」
    之类的文字，它们既不是商品也不是汇总——不收口的话，实测会把
    `SAMPLE DOCUMENT — FOR SOFTWARE TESTING ONLY` 当成一条商品行，
    `line_amount` 里填上 `Page 1`。
    """
    first = _key(row.first_text)
    if not first:
        return None
    for label, priority in TOTAL_LABEL_PRIORITY:
        if _label_match(first, label):
            return priority
    return None


def _has_numeric_role(assigned: dict[str, str]) -> bool:
    """这一行的数量 / 单价 / 金额里，有没有哪个真的是一个数。

    这是「新的一行商品」与「上一行描述的换行续行」的判据。

    ⚠️ 不能用「这一行有没有数字字符」——实测样本里的续行是
    `olive oil (1 L bottle)`，它**带数字**（那个 1 是容量），按"有数字就是新行"
    会把一个商品拆成两条，其中一条没有任何价格。反过来也不能放宽成"只要像文字
    就并进去"：把带金额的行并进上一行等于悄悄改掉金额。
    所以判据落在「数值列里是不是真有一个数」这件可验证的事上。
    """
    for role in (FIELD_QUANTITY, FIELD_UNIT_PRICE, FIELD_LINE_AMOUNT):
        text = assigned.get(role)
        if text and _NUMERIC_HINT.search(text):
            return True
    return False


def _assign(row: TextRow, columns: tuple[_Column, ...]) -> dict[str, str]:
    """把一行的格子归到各列，返回 `角色 → 文本`。同列多格按 x 顺序拼接。"""
    buckets: dict[str, list[str]] = {}
    for cell in row.cells:
        best: _Column | None = None
        best_score = 0.0
        for column in columns:
            score = column.overlaps(cell)
            if score > best_score:
                best, best_score = column, score
        if best is None or best_score <= 0.0:
            continue
        buckets.setdefault(best.role, []).append(cell.text)
    return {role: " ".join(parts).strip() for role, parts in buckets.items() if parts}


def _field(name: str, text: str | None, *, page: int, box: BoundingBox | None = None):
    """造一个契约字段。

    ⚠️ **文字层里没有 `UNREADABLE`**：要么有字符，要么没有。这和 OCR 不一样——
    OCR 看得见一团墨却认不出，文字层不存在这种状态。所以空值一律 `MISSING`，
    而不是 `UNREADABLE`。把它错标成 `UNREADABLE` 会让审核界面要求人去"补"一个
    纸上本来就没有的东西。
    """
    if text and text.strip():
        return RecognizedField.recognized(
            name, text.strip(), evidence=FieldEvidence(page=page, box=box)
        )
    return RecognizedField.missing(name)


def _document_fields(
    rows: list[TextRow], *, page: int, header_position: int | None
) -> tuple[dict[str, RecognizedField], list[str]]:
    """抽单据级字段：标签右侧那一格，或标签独占一行时取下一行。"""
    notes: list[str] = []
    found, label_notes = _assign_labels(rows)
    notes.extend(label_notes)

    total_text, total_note = _invoice_total(rows, header_position)
    if total_note:
        notes.append(total_note)

    currency = found.get(FIELD_CURRENCY)
    if currency is None:
        # 没有「币种」标签时，从全页文字里找一个币种代码。符号（`$`）**不认**：
        # `$` 可能是 USD、CAD、AUD、HKD，猜一个就是编造（步骤 05 已定「不默认 USD」）。
        joined = " ".join(row.raw for row in rows)
        match = _CURRENCY_PATTERN.search(joined)
        if match:
            found[FIELD_CURRENCY] = match.group(1).upper()
            notes.append(f"币种取自页面上的代码 {match.group(1)!r}（没有「币种」标签）")

    fields = {
        FIELD_SUPPLIER: _field(FIELD_SUPPLIER, found.get(FIELD_SUPPLIER), page=page),
        FIELD_INVOICE_NUMBER: _field(
            FIELD_INVOICE_NUMBER, found.get(FIELD_INVOICE_NUMBER), page=page
        ),
        FIELD_INVOICE_DATE: _field(
            FIELD_INVOICE_DATE, found.get(FIELD_INVOICE_DATE), page=page
        ),
        FIELD_INVOICE_TOTAL: _field(FIELD_INVOICE_TOTAL, total_text, page=page),
        FIELD_CURRENCY: _field(FIELD_CURRENCY, found.get(FIELD_CURRENCY), page=page),
    }
    return fields, notes


def _document_label_of(text: str) -> str | None:
    """这一格是不是一个**纯标签**（整格就是 `Invoice #` 这样的标签词）。"""
    key = _key(text)
    if not key:
        return None
    for name, labels in DOCUMENT_LABELS.items():
        for label in labels:
            if key == _key(label):
                return name
    return None


# `标签: 值` / `标签 值` 挤在同一格的匹配式，按别名长度从长到短试。
# 长的先试是必须的：`Invoice Date: 14.10.2026` 必须命中 `invoice date` 而不是
# 先被 `date` 咬掉一截。
_INLINE_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rf"^\s*{re.escape(label)}\s*[.:：]?\s+(\S.*)$", re.IGNORECASE))
    for name, label in sorted(
        ((name, label) for name, labels in DOCUMENT_LABELS.items() for label in labels),
        key=lambda entry: len(entry[1]),
        reverse=True,
    )
)


def _inline_label(text: str) -> tuple[str, str] | None:
    """把 `Vendor: Lakeview Produce` 这种一格里的「标签 + 值」拆开。

    纯文本单据几乎总是这么写。不拆的话这一格既不是标签也归不到别的标签上，
    于是整个字段白丢；更糟的是实测它会被**下一行的标签**认领——
    `Supplier: Northern Produce` 曾被接到 `Invoice #` 的值前面，
    变成 `Supplier: Northern ProduceNP-091`。
    """
    for name, pattern in _INLINE_LABELS:
        match = pattern.match(text)
        if match:
            value = match.group(1).strip()
            if value:
                return name, value
    return None


# 被渲染器切断的值尾巴长什么样：短的字母数字串，**不含空格**。
# 卡住「不含空格」是关键：`30 days`（Terms 的值）因此不会被接到 Date 后面。
_WRAPPED_TAIL = re.compile(r"[0-9A-Za-z\-/]{1,6}")

# 标签与它的值最多隔几行。1 行是因为实测值可能落在标签**上一行**：
# `Invoice #` 的标签竖直居中在单元格里，而它的值换行成了两行，标签正好夹在中间。
LABEL_ROW_WINDOW = 1


def _assign_labels(rows: list[TextRow]) -> tuple[dict[str, str], list[str]]:
    """把每个非标签格**就近归属**到一个标签，再拼出各标签的值。

    为什么要全局归属，而不是「从标签往右找一格」：实测
    `sample_grocery_supplier_invoices.pdf` 第 2 页的发票号
    `MP-2026-0611-219` 被渲染成**两段**，`MP-2026-0611-21` 在标签的**上一行**、
    `9` 在标签的**下一行**，标签自己夹在中间。往右找一格找不到任何东西；
    而放宽成「上下一行都看」之后，`9` 又会同时够得着下面的 `Date` 标签。

    就近归属一次解决这两件事：每个格子只归给**最近的**那个标签
    （先比行距、再比与标签右缘的水平距离），归完就不会被别的标签抢走。
    """
    notes: list[str] = []
    found: dict[str, str] = {}
    labels: list[tuple[int, TextCell, str]] = []
    inline_cells: set[tuple[int, float]] = set()
    for position, row in enumerate(rows):
        for cell in row.cells:
            name = _document_label_of(cell.text)
            if name is not None:
                labels.append((position, cell, name))
                continue
            pair = _inline_label(cell.text)
            if pair is not None:
                # 「标签 + 值」挤在一格：当场取值，并把这一格从候选里拿掉，
                # 免得它被别的标签认领。
                inline_cells.add((position, cell.left))
                found.setdefault(pair[0], pair[1])
    if not labels:
        return found, notes

    label_cells = {(position, cell.left) for position, cell, _ in labels} | inline_cells
    claims: dict[int, list[tuple[int, TextCell]]] = {}
    for position, row in enumerate(rows):
        for cell in row.cells:
            if (position, cell.left) in label_cells:
                continue
            best_index: int | None = None
            best_key: tuple[int, float] | None = None
            for index, (label_row, label_cell, _) in enumerate(labels):
                if abs(position - label_row) > LABEL_ROW_WINDOW:
                    continue
                if cell.left < label_cell.left:
                    continue
                key = (abs(position - label_row), abs(cell.left - label_cell.right))
                if best_key is None or key < best_key:
                    best_index, best_key = index, key
            if best_index is not None:
                claims.setdefault(best_index, []).append((position, cell))

    for index, (label_row, label_cell, name) in enumerate(labels):
        if name in found:
            continue
        assigned = claims.get(index) or []
        if not assigned:
            continue
        primary_position, primary = min(
            assigned,
            key=lambda entry: (
                abs(entry[0] - label_row),
                abs(entry[1].left - label_cell.right),
            ),
        )
        # 尾巴只接**primary 下方**的短串：换行是往下续的，往上接等于凭空改值。
        tails = [
            (position, cell)
            for position, cell in assigned
            if position > primary_position and _WRAPPED_TAIL.fullmatch(cell.text)
        ]
        tails.sort(key=lambda entry: (entry[0], entry[1].left))
        value = primary.text.strip()
        if tails:
            joined = value + "".join(cell.text.strip() for _, cell in tails)
            notes.append(
                f"文字层把 {name} 的值拆成了 {1 + len(tails)} 段，已按版式接回："
                f"{value!r} + "
                + " + ".join(repr(cell.text.strip()) for _, cell in tails)
                + f" → {joined!r}"
            )
            value = joined
        if value:
            found[name] = value
    return found, notes


def _invoice_total(
    rows: list[TextRow], header_position: int | None
) -> tuple[str | None, str | None]:
    """按标签优先级取单据总额。**绝不相加**，找不到就返回 None。"""
    best: tuple[int, str, str] | None = None
    start = 0 if header_position is None else header_position
    for row in rows[start:]:
        priority = _total_priority(row)
        if priority is None:
            continue
        value = _trailing_number(row)
        if value is None:
            continue
        if best is None or priority < best[0]:
            best = (priority, value, row.first_text)
    if best is None:
        return None, (
            "单据上没有找到总额标签——`invoice_total` 留空。"
            "⚠️ **不会把各行金额相加填进去**：那样评测会奖励编造，对账也会永远"
            "显示差额为 0。"
        )
    return best[1], f"单据总额取自标签 {best[2]!r}（优先级 {best[0]}）"


def _trailing_number(row: TextRow) -> str | None:
    """取一行里最右边那个数。汇总行的数值总在最右边。

    纯文本单据常把标签和值挤在一格（`Grand total 252.55`），所以还要从格子文本里
    把最后一个数抠出来——否则会把 `Grand total 252.55` 整串当成总额原文，
    后面的规范化一定失败，总额就白丢了。
    """
    for cell in reversed(row.cells):
        text = cell.text.strip()
        if not _NUMERIC_HINT.search(text):
            continue
        match = _LAST_NUMBER.search(text)
        return match.group(0).strip() if match else text
    return None


def _build_lines(
    rows: list[TextRow], header_position: int, columns: tuple[_Column, ...]
) -> tuple[list[RecognizedLine], list[dict[str, str]], list[str]]:
    """产出商品行、与之一一对应的额外列、以及改动说明。

    行与额外列**必须在同一个循环里产出**：拆成两个循环时，任何一边的收口条件
    改了另一边忘了改，就会出现「第 3 行的包装规格挂到了第 4 行」这种错位，
    而且不报错。
    """
    notes: list[str] = []
    lines: list[RecognizedLine] = []
    extras: list[dict[str, str]] = []
    pending: dict[str, str] | None = None
    pending_row: TextRow | None = None

    def flush() -> None:
        nonlocal pending, pending_row
        if pending is None or pending_row is None:
            return
        lines.append(_line_from(pending, pending_row, len(lines) + 1))
        extras.append(
            {
                name: pending[name]
                for name in (EXTRA_FIELD_PACK_SPEC, EXTRA_FIELD_SUPPLIER_CODE)
                if pending.get(name)
            }
        )
        pending, pending_row = None, None

    has_unit_column = any(column.role == FIELD_UNIT for column in columns)
    header_delimited = rows[header_position].delimited

    for row in rows[header_position + 1 :]:
        if _total_priority(row) is not None:
            # 总额行 = 明细表结束。之后的页脚、签收栏不再产出行。
            flush()
            notes.append(f"明细表在第 {row.index} 行（总额行）收口，之后的内容不产出商品行")
            break
        if _is_summary(row):
            flush()
            continue
        # 切分方式与表头不同的行（竖线表里夹一行没竖线的文字）**没法按列归位**。
        # 它只能是上一行的换行续行；没有上一行可接就丢掉，不硬归列。
        if row.delimited != header_delimited:
            assigned = {}
        else:
            assigned = _assign(row, columns)
        if pending is not None and not _has_numeric_role(assigned):
            extra = assigned.get(FIELD_ITEM_DESCRIPTION) or row.raw
            if extra.strip():
                pending[FIELD_ITEM_DESCRIPTION] = (
                    f"{pending.get(FIELD_ITEM_DESCRIPTION, '')} {extra}".strip()
                )
                notes.append(
                    f"第 {row.index} 行的数值列里一个数都没有，按上一行描述的换行"
                    f"续行接到一起：{extra.strip()!r}"
                )
            continue
        if not assigned or not any(assigned.values()):
            continue
        # 数值列全空、又没有上一行可接：当作版式噪声（页眉、备注），不产出行。
        if not _has_numeric_role(assigned):
            continue
        if not has_unit_column:
            unit_note = _split_unit_from_quantity(assigned, row.index)
            if unit_note:
                notes.append(unit_note)
        flush()
        pending, pending_row = assigned, row
    flush()
    return lines, extras, notes


def _split_unit_from_quantity(assigned: dict[str, str], row_index: int) -> str | None:
    """表头没有单位列时，从数量格的尾部取出单位。

    真实单据上 `74 lbs.`、`19.25 kg`、`24 bunches`、`72 heads` 都写在同一格里。
    丢掉这个单位的代价是步骤 09 没法换算，而且已入账的采购记录看不出计价单位。

    **`quantity` 的原文不动**（`raw_text` 仍是 `74 lbs.`，那是纸面事实）；
    这里只是**另外**把单位也填上，并留一条说明。数值那边由
    `normalization.normalize_number_text` 把尾部单位剥掉，同样留痕。
    """
    quantity = assigned.get(FIELD_QUANTITY, "")
    if not quantity or assigned.get(FIELD_UNIT):
        return None
    match = re.fullmatch(r"\s*([\d.,]+)\s*([A-Za-z一-鿿.]{1,12})\s*", quantity)
    if not match:
        return None
    unit = match.group(2).strip(" .")
    if not unit:
        return None
    assigned[FIELD_UNIT] = unit
    return (
        f"第 {row_index} 行：表头没有单位列，单位 {unit!r} 取自数量格 "
        f"{quantity!r} 的尾部（数量原文保持不变）"
    )


def _line_from(assigned: dict[str, str], row: TextRow, index: int) -> RecognizedLine:
    fields = [
        _field(name, assigned.get(name), page=row.page)
        for name in (
            FIELD_ITEM_DESCRIPTION,
            FIELD_QUANTITY,
            FIELD_UNIT,
            FIELD_UNIT_PRICE,
            FIELD_LINE_AMOUNT,
        )
    ]
    return RecognizedLine(index=index, page=row.page, raw_text=row.raw, fields=fields)


def _header_hint(rows: list[TextRow]) -> str:
    """说清「为什么找不到表头」，而不是只说找不到。

    排查这条提示的人要能直接知道下一步做什么，不然就只能来读代码。
    """
    if not rows:
        return "（这一页一个文本片段都没有。）"
    multi = sum(1 for row in rows if len(row.cells) > 1)
    if multi == 0:
        return (
            "（每一行都只切出一格：这份文本既没有竖线，也没有连续 2 个以上空格对齐"
            "的列，因此无法切列。单个空格不能当分隔符——商品名里本来就有空格。）"
        )
    return (
        f"（切出了多格的行有 {multi} 行，但没有一行的列名能对上。"
        "若这份单据的表头用的是别的说法，需要把它加进 COLUMN_ALIASES。）"
    )


def _parse(rows: list[TextRow], *, page: int, page_text: str, source_key: str | None) -> TextLayerParse:
    header = _find_header(rows)
    header_position = header[0] if header else None
    document_fields, notes = _document_fields(rows, page=page, header_position=header_position)

    lines: list[RecognizedLine] = []
    extras: list[dict[str, str]] = []
    reason = ""
    if header is None:
        reason = (
            "文字层里找不到可识别的表头行（需要至少 "
            f"{MIN_HEADER_MATCHES} 个能对上的列名，且含描述/单价/金额之一）。"
            "**因此不产出任何商品行**——没有列映射就没法知道哪个数字是数量、"
            "哪个是单价，猜一次就是一个错价格永久留在历史里。"
            + _header_hint(rows)
        )
        notes.append(reason)
    else:
        position, columns = header
        lines, extras, line_notes = _build_lines(rows, position, columns)
        notes.extend(line_notes)
        notes.append(
            "列映射："
            + "、".join(f"{column.role}" for column in columns)
        )

    result = RecognitionResult(
        provider=TEXT_LAYER_PROVIDER,
        is_fake=False,
        model=None,
        recognized_at=datetime.now(timezone.utc),
        pages=(RecognizedPage(number=page, text=page_text or None),),
        document_fields=document_fields,
        lines=tuple(lines),
        raw_response=None,
        warnings=tuple(notes),
        source_key=source_key,
    )
    return TextLayerParse(
        result=result,
        header_found=header is not None,
        reason=reason,
        notes=tuple(notes),
        extra_fields=tuple(extras),
    )


def parse_text_layout(
    fragments: list[TextFragment],
    *,
    page: int,
    page_width: float,
    page_height: float,
    source_key: str | None = None,
) -> TextLayerParse:
    """解析数字 PDF 一页的文字层。"""
    rows = fragments_to_rows(fragments, page_height=page_height)
    page_text = "\n".join(row.raw for row in rows)
    parse = _parse(rows, page=page, page_text=page_text, source_key=source_key)
    logger.info(
        "text layer parsed page=%s rows=%s header=%s lines=%s",
        page,
        len(rows),
        parse.header_found,
        len(parse.result.lines),
    )
    return parse


def parse_text_rows(
    text: str, *, page: int = 1, source_key: str | None = None
) -> TextLayerParse:
    """解析纯文本版式（合成回归样本、旧 OCR 输出）。"""
    rows = text_to_rows(text, page=page)
    return _parse(rows, page=page, page_text=text, source_key=source_key)


def page_box(left: float, top: float, right: float, bottom: float, *, width: float, height: float) -> BoundingBox | None:
    """把页面点坐标换成比例坐标。越界或零宽高返回 None，**不夹到边界**。"""
    if width <= 0 or height <= 0 or right <= left or bottom <= top:
        return None
    try:
        return BoundingBox(
            left=max(0.0, left / width),
            top=max(0.0, top / height),
            width=min(1.0, (right - left) / width),
            height=min(1.0, (bottom - top) / height),
            space=CoordinateSpace.RATIO,
        )
    except ValueError:
        return None


__all__ = [
    "COLUMN_ALIASES",
    "DOCUMENT_LABELS",
    "EXTRA_FIELD_PACK_SPEC",
    "EXTRA_FIELD_SUPPLIER_CODE",
    "MIN_HEADER_MATCHES",
    "SUMMARY_LABELS",
    "TEXT_LAYER_PROVIDER",
    "TOTAL_LABEL_PRIORITY",
    "TextCell",
    "TextLayerParse",
    "TextRow",
    "fragments_to_rows",
    "page_box",
    "parse_text_layout",
    "parse_text_rows",
    "text_to_rows",
]
