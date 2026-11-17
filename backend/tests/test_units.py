"""单位与包装规格换算的测试（步骤 09.1 / 09.2 / 09.4）。

本文件包含 09.4 要求的**转写用例**：`kitchen-ledger/tests/recipes.test.mjs` 的
`unit conversions enforce dimensions` 那一条，改写成 pytest 并用 `Decimal`。
其中一条断言被**刻意改掉**，理由写在对应用例里（不是漏改）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.services.ingredients import BASE_UNITS, normalize_unit_token
from src.services.purchases import PACKAGING_UNIT_SYNONYMS
from src.services.units import (
    DIMENSION_COUNT,
    DIMENSION_MASS,
    DIMENSION_VOLUME,
    PACKAGING_UNIT_TOKENS,
    UNIT_FACTORS,
    IncompatibleUnitError,
    UnitError,
    UnknownUnitError,
    canonical_packaging_unit,
    canonical_unit,
    convert,
    dimension_of,
    find_pack_spec_in_description,
    is_packaging_unit,
    parse_pack_spec_text,
    require_measurement_unit,
    units_of_dimension,
)

# --- 09.4：从 recipes.test.mjs 转写的换算用例 -------------------------------


def test_transcribed_mass_conversions_are_exact() -> None:
    """旧测试的两条断言，改用 Decimal 之后是**精确相等**而不是浮点近似。

    来源：`kitchen-ledger/tests/recipes.test.mjs`
    `assert.equal(convert(150,'g','kg'), .15)` 与
    `assert.equal(convert(1,'lb','g'), 453.59237)`。
    """
    assert convert(Decimal("150"), "g", "kg") == Decimal("0.15")
    assert convert(Decimal("1"), "lb", "g") == Decimal("453.59237")


def test_transcribed_volume_to_mass_is_refused() -> None:
    """`convert(1,'l','kg')` 必须报错：没有密度不能把体积换成重量。

    来源：旧测试的 `assert.throws(()=>convert(1,'l','kg'), /Cannot convert/)`。
    `docs/imp_notes.md` Feature 2 把它列为硬要求。
    """
    with pytest.raises(IncompatibleUnitError) as caught:
        convert(Decimal("1"), "l", "kg")

    assert "密度" in str(caught.value)
    assert caught.value.details["from_dimension"] == DIMENSION_VOLUME
    assert caught.value.details["to_dimension"] == DIMENSION_MASS


def test_same_packaging_unit_is_refused_unlike_the_legacy_test() -> None:
    """⚠️ **这一条与旧测试相反，是刻意的。**

    旧测试断言 `convert(2,'case','case') === 2`——那是 `from === to` 短路的副产品，
    而那条短路会把**任何**拼错的单位也放过去（`convert(2,'lbb','lbb')` 同样返回 2）。

    本项目改成「两边都必须是认识的计量单位」，于是：

    - `case` 的换算必须走箱规（一箱等于多少基础单位是人确认过的事实），
      identity 放行等于让包装单位悄悄混进标准化价格；
    - 拼错的单位在换算入口就被挡住，而不是静默返回一个看起来对的数。

    `docs/stru_dev.md` 09.1 写的「同单位也要校验输入」指的就是这件事。
    """
    with pytest.raises(UnknownUnitError) as caught:
        convert(Decimal("2"), "case", "case")

    assert "包装单位" in str(caught.value)
    assert caught.value.details["packaging_unit"] == "case"


def test_identical_measurement_units_pass_through_after_validation() -> None:
    """同一个计量单位：原值返回，但**校验仍然做过**（上一条用例就是它的另一面）。"""
    assert convert(Decimal("12.345"), "lb", "lb") == Decimal("12.345")
    assert convert(Decimal("12.345"), "lbs.", "lb") == Decimal("12.345")


def test_unknown_unit_is_refused_even_when_both_sides_are_the_same() -> None:
    with pytest.raises(UnknownUnitError):
        convert(Decimal("2"), "lbb", "lbb")


# --- 09.2：单位表与制式 -----------------------------------------------------


def test_every_step_03_base_unit_is_convertible() -> None:
    """步骤 03 白名单里的每个基础单位都必须能换算。

    少一个的后果是「食材建得出来、价格永远算不出来」，而且不报错。
    """
    assert set(BASE_UNITS) == set(UNIT_FACTORS)


def test_us_volume_system_is_pinned_to_the_defining_values() -> None:
    """体积按**美制**，而且用定义值。

    1 US gallon = 231 立方英寸 = 3.785411784 L（精确，因为 1 in = 2.54 cm 是定义）。
    英制 gallon 比它大约 20%，同一个单词在两种制式下差 20% 却都"看起来合理"——
    所以这组数字由测试钉住。
    """
    assert convert(Decimal("1"), "gallon", "l") == Decimal("3.785411784")
    assert convert(Decimal("1"), "gallon", "qt") == Decimal("4")
    assert convert(Decimal("1"), "gallon", "pt") == Decimal("8")
    assert convert(Decimal("1"), "gallon", "fl_oz") == Decimal("128")


def test_mass_units_are_pinned_to_the_defining_values() -> None:
    """1 lb = 453.59237 g（国际码磅定义）；1 oz = 1/16 lb，精确除尽。"""
    assert convert(Decimal("1"), "lb", "oz") == Decimal("16")
    assert convert(Decimal("1"), "kg", "g") == Decimal("1000")


def test_count_units_convert_between_each_and_dozen() -> None:
    assert convert(Decimal("2"), "dozen", "each") == Decimal("24")
    assert convert(Decimal("24"), "each", "dozen") == Decimal("2")
    assert dimension_of("dozen") == DIMENSION_COUNT


def test_non_terminating_conversion_keeps_enough_precision() -> None:
    """g → lb 除不尽：保留足够有效位，再换回去仍然是原值量级。

    舍入是**输出那一步**的事（`services/prices.py::quantize_price`），
    换算本身不提前量化。
    """
    grams = convert(Decimal("1"), "lb", "g")
    assert convert(grams, "g", "lb") == Decimal("1")
    one_gram_in_pounds = convert(Decimal("1"), "g", "lb")
    assert str(one_gram_in_pounds).startswith("0.00220462262184877")


def test_float_quantity_is_refused() -> None:
    """收 `float` 直接拒绝，不悄悄转换：那会把步骤 04 解决的精度问题带回来。"""
    with pytest.raises(UnitError) as caught:
        convert(1.5, "lb", "g")  # type: ignore[arg-type]

    assert "Decimal" in str(caught.value)


def test_nan_quantity_is_refused() -> None:
    with pytest.raises(UnitError):
        convert(Decimal("NaN"), "lb", "g")


def test_units_of_dimension_lists_from_small_to_large() -> None:
    assert units_of_dimension(DIMENSION_MASS) == ("g", "oz", "lb", "kg")
    assert units_of_dimension(DIMENSION_COUNT) == ("each", "dozen")


# --- 单位写法的归一 ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("lbs.", "lb"),  # ⚠️ 真实供应商发票上每一行都是这个写法
        ("LBS.", "lb"),
        ("oz.", "oz"),
        ("gal.", "gallon"),
        ("磅", "lb"),
        ("fl oz", "fl_oz"),
        ("pcs", "each"),
        ("打", "dozen"),
        ("  Kg  ", "kg"),
    ],
)
def test_measurement_unit_spellings_normalize(raw: str, expected: str) -> None:
    """⚠️ `lbs.` 这条不是凑数的。

    步骤 07 的 6 张印刷件 Ground Truth 里，New Moy's 每一行的单位列都印着 `lbs.`。
    句点不去掉的话这些行全部"单位认不出"，于是那家供应商一条价格也算不出来，
    老板得为每一行重新敲单位。
    """
    assert canonical_unit(raw) == expected
    assert normalize_unit_token(raw) == expected


@pytest.mark.parametrize("raw", ["heads", "head", "bunches", "颗", "把"])
def test_count_nouns_from_the_real_invoice_map_to_each(raw: str) -> None:
    """`72 heads`（球生菜）、`24 bunches`（香菜）→ `each`。

    依据：`samples/real/sample_grocery_supplier_invoices.pdf`。一颗生菜、一把香菜
    就是一件，按「个」计价是有意义的；不补这几个写法，这些行的单位永远认不出来。
    """
    assert canonical_unit(raw) == "each"


@pytest.mark.parametrize("raw", ["cups", "cans", "jars", "bottles", "罐", "杯", "瓶"])
def test_container_nouns_are_packaging_units_not_volume_units(raw: str) -> None:
    """⚠️ **`cup` 刻意不是体积单位**，尽管 1 US cup = 236.588 mL 是真的。

    依据同上那份真实单据：上面写着 `48 cups`（原味酸奶）与 `48 cans`（甜玉米罐头），
    指的是 **48 个杯装 / 48 个罐装**，不是 48 杯容积。两种读法差的正好是「一杯装
    里有多少」——而那恰恰是箱规要回答的问题。

    把 `cup` 当体积单位会算出一个**看起来合理**的 $/ml，而它可能差好几倍；
    归到包装单位之后，这些行会落到"待补箱规"清单，由人说一次「1 cup = 170 g」
    或「1 cup = 1 each」，然后历史与以后的单据一起算得出来。
    """
    assert canonical_unit(raw) is None
    assert is_packaging_unit(canonical_packaging_unit(raw))


def test_packaging_units_are_not_measurement_units() -> None:
    """包装单位不在换算表里：没有箱规之前，一箱不能换成任何基础单位。"""
    for token in PACKAGING_UNIT_TOKENS:
        assert canonical_unit(token) is None
        assert is_packaging_unit(token)


def test_packaging_tokens_match_step_04_synonyms() -> None:
    """本模块的包装单位 token 集合必须等于步骤 04 归一表的值域。

    两处各自演化的后果：某种写法在录入时归成 `case`，统计时却不认识它是包装单位，
    于是那些记录既不换算也不出现在"待补箱规"清单里——**从两边同时消失**。
    """
    assert PACKAGING_UNIT_TOKENS == frozenset(PACKAGING_UNIT_SYNONYMS.values())


@pytest.mark.parametrize("raw, expected", [("箱", "case"), ("CS", "case"), ("袋", "bag")])
def test_packaging_unit_spellings_normalize(raw: str, expected: str) -> None:
    assert canonical_packaging_unit(raw) == expected


def test_require_measurement_unit_rejects_packaging_units_with_a_reason() -> None:
    """箱规的内部单位不能又是一个包装单位，否则「1 箱 = 6 箱」会存进库里。"""
    with pytest.raises(UnknownUnitError) as caught:
        require_measurement_unit("case", field="pack_unit")

    assert "箱规" in str(caught.value)
    assert caught.value.details["field"] == "pack_unit"


def test_require_measurement_unit_accepts_base_units() -> None:
    assert require_measurement_unit("lbs.", field="pack_unit") == "lb"


# --- 包装规格原文 -----------------------------------------------------------
#
# 下面这组参数全部来自步骤 07 的真实 Ground Truth
# （`samples/real/ground_truth/*.json` 的 `pack_spec` 列）。


def test_pack_spec_with_an_explicit_unit_is_usable() -> None:
    """`1000g x 10`（Kun Kee）：带单位、各数相乘 → 每箱 10000 g。可当候选。"""
    reading = parse_pack_spec_text("1000g x 10")

    assert reading is not None
    assert reading.usable
    assert reading.pack_size == Decimal("10000")
    assert reading.pack_unit == "g"
    assert reading.factors == (Decimal("1000"), Decimal("10"))


@pytest.mark.parametrize("raw", ["6X30", "4X30", "6X44.10", "2X60.1", "2X30", "1", "2"])
def test_bare_numeric_pack_specs_are_ambiguous(raw: str) -> None:
    """⚠️ **裸数字的箱规一律读不出结论**，这是本步骤最重要的一条拒绝。

    真实单据上同形写法有两种互不相容的含义（步骤 07 的 Ground Truth）：

    | 原文 | 供应商 | 单据上的数量/单位 | 实际含义 |
    | --- | --- | --- | --- |
    | `1000g x 10` | Kun Kee | `20` `箱` | 每箱 10 × 1000 g |
    | `6X30` | New Moy's | `180` `lbs.` | **6 箱 × 每箱 30 磅** |

    差 6 倍，而且两种读法各自都算得通。所以这里只给 `factors`，不给 `pack_size`。
    """
    reading = parse_pack_spec_text(raw)

    assert reading is not None
    assert reading.ambiguous
    assert not reading.usable
    assert reading.pack_size is None
    assert reading.reason


def test_pack_spec_arithmetic_is_not_used_as_a_gate() -> None:
    """⚠️ `6X30` 恰好等于 New Moy's 那一行的数量 180 lbs——**不能**拿它当判据。

    步骤 07/08 两次记下这条：算术自洽的编造完全存在（`8 × 117 = 936`），
    拿"算得平"当放行条件等于专门给编造开绿灯。而 `6X30` 的正确读法是
    「6 箱 × 每箱 30 磅」，用算得平去确认，确认的恰恰是错的那一种。
    """
    reading = parse_pack_spec_text("6X30")

    assert reading is not None
    assert reading.factors == (Decimal("6"), Decimal("30"))
    # 6 × 30 = 180，与那张单据上的 quantity 一致，但仍然判为读不出结论。
    assert reading.factors[0] * reading.factors[1] == Decimal("180")
    assert not reading.usable


def test_pack_spec_with_an_unreadable_suffix_is_ambiguous() -> None:
    reading = parse_pack_spec_text("6X30箱")

    assert reading is not None
    assert not reading.usable
    assert "计量单位" in reading.reason


def test_pack_spec_with_conflicting_units_is_ambiguous() -> None:
    reading = parse_pack_spec_text("500g x 2kg")

    assert reading is not None
    assert not reading.usable
    assert "多个不同的计量单位" in reading.reason


def test_non_pack_spec_text_reads_as_nothing() -> None:
    assert parse_pack_spec_text("Pork Spare Rib") is None
    assert parse_pack_spec_text("") is None
    assert parse_pack_spec_text(None) is None


def test_pack_spec_is_found_inside_a_real_description() -> None:
    """Kun Kee 的箱规没有独立列，就写在商品描述里——那一列我们本来就存着。

    依据：`tools/gt_printed_invoices.py` 的标注说明「包装规格 `1000g x 10` 混在
    Description 里，没有独立列」。
    """
    reading = find_pack_spec_in_description("冷冻鸡胸 1000g x 10 整箱")

    assert reading is not None
    assert reading.usable
    assert reading.pack_size == Decimal("10000")
    assert reading.pack_unit == "g"


def test_bare_numeric_description_gives_no_suggestion() -> None:
    """`Beef Stomach - 牛肚6X44.10`：读不出唯一结论 → **不给候选**。

    给一个可能差 6 倍的数当候选，比不给更糟：老板会倾向于点确认
    （步骤 08 已记下这条：「给人一个已填好的 117.00 让他核对，他会倾向于点确认」）。
    """
    assert find_pack_spec_in_description("Beef Stomach - 牛肚6X44.10") is None
