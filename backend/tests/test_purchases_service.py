"""采购记录服务的业务规则测试（步骤 04）。

重点是步骤 04 的验收条件与那几条口径决定：

- 金额与数量**原值进原值出**，且底层确实不是浮点；
- 无效数值被拒绝，超位数**不被四舍五入**；
- 0 元行（赠品）可存，负数（退货）被拒；
- 行金额是权威，小差额静默接受、大差额要显式确认；
- 采购日期可空，未填日期的能单独筛出来；
- 纠错是直接修改 + 修改日志，删除是软删；
- 调整行（退货 / 折让）金额可以为负、可以没有食材，但默认不出现在查询结果里。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.errors import AmountMismatchError
from src.services.ingredients import PACKAGING_UNITS, FoodItemView, IngredientService
from src.services.purchases import (
    AMOUNT_TOLERANCE,
    PACKAGING_UNIT_SYNONYMS,
    PurchaseService,
    format_decimal,
    format_money,
    format_quantity,
    normalize_purchase_unit,
)


def record(
    service: PurchaseService,
    food_item: FoodItemView,
    **overrides: object,
):
    """录一条采购。默认是一张能对得上的正常单据行：10 lb × $6.00 = $60.00。"""
    payload: dict[str, object] = {
        "food_item_id": food_item.id,
        "quantity": "10",
        "purchase_unit": "lb",
        "unit_price": "6.00",
        "line_amount": "60.00",
        "purchase_date": "2026-11-08",
    }
    payload.update(overrides)
    return service.create_purchase(**payload)  # type: ignore[arg-type]


# --- 精确数值（04.2 / 04.9）-------------------------------------------------


@pytest.mark.parametrize(
    "column, value",
    [
        ("line_amount", "1234567.89"),
        ("unit_price", "0.07"),
        ("quantity", "12.345"),
    ],
)
def test_acceptance_values_round_trip_unchanged(
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    column: str,
    value: str,
) -> None:
    """步骤 04 验收点名的三个值：原值进，原值出。

    金额不等式会被 `_check_amount` 拦住，所以这里一并把三者调成自洽的，
    单独验证「存取不丢精度」这一件事。
    """
    amounts = {"quantity": "1", "unit_price": "1.00", "line_amount": "1.00"}
    amounts[column] = value
    if column == "quantity":
        amounts["line_amount"] = "12.35"  # 12.345 × 1.00，差 $0.005 在容差内
    elif column == "unit_price":
        amounts["line_amount"] = "0.07"
    elif column == "line_amount":
        amounts["quantity"] = "1234567.89"

    view = record(purchase_service, food_item, **amounts)
    reloaded = purchase_service.get_purchase(view.id)

    assert getattr(reloaded, column) == Decimal(value)
    rendered = (
        format_quantity(reloaded.quantity)
        if column == "quantity"
        else format_money(getattr(reloaded, column))
    )
    assert rendered == value


def test_values_are_stored_as_integers_not_floats(
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    migrated_database: Database,
) -> None:
    """数值列在 SQLite 里就是 INTEGER。

    这是精度的根据：`Numeric` 在 SQLite 下落成 REAL，超位数会被静默改掉、
    SQL 侧求和是浮点运算。断言 `typeof()` 才能证明没有走回那条路。
    """
    record(
        purchase_service,
        food_item,
        quantity="12.345",
        unit_price="0.07",
        line_amount="0.86",
    )

    with migrated_database.unit_of_work() as session:
        row = session.execute(
            text(
                "select quantity, typeof(quantity), unit_price, typeof(unit_price), "
                "line_amount, typeof(line_amount) from purchases"
            )
        ).one()

    assert row[1] == "integer" and row[0] == 123450  # 12.345 × 10^4
    assert row[3] == "integer" and row[2] == 700  # 0.07 × 10^4
    assert row[5] == "integer" and row[4] == 86  # 0.86 × 10^2


def test_values_survive_a_restart(
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    migrated_database: Database,
) -> None:
    """换一个 Database 实例连同一个文件（等价于重启进程），值仍然精确。"""
    created = record(
        purchase_service, food_item, quantity="12.345", unit_price="0.07", line_amount="0.86"
    )
    url = migrated_database.url
    migrated_database.dispose()

    restarted = Database(url)
    try:
        after = PurchaseService(restarted).get_purchase(created.id)
        assert after.quantity == Decimal("12.345")
        assert after.unit_price == Decimal("0.07")
        assert after.line_amount == Decimal("0.86")
    finally:
        restarted.dispose()


def test_more_decimals_than_allowed_are_rejected_not_rounded(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """超出 scale 的位数报错，不悄悄四舍五入——舍入是无声地改数据。"""
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, quantity="12.34567")

    assert "小数位数最多 4 位" in caught.value.message
    assert caught.value.details["max_decimal_places"] == 4


def test_json_float_is_rejected_with_an_explanation(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """JSON 小数（Python 的 float）被拒绝：它在送到这里之前就已经过了 double。"""
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, quantity=12.345)

    assert "不能用 JSON 小数传递" in caught.value.message


def test_booleans_are_not_mistaken_for_numbers(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """`bool` 是 `int` 的子类，不先挡住的话 True 会被当成数量 1。"""
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, quantity=True)

    assert "不能是布尔值" in caught.value.message


@pytest.mark.parametrize(
    "raw", ["1,234.56", "$6.00", "1e5", "６.００", "abc", "", "  ", "12.3.4", "NaN"]
)
def test_non_canonical_number_formats_are_rejected(
    purchase_service: PurchaseService, food_item: FoodItemView, raw: str
) -> None:
    """只收规范十进制：千分位、货币符号、科学计数法与全角数字都归步骤 06 的清洗。"""
    with pytest.raises(ValidationError):
        record(purchase_service, food_item, unit_price=raw)


@pytest.mark.parametrize("raw", ["-1", "-0.01"])
def test_negative_numbers_are_rejected_on_purchase_lines(
    purchase_service: PurchaseService, food_item: FoodItemView, raw: str
) -> None:
    """正常采购行不收负数，并指出负数该用调整行录。

    调整行自己能不能用负数见 `test_adjustment_lines_accept_negative_amounts`。
    """
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, unit_price=raw, line_amount="0")

    assert "line_kind" in caught.value.message
    assert caught.value.details["hint_line_kind"] == "adjustment"


def test_zero_quantity_is_rejected(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, quantity="0", line_amount="0")

    assert caught.value.details["field"] == "quantity"


def test_zero_price_and_amount_are_allowed_for_free_items(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """赠品行（买十送一的那一行）是 0 元，必须能录进来。"""
    view = record(
        purchase_service, food_item, quantity="1", unit_price="0", line_amount="0"
    )

    assert view.unit_price == Decimal("0")
    assert view.line_amount == Decimal("0")
    assert format_money(view.unit_price) == "0.00"


def test_integer_part_has_an_upper_bound(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, line_amount="1234567890123.00")

    assert caught.value.details["max_integer_digits"] == 12


# --- 行金额是权威（04.4）----------------------------------------------------


def test_supplier_rounding_is_accepted_silently(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """11.3 lb × $6.35 = $71.755，单据上印 $71.76：差 $0.005，不该烦老板。"""
    view = record(
        purchase_service,
        food_item,
        quantity="11.3",
        unit_price="6.35",
        line_amount="71.76",
    )

    assert view.amount_mismatch is False
    assert view.line_amount == Decimal("71.76")


def test_real_typo_is_blocked_until_explicitly_confirmed(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """3 × $10 印成 $35：差 $5，先拦住；核对后带标记重发才入账。"""
    with pytest.raises(AmountMismatchError) as caught:
        record(
            purchase_service,
            food_item,
            quantity="3",
            unit_price="10.00",
            line_amount="35.00",
        )

    assert caught.value.code == "amount_mismatch"
    assert caught.value.status_code == 400
    assert caught.value.details["expected"] == "30.00"
    assert caught.value.details["line_amount"] == "35.00"
    assert caught.value.details["difference"] == "5.00"

    view = record(
        purchase_service,
        food_item,
        quantity="3",
        unit_price="10.00",
        line_amount="35.00",
        accept_amount_mismatch=True,
    )

    # 行金额是权威：按单据上印的 $35 入账，不拿单价反算成 $30。
    assert view.line_amount == Decimal("35.00")
    assert view.amount_mismatch is True


def test_tolerance_boundary_is_inclusive(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """差额恰好等于容差时放过；再多一分就要确认。"""
    at_limit = record(
        purchase_service,
        food_item,
        quantity="10",
        unit_price="6.00",
        line_amount=format_money(Decimal("60.00") + AMOUNT_TOLERANCE),
    )
    assert at_limit.amount_mismatch is False

    with pytest.raises(AmountMismatchError):
        record(
            purchase_service,
            food_item,
            quantity="10",
            unit_price="6.00",
            line_amount=format_money(
                Decimal("60.00") + AMOUNT_TOLERANCE + Decimal("0.01")
            ),
        )


# --- 单位、来源、日期 -------------------------------------------------------


def test_packaging_units_are_valid_purchase_units(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """`case` 不能当食材的基础单位，但完全可以是采购单位——发票上就这么写。"""
    view = record(purchase_service, food_item, purchase_unit="CS")

    assert view.purchase_unit == "CS"  # 原文保留
    assert view.purchase_unit_normalized == "case"


def test_unrecognized_unit_does_not_block_the_entry(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """认不出的单位存 NULL 且不阻断录入；原文留着，换算留给步骤 09。"""
    view = record(purchase_service, food_item, purchase_unit="件/2")

    assert view.purchase_unit == "件/2"
    assert view.purchase_unit_normalized is None


def test_purchase_unit_is_required(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, purchase_unit="   ")

    assert caught.value.details["field"] == "purchase_unit"


@pytest.mark.parametrize(
    "raw, expected",
    [("lbs", "lb"), ("LB", "lb"), ("磅", "lb"), ("箱", "case"), ("袋", "bag"), ("pk", "pack")],
)
def test_unit_synonyms_normalize(raw: str, expected: str) -> None:
    assert normalize_purchase_unit(raw) == expected


def test_packaging_synonyms_cover_step_03_set() -> None:
    """本模块的归一表与步骤 03 的包装单位集合必须一致，否则两处各自演化。

    步骤 03 用那个集合**拒绝**把 case 当基础单位；这里把同样的写法**归一**成 token。
    少一个键就意味着某种写法在录入时认不出来。
    """
    assert set(PACKAGING_UNIT_SYNONYMS) == set(PACKAGING_UNITS)


def test_source_name_variants_group_together(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """Costco / costco / "  COSTCO " 是同一个来源，步骤 09 比价才不会拆成三组。"""
    for written in ("Costco", "costco", "  COSTCO "):
        record(purchase_service, food_item, source_name=written)

    page = purchase_service.search_purchases(source="costco")

    assert page.total == 3
    # 展示用的原文各自保留，不被规范化覆盖。
    assert {item.source_name for item in page.items} == {"Costco", "costco", "COSTCO"}


def test_purchase_date_may_be_omitted(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """手写单上没写日期、或日期认不出时，允许留空直接保存。"""
    view = record(purchase_service, food_item, purchase_date=None)

    assert view.purchase_date is None


def test_undated_records_can_be_listed_separately(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """没有这条筛选，未填日期的记录会悄悄从价格分析里消失，老板永远不知道。"""
    record(purchase_service, food_item, purchase_date="2026-11-08")
    missing = record(purchase_service, food_item, purchase_date=None)

    pending = purchase_service.search_purchases(undated=True)
    dated = purchase_service.search_purchases(undated=False)

    assert [item.id for item in pending.items] == [missing.id]
    assert missing.id not in {item.id for item in dated.items}
    assert purchase_service.search_purchases().total == 2


def test_undated_records_sort_last(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """未填日期的不该挤在最新采购前面。"""
    old = record(purchase_service, food_item, purchase_date="2026-10-01")
    new = record(purchase_service, food_item, purchase_date="2026-11-08")
    undated = record(purchase_service, food_item, purchase_date=None)

    page = purchase_service.search_purchases()

    assert [item.id for item in page.items] == [new.id, old.id, undated.id]


def test_future_dates_are_rejected(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """年份很容易敲错或被 OCR 读错（2026 → 2062），放过会凭空多出一个未来的月份。"""
    far = (date.today() + timedelta(days=30)).isoformat()

    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, purchase_date=far)

    assert "未来日期" in caught.value.message


@pytest.mark.parametrize("raw", ["08/11/2026", "2026-13-01", "1999-01-01", "昨天"])
def test_unparseable_dates_are_rejected(
    purchase_service: PurchaseService, food_item: FoodItemView, raw: str
) -> None:
    with pytest.raises(ValidationError):
        record(purchase_service, food_item, purchase_date=raw)


def test_date_range_filter(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    record(purchase_service, food_item, purchase_date="2026-09-30")
    inside = record(purchase_service, food_item, purchase_date="2026-10-15")
    record(purchase_service, food_item, purchase_date="2026-11-08")

    page = purchase_service.search_purchases(
        date_from="2026-10-01", date_to="2026-10-31"
    )

    assert [item.id for item in page.items] == [inside.id]


def test_reversed_date_range_is_rejected(purchase_service: PurchaseService) -> None:
    with pytest.raises(ValidationError) as caught:
        purchase_service.search_purchases(
            date_from="2026-11-01", date_to="2026-10-01"
        )

    assert caught.value.details["field"] == "date_from"


@pytest.mark.parametrize("raw", ["EUR", "usd ", "JPY"])
def test_currency_is_whitelisted(
    purchase_service: PurchaseService, food_item: FoodItemView, raw: str
) -> None:
    if raw.strip().upper() == "USD":
        assert record(purchase_service, food_item, currency=raw).currency == "USD"
        return
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, currency=raw)
    assert caught.value.details["field"] == "currency"


# --- 历史可查、不互相覆盖（04.5）--------------------------------------------


def test_repeated_purchases_accumulate_without_overwriting(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """同一食材多次采购按时间可查，新价格不覆盖旧价格。"""
    record(
        purchase_service,
        food_item,
        purchase_date="2026-09-15",
        unit_price="5.50",
        line_amount="55.00",
    )
    record(
        purchase_service,
        food_item,
        purchase_date="2026-10-15",
        unit_price="5.80",
        line_amount="58.00",
    )
    record(
        purchase_service,
        food_item,
        purchase_date="2026-11-08",
        unit_price="6.00",
        line_amount="60.00",
    )

    page = purchase_service.search_purchases(food_item_id=food_item.id)

    assert page.total == 3
    assert [format_money(item.unit_price) for item in page.items] == [
        "6.00",
        "5.80",
        "5.50",
    ]


def test_search_filters_by_food_item(
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    other_food_item: FoodItemView,
) -> None:
    mine = record(purchase_service, food_item)
    record(purchase_service, other_food_item)

    page = purchase_service.search_purchases(food_item_id=food_item.id)

    assert [item.id for item in page.items] == [mine.id]


def test_pagination_reports_the_full_total(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    for day in range(1, 6):
        record(purchase_service, food_item, purchase_date=f"2026-11-0{day}")

    first = purchase_service.search_purchases(limit=2, offset=0)
    second = purchase_service.search_purchases(limit=2, offset=2)

    assert first.total == second.total == 5
    assert len(first.items) == len(second.items) == 2
    assert not {item.id for item in first.items} & {item.id for item in second.items}


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_limit_is_bounded(purchase_service: PurchaseService, limit: int) -> None:
    with pytest.raises(ValidationError):
        purchase_service.search_purchases(limit=limit)


def test_unknown_food_item_is_a_bad_request_not_a_missing_resource(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """被操作的资源是采购记录；食材只是请求里的一个引用，写错属于请求不合法。"""
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, food_item_id=99999)

    assert caught.value.status_code == 400
    assert caught.value.details["field"] == "food_item_id"


def test_food_item_in_use_cannot_be_deleted(
    purchase_service: PurchaseService,
    ingredient_service: IngredientService,
    food_item: FoodItemView,
) -> None:
    """步骤 03 的「无引用才可删」现在真的有引用方了，由外键拦住。"""
    record(purchase_service, food_item)

    with pytest.raises(ConflictError) as caught:
        ingredient_service.delete_food_item(food_item.id)

    assert "已被采购记录或发票引用" in caught.value.message


# --- 纠错：直接修改 + 修改日志（04.6）---------------------------------------


def test_changing_the_food_item_is_a_plain_update(
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    other_food_item: FoodItemView,
) -> None:
    """最常见的纠错：匹配成了牛肉末，实际是牛筋。直接改，不用作废重录。"""
    created = record(purchase_service, food_item)

    updated = purchase_service.update_purchase(
        created.id, food_item_id=other_food_item.id
    )

    assert updated.food_item.id == other_food_item.id
    assert updated.quantity == created.quantity  # 其余字段没动


def test_correcting_a_number_records_before_and_after(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """$58 其实是 $85：当前值改成 85，日志里留下 58。

    这份日志不是可选的——统计按当前值算，上个月的均价会随修正变化，
    老板看到不同的数字时只能靠它解释。
    """
    created = record(
        purchase_service,
        food_item,
        quantity="10",
        unit_price="5.80",
        line_amount="58.00",
    )

    updated = purchase_service.update_purchase(
        created.id,
        unit_price="8.50",
        line_amount="85.00",
        reason="单据复核：单价看错了",
    )

    assert updated.line_amount == Decimal("85.00")
    assert updated.revision_count == 1

    (revision,) = purchase_service.list_revisions(created.id)
    assert revision.action == "update"
    assert revision.reason == "单据复核：单价看错了"
    assert revision.changed_fields["unit_price"] == {
        "before": "5.80",
        "after": "8.50",
    }
    assert revision.changed_fields["line_amount"] == {
        "before": "58.00",
        "after": "85.00",
    }


def test_revision_log_keeps_numbers_as_exact_strings(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """日志里的数值是字符串，不经 JSON 浮点中转——否则日志自己就丢精度了。"""
    created = record(
        purchase_service, food_item, quantity="12.345", line_amount="74.07"
    )

    purchase_service.update_purchase(created.id, quantity="0.07", line_amount="0.42")

    (revision,) = purchase_service.list_revisions(created.id)
    assert revision.changed_fields["quantity"] == {"before": "12.345", "after": "0.07"}


def test_updating_nothing_writes_no_revision(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """「点了保存但没改东西」不该在历史里留一条空记录。"""
    created = record(purchase_service, food_item)

    updated = purchase_service.update_purchase(created.id, unit_price="6.00")

    assert updated.revision_count == 0
    assert purchase_service.list_revisions(created.id) == []


def test_update_revalidates_the_amount_relationship(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """改完之后三个数字还得对得上，否则纠错会制造出新的不一致。"""
    created = record(purchase_service, food_item)

    with pytest.raises(AmountMismatchError):
        purchase_service.update_purchase(created.id, unit_price="9.00")

    updated = purchase_service.update_purchase(
        created.id, unit_price="9.00", accept_amount_mismatch=True
    )
    assert updated.amount_mismatch is True


def test_update_rejects_invalid_numbers_too(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    created = record(purchase_service, food_item)

    with pytest.raises(ValidationError):
        purchase_service.update_purchase(created.id, quantity="-5")


def test_optional_fields_can_be_cleared(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """显式传 null 表示清空；未出现的字段保持原值。"""
    created = record(
        purchase_service, food_item, source_name="Costco", original_description="GRND BF"
    )

    updated = purchase_service.update_purchase(created.id, source_name=None)

    assert updated.source_name is None
    assert updated.original_description == "GRND BF"


def test_updating_a_missing_purchase_is_a_404(
    purchase_service: PurchaseService,
) -> None:
    with pytest.raises(NotFoundError):
        purchase_service.update_purchase(99999, unit_price="1.00")


# --- 作废是软删（04.6）------------------------------------------------------


def test_voiding_hides_the_record_but_keeps_it(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """软删：默认查不到，但记录还在——步骤 05 的发票行要能追溯到它。"""
    created = record(purchase_service, food_item)

    purchase_service.void_purchase(created.id, reason="重复录入")

    assert purchase_service.search_purchases().total == 0
    assert purchase_service.search_purchases(include_voided=True).total == 1

    still_there = purchase_service.get_purchase(created.id)
    assert still_there.voided_at is not None

    (revision,) = purchase_service.list_revisions(created.id)
    assert revision.action == "void"
    assert revision.reason == "重复录入"


def test_voided_records_cannot_be_edited(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    created = record(purchase_service, food_item)
    purchase_service.void_purchase(created.id)

    with pytest.raises(ConflictError) as caught:
        purchase_service.update_purchase(created.id, unit_price="7.00")

    assert "已作废" in caught.value.message


def test_voiding_twice_is_a_conflict(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    created = record(purchase_service, food_item)
    purchase_service.void_purchase(created.id)

    with pytest.raises(ConflictError):
        purchase_service.void_purchase(created.id)


# --- 录入来源与本步骤的边界 -------------------------------------------------


def test_entry_source_defaults_to_manual_and_is_whitelisted(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """步骤 11 要按手工 / 发票 / 表格三条入口分别核对，所以必须分得出来。"""
    assert record(purchase_service, food_item).entry_source == "manual"
    assert (
        record(purchase_service, food_item, entry_source="invoice").entry_source
        == "invoice"
    )
    with pytest.raises(ValidationError):
        record(purchase_service, food_item, entry_source="ocr")


def test_no_normalized_price_is_produced(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """本步骤不换算：视图里没有任何标准化价格字段（那是步骤 09 的派生值）。"""
    view = record(purchase_service, food_item, purchase_unit="case")

    assert not any(
        name.startswith("normalized") or "standard" in name
        for name in view.__slots__
    )


def test_recording_path_does_not_depend_on_the_units_module() -> None:
    """录入路径不换算：`services/purchases.py` 不依赖步骤 09 的换算层。

    ⚠️ 这条用例**换过一次判据**。步骤 04—08 期间它断言的是
    `import src.services.units` 抛 `ModuleNotFoundError`（那时换算还不该存在）；
    步骤 09 把该模块建起来之后，原判据只能证明"文件还没写"，证不了它要守的那件事。

    要守的事没变：**采购记录的存取与换算无关**。原始数量、单位与金额是人工复核过
    的事实，标准化价格是派生值（`docs/stru_dev.md` 09.5）；录入时去碰换算，就会
    出现"存进去的数被换算改过一次"这种无法追溯的情况。所以判据改成"录入模块的
    导入里没有 units"。
    """
    import src.services.purchases as purchases_module

    source = Path(purchases_module.__file__).read_text(encoding="utf-8")
    import_lines = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "units" in line
    ]
    assert import_lines == [], f"步骤 04 的录入路径不应依赖换算层：{import_lines}"


# --- 调整行：退货 / 折让 / 其它调整 -----------------------------------------
#
# 口径（见 docs/stru_dev.md 步骤 04）：调整行的数量与金额可以为负，食材可以不填
# （整单折让），留在库里是为了对账，但**默认不出现在查询结果里**、也不参与步骤 09
# 的价格统计。


def adjustment(
    service: PurchaseService,
    food_item: FoodItemView | None = None,
    **overrides: object,
):
    """录一条调整行。默认是退回 2 lb @ $6.00，金额 -$12.00。"""
    payload: dict[str, object] = {
        "line_kind": "adjustment",
        "food_item_id": food_item.id if food_item is not None else None,
        "quantity": "-2",
        "purchase_unit": "lb",
        "unit_price": "6.00",
        "line_amount": "-12.00",
        "purchase_date": "2026-11-08",
    }
    payload.update(overrides)
    return service.create_purchase(**payload)  # type: ignore[arg-type]


def test_adjustment_lines_accept_negative_amounts(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """退货行：数量为负、金额为负，三者仍然自洽（-2 × 6.00 = -12.00）。"""
    view = adjustment(purchase_service, food_item)

    assert view.line_kind == "adjustment"
    assert view.quantity == Decimal("-2")
    assert view.line_amount == Decimal("-12.00")
    assert view.amount_mismatch is False
    assert format_money(view.line_amount) == "-12.00"


def test_discount_line_needs_no_food_item(
    purchase_service: PurchaseService,
) -> None:
    """整单折让（「本单让 $20」）在单据上不指向任何一种食材。

    强行挂到某个食材上会污染那个食材的采购历史，所以调整行允许不填食材。
    """
    view = adjustment(
        purchase_service,
        None,
        quantity="1",
        unit_price="-20.00",
        line_amount="-20.00",
        original_description="EARLY PAY DISCOUNT 2%",
    )

    assert view.food_item is None
    assert view.line_amount == Decimal("-20.00")


def test_purchase_lines_still_require_a_food_item(
    purchase_service: PurchaseService,
) -> None:
    """放宽的只是调整行。正常采购行没有食材就无从谈价格历史。"""
    with pytest.raises(ValidationError) as caught:
        purchase_service.create_purchase(
            food_item_id=None,
            quantity="10",
            purchase_unit="lb",
            unit_price="6.00",
            line_amount="60.00",
        )

    assert caught.value.details["field"] == "food_item_id"
    assert "adjustment" in caught.value.message


def test_adjustment_amount_cannot_be_zero(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """0 元的退货或折让没有意义，放过只会留下一条说不清是什么的行。"""
    with pytest.raises(ValidationError) as caught:
        adjustment(
            purchase_service, food_item, quantity="1", unit_price="0", line_amount="0"
        )

    assert caught.value.details["field"] == "line_amount"


def test_adjustment_still_checks_the_amount_relationship(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """符号放宽了，但「数量 × 单价 ≈ 行金额」这条校验照旧——负数也会算错。"""
    with pytest.raises(AmountMismatchError):
        adjustment(purchase_service, food_item, line_amount="-50.00")

    forced = adjustment(
        purchase_service,
        food_item,
        line_amount="-50.00",
        accept_amount_mismatch=True,
    )
    assert forced.amount_mismatch is True


def test_adjustments_are_excluded_from_search_by_default(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """默认排除，不是默认包含。

    两种错法的代价不对称：对账漏了调整行，各行相加对不上总额，一眼看得出；
    把一笔退货混进均价只会让价格悄悄偏低，没人会发现。
    """
    bought = record(purchase_service, food_item)
    returned = adjustment(purchase_service, food_item)

    default = purchase_service.search_purchases()
    only_adjustments = purchase_service.search_purchases(kind="adjustment")
    everything = purchase_service.search_purchases(kind="all")

    assert [item.id for item in default.items] == [bought.id]
    assert default.total == 1
    assert [item.id for item in only_adjustments.items] == [returned.id]
    assert {item.id for item in everything.items} == {bought.id, returned.id}
    assert everything.total == 2


def test_adjustments_are_excluded_from_a_food_items_history(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """按食材查采购历史时也默认排除——那是价格历史，不该混进退货。"""
    record(purchase_service, food_item, unit_price="6.00", line_amount="60.00")
    adjustment(purchase_service, food_item)

    history = purchase_service.search_purchases(food_item_id=food_item.id)

    assert history.total == 1
    assert all(item.line_kind == "purchase" for item in history.items)
    assert all(item.line_amount > 0 for item in history.items)


def test_reconciliation_can_ask_for_everything(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """对账口径：kind="all" 时各行金额相加等于单据总额。"""
    record(purchase_service, food_item, unit_price="6.00", line_amount="60.00")
    adjustment(purchase_service, food_item)  # -12.00

    page = purchase_service.search_purchases(kind="all")
    total = sum((item.line_amount for item in page.items), Decimal("0"))

    assert format_money(total) == "48.00"


@pytest.mark.parametrize("bad", ["purchases", "adjustments", "", "both", "none"])
def test_search_kind_is_whitelisted(
    purchase_service: PurchaseService, bad: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        purchase_service.search_purchases(kind=bad)

    assert caught.value.details["field"] == "kind"


@pytest.mark.parametrize("bad", ["refund", "return", "credit", "void"])
def test_line_kind_is_whitelisted(
    purchase_service: PurchaseService, food_item: FoodItemView, bad: str
) -> None:
    """只有两种行类别。`refund` / `credit` 这类叫法不收：退货管理不在本轮范围。"""
    with pytest.raises(ValidationError) as caught:
        record(purchase_service, food_item, line_kind=bad)

    assert caught.value.details["field"] == "line_kind"


@pytest.mark.parametrize("written", ["ADJUSTMENT", " Adjustment ", "adjustment"])
def test_line_kind_is_case_insensitive(
    purchase_service: PurchaseService, food_item: FoodItemView, written: str
) -> None:
    """大小写与空白差异照单全收，与 `currency` / `entry_source` 同一套约定。

    枚举值的写法差异不该变成一次失败的录入；真正写错的词（见上一个用例）才报错。
    """
    assert adjustment(purchase_service, food_item, line_kind=written).line_kind == (
        "adjustment"
    )


def test_search_kind_is_case_insensitive(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    record(purchase_service, food_item)
    adjustment(purchase_service, food_item)

    assert purchase_service.search_purchases(kind="ALL").total == 2


def test_line_kind_cannot_be_changed_by_an_update(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """录错了类别属于录错了行——作废重录比原地改类别清楚。

    `update_purchase()` 根本不接受这个参数，所以类型层面就改不了；这里断言的是
    「它没有被偷偷加进可改字段」。
    """
    created = record(purchase_service, food_item)

    with pytest.raises(TypeError):
        purchase_service.update_purchase(  # type: ignore[call-arg]
            created.id, line_kind="adjustment"
        )

    assert purchase_service.get_purchase(created.id).line_kind == "purchase"


def test_adjustments_can_be_corrected_and_voided_like_purchases(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """调整行也是记录：同样能改、能作废、同样留修改日志。"""
    created = adjustment(purchase_service, food_item)

    updated = purchase_service.update_purchase(
        created.id, quantity="-3", line_amount="-18.00", reason="退了 3 lb 不是 2 lb"
    )
    assert updated.line_amount == Decimal("-18.00")

    (revision,) = purchase_service.list_revisions(created.id)
    assert revision.changed_fields["line_amount"] == {
        "before": "-12.00",
        "after": "-18.00",
    }

    purchase_service.void_purchase(created.id)
    assert purchase_service.search_purchases(kind="all").total == 0
    assert (
        purchase_service.search_purchases(kind="all", include_voided=True).total == 1
    )


def test_adjustment_values_are_stored_exactly(
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    migrated_database: Database,
) -> None:
    """负数同样按整数最小单位精确存储，不经浮点。"""
    adjustment(
        purchase_service,
        food_item,
        quantity="-12.345",
        unit_price="0.07",
        line_amount="-0.86",
    )

    with migrated_database.unit_of_work() as session:
        row = session.execute(
            text(
                "select quantity, typeof(quantity), line_amount, typeof(line_amount) "
                "from purchases where line_kind = 'adjustment'"
            )
        ).one()

    assert row[1] == "integer" and row[0] == -123450
    assert row[3] == "integer" and row[2] == -86


# --- 格式化 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "stored, min_scale, rendered",
    [
        ("12.3450", 0, "12.345"),
        ("10.0000", 0, "10"),
        ("0.0700", 2, "0.07"),
        ("60.00", 2, "60.00"),
        ("100.00", 2, "100.00"),
        ("0.00", 2, "0.00"),
        ("0.0125", 2, "0.0125"),
        ("1234567.89", 2, "1234567.89"),
    ],
)
def test_formatting_trims_padding_but_keeps_money_at_two_places(
    stored: str, min_scale: int, rendered: str
) -> None:
    """按 scale 存会补出尾随零；输出时去掉，但钱至少两位，不出现 `$5.8`。"""
    assert format_decimal(Decimal(stored), min_scale=min_scale) == rendered
