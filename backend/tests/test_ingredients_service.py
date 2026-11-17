"""食材与分类业务规则的测试（步骤 03）。

这里测的是业务口径本身（规范化、重复判定、必填字段、删除守卫、搜索排序），
不经过 HTTP；接口层的状态码与响应体在 test_ingredients_api.py。

所有用例都跑在 `alembic upgrade head` 真实建出来的数据库上（见 conftest.py 的
`migrated_database`），因此唯一约束、外键与初始分类都是真的在起作用。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.services.ingredients import (
    BASE_UNITS,
    IngredientService,
    build_search_text,
    normalize_base_unit,
    normalize_name,
)

# 迁移写入的 7 个初始分类（docs/imp_notes.md Feature 1），外加步骤 10.8 另写一条
# 修订加的 `Uncategorized`：`food_items.category_id` 是 NOT NULL 外键，而 10.C 的
# 关键词猜不出分类时必须有地方可落，否则「分类认不出」会在发票逐行确认那一屏把
# 入账整个挡住。
EXPECTED_INITIAL_CATEGORIES = {
    "Meat & Poultry",
    "Seafood",
    "Vegetables",
    "Dairy & Eggs",
    "Dry Goods",
    "Frozen Food",
    "Oils & Condiments",
    "Uncategorized",
}


@pytest.fixture
def meat_category_id(ingredient_service: IngredientService) -> int:
    return next(
        view.id
        for view in ingredient_service.list_categories()
        if view.name == "Meat & Poultry"
    )


@pytest.fixture
def vegetable_category_id(ingredient_service: IngredientService) -> int:
    return next(
        view.id
        for view in ingredient_service.list_categories()
        if view.name == "Vegetables"
    )


def make_item(
    service: IngredientService,
    category_id: int,
    name: str,
    **kwargs: object,
) -> object:
    return service.create_food_item(
        name=name,
        category_id=category_id,
        base_unit=str(kwargs.pop("base_unit", "lb")),
        **kwargs,  # type: ignore[arg-type]
    )


# --- 规范化与基础单位（纯函数） ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ground Beef", "ground beef"),
        ("  Ground   Beef  ", "ground beef"),
        ("GROUND BEEF", "ground beef"),
        ("Ｇｒｏｕｎｄ　Ｂｅｅｆ", "ground beef"),
        ("Ground\tBeef\n", "ground beef"),
        ("牛肉末", "牛肉末"),
        ("　牛肉末　", "牛肉末"),
    ],
)
def test_normalize_name_folds_case_width_and_whitespace(raw: str, expected: str) -> None:
    """同一个名字的不同写法必须规范化成同一个值，否则会变成两条食材、也搜不到。"""
    assert normalize_name(raw) == expected


def test_build_search_text_covers_name_name_zh_and_specification() -> None:
    assert build_search_text("Ground Beef", "牛肉末", "80/20") == (
        "ground beef 牛肉末 80/20"
    )


def test_build_search_text_skips_absent_optional_fields() -> None:
    assert build_search_text("Ground Beef", None, None) == "ground beef"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("lb", "lb"),
        ("LB", "lb"),
        ("lbs", "lb"),
        ("Pounds", "lb"),
        ("磅", "lb"),
        ("fl oz", "fl_oz"),
        ("FL_OZ", "fl_oz"),
        ("floz", "fl_oz"),
        ("ea", "each"),
        ("Pieces", "each"),
        ("个", "each"),
        ("doz", "dozen"),
        ("Gallons", "gallon"),
        ("公斤", "kg"),
    ],
)
def test_base_unit_synonyms_collapse_to_one_token(raw: str, expected: str) -> None:
    """lb / lbs / LB / 磅 必须落到同一个 token，否则价格历史会按单位写法分叉。"""
    assert normalize_base_unit(raw) == expected


@pytest.mark.parametrize("unit", ["case", "CASE", "pack", "箱", "carton"])
def test_packaging_units_are_rejected_with_their_own_message(unit: str) -> None:
    """包装单位不是基础单位：没有包装规格就换算不出标准价（imp_notes Feature 2）。"""
    with pytest.raises(ValidationError) as excinfo:
        normalize_base_unit(unit)

    assert "包装单位" in excinfo.value.message
    assert excinfo.value.details["field"] == "base_unit"


@pytest.mark.parametrize("unit", ["斤", "两", "bushel", "zzz"])
def test_unknown_units_are_rejected_and_list_the_allowed_values(unit: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        normalize_base_unit(unit)

    assert excinfo.value.details["allowed"] == list(BASE_UNITS)


def test_packaging_units_are_not_in_the_base_unit_whitelist() -> None:
    """白名单里不能悄悄混进 case / pack。"""
    assert "case" not in BASE_UNITS
    assert "pack" not in BASE_UNITS


# --- 初始分类 ---


def test_initial_categories_are_seeded(ingredient_service: IngredientService) -> None:
    views = ingredient_service.list_categories()

    assert {view.name for view in views} == EXPECTED_INITIAL_CATEGORIES


def test_initial_categories_have_chinese_names(
    ingredient_service: IngredientService,
) -> None:
    assert all(view.name_zh for view in ingredient_service.list_categories())


def test_seeded_normalized_names_follow_the_service_rule(
    migrated_database: Database,
) -> None:
    """迁移里的规范化实现必须与 normalize_name() 一致。

    迁移脚本刻意不 import 应用代码（它是结构的历史快照），于是两处各有一份实现。
    这个用例把它们绑在一起：哪天规范化规则改了，这里会失败，提醒补一条数据迁移。
    """
    with migrated_database.unit_of_work() as session:
        rows = session.execute(text("select name, name_normalized from categories")).all()

    assert rows
    for name, stored in rows:
        assert stored == normalize_name(name)


# --- 分类维护 ---


def test_create_category_keeps_the_original_writing(
    ingredient_service: IngredientService,
) -> None:
    view = ingredient_service.create_category(name="  Baking Supplies  ", name_zh="烘焙用品")

    assert view.name == "Baking Supplies"
    assert view.name_zh == "烘焙用品"
    assert view.id > 0


def test_names_collapse_runs_of_whitespace(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """粘贴来的多余空格不该留在展示名里，但大小写原样保留。"""
    view = ingredient_service.create_food_item(
        name="  GROUND   beef ", category_id=meat_category_id, base_unit="lb"
    )

    assert view.name == "GROUND beef"


def test_create_category_rejects_a_blank_name(
    ingredient_service: IngredientService,
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.create_category(name="   ")

    assert excinfo.value.details["field"] == "name"


@pytest.mark.parametrize("duplicate", ["Seafood", "seafood", "  SEAFOOD  "])
def test_duplicate_category_name_is_rejected(
    ingredient_service: IngredientService, duplicate: str
) -> None:
    with pytest.raises(ConflictError) as excinfo:
        ingredient_service.create_category(name=duplicate)

    assert excinfo.value.details["existing"]["name"] == "Seafood"


def test_update_category_renames_it(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    view = ingredient_service.update_category(meat_category_id, name="Meat")

    assert view.name == "Meat"
    assert {v.name for v in ingredient_service.list_categories()} >= {"Meat"}


def test_update_category_can_clear_the_chinese_name(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """显式传 null 表示清空，与「没传这个字段」区分开。"""
    view = ingredient_service.update_category(meat_category_id, name_zh=None)

    assert view.name_zh is None
    assert view.name == "Meat & Poultry"


def test_update_category_keeps_untouched_fields(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    view = ingredient_service.update_category(meat_category_id, name="Meat")

    assert view.name_zh == "肉类与禽类"


def test_update_category_rejects_another_existing_name(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    with pytest.raises(ConflictError):
        ingredient_service.update_category(meat_category_id, name="Seafood")


def test_update_category_allows_rewriting_its_own_name(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """只改大小写不算和自己冲突。"""
    view = ingredient_service.update_category(meat_category_id, name="MEAT & POULTRY")

    assert view.name == "MEAT & POULTRY"


def test_update_missing_category_is_not_found(
    ingredient_service: IngredientService,
) -> None:
    with pytest.raises(NotFoundError):
        ingredient_service.update_category(9999, name="x")


def test_delete_unused_category(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    ingredient_service.delete_category(meat_category_id)

    assert "Meat & Poultry" not in {
        view.name for view in ingredient_service.list_categories()
    }


def test_delete_category_still_holding_food_items_is_rejected(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    make_item(ingredient_service, meat_category_id, "Ground Beef")

    with pytest.raises(ConflictError) as excinfo:
        ingredient_service.delete_category(meat_category_id)

    assert excinfo.value.details["food_item_count"] == 1
    assert "Meat & Poultry" in {
        view.name for view in ingredient_service.list_categories()
    }


def test_delete_missing_category_is_not_found(
    ingredient_service: IngredientService,
) -> None:
    with pytest.raises(NotFoundError):
        ingredient_service.delete_category(9999)


# --- 快速新增与编辑 ---


def test_quick_add_requires_only_name_category_and_base_unit(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """快速新增不要求中文名、规格与采购来源（imp_notes：不要一次填一堆字段）。"""
    view = ingredient_service.create_food_item(
        name="Chicken Breast", category_id=meat_category_id, base_unit="lb"
    )

    assert view.name == "Chicken Breast"
    assert view.name_zh is None
    assert view.specification is None
    assert view.base_unit == "lb"
    assert view.category.name == "Meat & Poultry"


def test_create_food_item_accepts_the_optional_fields(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    view = ingredient_service.create_food_item(
        name="Ground Beef",
        category_id=meat_category_id,
        base_unit="LBS",
        name_zh="牛肉末",
        specification="80/20",
    )

    assert (view.name, view.name_zh, view.specification) == (
        "Ground Beef",
        "牛肉末",
        "80/20",
    )
    assert view.base_unit == "lb"


def test_blank_optional_fields_become_null(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """只打了空格的中文名存成 NULL，否则两条「空中文名」会撞唯一约束。"""
    view = ingredient_service.create_food_item(
        name="Pork Belly",
        category_id=meat_category_id,
        base_unit="lb",
        name_zh="   ",
        specification="",
    )

    assert view.name_zh is None
    assert view.specification is None


def test_create_food_item_rejects_a_blank_name(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.create_food_item(
            name="  ", category_id=meat_category_id, base_unit="lb"
        )

    assert excinfo.value.details["field"] == "name"


def test_create_food_item_rejects_an_unknown_category(
    ingredient_service: IngredientService,
) -> None:
    """分类是请求数据里的引用，写错属于请求不合法（400），不是「资源不存在」。"""
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.create_food_item(
            name="Mystery", category_id=9999, base_unit="lb"
        )

    assert excinfo.value.details["field"] == "category_id"


def test_create_food_item_rejects_a_packaging_base_unit(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    with pytest.raises(ValidationError):
        ingredient_service.create_food_item(
            name="Beef Case", category_id=meat_category_id, base_unit="case"
        )


def test_long_names_are_rejected_instead_of_being_truncated(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.create_food_item(
            name="x" * 121, category_id=meat_category_id, base_unit="lb"
        )

    assert excinfo.value.details["max_length"] == 120


# --- 重复名称 ---


@pytest.mark.parametrize(
    "duplicate",
    [
        "Ground Beef",
        "ground beef",
        "  GROUND   BEEF ",
        "Ｇｒｏｕｎｄ Ｂｅｅｆ",
    ],
)
def test_duplicate_name_is_rejected_and_points_at_the_existing_item(
    ingredient_service: IngredientService, meat_category_id: int, duplicate: str
) -> None:
    """重复名称直接拒绝，并把已存在的那条交给调用方，便于「直接用它」。"""
    first = ingredient_service.create_food_item(
        name="Ground Beef", category_id=meat_category_id, base_unit="lb"
    )

    with pytest.raises(ConflictError) as excinfo:
        ingredient_service.create_food_item(
            name=duplicate, category_id=meat_category_id, base_unit="lb"
        )

    assert excinfo.value.details["existing"]["id"] == first.id
    assert excinfo.value.details["field"] == "name"


def test_duplicate_chinese_name_is_rejected(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """中英文名都指向同一条食材：中文名撞车同样要拦，否则价格历史会被拆散。"""
    first = ingredient_service.create_food_item(
        name="Ground Beef",
        category_id=meat_category_id,
        base_unit="lb",
        name_zh="牛肉末",
    )

    with pytest.raises(ConflictError) as excinfo:
        ingredient_service.create_food_item(
            name="Minced Beef",
            category_id=meat_category_id,
            base_unit="lb",
            name_zh="牛肉末",
        )

    assert excinfo.value.details["existing"]["id"] == first.id
    assert excinfo.value.details["field"] == "name_zh"


def test_a_duplicate_in_another_category_is_still_a_duplicate(
    ingredient_service: IngredientService,
    meat_category_id: int,
    vegetable_category_id: int,
) -> None:
    ingredient_service.create_food_item(
        name="Mixed Item", category_id=meat_category_id, base_unit="lb"
    )

    with pytest.raises(ConflictError):
        ingredient_service.create_food_item(
            name="Mixed Item", category_id=vegetable_category_id, base_unit="lb"
        )


def test_several_items_may_share_an_empty_chinese_name(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """中文名可空，而「可空 + 唯一」在 SQLite 里允许多个 NULL——正是需要的语义。"""
    ingredient_service.create_food_item(
        name="Beef Shank", category_id=meat_category_id, base_unit="lb"
    )
    ingredient_service.create_food_item(
        name="Beef Rib", category_id=meat_category_id, base_unit="lb"
    )

    assert ingredient_service.search_food_items(query="beef").total == 2


# --- 搜索 ---


@pytest.fixture
def three_items(
    ingredient_service: IngredientService,
    meat_category_id: int,
    vegetable_category_id: int,
) -> dict[str, int]:
    beef = ingredient_service.create_food_item(
        name="Ground Beef",
        category_id=meat_category_id,
        base_unit="lb",
        name_zh="牛肉末",
        specification="80/20",
    )
    chicken = ingredient_service.create_food_item(
        name="Chicken Breast",
        category_id=meat_category_id,
        base_unit="lb",
        name_zh="鸡胸肉",
    )
    tomato = ingredient_service.create_food_item(
        name="Tomato",
        category_id=vegetable_category_id,
        base_unit="lb",
        name_zh="番茜",
    )
    return {"beef": beef.id, "chicken": chicken.id, "tomato": tomato.id}


@pytest.mark.parametrize(
    "query",
    [
        "Ground Beef",
        "ground beef",
        "GROUND BEEF",
        "  ground   beef  ",
        "beef",
        "牛肉末",
        "牛肉",
        "80/20",
    ],
)
def test_english_chinese_and_specification_all_find_the_same_item(
    ingredient_service: IngredientService, three_items: dict[str, int], query: str
) -> None:
    """验收条件：中英文与规格都指向同一条食材，且只返回一条。"""
    page = ingredient_service.search_food_items(query=query)

    assert [item.id for item in page.items] == [three_items["beef"]]
    assert page.total == 1


def test_search_without_a_query_lists_everything(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    page = ingredient_service.search_food_items()

    assert page.total == 3
    assert len(page.items) == 3


def test_blank_query_is_treated_as_no_filter(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    assert ingredient_service.search_food_items(query="   ").total == 3


def test_search_results_carry_category_and_base_unit(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    """搜索结果要能直接显示名称、分类与基础单位（imp_notes Feature 3）。"""
    item = ingredient_service.search_food_items(query="tomato").items[0]

    assert item.category.name == "Vegetables"
    assert item.category.name_zh == "蔬菜"
    assert item.base_unit == "lb"


def test_search_filters_by_category(
    ingredient_service: IngredientService,
    three_items: dict[str, int],
    vegetable_category_id: int,
) -> None:
    page = ingredient_service.search_food_items(category_id=vegetable_category_id)

    assert [item.id for item in page.items] == [three_items["tomato"]]


def test_search_combines_query_and_category(
    ingredient_service: IngredientService,
    three_items: dict[str, int],
    vegetable_category_id: int,
) -> None:
    page = ingredient_service.search_food_items(
        query="beef", category_id=vegetable_category_id
    )

    assert page.total == 0


def test_search_rejects_an_unknown_category(
    ingredient_service: IngredientService,
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.search_food_items(category_id=9999)

    assert excinfo.value.details["field"] == "category_id"


def test_exact_matches_rank_before_prefix_and_substring(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    ingredient_service.create_food_item(
        name="Lean Beef Trim", category_id=meat_category_id, base_unit="lb"
    )
    ingredient_service.create_food_item(
        name="Beef Shank", category_id=meat_category_id, base_unit="lb"
    )
    ingredient_service.create_food_item(
        name="Beef", category_id=meat_category_id, base_unit="lb"
    )

    names = [item.name for item in ingredient_service.search_food_items(query="beef").items]

    assert names == ["Beef", "Beef Shank", "Lean Beef Trim"]


def test_wildcards_in_the_query_are_literal(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """用户搜 "100%" 不能变成「匹配所有」。"""
    ingredient_service.create_food_item(
        name="Olive Oil", category_id=meat_category_id, base_unit="l"
    )
    hit = ingredient_service.create_food_item(
        name="Pure 100% Olive Oil", category_id=meat_category_id, base_unit="l"
    )

    assert [item.id for item in ingredient_service.search_food_items(query="100%").items] == [
        hit.id
    ]
    assert ingredient_service.search_food_items(query="_live").total == 0


def test_pagination_reports_the_full_total(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    page = ingredient_service.search_food_items(limit=2, offset=0)

    assert len(page.items) == 2
    assert page.total == 3
    assert (page.limit, page.offset) == (2, 0)


def test_pagination_pages_do_not_overlap(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    first = ingredient_service.search_food_items(limit=2, offset=0)
    second = ingredient_service.search_food_items(limit=2, offset=2)

    assert len(second.items) == 1
    assert not {item.id for item in first.items} & {item.id for item in second.items}


@pytest.mark.parametrize("limit", [0, -1, 101, "20", True, None])
def test_invalid_limit_is_rejected(
    ingredient_service: IngredientService, limit: object
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.search_food_items(limit=limit)  # type: ignore[arg-type]

    assert excinfo.value.details["field"] == "limit"


@pytest.mark.parametrize("offset", [-1, "0", None])
def test_invalid_offset_is_rejected(
    ingredient_service: IngredientService, offset: object
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.search_food_items(offset=offset)  # type: ignore[arg-type]

    assert excinfo.value.details["field"] == "offset"


def test_overlong_query_is_rejected(ingredient_service: IngredientService) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.search_food_items(query="x" * 101)

    assert excinfo.value.details["field"] == "q"


# --- 单条读取与编辑 ---


def test_get_food_item(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    view = ingredient_service.get_food_item(three_items["beef"])

    assert view.name == "Ground Beef"


def test_get_missing_food_item_is_not_found(
    ingredient_service: IngredientService,
) -> None:
    with pytest.raises(NotFoundError):
        ingredient_service.get_food_item(9999)


def test_update_changes_only_what_was_passed(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    view = ingredient_service.update_food_item(three_items["beef"], base_unit="kg")

    assert view.base_unit == "kg"
    assert (view.name, view.name_zh, view.specification) == (
        "Ground Beef",
        "牛肉末",
        "80/20",
    )


def test_rename_updates_what_search_can_find(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    """改名后必须重算检索列，否则搜新名字找不到、搜旧名字还在。"""
    ingredient_service.update_food_item(three_items["beef"], name="Minced Beef")

    assert ingredient_service.search_food_items(query="minced").total == 1
    assert ingredient_service.search_food_items(query="ground").total == 0


def test_update_can_clear_the_chinese_name_and_free_it_for_another_item(
    ingredient_service: IngredientService,
    three_items: dict[str, int],
    meat_category_id: int,
) -> None:
    ingredient_service.update_food_item(three_items["beef"], name_zh=None)

    reused = ingredient_service.create_food_item(
        name="Minced Beef",
        category_id=meat_category_id,
        base_unit="lb",
        name_zh="牛肉末",
    )

    assert reused.name_zh == "牛肉末"
    assert ingredient_service.get_food_item(three_items["beef"]).name_zh is None


def test_update_rejects_a_name_another_item_already_uses(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    with pytest.raises(ConflictError) as excinfo:
        ingredient_service.update_food_item(three_items["beef"], name="Tomato")

    assert excinfo.value.details["existing"]["id"] == three_items["tomato"]


def test_update_allows_an_item_to_keep_its_own_name(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    view = ingredient_service.update_food_item(
        three_items["beef"], name="GROUND BEEF", specification="85/15"
    )

    assert view.name == "GROUND BEEF"
    assert view.specification == "85/15"


def test_update_moves_the_item_to_another_category(
    ingredient_service: IngredientService,
    three_items: dict[str, int],
    vegetable_category_id: int,
) -> None:
    view = ingredient_service.update_food_item(
        three_items["beef"], category_id=vegetable_category_id
    )

    assert view.category.id == vegetable_category_id
    assert view.category.name == "Vegetables"


def test_update_rejects_an_unknown_category(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ingredient_service.update_food_item(three_items["beef"], category_id=9999)

    assert excinfo.value.details["field"] == "category_id"


def test_update_missing_food_item_is_not_found(
    ingredient_service: IngredientService,
) -> None:
    with pytest.raises(NotFoundError):
        ingredient_service.update_food_item(9999, name="x")


# --- 删除 ---


def test_delete_food_item(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    ingredient_service.delete_food_item(three_items["beef"])

    with pytest.raises(NotFoundError):
        ingredient_service.get_food_item(three_items["beef"])
    assert ingredient_service.search_food_items().total == 2


def test_delete_missing_food_item_is_not_found(
    ingredient_service: IngredientService,
) -> None:
    with pytest.raises(NotFoundError):
        ingredient_service.delete_food_item(9999)


def test_deleting_a_food_item_keeps_its_category(
    ingredient_service: IngredientService, three_items: dict[str, int]
) -> None:
    ingredient_service.delete_food_item(three_items["tomato"])

    assert "Vegetables" in {
        view.name for view in ingredient_service.list_categories()
    }


# --- 持久化 ---


def test_data_survives_reopening_the_database(
    migrated_database: Database, meat_category_id: int
) -> None:
    """验收条件：重启后数据保留。

    这里用「断开连接池、按同一个文件重新建 Database」模拟重启：进程没了，
    数据必须还在文件里。
    """
    IngredientService(migrated_database).create_food_item(
        name="Ground Beef",
        category_id=meat_category_id,
        base_unit="lb",
        name_zh="牛肉末",
    )
    url = str(migrated_database.url)
    migrated_database.dispose()

    reopened = Database(url)
    try:
        page = IngredientService(reopened).search_food_items(query="牛肉末")
    finally:
        reopened.dispose()

    assert [item.name for item in page.items] == ["Ground Beef"]


def test_rollback_leaves_no_half_written_item(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    """重复名称被拒之后，数据库里不能留下任何痕迹。"""
    ingredient_service.create_food_item(
        name="Ground Beef", category_id=meat_category_id, base_unit="lb"
    )

    with pytest.raises(ConflictError):
        ingredient_service.create_food_item(
            name="ground beef", category_id=meat_category_id, base_unit="lb"
        )

    assert ingredient_service.search_food_items(query="beef").total == 1


# --- 「从没买过」筛选（步骤 10.15）-----------------------------------------


def test_never_purchased_filters_down_to_orphan_items(
    ingredient_service: IngredientService,
    purchase_service,
    meat_category_id: int,
) -> None:
    """10.D 把新建食材改成"填完即落库"之后，中途放弃的新建会留下孤立食材。

    它们**不进价格历史**（统计只读 `purchases`），所以留着不污染任何数字，
    但该有个地方能把它们找出来。
    """
    bought = ingredient_service.create_food_item(
        name="Beef Stomach", category_id=meat_category_id, base_unit="lb"
    )
    orphan = ingredient_service.create_food_item(
        name="Typo Item", category_id=meat_category_id, base_unit="lb"
    )
    purchase_service.create_purchase(
        food_item_id=bought.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
    )

    never = ingredient_service.search_food_items(never_purchased=True)
    ever = ingredient_service.search_food_items(never_purchased=False)

    assert [item.id for item in never.items] == [orphan.id]
    assert [item.id for item in ever.items] == [bought.id]


def test_a_voided_purchase_still_counts_as_purchased(
    ingredient_service: IngredientService,
    purchase_service,
    meat_category_id: int,
) -> None:
    """⚠️ 判据刻意**包含已作废的采购记录**。

    作废是软删，那条记录仍然是一个外键引用——带着它的食材删不掉。把它算成
    "从没买过"只会让老板在删除时撞 409，而这个筛选的全部用处是"要清随时清"。
    """
    item = ingredient_service.create_food_item(
        name="Returned Item", category_id=meat_category_id, base_unit="lb"
    )
    purchase = purchase_service.create_purchase(
        food_item_id=item.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
    )
    purchase_service.void_purchase(purchase.id)

    assert ingredient_service.search_food_items(never_purchased=True).total == 0
    with pytest.raises(ConflictError):
        ingredient_service.delete_food_item(item.id)


def test_never_purchased_combines_with_the_other_filters(
    ingredient_service: IngredientService, meat_category_id: int
) -> None:
    vegetables = next(
        view.id
        for view in ingredient_service.list_categories()
        if view.name == "Vegetables"
    )
    meat_orphan = ingredient_service.create_food_item(
        name="Orphan Beef", category_id=meat_category_id, base_unit="lb"
    )
    ingredient_service.create_food_item(
        name="Orphan Carrot", category_id=vegetables, base_unit="lb"
    )

    page = ingredient_service.search_food_items(
        query="orphan", category_id=meat_category_id, never_purchased=True
    )

    assert [item.id for item in page.items] == [meat_orphan.id]
