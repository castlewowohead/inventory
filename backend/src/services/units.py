"""单位与包装规格换算（步骤 09）。

职责与边界：

- **纯计算，不碰数据库、不碰 Flask**。本模块只回答两个问题：「这两个单位能不能
  换、换过去是多少」与「这段包装规格原文能读出什么」。哪条箱规适用于哪一行采购
  记录、换不出来的行怎么呈现，是 `services/prices.py` 的事。
- **全程 `Decimal`，不经 `float`**。换算系数都是精确值（1 lb = 453.59237 g 是定义，
  不是测量），所以除了真正除不尽的那一步，运算结果都是精确的。舍入只发生在输出，
  由调用方决定（见 `services/prices.py`）。
- **不认识就报错，不猜**。`docs/imp_notes.md` Feature 2 要求「不要在没有密度等必要
  信息的情况下把 gallon 与 lb 互转」；本模块把这条做成**异常**而不是返回一个近似值。

### 三条口径（步骤 09 定下，后续步骤引用这里）

1. **制式写明是美制**。`gallon` / `qt` / `pt` / `fl_oz` 一律按**美制液量**
   （1 US gallon = 3.785411784 L，定义值）。英制 gallon 比它大约 20%，同一个
   单词在两种制式下差 20% 却都"看起来合理"，所以必须在代码里写死一种并说出来。
   以后真要支持英制，加的是 `imp_gallon` 这样**另一个 token**，不是改这个的系数。
2. **包装单位不是计量单位**。`case` / `pack` / `box` / `carton` / `bag` 不在换算表
   里：没有「一箱等于多少」这个事实之前，一箱不能换成任何基础单位。它们在这里只
   有一个集合（`PACKAGING_UNIT_TOKENS`），用于让调用方判断「这一行要先去查箱规」。
3. **同单位也要校验输入**。转写来源（`kitchen-ledger/web/recipes.js` 的 `convert`）
   在 `from === to` 时直接 `return Number(quantity)`，于是 `convert(2,'case','case')`
   与 `convert(2,'lbb','lbb')` 都会返回 2——**拼错的单位和包装单位都被放过去了**。
   本实现把这条短路去掉：两边都必须是认识的计量单位，只是系数相同时不做乘除。
   旧测试里那条 `convert(2,'case','case') === 2` 的断言因此**被刻意改掉**，
   改成「包装单位必须走箱规」，理由见 `tests/test_units.py` 的对应用例。

### 为什么不在这里判断「这一行能不能算价格」

`resolve` 之类的业务判断需要知道食材的基础单位、这家供应商的箱规、这条行是不是
调整行——那是 Service 的知识。识别层与计算层多猜一步，错误就会被当成事实存下来
（`docs/struct.md`「识别层不做业务判断」同一条）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final

from src.errors import ValidationError
from src.services.ingredients import (
    BASE_UNITS,
    normalize_name,
    normalize_unit_token,
)

# --- 量纲与换算系数 ---------------------------------------------------------

DIMENSION_MASS: Final[str] = "mass"
DIMENSION_VOLUME: Final[str] = "volume"
DIMENSION_COUNT: Final[str] = "count"

DIMENSIONS: Final[tuple[str, ...]] = (
    DIMENSION_MASS,
    DIMENSION_VOLUME,
    DIMENSION_COUNT,
)

# 每个量纲的参考单位。系数表里的数字是「1 个该单位等于多少个参考单位」。
# 参考单位取最小的那个（g / ml / each），于是所有系数都 ≥ 1、都是精确值，
# 常见换算（lb → g、gallon → ml）是乘法而不是除法。
DIMENSION_REFERENCE_UNITS: Final[dict[str, str]] = {
    DIMENSION_MASS: "g",
    DIMENSION_VOLUME: "ml",
    DIMENSION_COUNT: "each",
}

# 单位 token → (量纲, 1 个该单位 = 多少个参考单位)。
#
# 键必须与步骤 03 的 `BASE_UNITS` 完全一致——食材的 `base_unit` 取的就是那张白名单，
# 这里少一个就会出现「食材建得出来、价格永远算不了」。
# `tests/test_units.py::test_every_step_03_base_unit_is_convertible` 守着这条。
#
# 每个数字的来源都写明，因为它们都是**定义值**，不是测量值，以后不该被"更精确的
# 数据"改掉：
#
# - 1 lb = 453.59237 g：国际码磅定义（1959 年英美协定）。
# - 1 oz = 1/16 lb = 28.349523125 g：精确除尽。
# - 1 US gallon = 231 立方英寸 = 3.785411784 L：精确（1 in = 2.54 cm 是定义）。
# - 1 qt = 1/4 gallon，1 pt = 1/8 gallon，1 fl oz = 1/128 gallon：均精确除尽。
# - 1 dozen = 12 each：定义。
UNIT_FACTORS: Final[dict[str, tuple[str, Decimal]]] = {
    # 质量
    "g": (DIMENSION_MASS, Decimal("1")),
    "kg": (DIMENSION_MASS, Decimal("1000")),
    "lb": (DIMENSION_MASS, Decimal("453.59237")),
    "oz": (DIMENSION_MASS, Decimal("28.349523125")),
    # 体积（美制液量）
    "ml": (DIMENSION_VOLUME, Decimal("1")),
    "l": (DIMENSION_VOLUME, Decimal("1000")),
    "fl_oz": (DIMENSION_VOLUME, Decimal("29.5735295625")),
    "pt": (DIMENSION_VOLUME, Decimal("473.176473")),
    "qt": (DIMENSION_VOLUME, Decimal("946.352946")),
    "gallon": (DIMENSION_VOLUME, Decimal("3785.411784")),
    # 计数
    "each": (DIMENSION_COUNT, Decimal("1")),
    "dozen": (DIMENSION_COUNT, Decimal("12")),
}

# 体积单位按的是**美制**。这不是注释里的免责声明，而是要随统计结果一起输出的事实
# （`api/prices.py` 的 `GET /api/prices/units` 会把它摆出来），否则同一个 "gallon"
# 在英制口径下差 20%，而两个数都"看起来合理"。
VOLUME_SYSTEM: Final[str] = "US"

# 包装单位的 token 集合。与 `services/purchases.py::PACKAGING_UNIT_SYNONYMS` 的**值域**
# 一致，由 `tests/test_units.py::test_packaging_tokens_match_step_04_synonyms` 断言。
#
# 它们**刻意不在** `UNIT_FACTORS` 里：一箱等于多少必须是一条明确的箱规，不是单位
# 体系能回答的问题（`docs/imp_notes.md` Feature 2：「Case、pack 等包装单位必须知道
# 具体包装规格后才能换算」）。
PACKAGING_UNIT_TOKENS: Final[frozenset[str]] = frozenset(
    {"case", "pack", "box", "carton", "bag", "can", "cup", "jar", "bottle"}
)

# 中间运算的精度。除不尽的那几步（例如 g → lb）需要足够多的有效位，使得最终
# 量化到 4 位小数时不会受中间截断影响；28 位是 decimal 的默认值，这里写明并
# 上调到 34，与 IEEE 754 decimal128 一致，便于复核。
#
# ⚠️ 不要用它去"顺便"做舍入：舍入是输出那一步的决定，见 `services/prices.py`。
CALCULATION_PRECISION: Final[int] = 34


# --- 异常 -------------------------------------------------------------------


class UnitError(ValidationError):
    """换算失败的基类。继承 `ValidationError`，所以经 API 直接是 400。

    统计路径**不会**把它变成 400：`services/prices.py` 捕获它并把那一行标成
    「换不出来」连同原因一起返回。接口拒绝的只有「用户明确要求换算一个换不了的
    组合」这种情况（例如维护一条 gallon → lb 的箱规）。
    """


class UnknownUnitError(UnitError):
    """这个单位不在计量单位表里（可能是包装单位，也可能是拼错或自造的写法）。"""


class IncompatibleUnitError(UnitError):
    """两个单位不是同一个量纲：没有密度就不能把体积换成重量。"""


# --- 单位解析 ---------------------------------------------------------------


def canonical_unit(value: str | None) -> str | None:
    """把单位原文归一到计量单位 token；不是计量单位返回 `None`。

    归一复用步骤 03 的 `normalize_unit_token()`（`lbs` / `lbs.` / `磅` / `pounds`
    → `lb`），不另写一张同义词表——两张表各自演化的后果是同一个单位在录入时和统计时
    归到不同 token，而这种错不会报错，只会让一部分采购记录静默地算不进均价。

    返回 `None` 的三种情况由调用方区分：包装单位（`case`）、认不出的写法（`件/2`）、
    空值。判断包装单位用 `is_packaging_unit()`。
    """
    candidate = normalize_unit_token(value)
    if candidate and candidate in UNIT_FACTORS:
        return candidate
    return None


def is_packaging_unit(token: str | None) -> bool:
    """这个 token 是不是包装单位（要先知道箱规才能换算）。

    收的是**已归一的 token**（`purchases.purchase_unit_normalized` 存的就是它），
    不是原文：原文到 token 的归一在步骤 04 的 `normalize_purchase_unit()`。
    """
    return token is not None and token in PACKAGING_UNIT_TOKENS


def require_measurement_unit(value: str | None, *, field: str) -> str:
    """解析一个**必须是计量单位**的输入，不是就抛 400。

    用在「维护箱规」这类接口上：箱规的内部单位必须是基础单位，否则
    「1 箱 = 6 箱」这种循环定义会存进库里。
    """
    token = canonical_unit(value)
    if token is not None:
        return token
    shown = "" if value is None else str(value)
    if is_packaging_unit(canonical_packaging_unit(shown)):
        raise UnknownUnitError(
            f"字段 {field} 不能是包装单位（收到 {shown!r}）："
            "箱规要说明「一箱等于多少基础单位」，内部单位本身不能又是一个包装单位。"
            f"可选值：{', '.join(BASE_UNITS)}。",
            details={"field": field, "allowed": list(BASE_UNITS)},
        )
    raise UnknownUnitError(
        f"字段 {field} 不是支持的计量单位（收到 {shown!r}）。"
        f"可选值：{', '.join(BASE_UNITS)}。",
        details={"field": field, "allowed": list(BASE_UNITS)},
    )


def canonical_packaging_unit(value: str | None) -> str | None:
    """把包装单位原文归一到 token（`箱` / `CS` / `cases` → `case`）；不是就 `None`。

    实现刻意放在这里而不是复用 `services/purchases.py` 的那张表：本模块是被
    `prices.py` 依赖的底层计算层，反过来 import 服务层会把依赖方向绕回去。
    两处的取值一致由测试断言，不靠人记得。
    """
    from src.services.purchases import PACKAGING_UNIT_SYNONYMS  # 局部导入避免环

    if value is None:
        return None
    candidate = normalize_name(value)
    if not candidate:
        return None
    return PACKAGING_UNIT_SYNONYMS.get(candidate)


def dimension_of(token: str) -> str:
    """这个计量单位属于哪个量纲。不认识直接抛 `UnknownUnitError`。"""
    entry = UNIT_FACTORS.get(token)
    if entry is None:
        raise UnknownUnitError(
            f"{token!r} 不是支持的计量单位。",
            details={"unit": token, "allowed": list(BASE_UNITS)},
        )
    return entry[0]


def units_of_dimension(dimension: str) -> tuple[str, ...]:
    """某个量纲下的全部单位，按系数从小到大。供接口列出「能换成什么」。"""
    members = [
        (factor, token)
        for token, (dim, factor) in UNIT_FACTORS.items()
        if dim == dimension
    ]
    return tuple(token for _, token in sorted(members))


# --- 换算 -------------------------------------------------------------------


def convert(quantity: Decimal, from_unit: str | None, to_unit: str | None) -> Decimal:
    """把 `quantity` 从 `from_unit` 换成 `to_unit`。

    两个单位都必须是计量单位且同量纲，否则抛 `UnknownUnitError` /
    `IncompatibleUnitError`。**同一个单位也要校验**（见模块文档第 3 条）：
    `convert(x, "case", "case")` 会报错，因为包装单位的换算必须走箱规。

    数量必须是 `Decimal`：收 `float` 会把步骤 04 刚解决的精度问题带回来，所以这里
    直接拒绝而不是转换。结果不做量化——除不尽时保留 `CALCULATION_PRECISION` 位
    有效数字，由输出层决定舍入位数。
    """
    if isinstance(quantity, bool) or not isinstance(quantity, Decimal):
        raise UnitError(
            f"换算的数量必须是 Decimal，收到 {type(quantity).__name__}："
            "float 会引入精度损失，请在服务层先解析成 Decimal。",
            details={"field": "quantity"},
        )
    if not quantity.is_finite():
        raise UnitError(
            f"换算的数量不是有效数字（收到 {quantity!r}）。",
            details={"field": "quantity"},
        )

    source = _resolve_for_convert(from_unit, field="from_unit")
    target = _resolve_for_convert(to_unit, field="to_unit")

    source_dimension, source_factor = UNIT_FACTORS[source]
    target_dimension, target_factor = UNIT_FACTORS[target]
    if source_dimension != target_dimension:
        raise IncompatibleUnitError(
            f"{source} 与 {target} 不是同一类计量（{source_dimension} vs "
            f"{target_dimension}）：没有密度就不能把体积换成重量，反之亦然。"
            "请把食材的基础单位改成同一类，或按实际称重录入。",
            details={
                "from_unit": source,
                "to_unit": target,
                "from_dimension": source_dimension,
                "to_dimension": target_dimension,
            },
        )
    if source_factor == target_factor:
        # 同单位（或同系数的别名）：不做乘除，原值返回。校验已经做过了。
        return quantity
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        return quantity * source_factor / target_factor


def _resolve_for_convert(value: str | None, *, field: str) -> str:
    """换算入口的单位解析。包装单位给出**专门的**错误，不混在「不认识」里。"""
    token = canonical_unit(value)
    if token is not None:
        return token
    shown = "" if value is None else str(value)
    packaging = canonical_packaging_unit(shown)
    if packaging is not None:
        raise UnknownUnitError(
            f"{shown!r} 是包装单位（{packaging}），不能直接参与换算："
            "必须先有一条「一" + packaging + " 等于多少基础单位」的包装规格。",
            details={"field": field, "unit": shown, "packaging_unit": packaging},
        )
    raise UnknownUnitError(
        f"{shown!r} 不是支持的计量单位，无法换算。"
        f"可选值：{', '.join(BASE_UNITS)}。",
        details={"field": field, "unit": shown, "allowed": list(BASE_UNITS)},
    )


# --- 包装规格原文 -----------------------------------------------------------

# 包装规格原文里的乘号：`X` / `x` / `*` / `×`，两侧空格随意。
_PACK_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"\s*[x×*]\s*", re.IGNORECASE)

# 一个「数字 + 可选单位」的片段，例如 `1000g`、`44.10`、`10`、`355 ml`。
_PACK_FACTOR: Final[re.Pattern[str]] = re.compile(
    r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[^\d\s]*)$", re.ASCII
)


@dataclass(frozen=True, slots=True)
class PackSpecReading:
    """一段包装规格原文读出来的东西。**是候选，不是结论。**

    - `factors`：原文里的各个数字，按出现顺序。`1000g x 10` → `(1000, 10)`。
    - `unit`：原文里出现的计量单位 token；裸数字写法为 `None`。
    - `pack_size` / `pack_unit`：**只有在能唯一确定时**才有值，含义是
      「1 个包装单位 = pack_size pack_unit」。
    - `ambiguous`：读不出唯一结论。`reason` 说明为什么。

    ⚠️ **为什么大多数情况是 `ambiguous`**：真实单据上同样的 `数字X数字` 写法有
    两种互不相容的读法，实测两种都存在（步骤 07 的 Ground Truth）：

    | 原文 | 供应商 | 单据上的数量/单位 | 实际含义 |
    | --- | --- | --- | --- |
    | `1000g x 10` | Kun Kee | `20` `箱` | 每箱 10 × 1000 g = 10 kg |
    | `6X30` | New Moy's | `180` `lbs.` | **6 箱 × 30 磅**，箱规是每箱 30 磅 |

    前者两个数字都属于箱内，后者第一个数字是箱数。**差 6 倍**，而且两种读法
    都算得通。所以裸数字写法一律 `ambiguous`，必须人确认；带单位的写法才给
    `pack_size`。这与步骤 06「逗号含义定不死就拒绝」、步骤 08「两模型分歧就置
    UNREADABLE」是同一条口径：**宁可留空让人填，也不猜一个会被当成事实的数。**
    """

    raw_text: str
    factors: tuple[Decimal, ...]
    unit: str | None
    pack_size: Decimal | None
    pack_unit: str | None
    ambiguous: bool
    reason: str

    @property
    def usable(self) -> bool:
        """能不能直接当候选预填给人确认。"""
        return self.pack_size is not None and self.pack_unit is not None


def parse_pack_spec_text(text: str | None) -> PackSpecReading | None:
    """读一段包装规格原文（`1000g x 10`、`6X30`、`24x355ml`、`1`）。

    返回 `None` 表示这段文本里**没有**包装规格的样子（例如纯中文品名）；返回
    `PackSpecReading` 表示读出了结构，但**是否可用看 `usable`**。

    能给出 `pack_size` 的唯一情况：原文里恰好出现**一个**计量单位，且其余都是
    纯数字。此时各数字相乘、单位取那一个，含义是「一个包装单位里有这么多」。
    `1000g x 10` → `10000 g`。这条规则对 `24 x 355ml` 同样成立（= 8520 ml）。

    ⚠️ 本函数**从不**用「乘出来刚好等于这一行的数量」当判据。步骤 07/08 已经两次
    记下这条：算术自洽的编造完全存在（`8 × 117 = 936`），拿算得平当放行条件等于
    专门给编造开绿灯。`6X30` 恰好等于 New Moy's 那一行的 180 lbs，而它的正确读法
    是「6 箱 × 每箱 30 磅」——用算得平去确认，确认的恰恰是错的那一种。
    """
    if text is None:
        return None
    candidate = normalize_name(text)
    if not candidate:
        return None

    parts = [part for part in _PACK_SEPARATOR.split(candidate) if part]
    if not parts:
        return None

    numbers: list[Decimal] = []
    units: list[str] = []
    for part in parts:
        matched = _PACK_FACTOR.match(part)
        if matched is None:
            return None
        numbers.append(Decimal(matched.group("number")))
        unit_text = matched.group("unit")
        if unit_text:
            token = canonical_unit(unit_text)
            if token is None:
                # 带着一个认不出的后缀（`6X30箱` / `2X4件`）：读出了数字，
                # 但单位不明，仍然算读不出结论。
                return PackSpecReading(
                    raw_text=text,
                    factors=tuple(numbers),
                    unit=None,
                    pack_size=None,
                    pack_unit=None,
                    ambiguous=True,
                    reason=f"包装规格里的 {unit_text!r} 不是支持的计量单位。",
                )
            units.append(token)

    if len(units) > 1 and len(set(units)) > 1:
        return PackSpecReading(
            raw_text=text,
            factors=tuple(numbers),
            unit=None,
            pack_size=None,
            pack_unit=None,
            ambiguous=True,
            reason="包装规格里出现了多个不同的计量单位，读不出唯一结论。",
        )

    if not units:
        return PackSpecReading(
            raw_text=text,
            factors=tuple(numbers),
            unit=None,
            pack_size=None,
            pack_unit=None,
            ambiguous=True,
            reason=(
                "包装规格只有数字、没有单位，无法确定是「每箱的规格」还是"
                "「箱数 × 每箱规格」——真实单据上两种写法都有，差好几倍。"
                "请直接填「一箱等于多少基础单位」。"
            ),
        )

    unit = units[0]
    total = numbers[0]
    for number in numbers[1:]:
        total = total * number
    return PackSpecReading(
        raw_text=text,
        factors=tuple(numbers),
        unit=unit,
        pack_size=total,
        pack_unit=unit,
        ambiguous=False,
        reason="",
    )


def find_pack_spec_in_description(description: str | None) -> PackSpecReading | None:
    """从商品描述里找包装规格（Kun Kee 把 `1000g x 10` 直接写在描述里）。

    实测依据：步骤 07 的 Ground Truth 里，Kun Kee 的包装规格没有独立列，就混在
    Description 中（`samples/real/ground_truth/03_Kunkee_Trading_1p__p01.json`
    与 `tools/gt_printed_invoices.py` 的标注说明）。采购记录里本来就存着
    `original_description`，所以这条候选**不需要任何新数据**。

    只返回 `usable` 的读数（带单位的那种）。裸数字（`6X30`）不返回：那种既读不出
    唯一结论，又会在描述里和品名中的数字（`80/20`、`牛肚6`）混起来，给出来只会
    让人以为系统知道答案。
    """
    if description is None:
        return None
    text = normalize_name(description)
    if not text:
        return None
    # 先整段试：`1000g x 10` 整段才读得出 10000 g，按空白切开只会得到两个片段。
    reading = parse_pack_spec_text(text)
    if reading is not None and reading.usable:
        return reading
    # 描述里嵌着 `1000g x 10` 这样的片段、前后还有品名时，把那一段抠出来再读。
    embedded = re.search(
        r"\d+(?:\.\d+)?\s*[a-z_]+\s*[x×*]\s*\d+(?:\.\d+)?"
        r"|\d+(?:\.\d+)?\s*[x×*]\s*\d+(?:\.\d+)?\s*[a-z_]+",
        text,
    )
    if embedded is not None:
        reading = parse_pack_spec_text(embedded.group(0))
        if reading is not None and reading.usable:
            return reading
    return None


__all__ = [
    "CALCULATION_PRECISION",
    "DIMENSIONS",
    "DIMENSION_COUNT",
    "DIMENSION_MASS",
    "DIMENSION_REFERENCE_UNITS",
    "DIMENSION_VOLUME",
    "IncompatibleUnitError",
    "PACKAGING_UNIT_TOKENS",
    "PackSpecReading",
    "UNIT_FACTORS",
    "UnitError",
    "UnknownUnitError",
    "VOLUME_SYSTEM",
    "canonical_packaging_unit",
    "canonical_unit",
    "convert",
    "dimension_of",
    "find_pack_spec_in_description",
    "is_packaging_unit",
    "parse_pack_spec_text",
    "require_measurement_unit",
    "units_of_dimension",
]
