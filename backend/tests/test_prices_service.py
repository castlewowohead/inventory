"""标准化价格与价格统计的业务测试（步骤 09）。

本文件的结构对着 Check List 走：

- 09.10 的三个验收算例在「验收算例」一节，用例名里带 `acceptance`。
- 09.11 的四条拒绝（未知包装、待确认、不可换算、缺基期）在「不静默混算」一节。
- 其余按能力分节：箱规维护、价格历史、月度与期间、来源比价、涨价提醒。

**日期一律相对 `date.today()` 构造**，不写死 `2026-11-13`：统计的核心就是"本月
与上月"，写死日期的用例会在下个月自己变成另一种情况（而且是静默的）。
各用例把采购记录放在"本月 1 日"与"上月 1 日"，这两天在任何真实今天下都落在
可比期间内。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.errors import NotFoundError, ValidationError
from src.services import prices as prices_module
from src.services.ingredients import FoodItemView, IngredientService
from src.services.invoices import ConfirmOption, UploadedFile
from src.services.prices import (
    COMPARISON_AVAILABLE,
    COMPARISON_BASE_PRICE_IS_ZERO,
    COMPARISON_NO_BASE_DATA,
    COMPARISON_NO_CURRENT_DATA,
    CONVERSION_AVAILABLE,
    CONVERSION_INCOMPATIBLE,
    CONVERSION_PENDING_PACK_SPEC,
    CONVERSION_UNIT_UNKNOWN,
    PACK_SPEC_MATCH_ANY_SOURCE,
    PACK_SPEC_MATCH_EXACT,
    PriceService,
    comparable_periods,
    month_start,
    previous_month_start,
    comparable_weeks,
    week_start,
)
from src.services.purchases import PurchaseService

# --- 夹具与小工具 -----------------------------------------------------------


def today() -> date:
    return date.today()


def this_month() -> date:
    """本月 1 日。任何真实今天下它都 ≤ 今天，也一定落在"本月截至今天"里。"""
    return month_start(today())


def last_month() -> date:
    """上月 1 日。一定落在基期（上月同样天数）里，因为基期从 1 日开始。"""
    return previous_month_start(today())


def two_months_ago() -> date:
    return previous_month_start(last_month())


def test_weekly_comparison_uses_matching_weekdays() -> None:
    current, previous = comparable_weeks(date(2026, 11, 19))
    assert (current.date_from, current.date_to, current.days) == (
        date(2026, 11, 16), date(2026, 11, 19), 4
    )
    assert (previous.date_from, previous.date_to, previous.days) == (
        date(2026, 11, 9), date(2026, 11, 12), 4
    )
    assert current.key == "2026-W47" and previous.key == "2026-W46"


def test_weekly_prices_keep_monthly_report_available(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    reference = date.today()
    current_start = week_start(reference)
    previous_start = current_start - timedelta(weeks=1)
    record(purchase_service, food_item, purchase_date=previous_start.isoformat(),
           quantity="10", unit_price="4", line_amount="40")
    record(purchase_service, food_item, purchase_date=current_start.isoformat(),
           quantity="10", unit_price="6", line_amount="60")
    report = price_service.weekly_prices(food_item.id, weeks=3, today=reference)
    assert len(report.weeks) == 3
    assert report.weeks[-1].period.date_from == current_start
    assert report.weeks[-1].weighted_average_unit_price == Decimal("6.0000")
    assert report.comparison.base.weighted_average_unit_price == Decimal("4.0000")
    assert report.comparison.change_percent == Decimal("50.00")
    assert len(price_service.monthly_prices(food_item.id, months=3, today=reference).months) == 3


def record(
    service: PurchaseService,
    food_item: FoodItemView | None,
    **overrides: object,
):
    """录一条采购。默认 10 lb × $6.00 = $60.00，日期在本月 1 日。"""
    payload: dict[str, object] = {
        "food_item_id": food_item.id if food_item is not None else None,
        "quantity": "10",
        "purchase_unit": "lb",
        "unit_price": "6.00",
        "line_amount": "60.00",
        "purchase_date": this_month().isoformat(),
    }
    payload.update(overrides)
    return service.create_purchase(**payload)  # type: ignore[arg-type]


@pytest.fixture
def case_item(ingredient_service: IngredientService) -> FoodItemView:
    """基础单位是 lb 的食材，用于按箱采购的换算测试。"""
    return ingredient_service.create_food_item(
        name="Chicken Wing", name_zh="鸡翅", category_id=1, base_unit="lb"
    )


@pytest.fixture
def gram_item(ingredient_service: IngredientService) -> FoodItemView:
    """基础单位是 kg 的食材，对应 Kun Kee 那种 `1000g x 10` 的箱规。"""
    return ingredient_service.create_food_item(
        name="Frozen Chicken Breast", name_zh="冷冻鸡胸", category_id=1, base_unit="kg"
    )


# --- 09.10 验收算例 ---------------------------------------------------------


def test_acceptance_ten_pounds_for_sixty_dollars_is_six_per_pound(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """10 lb / $60 → $6.00/lb（09.10 第一个算例）。"""
    record(purchase_service, food_item, quantity="10", unit_price="6.00", line_amount="60.00")

    page = price_service.price_history(food_item.id)

    assert len(page.items) == 1
    conversion = page.items[0].conversion
    assert conversion.status == CONVERSION_AVAILABLE
    assert conversion.base_unit == "lb"
    assert conversion.base_quantity == Decimal("10.0000")
    assert conversion.normalized_unit_price == Decimal("6.0000")


def test_acceptance_one_case_of_forty_pounds_for_220_is_5_50_per_pound(
    price_service: PriceService, purchase_service: PurchaseService, case_item
) -> None:
    """1 case = 40 lb，$220 → $5.50/lb（09.10 第二个算例）。

    ⚠️ 走的是箱规那条路：`case` 不是计量单位，必须先有「1 case = 40 lb」这条
    人确认过的事实，否则这一行**不出价格**（见下面的 pending 用例）。
    """
    price_service.save_pack_spec(
        food_item_id=case_item.id,
        purchase_unit="case",
        pack_size="40",
        pack_unit="lb",
        spec_text="40 lb / case",
    )
    record(
        purchase_service,
        case_item,
        quantity="1",
        purchase_unit="case",
        unit_price="220.00",
        line_amount="220.00",
    )

    page = price_service.price_history(case_item.id)
    conversion = page.items[0].conversion

    assert conversion.status == CONVERSION_AVAILABLE
    assert conversion.base_quantity == Decimal("40.0000")
    assert conversion.normalized_unit_price == Decimal("5.5000")
    assert conversion.pack_spec is not None
    assert conversion.pack_spec.pack_size == Decimal("40.0000")


def test_acceptance_weighted_average_is_5_91_not_the_simple_average(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """10 lb × $5 与 100 lb × $6 的加权均价 ≈ $5.91/lb（09.10 第三个算例）。

    ⚠️ 简单平均会得到 $5.50——差 7%，而且总是偏向买得少的那一次。
    `imp_notes.md` Feature 2 明确要求加权：总金额 ÷ 按基础单位换算后的总数量。
    """
    record(purchase_service, food_item, quantity="10", unit_price="5.00", line_amount="50.00")
    record(purchase_service, food_item, quantity="100", unit_price="6.00", line_amount="600.00")

    report = price_service.monthly_prices(food_item.id)
    current = report.comparison.current

    assert current.total_amount == Decimal("650.00")
    assert current.total_base_quantity == Decimal("110.0000")
    # 650 / 110 = 5.909090…，4 位四舍五入 → 5.9091（两位展示即 $5.91）
    assert current.weighted_average_unit_price == Decimal("5.9091")
    assert prices_module.format_price(current.weighted_average_unit_price) == "5.9091"
    simple_average = (Decimal("5.00") + Decimal("6.00")) / 2
    assert current.weighted_average_unit_price != simple_average


def test_weighted_average_mixes_pack_and_weight_purchases(
    price_service: PriceService,
    purchase_service: PurchaseService,
    gram_item,
) -> None:
    """按箱买的和按公斤买的能算进同一个均价——这才是"可比价格"的意义。

    Kun Kee 的真实行：`20 箱 × $52.00 = $1,040.00`，箱规 `1000g x 10`
    → 每箱 10 kg → 200 kg → $5.20/kg。
    """
    price_service.save_pack_spec(
        food_item_id=gram_item.id,
        source_name="Kun Kee Trading",
        purchase_unit="箱",
        pack_size="10000",
        pack_unit="g",
        spec_text="1000g x 10",
    )
    record(
        purchase_service,
        gram_item,
        source_name="Kun Kee Trading",
        quantity="20",
        purchase_unit="箱",
        unit_price="52.00",
        line_amount="1040.00",
    )

    page = price_service.price_history(gram_item.id)
    conversion = page.items[0].conversion

    assert conversion.base_unit == "kg"
    assert conversion.base_quantity == Decimal("200.0000")
    assert conversion.normalized_unit_price == Decimal("5.2000")


# --- 09.11 不静默混算 -------------------------------------------------------


def test_unknown_packaging_does_not_enter_standardized_comparison(
    price_service: PriceService, purchase_service: PurchaseService, case_item
) -> None:
    """未知包装不进标准化价格比较，而且**说得出为什么**（09.11 第一条）。"""
    record(
        purchase_service,
        case_item,
        quantity="2",
        purchase_unit="CS",
        unit_price="100.00",
        line_amount="200.00",
    )

    page = price_service.price_history(case_item.id)
    conversion = page.items[0].conversion

    assert conversion.status == CONVERSION_PENDING_PACK_SPEC
    assert conversion.normalized_unit_price is None  # 不是 0
    assert conversion.base_quantity is None
    assert "包装规格" in (conversion.reason or "")
    assert page.convertible_count == 0
    assert page.excluded.pending_pack_spec == 1
    assert any("采购单位" in note for note in page.notes)


def test_pending_lines_never_reach_the_statistics(
    price_service: PriceService,
    purchase_service: PurchaseService,
    invoice_service,
    food_item,
) -> None:
    """待确认的草稿行不参与统计（09.11 第二条）。

    这条在**结构上**就成立：草稿在 `invoice_lines`，统计只读 `purchases`，
    系统从不自己把数据写进采购历史（步骤 05 的口径）。这里用真实的「三行只确认
    两行」把它钉住，而不是只在文档里声明。
    """
    invoice = invoice_service.create_invoice(
        [
            UploadedFile(
                content=b"\x89PNG\r\n\x1a\n fake photo",
                original_name="invoice.png",
                content_type="image/png",
            )
        ],
        recognize=False,
    )
    added = [
        invoice_service.add_line(
            invoice.id,
            description=f"line {index}",
            quantity="10",
            purchase_unit="lb",
            unit_price="6.00",
            line_amount="60.00",
            food_item_id=food_item.id,
            purchase_date=this_month().isoformat(),
            source_name="Sysco",
        )
        for index in range(3)
    ]
    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=added[0].id),
            ConfirmOption(line_id=added[1].id),
        ],
    )
    assert outcome.confirmed_count == 2

    report = price_service.monthly_prices(food_item.id)

    assert report.comparison.current.purchase_count == 2
    assert report.comparison.current.total_amount == Decimal("120.00")


def test_incompatible_units_are_excluded_and_explained(
    price_service: PriceService,
    purchase_service: PurchaseService,
    ingredient_service: IngredientService,
) -> None:
    """体积买、重量计的记录算不出价格，也不混进均价（09.11 第三条）。"""
    item = ingredient_service.create_food_item(
        name="Soy Sauce", category_id=1, base_unit="lb"
    )
    record(
        purchase_service,
        item,
        quantity="5",
        purchase_unit="gallon",
        unit_price="12.00",
        line_amount="60.00",
    )

    page = price_service.price_history(item.id)
    conversion = page.items[0].conversion

    assert conversion.status == CONVERSION_INCOMPATIBLE
    assert conversion.normalized_unit_price is None
    assert "密度" in (conversion.reason or "")
    assert page.excluded.incompatible_unit == 1


def test_unrecognized_unit_is_excluded_and_explained(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """单位认不出（`件/2`）：不换算、不报错、进"算不出"那一栏。"""
    record(purchase_service, food_item, purchase_unit="件/2")

    page = price_service.price_history(food_item.id)

    assert page.items[0].conversion.status == CONVERSION_UNIT_UNKNOWN
    assert page.excluded.unit_unknown == 1


def test_missing_base_period_is_not_reported_as_zero_percent(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """缺失基期不显示 0% 变化（09.11 第四条）。"""
    record(purchase_service, food_item)  # 只有本月

    report = price_service.monthly_prices(food_item.id)
    comparison = report.comparison

    assert comparison.status == COMPARISON_NO_BASE_DATA
    assert comparison.change_percent is None
    assert comparison.change_amount is None
    assert "0%" in (comparison.reason or "")


def test_missing_current_period_is_distinguished_from_missing_base(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """本月没买过与上月没买过是两种情况，处置方式不同，所以状态分开。"""
    record(purchase_service, food_item, purchase_date=last_month().isoformat())

    comparison = price_service.monthly_prices(food_item.id).comparison

    assert comparison.status == COMPARISON_NO_CURRENT_DATA
    assert comparison.change_percent is None


def test_zero_base_price_does_not_divide_by_zero(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """上月全是赠品（均价 0）：涨幅没有定义，金额变化仍然给出。"""
    record(
        purchase_service,
        food_item,
        purchase_date=last_month().isoformat(),
        unit_price="0",
        line_amount="0",
    )
    record(purchase_service, food_item)

    comparison = price_service.monthly_prices(food_item.id).comparison

    assert comparison.status == COMPARISON_BASE_PRICE_IS_ZERO
    assert comparison.change_percent is None
    assert comparison.change_amount == Decimal("6.0000")


# --- 必须排除的记录（步骤 04 定下的硬口径）---------------------------------


def test_adjustment_lines_never_enter_the_average(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """⚠️ 退货 / 折让（调整行）不参与均价。

    混进去只会让价格**悄悄偏低**：不报错、没人察觉。所以这条由 SQL 的 WHERE
    保证，而不是靠调用方记得传参数。
    """
    record(purchase_service, food_item, quantity="10", unit_price="6.00", line_amount="60.00")
    purchase_service.create_purchase(
        line_kind="adjustment",
        food_item_id=food_item.id,
        quantity="-2",
        purchase_unit="lb",
        unit_price="6.00",
        line_amount="-12.00",
        purchase_date=this_month().isoformat(),
    )

    current = price_service.monthly_prices(food_item.id).comparison.current

    assert current.purchase_count == 1
    assert current.total_amount == Decimal("60.00")
    assert current.weighted_average_unit_price == Decimal("6.0000")


def test_voided_purchases_never_enter_the_average(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    kept = record(purchase_service, food_item)
    voided = record(purchase_service, food_item, unit_price="60.00", line_amount="600.00")
    purchase_service.void_purchase(voided.id, reason="录错了")

    current = price_service.monthly_prices(food_item.id).comparison.current

    assert current.purchase_ids == (kept.id,)
    assert current.weighted_average_unit_price == Decimal("6.0000")


def test_undated_purchases_are_counted_not_silently_dropped(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """没有日期的记录进不了月度统计，但要**数出来**并给出去哪儿找。"""
    record(purchase_service, food_item)
    record(purchase_service, food_item, purchase_date=None)

    report = price_service.monthly_prices(food_item.id)

    assert report.comparison.current.purchase_count == 1
    assert report.excluded.undated == 1
    assert any("没有日期" in note for note in report.notes)


def test_zero_amount_and_mismatch_rows_are_included_but_reported(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """0 元赠品行与金额异常行计入均价，但在响应里单独报数。

    计入的理由：0 元的赠品确实压低了到手成本，那是事实。报数的理由：
    "均价怎么突然降了一半"必须解释得清。
    """
    record(purchase_service, food_item, quantity="10", unit_price="6.00", line_amount="60.00")
    record(purchase_service, food_item, quantity="10", unit_price="0", line_amount="0")
    record(
        purchase_service,
        food_item,
        quantity="10",
        unit_price="6.00",
        line_amount="100.00",
        accept_amount_mismatch=True,
    )

    current = price_service.monthly_prices(food_item.id).comparison.current

    assert current.purchase_count == 3
    assert current.zero_amount_count == 1
    assert current.amount_mismatch_count == 1
    # 行金额是权威：160 / 30 lb
    assert current.total_amount == Decimal("160.00")
    assert current.weighted_average_unit_price == Decimal("5.3333")


# --- 币种 -------------------------------------------------------------------


def test_two_currencies_are_refused_for_an_average(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """同一食材两种币种时**不猜**：报 400 并列出可选币种。"""
    record(purchase_service, food_item, currency="USD")
    record(purchase_service, food_item, currency="CNY")

    with pytest.raises(ValidationError) as caught:
        price_service.monthly_prices(food_item.id)

    assert caught.value.details["available"] == ["CNY", "USD"]
    assert "汇率" in str(caught.value)


def test_explicit_currency_splits_the_statistics(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    record(purchase_service, food_item, currency="USD")
    record(purchase_service, food_item, currency="CNY", unit_price="40.00", line_amount="400.00")

    usd = price_service.monthly_prices(food_item.id, currency="USD")
    cny = price_service.monthly_prices(food_item.id, currency="cny")

    assert usd.comparison.current.weighted_average_unit_price == Decimal("6.0000")
    assert usd.excluded.other_currency == 1
    assert cny.comparison.current.weighted_average_unit_price == Decimal("40.0000")
    assert cny.currency == "CNY"


def test_history_lists_both_currencies_without_summing_them(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """价格历史是逐条清单、不做加总，所以多币种可以混着列——但要标出来。"""
    record(purchase_service, food_item, currency="USD")
    record(purchase_service, food_item, currency="CNY")

    page = price_service.price_history(food_item.id)

    assert page.currency is None
    assert page.available_currencies == ("CNY", "USD")
    assert page.total == 2


def test_unsupported_currency_is_refused(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    record(purchase_service, food_item)

    with pytest.raises(ValidationError) as caught:
        price_service.monthly_prices(food_item.id, currency="EUR")

    assert caught.value.details["field"] == "currency"


# --- 箱规维护 ---------------------------------------------------------------


def test_saving_the_same_key_twice_updates_instead_of_conflicting(
    price_service: PriceService, purchase_service: PurchaseService, case_item
) -> None:
    """同一个（食材, 来源, 包装单位）再提交一次是**更新**。

    报 409 会让老板必须先找到那条旧的再改，而他看到的现象只是"保存不了"。
    """
    record(purchase_service, case_item, purchase_unit="case", quantity="1",
           unit_price="220.00", line_amount="220.00")

    first, created_first, affected_first = price_service.save_pack_spec(
        food_item_id=case_item.id, purchase_unit="case", pack_size="40", pack_unit="lb"
    )
    second, created_second, affected_second = price_service.save_pack_spec(
        food_item_id=case_item.id, purchase_unit="case", pack_size="30", pack_unit="lb"
    )

    assert created_first and not created_second
    assert first.id == second.id
    assert second.pack_size == Decimal("30.0000")
    assert affected_first == affected_second == 1

    page = price_service.price_history(case_item.id)
    # 改箱规 → 历史标准化价格跟着变（口径与步骤 04「统计按当前值算」一致）。
    assert page.items[0].conversion.normalized_unit_price == Decimal("7.3333")


def test_exact_source_pack_spec_wins_over_the_any_source_one(
    price_service: PriceService, purchase_service: PurchaseService, case_item
) -> None:
    """⚠️ 顺序不能颠倒：这家的 44.1 lb 必须盖住通用的 30 lb，差 47%。"""
    price_service.save_pack_spec(
        food_item_id=case_item.id, purchase_unit="case", pack_size="30", pack_unit="lb"
    )
    price_service.save_pack_spec(
        food_item_id=case_item.id,
        source_name="New Moy's Meat Market",
        purchase_unit="case",
        pack_size="44.1",
        pack_unit="lb",
    )
    record(
        purchase_service,
        case_item,
        source_name="new moys meat market",
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
    )
    record(
        purchase_service,
        case_item,
        source_name="Somewhere Else",
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
    )

    page = price_service.price_history(case_item.id, limit=10)
    by_source = {
        row.purchase.source_name: row.conversion for row in page.items
    }

    # ⚠️ 规范化后不是同一家："new moys" 少了撇号，与 "New Moy's" 不是一个 key。
    assert by_source["Somewhere Else"].pack_spec_match == PACK_SPEC_MATCH_ANY_SOURCE
    assert by_source["Somewhere Else"].base_quantity == Decimal("30.0000")

    price_service.save_pack_spec(
        food_item_id=case_item.id,
        source_name="new moys meat market",
        purchase_unit="case",
        pack_size="44.1",
        pack_unit="lb",
    )
    page = price_service.price_history(case_item.id, limit=10)
    by_source = {row.purchase.source_name: row.conversion for row in page.items}
    assert by_source["new moys meat market"].pack_spec_match == PACK_SPEC_MATCH_EXACT
    assert by_source["new moys meat market"].base_quantity == Decimal("44.1000")


def test_pack_spec_requires_a_packaging_unit(
    price_service: PriceService, case_item
) -> None:
    """按 lb 计价的记录不需要箱规，所以 `purchase_unit="lb"` 直接报错并说明原因。"""
    with pytest.raises(ValidationError) as caught:
        price_service.save_pack_spec(
            food_item_id=case_item.id, purchase_unit="lb", pack_size="40", pack_unit="lb"
        )

    assert caught.value.details["unit"] == "lb"
    assert "不需要箱规" in str(caught.value)


def test_pack_spec_unit_must_be_compatible_with_the_base_unit(
    price_service: PriceService, case_item
) -> None:
    """「1 箱 = 5 gallon」配 lb 的食材：存下来也算不出价格，所以当场拒绝。"""
    with pytest.raises(ValidationError) as caught:
        price_service.save_pack_spec(
            food_item_id=case_item.id,
            purchase_unit="case",
            pack_size="5",
            pack_unit="gallon",
        )

    assert caught.value.details["field"] == "pack_unit"
    assert "密度" in str(caught.value)


def test_pack_spec_inner_unit_cannot_be_a_packaging_unit(
    price_service: PriceService, case_item
) -> None:
    with pytest.raises(ValidationError):
        price_service.save_pack_spec(
            food_item_id=case_item.id,
            purchase_unit="case",
            pack_size="6",
            pack_unit="box",
        )


def test_pack_size_must_be_positive(price_service: PriceService, case_item) -> None:
    with pytest.raises(ValidationError):
        price_service.save_pack_spec(
            food_item_id=case_item.id, purchase_unit="case", pack_size="0", pack_unit="lb"
        )


def test_pack_size_rejects_json_floats(price_service: PriceService, case_item) -> None:
    """数值口径沿用步骤 04：JSON 小数直接拒绝，不经 float。"""
    with pytest.raises(ValidationError) as caught:
        price_service.save_pack_spec(
            food_item_id=case_item.id,
            purchase_unit="case",
            pack_size=44.1,
            pack_unit="lb",
        )

    assert "浮点" in str(caught.value) or "小数" in str(caught.value)


def test_deleting_a_pack_spec_only_removes_the_price_not_the_purchase(
    price_service: PriceService, purchase_service: PurchaseService, case_item
) -> None:
    spec, _, _ = price_service.save_pack_spec(
        food_item_id=case_item.id, purchase_unit="case", pack_size="40", pack_unit="lb"
    )
    purchase = record(
        purchase_service,
        case_item,
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
    )

    affected = price_service.delete_pack_spec(spec.id)

    assert affected == 1
    page = price_service.price_history(case_item.id)
    assert page.items[0].conversion.status == CONVERSION_PENDING_PACK_SPEC
    # 采购记录本身一点没变。
    assert purchase_service.get_purchase(purchase.id).line_amount == Decimal("220.00")


def test_deleting_a_missing_pack_spec_is_404(price_service: PriceService) -> None:
    with pytest.raises(NotFoundError):
        price_service.delete_pack_spec(9999)


def test_pack_spec_for_a_missing_food_item_is_404(price_service: PriceService) -> None:
    with pytest.raises(NotFoundError):
        price_service.save_pack_spec(
            food_item_id=9999, purchase_unit="case", pack_size="40", pack_unit="lb"
        )


# --- 待补箱规清单 -----------------------------------------------------------


def test_pending_list_groups_by_item_source_and_unit_sorted_by_amount(
    price_service: PriceService,
    purchase_service: PurchaseService,
    case_item,
    gram_item,
) -> None:
    """按涉及金额从大到小排：先补影响最大的那组。"""
    record(
        purchase_service,
        case_item,
        source_name="Sysco",
        purchase_unit="case",
        quantity="1",
        unit_price="100.00",
        line_amount="100.00",
    )
    for _ in range(2):
        record(
            purchase_service,
            gram_item,
            source_name="Kun Kee",
            purchase_unit="箱",
            quantity="20",
            unit_price="52.00",
            line_amount="1040.00",
        )

    rows, total = price_service.pending_pack_specs()

    assert total == 2
    assert rows[0].food_item.id == gram_item.id
    assert rows[0].purchase_count == 2
    assert rows[0].total_amount == Decimal("2080.00")
    assert rows[0].purchase_unit == "case"  # 箱 归一成 case
    assert rows[1].food_item.id == case_item.id


def test_pending_list_suggests_a_pack_spec_only_when_the_text_is_unambiguous(
    price_service: PriceService, purchase_service: PurchaseService, gram_item, case_item
) -> None:
    """候选只在原文能读出唯一结论时给出。

    - `1000g x 10`（Kun Kee 的真实写法，混在描述里）→ 给候选 10000 g；
    - `6X44.10`（New Moy's 的真实写法）→ **不给**：它可能是"每箱 44.1 磅"，
      也可能是"6 箱 × 44.1 磅"，差 6 倍。
    """
    record(
        purchase_service,
        gram_item,
        original_description="冷冻鸡胸 1000g x 10",
        purchase_unit="箱",
        quantity="20",
        unit_price="52.00",
        line_amount="1040.00",
    )
    record(
        purchase_service,
        case_item,
        original_description="Beef Stomach - 牛肚6X44.10",
        purchase_unit="case",
        quantity="1",
        unit_price="100.00",
        line_amount="100.00",
    )

    rows, _ = price_service.pending_pack_specs()
    by_item = {row.food_item.id: row for row in rows}

    suggestion = by_item[gram_item.id].suggestion
    assert suggestion is not None
    assert suggestion.pack_size == Decimal("10000")
    assert suggestion.pack_unit == "g"
    assert by_item[case_item.id].suggestion is None


def test_pending_list_ignores_rows_that_already_have_a_pack_spec(
    price_service: PriceService, purchase_service: PurchaseService, case_item
) -> None:
    price_service.save_pack_spec(
        food_item_id=case_item.id, purchase_unit="case", pack_size="40", pack_unit="lb"
    )
    record(
        purchase_service,
        case_item,
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
    )

    rows, total = price_service.pending_pack_specs()

    assert total == 0 and rows == ()


def test_pending_list_includes_undated_rows(
    price_service: PriceService, purchase_service: PurchaseService, case_item
) -> None:
    """缺箱规和缺日期是两件独立的待办，一条记录可以同时欠两样。"""
    record(
        purchase_service,
        case_item,
        purchase_date=None,
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
    )

    rows, total = price_service.pending_pack_specs()

    assert total == 1
    assert rows[0].latest_purchase_date is None


# --- 月度序列与期间口径 -----------------------------------------------------


def test_comparable_periods_align_the_day_counts() -> None:
    """本月没过完时，本期与基期的天数对齐（`imp_notes.md` Feature 2 的要求）。"""
    current, base = comparable_periods(date(2026, 11, 13))

    assert (current.date_from, current.date_to) == (date(2026, 11, 1), date(2026, 11, 13))
    assert (base.date_from, base.date_to) == (date(2026, 10, 1), date(2026, 10, 13))
    assert current.days == base.days == 13
    assert current.basis == "month_to_date"


def test_comparable_periods_use_full_months_on_the_last_day() -> None:
    current, base = comparable_periods(date(2026, 11, 30))

    assert current.basis == "full_month"
    assert base.date_to == date(2026, 10, 30)
    assert current.days == 30


def test_comparable_periods_clamp_to_a_shorter_previous_month() -> None:
    """3/31 的上月只有 28 天：基期取到 2/28，并把天数差摆出来。"""
    current, base = comparable_periods(date(2026, 3, 31))

    assert base.date_to == date(2026, 2, 28)
    assert current.days == 31
    assert base.days == 28


def test_monthly_series_covers_the_requested_months(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    record(purchase_service, food_item, purchase_date=two_months_ago().isoformat(),
           unit_price="4.00", line_amount="40.00")
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           unit_price="5.00", line_amount="50.00")
    record(purchase_service, food_item)

    report = price_service.monthly_prices(food_item.id, months=3)
    averages = {
        stats.period.key: stats.weighted_average_unit_price for stats in report.months
    }

    assert len(report.months) == 3
    assert averages[f"{two_months_ago():%Y-%m}"] == Decimal("4.0000")
    assert averages[f"{last_month():%Y-%m}"] == Decimal("5.0000")
    assert averages[f"{this_month():%Y-%m}"] == Decimal("6.0000")


def test_month_to_date_comparison_reports_a_real_change(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """上月 $5/lb、本月 $6/lb → +$1.00、+20%。"""
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           unit_price="5.00", line_amount="50.00")
    record(purchase_service, food_item)

    comparison = price_service.monthly_prices(food_item.id).comparison

    assert comparison.status == COMPARISON_AVAILABLE
    assert comparison.change_amount == Decimal("1.0000")
    assert comparison.change_percent == Decimal("20.00")


def test_months_out_of_range_is_refused(
    price_service: PriceService, food_item
) -> None:
    with pytest.raises(ValidationError):
        price_service.monthly_prices(food_item.id, months=0)
    with pytest.raises(ValidationError):
        price_service.monthly_prices(food_item.id, months=37)


def test_statistics_row_limit_errors_instead_of_truncating(
    price_service: PriceService,
    purchase_service: PurchaseService,
    food_item,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 触顶**报错**而不是少算：截断之后的均价看起来完全正常。"""
    monkeypatch.setattr(prices_module, "STATISTICS_ROW_LIMIT", 2)
    record(purchase_service, food_item)
    record(purchase_service, food_item)

    with pytest.raises(ValidationError) as caught:
        price_service.monthly_prices(food_item.id)

    assert "上限" in str(caught.value)


# --- 来源比价 ---------------------------------------------------------------


def test_source_comparison_ranks_sources_and_reports_the_spread(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """`imp_notes.md` 的例子：Sysco $5.80/lb vs Costco $5.50/lb。"""
    record(purchase_service, food_item, source_name="Sysco", quantity="10",
           unit_price="5.80", line_amount="58.00")
    record(purchase_service, food_item, source_name="Costco", quantity="10",
           unit_price="5.50", line_amount="55.00")

    report = price_service.source_comparison(food_item.id)
    names = [stats.source_name for stats in report.sources]

    assert names == ["Costco", "Sysco"]
    assert report.cheapest_source_normalized == "costco"
    assert report.spread_amount == Decimal("0.3000")
    # 0.30 / 5.50 = 5.4545…% → 5.45%
    assert report.spread_percent == Decimal("5.45")


def test_source_comparison_normalizes_the_same_supplier_written_differently(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """`Costco` / `costco` / `  COSTCO ` 是同一家，不拆成三组。"""
    for name in ("Costco", "costco", "  COSTCO "):
        record(purchase_service, food_item, source_name=name)

    report = price_service.source_comparison(food_item.id)

    assert len(report.sources) == 1
    assert report.sources[0].purchase_count == 3


def test_source_comparison_keeps_descriptions_for_judging_comparability(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """系统只保证单位可比；规格与品质要靠原始描述自己看，所以必须带上它们。"""
    record(purchase_service, food_item, source_name="Sysco",
           original_description="Ground Beef 80/20")
    record(purchase_service, food_item, source_name="Costco",
           original_description="80/20 Ground Chuck")

    report = price_service.source_comparison(food_item.id)
    descriptions = {
        stats.source_name: stats.descriptions for stats in report.sources
    }

    assert descriptions["Sysco"] == ("Ground Beef 80/20",)
    assert descriptions["Costco"] == ("80/20 Ground Chuck",)
    assert any("规格与品质" in note for note in report.notes)


def test_single_source_is_not_called_the_cheapest(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    record(purchase_service, food_item, source_name="Sysco")

    report = price_service.source_comparison(food_item.id)

    assert report.spread_amount is None
    assert any("没有可比对象" in note for note in report.notes)


def test_source_comparison_rejects_an_inverted_window(
    price_service: PriceService, food_item
) -> None:
    with pytest.raises(ValidationError):
        price_service.source_comparison(
            food_item.id, date_from="2026-05-01", date_to="2026-04-01"
        )


def test_statistics_window_may_end_in_the_future(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """⚠️ 查「到年底」是正常请求，不能按录入日期的规则拒掉。

    步骤 04 的 `parse_purchase_date` 拒绝未来日期，理由是"年份敲错或被 OCR 读错
    会让月度统计里凭空多出一个几十年后的月份"——那条规则管的是**被记录下来的
    采购日期**。查询窗口的右端本来就可以在今天之后，只是那段时间还没有采购。
    这条用例是步骤 09 真实数据实跑撞出来的回归。
    """
    record(purchase_service, food_item, source_name="Sysco")
    end_of_next_year = date(date.today().year + 1, 12, 31)

    report = price_service.source_comparison(
        food_item.id, date_from=this_month().isoformat(), date_to=end_of_next_year.isoformat()
    )

    assert report.period.date_to == end_of_next_year
    assert len(report.sources) == 1


def test_statistics_window_still_rejects_an_absurdly_early_year(
    price_service: PriceService, food_item
) -> None:
    with pytest.raises(ValidationError) as caught:
        price_service.source_comparison(food_item.id, date_from="0026-01-01")

    assert caught.value.details["field"] == "date_from"


# --- 涨价提醒 ---------------------------------------------------------------


def test_alert_settings_default_to_ten_percent_and_enabled(
    price_service: PriceService,
) -> None:
    settings = price_service.get_alert_settings()

    assert settings.enabled is True
    assert settings.threshold_percent == Decimal("10.00")


def test_alert_settings_can_be_changed_and_turned_off(
    price_service: PriceService,
) -> None:
    updated = price_service.update_alert_settings(enabled=False, threshold_percent="25")

    assert updated.enabled is False
    assert updated.threshold_percent == Decimal("25")
    assert price_service.get_alert_settings().threshold_percent == Decimal("25")


def test_alert_settings_update_requires_a_field(price_service: PriceService) -> None:
    with pytest.raises(ValidationError):
        price_service.update_alert_settings()


def test_absurd_threshold_is_refused(price_service: PriceService) -> None:
    with pytest.raises(ValidationError):
        price_service.update_alert_settings(threshold_percent="5000")


def test_alert_fires_above_the_threshold_and_is_traceable(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """上月 $5/lb → 本月 $6/lb 是 +20%，超过默认 10%。

    `purchase_ids` 必须给出来：`imp_notes.md` 要求「均价、涨幅和提醒应能追溯到
    参与计算的采购记录」。
    """
    base = record(purchase_service, food_item, purchase_date=last_month().isoformat(),
                  unit_price="5.00", line_amount="50.00")
    current = record(purchase_service, food_item)

    report = price_service.alerts()

    assert len(report.alerts) == 1
    alert = report.alerts[0]
    assert alert.food_item.id == food_item.id
    assert alert.comparison.change_percent == Decimal("20.00")
    assert alert.comparison.current.purchase_ids == (current.id,)
    assert alert.comparison.base.purchase_ids == (base.id,)


def test_alert_does_not_fire_below_the_threshold(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           unit_price="5.90", line_amount="59.00")
    record(purchase_service, food_item)  # +1.69%

    report = price_service.alerts()

    assert report.alerts == ()
    assert report.compared_count == 1


def test_price_drops_never_alert(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           unit_price="12.00", line_amount="120.00")
    record(purchase_service, food_item)  # -50%

    report = price_service.alerts()

    assert report.alerts == ()
    assert any("只提示上涨" in note for note in report.notes)


def test_threshold_override_does_not_touch_the_saved_setting(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           unit_price="5.90", line_amount="59.00")
    record(purchase_service, food_item)

    report = price_service.alerts(threshold_percent="1")

    assert len(report.alerts) == 1
    assert report.threshold_percent == Decimal("1")
    assert price_service.get_alert_settings().threshold_percent == Decimal("10.00")


def test_disabled_alerts_return_nothing_and_say_so(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """关掉就是关掉，不要"虽然关了但还是给你看一眼"。"""
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           unit_price="5.00", line_amount="50.00")
    record(purchase_service, food_item)
    price_service.update_alert_settings(enabled=False)

    report = price_service.alerts()

    assert report.alerts == ()
    assert report.compared_count == 0
    assert any("已关闭" in note for note in report.notes)


def test_alerts_skip_items_that_cannot_be_compared(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """比较不了的食材要按原因计数——那不是"没涨"。"""
    record(purchase_service, food_item)  # 只有本月，没有基期

    report = price_service.alerts()

    assert report.alerts == ()
    assert report.skipped == {COMPARISON_NO_BASE_DATA: 1}


def test_alerts_do_not_mix_currencies(
    price_service: PriceService, purchase_service: PurchaseService, food_item
) -> None:
    """按（食材, 币种）分组比较，所以多币种不会报 400，也不会被相加。"""
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           currency="USD", unit_price="5.00", line_amount="50.00")
    record(purchase_service, food_item, currency="USD")
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           currency="CNY", unit_price="30.00", line_amount="300.00")
    record(purchase_service, food_item, currency="CNY",
           unit_price="40.00", line_amount="400.00")

    report = price_service.alerts()
    by_currency = {alert.currency: alert for alert in report.alerts}

    assert set(by_currency) == {"USD", "CNY"}
    assert by_currency["USD"].comparison.change_percent == Decimal("20.00")
    assert by_currency["CNY"].comparison.change_percent == Decimal("33.33")


def test_alerts_sort_by_change_percent(
    price_service: PriceService,
    purchase_service: PurchaseService,
    food_item,
    other_food_item,
) -> None:
    record(purchase_service, food_item, purchase_date=last_month().isoformat(),
           unit_price="5.00", line_amount="50.00")
    record(purchase_service, food_item)  # +20%
    record(purchase_service, other_food_item, purchase_date=last_month().isoformat(),
           unit_price="2.00", line_amount="20.00")
    record(purchase_service, other_food_item, unit_price="6.00", line_amount="60.00")  # +200%

    report = price_service.alerts()

    assert [alert.food_item.id for alert in report.alerts] == [
        other_food_item.id,
        food_item.id,
    ]


# --- 单位目录 ---------------------------------------------------------------


def test_units_catalog_states_the_volume_system_and_the_two_refusals() -> None:
    catalog = PriceService.units_catalog()

    assert catalog["volume_system"] == "US"
    assert sorted(catalog["packaging_units"]) == [
        "bag",
        "bottle",
        "box",
        "can",
        "carton",
        "case",
        "cup",
        "jar",
        "pack",
    ]
    # ⚠️ `cup` 在包装单位里而**不在** mass/volume/count 任何一个量纲里。
    # 1 US cup = 236.588 mL 是真的，但单据上的「48 cups 酸奶」指 48 个杯装，
    # 不是 48 杯容积；差的正好是一杯装里有多少，而那是箱规要回答的问题。
    all_units = {
        entry["unit"]
        for dimension in catalog["dimensions"]
        for entry in dimension["units"]
    }
    assert "cup" not in all_units and "can" not in all_units
    assert any("密度" in rule for rule in catalog["rules"])
    assert any("箱规" in rule for rule in catalog["rules"])
