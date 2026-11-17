"""食材建议的测试：模糊匹配、最近 / 常用（步骤 10.1 / 10.2）。

与 `tests/test_matching.py` 的分工：那边验算法（两段文本有多像），这边验业务判断
（什么算候选、怎么排、哪条来源优先、哪些记录不该进榜）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.errors import ValidationError
from src.services.ingredients import IngredientService
from src.services.matching import (
    REASON_CONTAINS,
    REASON_EXACT,
    REASON_FREQUENT,
    REASON_FUZZY,
    REASON_PREFIX,
    REASON_RECENT,
)
from src.services.purchases import PurchaseService
from src.services.suggestions import FREQUENT_WINDOW_DAYS, SuggestionService

MEAT = 1
VEGETABLES = 3


@pytest.fixture
def library(ingredient_service: IngredientService) -> dict[str, int]:
    """一个小食材库。名字取自 `samples/real/ground_truth/` 的真实描述。"""
    items = {
        "stomach": ingredient_service.create_food_item(
            name="Beef Stomach", name_zh="牛肚", category_id=MEAT, base_unit="lb"
        ),
        "tripe": ingredient_service.create_food_item(
            name="Beef Tripe", name_zh="牛柏叶", category_id=MEAT, base_unit="lb"
        ),
        "duck": ingredient_service.create_food_item(
            name="Duck Feet", name_zh="鸭脚", category_id=MEAT, base_unit="lb"
        ),
        "crock": ingredient_service.create_food_item(
            name="Country Crock", category_id=VEGETABLES, base_unit="each"
        ),
        "breast": ingredient_service.create_food_item(
            name="Chicken Breast", name_zh="鸡胸肉", category_id=MEAT, base_unit="lb"
        ),
    }
    return {key: view.id for key, view in items.items()}


def _reasons(items) -> list[str]:
    return [item.match_reason for item in items]


# --- 四档名称匹配 -----------------------------------------------------------


def test_exact_prefix_and_contains_hits_keep_step_03_ordering(
    suggestion_service: SuggestionService, library: dict[str, int]
) -> None:
    """精确 → 前缀 → 包含，与步骤 03 的排序同口径。"""
    result = suggestion_service.suggest_food_items(query="beef")

    ids = [item.food_item.id for item in result.matches]
    assert ids == [library["stomach"], library["tripe"]]
    assert _reasons(result.matches) == [REASON_PREFIX, REASON_PREFIX]


def test_an_exact_name_is_reported_as_exact_not_merely_contained(
    suggestion_service: SuggestionService, library: dict[str, int]
) -> None:
    result = suggestion_service.suggest_food_items(query="Duck Feet")

    assert result.matches[0].food_item.id == library["duck"]
    assert result.matches[0].match_reason == REASON_EXACT
    assert result.matches[0].score == 1.0


def test_chinese_name_finds_the_same_item(
    suggestion_service: SuggestionService, library: dict[str, int]
) -> None:
    result = suggestion_service.suggest_food_items(query="牛肚")

    assert [item.food_item.id for item in result.matches] == [library["stomach"]]
    assert result.matches[0].match_reason == REASON_EXACT


def test_a_vowel_dropped_abbreviation_is_found_by_the_fuzzy_layer(
    suggestion_service: SuggestionService, library: dict[str, int]
) -> None:
    """`CNTRY CROCK` 是 Target 小票上的真实写法；包含匹配一条都命中不了。"""
    result = suggestion_service.suggest_food_items(query="CNTRY CROCK")

    assert [item.food_item.id for item in result.matches] == [library["crock"]]
    assert result.matches[0].match_reason == REASON_FUZZY
    assert result.matches[0].score is not None and result.matches[0].score < 1.0


def test_an_industry_abbreviation_in_the_word_table_is_found(
    suggestion_service: SuggestionService, library: dict[str, int]
) -> None:
    """`CHIX` 的 `x` 不在 `chicken` 里，任何通用规则都推不出来——靠词表。"""
    result = suggestion_service.suggest_food_items(query="CHIX-BRST")

    assert [item.food_item.id for item in result.matches] == [library["breast"]]
    assert result.matches[0].match_reason == REASON_FUZZY


def test_precise_hits_always_come_before_fuzzy_hits(
    suggestion_service: SuggestionService,
    ingredient_service: IngredientService,
    library: dict[str, int],
) -> None:
    """⚠️ 模糊分数可能高于包含命中（`CNTRY CROCK` 能到 0.96）。

    排序必须先按档位：确定命中的那条排在"像的"后面，老板就会以为系统没找到它。
    """
    exact = ingredient_service.create_food_item(
        name="Crock", category_id=VEGETABLES, base_unit="each"
    )
    result = suggestion_service.suggest_food_items(query="crock")

    assert [item.food_item.id for item in result.matches] == [
        exact.id,
        library["crock"],
    ]
    assert _reasons(result.matches) == [REASON_EXACT, REASON_CONTAINS]


def test_the_fuzzy_layer_does_not_run_when_precise_hits_already_fill_the_list(
    suggestion_service: SuggestionService, library: dict[str, int]
) -> None:
    result = suggestion_service.suggest_food_items(query="beef", limit=1)

    assert len(result.matches) == 1
    assert result.matches[0].match_reason == REASON_PREFIX


def test_nothing_similar_enough_yields_no_candidates(
    suggestion_service: SuggestionService, library: dict[str, int]
) -> None:
    """「候选里没有」必须是一个确定答案——那是老板该去新建食材的信号。"""
    result = suggestion_service.suggest_food_items(query="olive oil")

    assert result.matches == ()
    assert result.recent == ()


def test_category_filter_applies_to_the_fuzzy_layer_too(
    suggestion_service: SuggestionService, library: dict[str, int]
) -> None:
    result = suggestion_service.suggest_food_items(
        query="CNTRY CROCK", category_id=MEAT
    )

    assert result.matches == ()


def test_an_unknown_category_is_rejected_rather_than_ignored(
    suggestion_service: SuggestionService,
) -> None:
    with pytest.raises(ValidationError):
        suggestion_service.suggest_food_items(category_id=9999)


def test_an_over_long_query_is_rejected(
    suggestion_service: SuggestionService,
) -> None:
    with pytest.raises(ValidationError):
        suggestion_service.suggest_food_items(query="x" * 101)


# --- 最近 / 常用 ------------------------------------------------------------


def _buy(
    purchase_service: PurchaseService,
    food_item_id: int,
    *,
    when: date | None,
    source: str = "NewMoys",
    **extra,
) -> None:
    purchase_service.create_purchase(
        food_item_id=food_item_id,
        quantity="10",
        purchase_unit="lb",
        unit_price="6.00",
        line_amount="60.00",
        purchase_date=when.isoformat() if when is not None else None,
        source_name=source,
        **extra,
    )


def test_recent_ranks_by_the_latest_entry_not_by_the_date_on_the_paper(
    suggestion_service: SuggestionService,
    purchase_service: PurchaseService,
    library: dict[str, int],
) -> None:
    """⚠️ 步骤 04 定下采购日期可空（手写单上常常没写，认不出就留空）。

    按 `purchase_date` 排会让这些记录永远沉到最后，而"刚刚录进去的那一批"恰恰是
    老板下一分钟最可能再选的。所以「最近」按最近一次入账排。
    """
    today = date(2026, 11, 14)
    _buy(purchase_service, library["stomach"], when=today)
    _buy(purchase_service, library["duck"], when=None)  # 认不出日期，留空

    result = suggestion_service.suggest_food_items(today=today)

    assert [item.food_item.id for item in result.recent] == [
        library["duck"],
        library["stomach"],
    ]
    assert _reasons(result.recent) == [REASON_RECENT, REASON_RECENT]


def test_frequent_ranks_by_how_often_inside_the_window(
    suggestion_service: SuggestionService,
    purchase_service: PurchaseService,
    library: dict[str, int],
) -> None:
    today = date(2026, 11, 14)
    for _ in range(3):
        _buy(purchase_service, library["tripe"], when=today - timedelta(days=5))
    _buy(purchase_service, library["stomach"], when=today)

    result = suggestion_service.suggest_food_items(today=today)

    assert [item.food_item.id for item in result.frequent] == [
        library["tripe"],
        library["stomach"],
    ]
    assert result.frequent[0].purchase_count == 3
    assert result.frequent[0].match_reason == REASON_FREQUENT
    # 「最近」与「常用」是两件事：最近一次买的是 stomach。
    assert result.recent[0].food_item.id == library["stomach"]


def test_purchases_outside_the_window_leave_the_frequent_list(
    suggestion_service: SuggestionService,
    purchase_service: PurchaseService,
    library: dict[str, int],
) -> None:
    """餐厅的菜单会换季：半年前每周买的那样东西，今天不该排在榜首挡住别的。"""
    today = date(2026, 11, 14)
    stale = today - timedelta(days=FREQUENT_WINDOW_DAYS + 1)
    for _ in range(5):
        _buy(purchase_service, library["tripe"], when=stale)
    _buy(purchase_service, library["stomach"], when=today)

    result = suggestion_service.suggest_food_items(today=today)

    assert [item.food_item.id for item in result.frequent] == [library["stomach"]]
    # 它仍然在「最近」里——那条榜单不看窗口。
    assert library["tripe"] in [item.food_item.id for item in result.recent]


def test_undated_purchases_cannot_enter_the_frequent_list(
    suggestion_service: SuggestionService,
    purchase_service: PurchaseService,
    library: dict[str, int],
) -> None:
    """与步骤 09 的月度均价同一条口径：算不进任何一个月的记录不进月度榜。

    这件事由 `notes` 说出来，不靠老板自己发现。
    """
    today = date(2026, 11, 14)
    for _ in range(4):
        _buy(purchase_service, library["duck"], when=None)

    result = suggestion_service.suggest_food_items(today=today)

    assert result.frequent == ()
    assert [item.food_item.id for item in result.recent] == [library["duck"]]
    assert any("未填采购日期" in note for note in result.notes)


def test_adjustment_lines_and_voided_records_are_not_usage(
    suggestion_service: SuggestionService,
    purchase_service: PurchaseService,
    library: dict[str, int],
) -> None:
    """⚠️ 步骤 04 的口径不是可选项。

    一笔退货不代表"用过这个食材"；作废的记录是老板明确否掉的，拿它当记忆只会把
    错的猜法再推一次。
    """
    today = date(2026, 11, 14)
    purchase_service.create_purchase(
        food_item_id=library["tripe"],
        quantity="-2",
        purchase_unit="lb",
        unit_price="6.00",
        line_amount="-12.00",
        line_kind="adjustment",
        purchase_date=today.isoformat(),
    )
    voided = purchase_service.create_purchase(
        food_item_id=library["duck"],
        quantity="5",
        purchase_unit="lb",
        unit_price="4.00",
        line_amount="20.00",
        purchase_date=today.isoformat(),
    )
    purchase_service.void_purchase(voided.id)

    result = suggestion_service.suggest_food_items(today=today)

    assert result.recent == ()
    assert result.frequent == ()


def test_recent_and_frequent_are_empty_once_a_query_is_given(
    suggestion_service: SuggestionService,
    purchase_service: PurchaseService,
    library: dict[str, int],
) -> None:
    """两种模式分开：打了字就是在找特定的那条，这时榜单只是噪声。"""
    _buy(purchase_service, library["stomach"], when=date(2026, 11, 14))

    result = suggestion_service.suggest_food_items(query="duck")

    assert result.recent == ()
    assert result.frequent == ()
    assert [item.food_item.id for item in result.matches] == [library["duck"]]
