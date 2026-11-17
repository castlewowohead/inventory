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


def test_impossible_dates_are_refused() -> None:
    assert normalize_date_text("2026-02-30").text is None


def test_auto_is_the_default_for_the_ocr_path() -> None:
    assert normalize_number_text("$1,991.38", number_format=NUMBER_FORMAT_AUTO).text == "1991.38"
