"""关键词认食材、自动建食材与「未分类」转正（步骤 10.8—10.13）。

分四组：

1. **关键词剥得对不对**（纯文本，对着 48 行真实描述）；
2. **词表猜的分类对不对**，以及猜不出时落「未分类」；
3. **什么时候可以自动建、什么时候必须停下来让老板填**——分界线是基础单位能不能
   定出来，不是关键词像不像；
4. **`needs_review` 什么时候清掉**。
"""

from __future__ import annotations

import pytest

from src.errors import ConflictError
from src.services.ingredients import (
    UNCATEGORIZED_NAME_NORMALIZED,
    IngredientService,
)
from src.services.matching import (
    Keywords,
    extract_keywords,
    guess_category_key,
)

# `samples/real/ground_truth/` 里的真实商品描述（48 行里挑出有代表性的那些）。
REAL_DESCRIPTIONS = [
    # (原文, 英文关键词, 中文关键词, 猜出来的分类)
    ("Beef Stomach - 牛肚6X44.10", "Beef Stomach", "牛肚", "meat & poultry"),
    ("Beef Tripe - 牛柏叶", "Beef Tripe", "牛柏叶", "meat & poultry"),
    ("Beef Eye Round - 老牛", "Beef Eye Round", "老牛", "meat & poultry"),
    ("Pork Backfat - 肥板2X60.1", "Pork Backfat", "肥板", "meat & poultry"),
    ("Pork Spare Rib(Cube) - 靓排粒", "Pork Spare Rib Cube", "靓排粒", "meat & poultry"),
    ("Duck Feet - 鸭脚", "Duck Feet", "鸭脚", "meat & poultry"),
    ("Pork - CT", "Pork CT", None, "meat & poultry"),
    ("箱　麻薯粉 WHEAT FLOUR 1000g x 10", "WHEAT FLOUR", "麻薯粉", "dry goods"),
    ("三文鱼三联包 600克", None, "三文鱼三联包", "seafood"),
    ("香菜", None, "香菜", "vegetables"),
    ("西红柿", None, "西红柿", "vegetables"),
    # Target 小票给出的全是品牌名，词表认不出 → 未分类（落的不是一个猜的分类）。
    ("GAIN 2X", "GAIN", None, None),
    ("COLGATE", "COLGATE", None, None),
    ("GIFT CARDS", "GIFT CARDS", None, None),
    ("LCC SYSTEM USE ONLY", "LCC SYSTEM USE ONLY", None, None),
]


@pytest.mark.parametrize(
    ("raw", "name", "name_zh", "_category"), REAL_DESCRIPTIONS
)
def test_keywords_come_out_of_real_descriptions(
    raw: str, name: str | None, name_zh: str | None, _category: str | None
) -> None:
    """包装规格、单位字与纯数字剥掉，**大小写原样保留**。

    不保留大小写的话，自动建出来的食材名是 `beef stomach`——一个老板自己绝不会
    这么写的名字。
    """
    keywords = extract_keywords(raw)

    assert keywords.name == name
    assert keywords.name_zh == name_zh


@pytest.mark.parametrize(("raw", "_n", "_z", "category"), REAL_DESCRIPTIONS)
def test_category_is_guessed_from_the_head_word(
    raw: str, _n: str | None, _z: str | None, category: str | None
) -> None:
    assert guess_category_key(extract_keywords(raw)) == category


def test_the_seven_newmoys_items_all_land_in_one_category() -> None:
    """⚠️ 这一条是「关键词落在食材上、不落在类别上」那个决定的依据。

    若按关键词**新建类别**，NewMoys 一家肉铺 5 页 13 行就会造出 7 个类别
    （beef eye round / beef stomach / beef tripe / duck feet / pork /
    pork backfat / pork spare rib），而这 7 个**全部属于肉类**。类别一按描述增长，
    10.A 的收口面板就退化成行列表的重印——而那个面板的全部价值正是把 15 行压成
    4 个数。
    """
    newmoys = [row for row in REAL_DESCRIPTIONS if " - " in row[0]]
    assert len(newmoys) == 7  # 文档点名的那 7 条，`Pork - CT` 在内

    names = {extract_keywords(row[0]).name for row in newmoys}
    categories = {guess_category_key(extract_keywords(row[0])) for row in newmoys}

    assert len(names) == 7, "7 个不同的**食材**"
    assert categories == {"meat & poultry"}, "但只有 1 个**类别**"


def test_a_renamed_or_missing_category_is_not_recreated_from_the_word_table() -> None:
    """词表只返回一个**待查的键**，不碰数据库。

    老板可以改分类名（`imp_notes.md`：不强制所有餐厅采用同一套分类体系），改完
    这里就对不上了——那时落「未分类」是正确行为。⚠️ 绝不能反过来按词表去新建分类。
    """
    assert guess_category_key(Keywords(name="beef", name_zh=None)) == "meat & poultry"
    assert guess_category_key(Keywords(name="zzzz", name_zh=None)) is None
    assert guess_category_key(Keywords(name=None, name_zh=None)) is None


# --- 「未分类」转正（10.8）-------------------------------------------------


def test_uncategorized_is_seeded_and_found_by_its_normalized_name(
    ingredient_service: IngredientService,
) -> None:
    views = ingredient_service.list_categories()

    anchor = [
        view
        for view in views
        if view.name.casefold() == UNCATEGORIZED_NAME_NORMALIZED
    ]
    assert len(anchor) == 1
    assert anchor[0].name_zh == "未分类"


def test_the_uncategorized_anchor_cannot_be_deleted(
    ingredient_service: IngredientService,
) -> None:
    """⚠️ 删掉它之后，10.C 的关键词猜不出分类时就无处可落。

    而 `food_items.category_id` 是 NOT NULL 外键——表现是"自动建食材突然不工作了"，
    不报「分类没了」。
    """
    anchor = next(
        view
        for view in ingredient_service.list_categories()
        if view.name.casefold() == UNCATEGORIZED_NAME_NORMALIZED
    )

    with pytest.raises(ConflictError) as error:
        ingredient_service.delete_category(anchor.id)

    assert error.value.details["reserved"] is True


def test_the_uncategorized_anchor_can_still_be_renamed(
    ingredient_service: IngredientService,
) -> None:
    """判定走 `name_normalized`，所以显示名随便改——改完仍然认得它。"""
    anchor = next(
        view
        for view in ingredient_service.list_categories()
        if view.name.casefold() == UNCATEGORIZED_NAME_NORMALIZED
    )

    renamed = ingredient_service.update_category(anchor.id, name_zh="待归类")

    assert renamed.name_zh == "待归类"
    # 名字没动，锚点还在。
    with pytest.raises(ConflictError):
        ingredient_service.delete_category(anchor.id)


# --- `needs_review`（10.13）------------------------------------------------


def test_an_item_the_boss_creates_himself_is_not_flagged(
    ingredient_service: IngredientService,
) -> None:
    created = ingredient_service.create_food_item(
        name="Beef Stomach", category_id=1, base_unit="lb"
    )

    assert created.needs_review is False


def test_editing_an_auto_created_item_clears_the_flag(
    ingredient_service: IngredientService,
) -> None:
    """他改的可能只是名字，但那说明他看过这一条了——而这个标记问的正是"有没有人
    看过"。"""
    created = ingredient_service.create_food_item(
        name="beef stomach", category_id=1, base_unit="lb", needs_review=True
    )
    assert created.needs_review is True

    updated = ingredient_service.update_food_item(created.id, name="Beef Stomach")

    assert updated.needs_review is False
