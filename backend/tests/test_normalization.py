"""数值与日期规范化的测试（步骤 08.3e，表格导入与 OCR 通路共用这一份口径）。

这组用例守的是一句话：**改动要显式可见，定不死就拒绝。**
"""

from __future__ import annotations

import pytest

from src.normalization import (
    DATE_ORDER_DMY,
    DATE_ORDER_MDY,
    NUMBER_FORMAT_AUTO,
    NUMBER_FORMAT_COMMA,
    NUMBER_FORMAT_DOT,
    NUMBER_FORMAT_STRICT,
    _TRAILING_UNIT_WORDS,
    DATE_ORDER_ASSUMED,
    date_text_readings,
    normalize_date_text,
    normalize_number_text,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("6.39", "6.39"),
        ("$ 6.39", "6.39"),
        ("$1,991.38", "1991.38"),
        ("€1.234,56", "1234.56"),
        ("€0,00", "0.00"),
        ("(12.50)", "-12.50"),
        ("0", "0"),
        ("USD 12.00", "12.00"),
    ],
)
def test_unambiguous_numbers_are_accepted(raw: str, expected: str) -> None:
    assert normalize_number_text(raw).text == expected


@pytest.mark.parametrize("raw", ["1,234", "1.234", "1.234.567", "abc", "5 85"])
def test_ambiguous_numbers_are_refused_rather_than_guessed(raw: str) -> None:
    """⚠️ 这一组是本模块最要紧的地方。

    `1,234` 在美式约定下读作 1234、欧式读作 1.234，**差 1000 倍**，而文本本身
    定不死。猜错一次就是一个错价格永久留在历史里，而且不报错。

    `5 85` 是步骤 07 的实测案例：纸面写的是 5.85，两个模型都读成了 `185`。
    """
    result = normalize_number_text(raw)

    assert result.text is None
    assert result.problem


def test_full_width_conversion_is_recorded_not_silent() -> None:
    """全角转半角必须留下一条人看得见的说明（08.3e）。

    步骤 07 实测：同一张图换个提示词，`1,040.00` 会变成全角逗号的 `1，040.00`。
    转换必须发生——步骤 04 的 parse_decimal 明确拒绝全角数字——但必须看得见。
    """
    result = normalize_number_text("1，040.00")

    assert result.text == "1040.00"
    assert any("全角转半角" in note for note in result.notes)


def test_trailing_unit_is_stripped_for_ocr_but_not_for_spreadsheets() -> None:
    """数量格里写着 `74 lbs.` 是纸面事实；表格有独立单位列，那里要报错。"""
    for_ocr = normalize_number_text("74 lbs.")
    for_spreadsheet = normalize_number_text("74 lbs.", strip_trailing_unit=False)

    assert for_ocr.text == "74"
    assert any("去掉尾部单位" in note for note in for_ocr.notes)
    assert for_spreadsheet.text is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("72 heads", "72"),
        ("24 bunches", "24"),
        ("16 gal.", "16"),
        ("90 dozen", "90"),
        ("12 qt.", "12"),
        ("48 cups", "48"),
        ("48 cans", "48"),
        ("20 箱", "20"),
    ],
)
def test_trailing_units_from_the_real_digital_pdf_are_stripped(
    raw: str, expected: str
) -> None:
    """⚠️ 这组值是步骤 09 的真实数据实跑抓出来的回归。

    `samples/real/sample_grocery_supplier_invoices.pdf` 的数量格里就是这些写法。
    尾部单位表原来只有 lb / kg / g / each 那几个，于是 **29 行真实单据里有 7 行的
    数量被判成"认不出"**——不报错，只是价格少算了 7 行。
    """
    assert normalize_number_text(raw, number_format=NUMBER_FORMAT_DOT).text == expected


def test_trailing_unit_words_cover_the_unit_vocabulary() -> None:
    """尾部单位表必须覆盖项目认识的全部单位写法。

    两张表分处两个模块（`normalization.py` 不该经由服务层把数据库依赖拉进来），
    所以一致性由这条断言保证，不靠人记得——漏一项的后果是某种写法的数量被静默
    判成"认不出"，而那一行的价格就这么没了。
    """
    from src.services.ingredients import (
        BASE_UNIT_SYNONYMS,
        BASE_UNITS,
        PACKAGING_UNITS,
    )

    known = (
        {unit.replace("_", " ") for unit in BASE_UNITS}
        | set(BASE_UNIT_SYNONYMS)
        | set(PACKAGING_UNITS)
    )
    covered = {word.replace("_", " ").lower() for word in _TRAILING_UNIT_WORDS}
    missing = sorted(word for word in known if word.lower() not in covered)

    assert missing == [], f"尾部单位表漏了这些写法：{missing}"


@pytest.mark.parametrize(
    ("raw", "number_format", "expected"),
    [
        ("1,234.50", NUMBER_FORMAT_DOT, "1234.50"),
        ("1.234,50", NUMBER_FORMAT_COMMA, "1234.50"),
        ("1234.50", NUMBER_FORMAT_STRICT, "1234.50"),
    ],
)
def test_explicit_formats_still_work(raw: str, number_format: str, expected: str) -> None:
    """显式选了格式就按它读——步骤 06 的导入选项仍然有效。"""
    assert normalize_number_text(raw, number_format=number_format).text == expected


def test_strict_refuses_commas() -> None:
    assert normalize_number_text("1,234.50", number_format=NUMBER_FORMAT_STRICT).text is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-08", "2026-11-08"),
        ("2026年11月8日", "2026-11-08"),
        ("14.10.2026", "2026-10-14"),
        ("Oct 4th, 2026", "2026-10-04"),
        ("14 October 2026", "2026-10-14"),
        ("13/10/2026", "2026-10-13"),
    ],
)
def test_unambiguous_dates_are_accepted(raw: str, expected: str) -> None:
    assert normalize_date_text(raw).text == expected


@pytest.mark.parametrize("raw", ["6/11/2026", "03/04/2026", "11/08/26"])
def test_ambiguous_dates_are_refused(raw: str) -> None:
    """日/月顺序或世纪定不死时留空，**绝不编造日期**。

    代价已经被设计掉了：步骤 04 允许 purchase_date 为空，`undated=true` 能筛出
    「待补日期」清单。编一个日期的代价是那笔采购被算进错月份的均价，而且不报错。
    """
    result = normalize_date_text(raw)

    assert result.text is None
    assert result.problem


def test_date_order_resolves_the_ambiguity_once_per_supplier() -> None:
    """供应商路由上设了 date_order，这家的单据就按它读（08 的 ocr_routes）。"""
    assert normalize_date_text("6/11/2026", date_order=DATE_ORDER_MDY).text == "2026-06-11"
    assert normalize_date_text("6/11/2026", date_order=DATE_ORDER_DMY).text == "2026-11-06"


def test_ocr_short_year_is_expanded_when_month_and_day_are_clear() -> None:
    assert normalize_date_text("6/29/26", allow_short_year=True).text == "2026-06-29"
    assert normalize_date_text("29/6/26", allow_short_year=True).text == "2026-06-29"
    assert normalize_date_text("6/11/26", allow_short_year=True).text is None
    assert normalize_date_text("6/11/26", date_order=DATE_ORDER_MDY, allow_short_year=True).text == "2026-06-11"
    assert normalize_date_text("6/29/26").text is None


def test_impossible_dates_are_refused() -> None:
    assert normalize_date_text("2026-02-30").text is None


def test_auto_is_the_default_for_the_ocr_path() -> None:
    assert normalize_number_text("$1,991.38", number_format=NUMBER_FORMAT_AUTO).text == "1991.38"


# --- 歧义日期的两种读法（多文件计划 M05） -----------------------------------
#
# `normalize_date_text` 回答「能不能定死一个日期」，定不死就拒绝——写进采购记录的
# 日期必须走它。`date_text_readings` 回答「定不死的时候到底有哪几种可能」，整理阶段
# 的**分类候选**走它：把两种可能摆出来让人挑一个，比留一个空格子有用得多，而且
# **两条都只是候选，没有一条会自己变成决定**。


def test_an_unambiguous_date_has_exactly_one_reading() -> None:
    result = date_text_readings("2026-11-08")

    assert [item.text for item in result.readings] == ["2026-11-08"]
    assert result.ambiguous is False
    assert result.certain is not None


def test_an_ambiguous_numeric_date_has_both_readings_and_no_preference() -> None:
    """⚠️ 顺序只是为了让界面有稳定次序，**不表示 mdy 更可能**。"""
    result = date_text_readings("6/11/2026")

    assert result.ambiguous is True
    assert [(item.text, item.order) for item in result.readings] == [
        ("2026-06-11", DATE_ORDER_MDY),
        ("2026-11-06", DATE_ORDER_DMY),
    ]
    assert result.certain is None  # 没有「那一条」答案
    assert result.problem  # 为什么定不死，原话留着


def test_the_project_default_reads_an_ambiguous_date_month_first() -> None:
    """第 15 节：没有别的证据时 `6/11/2026` 读作 **6 月 11 日**，不摆两个选项。

    用户的话是「6/3 肯定是 6 月 3 日，这个就根本没有歧义」——这家店收的是美国
    供应商开的单据。⚠️ `ambiguous` 仍然是 True：文本本身定不死是事实，留痕之后
    换一家写日/月/年的商家还能用原文重新解析。
    """
    result = date_text_readings("6/11/2026", assume_order=DATE_ORDER_ASSUMED)

    assert [(item.text, item.order) for item in result.readings] == [
        ("2026-06-11", DATE_ORDER_MDY)
    ]
    assert result.ambiguous is True
    assert result.assumed_order == DATE_ORDER_MDY
    assert "另一种读法是 2026-11-06" in " ".join(result.notes)


def test_the_default_never_overrides_a_text_that_defines_itself() -> None:
    """⚠️ 默认读法只在**两种都成立**时才出场。

    `18/6/2026` 按月/日/年根本读不出来（没有第 18 个月），所以它不是歧义——它
    自己就定死了。如果把默认值当成 `date_order=mdy` 一路传下去，这一天会**整个
    丢掉**，而不是读成 6 月 18 日。这条断言就是守着这个区别。
    """
    result = date_text_readings("18/6/2026", assume_order=DATE_ORDER_ASSUMED)

    assert [item.text for item in result.readings] == ["2026-06-18"]
    assert result.ambiguous is False
    assert result.assumed_order is None


def test_a_confirmed_supplier_order_beats_the_project_default() -> None:
    """这家明说写日/月/年时，默认读法让路：确认过的商家比默认值更有证据。"""
    result = date_text_readings(
        "6/11/2026", date_order=DATE_ORDER_DMY, assume_order=DATE_ORDER_ASSUMED
    )

    assert [item.text for item in result.readings] == ["2026-11-06"]
    assert result.assumed_order is None


def test_a_confirmed_date_order_collapses_the_ambiguity_to_one_reading() -> None:
    """这家商家说过它写月/日/年之后，同一段原文只剩一条读法。"""
    assert [item.text for item in date_text_readings("6/11/2026", date_order=DATE_ORDER_MDY).readings] == ["2026-06-11"]
    assert [item.text for item in date_text_readings("6/11/2026", date_order=DATE_ORDER_DMY).readings] == ["2026-11-06"]


def test_a_date_whose_day_is_over_twelve_is_not_ambiguous() -> None:
    """`13/11/2026` 只有一种读法：13 不可能是月份。文本本身定死了，不需要商家。"""
    result = date_text_readings("13/11/2026")

    assert result.ambiguous is False
    assert [item.text for item in result.readings] == ["2026-11-13"]


def test_the_same_number_twice_is_not_ambiguous() -> None:
    """`3/3/2026` 两种顺序读出同一天，所以它不是歧义，只有一条读法。"""
    result = date_text_readings("3/3/2026")

    assert result.ambiguous is False
    assert [item.text for item in result.readings] == ["2026-03-03"]


def test_something_that_is_not_a_date_has_no_readings_at_all() -> None:
    """认不出就是零条读法，**不是**两条瞎猜的。"""
    result = date_text_readings("商家留的手写备注")

    assert result.readings == ()
    assert result.ambiguous is False
    assert result.problem


def test_an_impossible_date_is_refused_in_both_orders() -> None:
    """`2/30/2026` 与 `30/2/2026` 两种读法都不存在 → 零条，而不是挑一个。"""
    assert date_text_readings("2/30/2026").readings == ()


def test_a_two_digit_year_is_still_ambiguous_on_the_day_month_order() -> None:
    """OCR 通路允许两位年份，但日/月顺序仍然要人（或商家路由）来定。"""
    result = date_text_readings("11/08/26", allow_short_year=True)

    assert result.ambiguous is True
    assert sorted(item.text for item in result.readings) == [
        "2026-08-11",
        "2026-11-08",
    ]
    # 不允许两位年份时一条都不给——不会偷偷按 2026 年读。
    assert date_text_readings("11/08/26").readings == ()


def test_an_invalid_date_order_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="date_order"):
        date_text_readings("2026-11-08", date_order="whatever")
