"""数值与日期的规范化：**一份口径，两个调用方，每一步都留痕**（步骤 08.3e）。

### 为什么要有这个模块

步骤 06 已经把「表格里的一格文本怎么变成精确数值」想清楚了：货币符号剥掉、括号
记负、**逗号含义不明时拒绝而不是猜**、日/月顺序不明时拒绝。步骤 08 的 OCR 通路
需要**同一套**规则——否则同一张单据走表格导入和走拍照识别会得到不同的数值，
而这种不一致不会报错。OCR 单据日期可单独启用两位年份转换：本应用只接受
2000 年起的采购日期，且月/日顺序能确定时才转换；表格导入的默认规则保持严格。

于是把纯文本的那部分抽到这里，`services/spreadsheet_parsing.py` 与
`services/extraction.py` 都调它。步骤 04 的 `parse_decimal` 仍是最后一道闸门
（scale 超限**拒绝而不是舍入**），本模块只负责「把人写的写法变成 parse_decimal
认得的写法」。

### 两个调用方要的失败方式不一样

- **表格导入**（步骤 06）：解析不了就**报错 400**，让人选格式再传一次。
  整批导入是一个事务，一行坏了不该把半张表写进去。
- **OCR 提取**（步骤 08）：解析不了**不能报错**。单据已经上传、草稿已经建好，
  这一格认不出就该**留空 + 挂一条提示**，等人填——报 400 会把整张单据挡在门外，
  而 `imp_notes.md` 4.7 明确「不应因为一两个模糊商品而阻止其他正确商品的保存」。

所以这里的函数**返回 `(值, 改动说明, 失败原因)` 而不抛异常**，由调用方决定是
`fail()` 还是挂提示。

### 改动说明是给人看的，不是给日志看的

`notes` 里每一条都是一次**人看得见的转换**：
`全角转半角：'1，040.00' → '1,040.00'`。步骤 07 实测同一张图换个提示词，
`1,040.00` 会变成全角逗号的 `1，040.00`；步骤 04 的 `parse_decimal` 明确拒绝千分位
与全角数字，所以 OCR 原文**不能直接喂进确认流程**。转换必须发生，但必须看得见，
而且 `raw_text` 原样保留。

### `auto`：文本本身能定死就接受，定不死就拒绝

步骤 06 给了三种数字格式（`strict` / `decimal_dot` / `decimal_comma`），默认
`strict`——有逗号就拒绝，让人显式选。OCR 通路没有「让人先选格式」这一步
（照片是拍完就传的），所以这里多一个 `auto`：

| 文本 | 判定 | 理由 |
| --- | --- | --- |
| `6.39` | 6.39 | 点后 2 位，不可能是千分位组（千分位组必须恰好 3 位） |
| `$1,991.38` | 1991.38 | 两种分隔符都在，点在后 → 点是小数点，逗号组合法 |
| `€1.234,56` | 1234.56 | 逗号在后 → 逗号是小数点，点组合法 |
| `0,00` | 0.00 | 逗号后 2 位，不可能是千分位组 |
| `1,234` | **拒绝** | 美式读作 1234、欧式读作 1.234，**文本本身定不死** |
| `1.234` | **拒绝** | 同上，差 1000 倍 |

⚠️ 最后两行是本模块最要紧的地方。`1,234` 猜错一次就是一个错价格永久留在历史里，
而且不报错。宁可留空让人填。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field as dataclass_field
from datetime import date
from typing import Final

# 支持的数字格式。`auto` 只给 OCR 通路用，表格导入保持 `strict` 默认不变。
NUMBER_FORMAT_STRICT = "strict"
NUMBER_FORMAT_DOT = "decimal_dot"
NUMBER_FORMAT_COMMA = "decimal_comma"
NUMBER_FORMAT_AUTO = "auto"
NUMBER_FORMATS = (
    NUMBER_FORMAT_STRICT,
    NUMBER_FORMAT_DOT,
    NUMBER_FORMAT_COMMA,
    NUMBER_FORMAT_AUTO,
)

DATE_ORDER_STRICT = "strict"
DATE_ORDER_MDY = "mdy"
DATE_ORDER_DMY = "dmy"
DATE_ORDERS = (DATE_ORDER_STRICT, DATE_ORDER_MDY, DATE_ORDER_DMY)

# 币种代码前后缀与货币符号。剥掉它们不改变数值，所以不算"猜"。
_CURRENCY_CODE = re.compile(
    r"^(?:USD|CNY|RMB|EUR|GBP|CAD|AUD|JPY)\s*|\s*(?:USD|CNY|RMB|EUR|GBP|CAD|AUD|JPY)$",
    re.IGNORECASE,
)
_CURRENCY_SYMBOL = re.compile(r"^[$¥€£￥]\s*")

# 尾部计量单位。`12.00/kg`、`19.25 kg`、`74 lbs.` 这类写法在真实单据上到处都是。
# ⚠️ 剥掉的单位**不丢**：调用方会把它记进 notes，而原文始终保留在 raw_text 里。
#
# ⚠️ **这张表必须覆盖项目认识的全部单位写法**，由
# `tests/test_normalization.py::test_trailing_unit_words_cover_the_unit_vocabulary`
# 断言（它把这里与 `services/ingredients.py` 的两张单位表逐项比对）。
#
# 为什么要有这条断言：步骤 09 的真实数据实跑暴露了漏项的后果——
# `samples/real/sample_grocery_supplier_invoices.pdf` 的数量格里写着 `72 heads`、
# `24 bunches`、`16 gal.`、`90 dozen`、`12 qt.`、`48 cups`、`48 cans`，而原来的表
# 只有 lb / kg / g / each 那几个，于是 **29 行真实单据里有 7 行的数量被判成"认不出"**，
# 价格一条也算不出来。这不是报错，是悄悄少了 7 行——正是本项目最该防的那一类。
#
# 不从 `services/ingredients.py` import 过来，是因为本模块被 `documents/` 与
# 表格解析使用，它们都不该经由服务层把数据库依赖拉进来。一致性靠测试保证，
# 不靠人记得（同 `units.py` 的 `PACKAGING_UNIT_TOKENS`）。
_TRAILING_UNIT_WORDS: Final[tuple[str, ...]] = (
    # 质量
    "lb", "lbs", "pound", "pounds", "磅", "斤",
    "oz", "ounce", "ounces",
    "g", "gm", "gram", "grams", "克",
    "kg", "kgs", "kilogram", "kilograms", "公斤", "千克",
    # 体积
    "gal", "gallon", "gallons",
    "qt", "quart", "quarts",
    "pt", "pint", "pints",
    "fl oz", "floz", "fl_oz", "fluid ounce", "fluid ounces",
    "l", "liter", "liters", "litre", "litres", "升",
    "ml", "milliliter", "milliliters", "millilitre", "毫升",
    # 计数
    "ea", "each", "pc", "pcs", "piece", "pieces", "个", "只",
    "head", "heads", "bunch", "bunches", "颗", "把",
    "doz", "dozen", "dozens", "打",
    # 包装（单据的数量格里同样会写 `20 箱`、`48 cans`）
    "case", "cases", "cs", "pack", "packs", "pk", "package",
    "box", "boxes", "carton", "cartons", "bag", "bags",
    "can", "cans", "cup", "cups", "jar", "jars", "bottle", "bottles",
    "箱", "包", "袋", "件", "盒", "罐", "杯", "瓶",
)

# 按长度倒序拼接：否则 `lb` 会先吃掉 `lbs` 的前两个字母，剩一个 `s` 让整串解析失败。
_TRAILING_UNIT = re.compile(
    r"(?i)\s*(?:/\s*)?(?:"
    + "|".join(
        re.escape(word)
        for word in sorted(_TRAILING_UNIT_WORDS, key=len, reverse=True)
    )
    + r")\.?\s*$"
)

# 千分位写法：整数部分是 1—3 位，后面跟着若干组恰好 3 位。
_DOT_THOUSANDS = re.compile(r"^\d{1,3}(?:\.\d{3})+$", re.ASCII)
_COMMA_THOUSANDS = re.compile(r"^\d{1,3}(?:,\d{3})+$", re.ASCII)

_PLAIN_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$", re.ASCII)

_MONTH_ABBR = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        1,
    )
}
_MONTH_FULL = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


@dataclass(frozen=True)
class Normalized:
    """一次规范化的结果。

    `text` 为 None 表示**没能规范化**，此时 `problem` 说明原因。
    `notes` 无论成功失败都要保留——「为什么没认出来」和「怎么认出来的」同样要
    让人看得见。
    """

    text: str | None
    notes: tuple[str, ...] = dataclass_field(default_factory=tuple)
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.text is not None


def fold_width(text: str) -> tuple[str, list[str]]:
    """全角转半角（NFKC）。转了就留一条说明。

    步骤 07 实测：同一张图换个提示词，`1,040.00` 会变成全角逗号的 `1，040.00`。
    这是**必须发生但必须看得见**的那类转换。
    """
    folded = unicodedata.normalize("NFKC", text)
    if folded == text:
        return text, []
    return folded, [f"全角转半角：{text!r} → {folded!r}"]


def normalize_number_text(
    raw: str,
    *,
    number_format: str = NUMBER_FORMAT_AUTO,
    strip_trailing_unit: bool = True,
) -> Normalized:
    """把一个数值原文变成 `parse_decimal` 认得的写法。

    返回的 `text` 仍是**字符串**：转成 Decimal 是步骤 04 `parse_decimal` 的事，
    scale 超限要在那里被拒绝，不在这里偷偷舍入。

    `strip_trailing_unit` 在两个调用方取不同值，这个差别是**有理由的**，不是
    两套口径：
    - OCR 通路取 True。真实单据的数量格子里就写着 `74 lbs.`、`12.00/kg`——
      单位和数字挤在一格是纸面事实，拒绝它等于拒绝这张单据。
    - 表格导入取 False。表格有**独立的单位列**，所以数量格里出现 `kg` 说明列
      映射错了，这时报错让人改映射，比默默吃掉单位正确。
    """
    if number_format not in NUMBER_FORMATS:
        raise ValueError(f"number_format={number_format!r} 无效，可选：{NUMBER_FORMATS}")

    notes: list[str] = []
    text = (raw or "").strip()
    if not text:
        return Normalized(None, (), "空字符串")

    text, width_notes = fold_width(text)
    notes.extend(width_notes)
    text = text.strip()

    # Unicode 减号、全角减号都当负号。NFKC 已经处理了一部分，这里补 U+2212。
    if "−" in text:
        text = text.replace("−", "-")
        notes.append("Unicode 减号 U+2212 转 ASCII '-'")

    if "%" in text:
        return Normalized(None, tuple(notes), "带百分号的值不是采购数量或金额")

    stripped = _CURRENCY_CODE.sub("", text).strip()
    if stripped != text:
        notes.append(f"去掉币种代码：{text!r} → {stripped!r}")
        text = stripped
    stripped = _CURRENCY_SYMBOL.sub("", text).strip()
    if stripped != text:
        notes.append(f"去掉货币符号：{text!r} → {stripped!r}")
        text = stripped
    if strip_trailing_unit:
        stripped = _TRAILING_UNIT.sub("", text).strip()
        if stripped != text:
            notes.append(
                f"去掉尾部单位：{text!r} → {stripped!r}（单位本身保留在原文里）"
            )
            text = stripped

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
        notes.append("括号记负")
        if text.startswith(("-", "+")):
            return Normalized(None, tuple(notes), "括号负数不能同时带正负号")

    sign = ""
    if text.startswith(("+", "-")):
        sign, text = ("-" if text[0] == "-" else ""), text[1:].strip()

    digits, separator_notes, problem = _resolve_separators(text, number_format)
    notes.extend(separator_notes)
    if digits is None:
        return Normalized(None, tuple(notes), problem)

    if negative:
        sign = "-"
    candidate = f"{sign}{digits}"
    if not _PLAIN_NUMBER.fullmatch(candidate):
        return Normalized(
            None, tuple(notes), f"去掉符号与分隔符之后仍不是一个数：{candidate!r}"
        )
    return Normalized(candidate, tuple(notes))


def _resolve_separators(
    text: str, number_format: str
) -> tuple[str | None, list[str], str | None]:
    """决定 `.` 与 `,` 各自是小数点还是千分位，返回只含数字与一个小数点的写法。"""
    notes: list[str] = []
    has_dot = "." in text
    has_comma = "," in text

    if not has_dot and not has_comma:
        return text, notes, None

    if number_format == NUMBER_FORMAT_DOT:
        if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text, re.ASCII):
            return None, notes, "数字不符合「小数点为 . 、千分位为 ,」的格式"
        if has_comma:
            notes.append(f"按 decimal_dot 去掉千分位逗号：{text!r}")
        return text.replace(",", ""), notes, None

    if number_format == NUMBER_FORMAT_COMMA:
        if not re.fullmatch(r"(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?", text, re.ASCII):
            return None, notes, "数字不符合「小数点为 , 、千分位为 .」的格式"
        notes.append(f"按 decimal_comma 把 , 当小数点、. 当千分位：{text!r}")
        return text.replace(".", "").replace(",", "."), notes, None

    if number_format == NUMBER_FORMAT_STRICT:
        if has_comma:
            return (
                None,
                notes,
                "逗号含义不明确（美式千分位还是欧式小数点），请显式选择数字格式",
            )
        return text, notes, None

    # --- auto：只在文本本身能定死时才接受 ---------------------------------
    if has_dot and has_comma:
        last_dot = text.rfind(".")
        last_comma = text.rfind(",")
        if last_dot > last_comma:
            if not _COMMA_THOUSANDS.fullmatch(text[:last_dot]):
                return (
                    None,
                    notes,
                    f"两种分隔符都出现，但逗号不在合法的千分位位置：{text!r}",
                )
            notes.append(f"点在逗号之后 → 点是小数点、逗号是千分位：{text!r}")
            return text.replace(",", ""), notes, None
        if not _DOT_THOUSANDS.fullmatch(text[:last_comma]):
            return (
                None,
                notes,
                f"两种分隔符都出现，但点不在合法的千分位位置：{text!r}",
            )
        notes.append(f"逗号在点之后 → 逗号是小数点、点是千分位：{text!r}")
        return text[:last_comma].replace(".", "") + "." + text[last_comma + 1 :], notes, None

    separator = "." if has_dot else ","
    head, _, tail = text.rpartition(separator)
    if text.count(separator) > 1:
        # 多个同种分隔符只可能是千分位（`1.234.567`）。但那样整个数就没有小数部分，
        # 而"没有小数部分"在另一种约定下读法完全不同 → 定不死，拒绝。
        return (
            None,
            notes,
            f"出现多个 {separator!r}，两种约定下读法不同，文本本身定不死：{text!r}",
        )
    if len(tail) == 3 and _is_digits(head) and _is_digits(tail) and 1 <= len(head) <= 3:
        return (
            None,
            notes,
            (
                f"{text!r} 在美式约定下读作 {head + tail}、在欧式约定下读作 "
                f"{head}.{tail}，差 1000 倍，文本本身定不死——请人工填写"
            ),
        )
    if not _is_digits(head) or not _is_digits(tail):
        return None, notes, f"分隔符两侧不是纯数字：{text!r}"
    if separator == ",":
        notes.append(
            f"逗号后只有 {len(tail)} 位（千分位组必须恰好 3 位）→ 逗号是小数点：{text!r}"
        )
        return f"{head}.{tail}", notes, None
    return text, notes, None


def _is_digits(text: str) -> bool:
    return bool(text) and text.isdigit() and text.isascii()


def normalize_date_text(
    raw: str, *, date_order: str = DATE_ORDER_STRICT, allow_short_year: bool = False
) -> Normalized:
    """把一个日期原文变成 `YYYY-MM-DD`。认不出就返回 None，**绝不编造日期**。

    认不出日期的代价已经被设计掉了：步骤 04 允许 `purchase_date` 为空，
    `undated=true` 能筛出「待补日期」清单。编一个日期的代价是那笔采购被算进了错
    月份的均价，而且不报错。
    """
    if date_order not in DATE_ORDERS:
        raise ValueError(f"date_order={date_order!r} 无效，可选：{DATE_ORDERS}")

    notes: list[str] = []
    text = (raw or "").strip()
    if not text:
        return Normalized(None, (), "空字符串")
    text, width_notes = fold_width(text)
    notes.extend(width_notes)
    text = text.strip()

    iso = re.fullmatch(r"(\d{4})(?:[-/.年])(\d{1,2})(?:[-/.月])(\d{1,2})日?", text)
    if iso:
        return _build_date(*(int(part) for part in iso.groups()), notes=notes, raw=raw)

    numeric = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", text)
    if numeric:
        first_text, second_text, year_text = numeric.groups()
        first, second = int(first_text), int(second_text)
        if len(year_text) == 2 and not allow_short_year:
            return Normalized(None, tuple(notes), f"{text!r} 使用两位年份，请填写完整年份")
        # 本应用只接收 2000 年起的采购日期；未来日期仍由保存时的日期校验拒绝。
        year = 2000 + int(year_text) if len(year_text) == 2 else int(year_text)
        if len(year_text) == 2:
            notes.append(f"两位年份 {year_text} 按 {year} 年读取")
        if date_order == DATE_ORDER_STRICT and first <= 12 and second <= 12 and first != second:
            return Normalized(
                None,
                tuple(notes),
                f"{text!r} 的日/月顺序不明确（{first}/{second} 两种读法都成立），"
                "请人工填写——猜错会把这笔采购算进错误的月份",
            )
        if date_order == DATE_ORDER_DMY or (date_order == DATE_ORDER_STRICT and first > 12):
            day, month = first, second
            notes.append(f"按「日/月/年」读 {text!r}")
        else:
            month, day = first, second
            notes.append(f"按「月/日/年」读 {text!r}")
        return _build_date(year, month, day, notes=notes, raw=raw)

    english = re.fullmatch(
        r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text
    )
    reverse = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})", text)
    if english or reverse:
        if english:
            month_name, day_text, year_text = english.groups()
        else:
            day_text, month_name, year_text = reverse.groups()  # type: ignore[union-attr]
        name = month_name.lower()
        if name not in _MONTH_ABBR and name not in _MONTH_FULL and name != "sept":
            return Normalized(None, tuple(notes), f"认不出英文月份 {month_name!r}")
        notes.append(f"英文月份 {month_name!r} → {_MONTH_ABBR[name[:3]]}")
        return _build_date(
            int(year_text), _MONTH_ABBR[name[:3]], int(day_text), notes=notes, raw=raw
        )

    return Normalized(None, tuple(notes), f"认不出日期写法 {raw!r}")


@dataclass(frozen=True)
class DateReading:
    """一个日期原文的**一种**读法。

    `order` 是这条读法按什么顺序读出来的：`strict` 表示文本本身就定死了
    （ISO、英文月份、或者第一个数大于 12），`mdy` / `dmy` 表示日/月顺序**定不死**，
    这是其中一种可能。
    """

    text: str
    """ISO 日期文本 `YYYY-MM-DD`。"""

    order: str
    """`strict` / `mdy` / `dmy`，取自本模块的 `DATE_ORDER_*`。"""

    note: str = ""


@dataclass(frozen=True)
class DateReadings:
    """一个日期原文**所有能成立的读法**。

    与 `normalize_date_text` 的关系：那个函数回答「能不能定死一个日期」，定不死就
    拒绝；本函数回答「定不死的时候，到底有哪几种可能」。

    两者的使用场合不同，所以不是两套口径：
    - **要写进采购记录的日期**用 `normalize_date_text`——宁可留空让人填，也不猜。
    - **整理阶段的分类候选**用本函数——`6/11/2026` 的两种可能都摆出来让人挑一个，
      比留一个空格子有用得多，而且**两条都只是候选，没有一条会自己变成决定**
      （docs/multifiles.md 第 4 节）。

    `ambiguous` 为 True 时 `readings` 恰好有两条（mdy 与 dmy），顺序是 mdy 在前
    ——⚠️ 这个顺序**不表示 mdy 更可能**，只是为了让界面有稳定次序。谁对要么看
    纸面上别的线索，要么等这家商家的 `date_order` 定下来。
    """

    readings: tuple[DateReading, ...] = dataclass_field(default_factory=tuple)
    ambiguous: bool = False
    notes: tuple[str, ...] = dataclass_field(default_factory=tuple)
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.readings)

    @property
    def certain(self) -> DateReading | None:
        """唯一一条读法；有歧义或读不出来时是 None。"""
        return self.readings[0] if len(self.readings) == 1 else None


def date_text_readings(
    raw: str, *, date_order: str = DATE_ORDER_STRICT, allow_short_year: bool = False
) -> DateReadings:
    """一个日期原文能成立的全部读法。**一个字都不猜，但也不把歧义丢掉。**

    三种结果：

    | 原文 | `date_order` | 结果 |
    | --- | --- | --- |
    | `2026-11-08` | 任意 | 一条读法，`ambiguous=False` |
    | `6/11/2026` | `strict` | **两条**读法（6 月 11 日 / 11 月 6 日），`ambiguous=True` |
    | `6/11/2026` | `mdy` | 一条读法（6 月 11 日）——这家商家说过它写月/日/年 |
    | `这是什么` | 任意 | 零条读法，`problem` 说明原因 |

    ⚠️ **`date_order` 只能来自人工确认过的商家**（docs/multifiles.md M05）：拿一个
    还没人确认的商家候选去套它的日/月顺序，等于用相似度决定了一个日历日，而那种
    错误不报错——它只是把这笔采购算进了错误的月份。
    """
    if date_order not in DATE_ORDERS:
        raise ValueError(f"date_order={date_order!r} 无效，可选：{DATE_ORDERS}")

    direct = normalize_date_text(
        raw, date_order=date_order, allow_short_year=allow_short_year
    )
    if direct.ok and direct.text is not None:
        return DateReadings(
            readings=(DateReading(direct.text, date_order, _last_note(direct.notes)),),
            notes=direct.notes,
        )

    # 读不出来的原因可能是「日/月顺序定不死」，也可能是「根本不是个日期」。
    # 前者有两种可能要摆出来，后者一条也没有——所以这里不看错误文案，直接把两种
    # 顺序各试一次：两种都成立才是歧义。
    if date_order != DATE_ORDER_STRICT:
        return DateReadings(notes=direct.notes, problem=direct.problem)

    as_mdy = normalize_date_text(
        raw, date_order=DATE_ORDER_MDY, allow_short_year=allow_short_year
    )
    as_dmy = normalize_date_text(
        raw, date_order=DATE_ORDER_DMY, allow_short_year=allow_short_year
    )
    if not (as_mdy.ok and as_dmy.ok) or as_mdy.text == as_dmy.text:
        # 一种顺序都读不出来（不是日期写法、两位年份、日期不存在……），或者两种
        # 顺序读出同一天（`3/3/2026`）——后者在 strict 下本来就不该被拒，真被拒了
        # 也说明问题不在日/月顺序上。原因原样透出。
        return DateReadings(notes=direct.notes, problem=direct.problem)

    assert as_mdy.text is not None and as_dmy.text is not None
    return DateReadings(
        readings=(
            DateReading(as_mdy.text, DATE_ORDER_MDY, f"按「月/日/年」读 {raw!r}"),
            DateReading(as_dmy.text, DATE_ORDER_DMY, f"按「日/月/年」读 {raw!r}"),
        ),
        ambiguous=True,
        notes=tuple(direct.notes) + (
            f"{raw!r} 的日/月顺序文本本身定不死，两种读法都留着等人确认；"
            "确认商家并给这家设 date_order 之后可以用保存的原文重新解析。",
        ),
        problem=direct.problem,
    )


def _last_note(notes: tuple[str, ...]) -> str:
    """取最后一条改动说明当这条读法的说明。没有就是空串（ISO 原文不需要解释）。"""
    return notes[-1] if notes else ""


def _build_date(
    year: int, month: int, day: int, *, notes: list[str], raw: str
) -> Normalized:
    try:
        value = date(year, month, day)
    except ValueError as error:
        return Normalized(None, tuple(notes), f"{raw!r} 不是一个存在的日期：{error}")
    return Normalized(value.isoformat(), tuple(notes))


__all__ = [
    "DATE_ORDERS",
    "DATE_ORDER_DMY",
    "DATE_ORDER_MDY",
    "DATE_ORDER_STRICT",
    "NUMBER_FORMATS",
    "NUMBER_FORMAT_AUTO",
    "NUMBER_FORMAT_COMMA",
    "NUMBER_FORMAT_DOT",
    "NUMBER_FORMAT_STRICT",
    "DateReading",
    "DateReadings",
    "Normalized",
    "date_text_readings",
    "fold_width",
    "normalize_date_text",
    "normalize_number_text",
]
