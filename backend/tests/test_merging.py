"""重复食材合并的测试（步骤 10.3 / 10.6）。

合并是本轮唯一会**删掉业务数据**的操作，所以用例分三组：

1. **搬动了什么**：采购记录、发票草稿行、箱规都要跟过去，一条都不丢；
2. **拦住了什么**：不兼容单位与互相矛盾的箱规必须拒绝，而不是替老板挑一个；
3. **记住了什么**：旧叫法还搜得到（10.6 的「保存过的纠正能够辅助下一次匹配」）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.errors import ConflictError, NotFoundError, ValidationError
from src.services.ingredients import IngredientService
from src.services.invoices import ConfirmOption, InvoiceService, UploadedFile
from src.services.matching import REASON_MERGED_FROM
from src.services.merging import (
    BLOCK_INCOMPATIBLE_BASE_UNIT,
    BLOCK_PACK_SPEC_CONFLICT,
    BLOCK_SELF_MERGE,
    MAX_MERGE_SOURCES,
    MergeService,
)
from src.services.prices import PriceService
from src.services.purchases import PurchaseService
from src.services.suggestions import SuggestionService

MEAT = 1
OILS = 7

PHOTO = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"0" * 64


@pytest.fixture
def merge_service(migrated_database) -> MergeService:
    return MergeService(migrated_database)


@pytest.fixture
def duplicates(ingredient_service: IngredientService) -> dict[str, int]:
    """同一样东西被建了两条：真实场景是老板第一次打了缩写，第二次打了全名。"""
    keep = ingredient_service.create_food_item(
        name="Ground Beef", name_zh="牛肉末", category_id=MEAT, base_unit="lb"
    )
    dupe = ingredient_service.create_food_item(
        name="Grnd Beef 80/20", category_id=MEAT, base_unit="lb"
    )
    return {"keep": keep.id, "dupe": dupe.id}


def _buy(
    purchase_service: PurchaseService, food_item_id: int, description: str
) -> int:
    view = purchase_service.create_purchase(
        food_item_id=food_item_id,
        quantity="10",
        purchase_unit="lb",
        unit_price="6.00",
        line_amount="60.00",
        original_description=description,
        source_name="NewMoys",
        purchase_date="2026-11-10",
    )
    return view.id


# --- 搬动了什么 -------------------------------------------------------------


def test_merging_moves_the_purchase_history_without_changing_a_single_value(
    merge_service: MergeService,
    purchase_service: PurchaseService,
    duplicates: dict[str, int],
) -> None:
    """⚠️ 合并改的只是「这些钱花在哪条食材上」，不改任何数值。

    供应商当初那句原话留在每一行的 `original_description` 上——10.3 要求的
    「采购历史保持可追溯」靠的就是它。
    """
    purchase_id = _buy(purchase_service, duplicates["dupe"], "GRND BEEF 80/20")

    plan = merge_service.merge(duplicates["keep"], [duplicates["dupe"]])

    assert plan.moved_purchase_count == 1
    moved = purchase_service.get_purchase(purchase_id)
    assert moved.food_item.id == duplicates["keep"]
    assert moved.original_description == "GRND BEEF 80/20"
    assert moved.quantity == Decimal("10.0000")
    assert moved.line_amount == Decimal("60.00")


def test_merging_moves_voided_records_too(
    merge_service: MergeService,
    purchase_service: PurchaseService,
    ingredient_service: IngredientService,
    duplicates: dict[str, int],
) -> None:
    """⚠️ 作废是软删：那些行照样指向这条食材，不搬走就会留下悬空外键。

    预览上的条数也必须把它们算进去，否则老板看到的"会搬动几条"比实际少。
    """
    voided = purchase_service.create_purchase(
        food_item_id=duplicates["dupe"],
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
    )
    purchase_service.void_purchase(voided.id)

    preview = merge_service.preview(duplicates["keep"], [duplicates["dupe"]])
    assert preview.sources[0].purchase_count == 1

    merge_service.merge(duplicates["keep"], [duplicates["dupe"]])

    assert purchase_service.get_purchase(voided.id).food_item.id == duplicates["keep"]
    # 被并掉的那条真的没了。
    with pytest.raises(NotFoundError):
        ingredient_service.get_food_item(duplicates["dupe"])


def test_merging_moves_invoice_lines_and_keeps_them_consistent_with_purchases(
    merge_service: MergeService,
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    duplicates: dict[str, int],
) -> None:
    """已确认的行与它那条采购记录必须指向同一条食材。

    不一致的话，「从采购记录追溯到原始单据」会得到两个不同的食材，而两边都不
    报错。
    """
    invoice = invoice_service.create_invoice(
        [UploadedFile(content=PHOTO, original_name="a.png", content_type="image/png")]
    )
    line_id = invoice.lines[0].id
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=line_id, food_item_id=duplicates["dupe"])],
    )

    plan = merge_service.merge(duplicates["keep"], [duplicates["dupe"]])

    assert plan.moved_invoice_line_count == 1
    refreshed = invoice_service.get_invoice(invoice.id)
    line = next(item for item in refreshed.lines if item.id == line_id)
    assert line.food_item is not None
    assert line.food_item.id == duplicates["keep"]
    purchase = purchase_service.get_purchase(line.purchase_id)
    assert purchase.food_item.id == duplicates["keep"]
    # ⚠️ 乐观锁不动：合并不是"人改了这一行"，加一会让正在审的那一页凭空收到 409。
    assert line.version == refreshed.lines[0].version


def test_merging_moves_pack_specs(
    merge_service: MergeService,
    price_service: PriceService,
    duplicates: dict[str, int],
) -> None:
    price_service.save_pack_spec(
        food_item_id=duplicates["dupe"],
        purchase_unit="case",
        pack_size="30",
        pack_unit="lb",
        source_name="NewMoys",
    )

    plan = merge_service.merge(duplicates["keep"], [duplicates["dupe"]])

    assert plan.moved_pack_spec_count == 1
    specs = price_service.list_pack_specs(food_item_id=duplicates["keep"])
    assert [spec.pack_size for spec in specs.items] == [Decimal("30.0000")]


def test_an_identical_pack_spec_on_both_sides_drops_the_duplicate(
    merge_service: MergeService,
    price_service: PriceService,
    duplicates: dict[str, int],
) -> None:
    """两边记的是同一个事实，丢掉的是一份副本，不是一个事实。"""
    for food_item_id in (duplicates["keep"], duplicates["dupe"]):
        price_service.save_pack_spec(
            food_item_id=food_item_id,
            purchase_unit="case",
            pack_size="30",
            pack_unit="lb",
            source_name="NewMoys",
        )

    preview = merge_service.preview(duplicates["keep"], [duplicates["dupe"]])
    assert preview.can_merge is True
    assert preview.sources[0].duplicate_pack_spec_count == 1
    assert preview.moved_pack_spec_count == 0

    merge_service.merge(duplicates["keep"], [duplicates["dupe"]])

    specs = price_service.list_pack_specs(food_item_id=duplicates["keep"])
    assert len(specs.items) == 1


# --- 拦住了什么 -------------------------------------------------------------


def test_conflicting_pack_specs_block_the_merge(
    merge_service: MergeService,
    price_service: PriceService,
    duplicates: dict[str, int],
) -> None:
    """⚠️ 「一箱等于多少」是人确认过一次的事实，系统不替他挑一个。

    步骤 09 实测：某家的 44.1 lb 被通用的 30 lb 盖掉，**差 47% 且不报错**。
    """
    price_service.save_pack_spec(
        food_item_id=duplicates["keep"],
        purchase_unit="case",
        pack_size="30",
        pack_unit="lb",
        source_name="NewMoys",
    )
    price_service.save_pack_spec(
        food_item_id=duplicates["dupe"],
        purchase_unit="case",
        pack_size="44.1",
        pack_unit="lb",
        source_name="NewMoys",
    )

    preview = merge_service.preview(duplicates["keep"], [duplicates["dupe"]])
    assert preview.can_merge is False
    assert [b.code for b in preview.blockers] == [BLOCK_PACK_SPEC_CONFLICT]

    with pytest.raises(ConflictError) as error:
        merge_service.merge(duplicates["keep"], [duplicates["dupe"]])
    assert error.value.details["blockers"][0]["code"] == BLOCK_PACK_SPEC_CONFLICT
    # 拦住了就什么都没动。
    assert len(price_service.list_pack_specs(food_item_id=duplicates["dupe"]).items) == 1


def test_a_weight_item_cannot_be_merged_into_a_volume_item(
    merge_service: MergeService, ingredient_service: IngredientService
) -> None:
    """没有密度就不能把体积换成重量（步骤 09 的硬规则）。

    强行合并会让一半历史永远算不出标准化价格，而界面上只显示成"暂无数据"。
    """
    oil = ingredient_service.create_food_item(
        name="Olive Oil", category_id=OILS, base_unit="gallon"
    )
    lard = ingredient_service.create_food_item(
        name="Olive Oil Block", category_id=OILS, base_unit="lb"
    )

    preview = merge_service.preview(oil.id, [lard.id])

    assert preview.can_merge is False
    blocker = preview.blockers[0]
    assert blocker.code == BLOCK_INCOMPATIBLE_BASE_UNIT
    assert blocker.details["from_dimension"] != blocker.details["to_dimension"]


def test_different_units_of_the_same_dimension_are_allowed_but_announced(
    merge_service: MergeService, ingredient_service: IngredientService
) -> None:
    """lb 与 kg 换得过去，所以允许；但价格数字会换一个单位显示，必须说出来。"""
    keep = ingredient_service.create_food_item(
        name="Beef Stomach", category_id=MEAT, base_unit="kg"
    )
    dupe = ingredient_service.create_food_item(
        name="Beef Stomach Lb", category_id=MEAT, base_unit="lb"
    )

    preview = merge_service.preview(keep.id, [dupe.id])

    assert preview.can_merge is True
    assert any("原来按 lb" in note for note in preview.notes)


def test_merging_an_item_into_itself_is_refused(
    merge_service: MergeService, duplicates: dict[str, int]
) -> None:
    preview = merge_service.preview(duplicates["keep"], [duplicates["keep"]])

    assert [b.code for b in preview.blockers] == [BLOCK_SELF_MERGE]


def test_every_plan_says_the_merge_cannot_be_undone(
    merge_service: MergeService, duplicates: dict[str, int]
) -> None:
    preview = merge_service.preview(duplicates["keep"], [duplicates["dupe"]])

    assert any("无法撤销" in note for note in preview.notes)


def test_an_unknown_source_is_a_not_found(
    merge_service: MergeService, duplicates: dict[str, int]
) -> None:
    with pytest.raises(NotFoundError):
        merge_service.preview(duplicates["keep"], [9999])


@pytest.mark.parametrize(
    "source_ids", [[], "1", [1, "2"], [True], list(range(MAX_MERGE_SOURCES + 1))]
)
def test_bad_source_ids_are_rejected(
    merge_service: MergeService, duplicates: dict[str, int], source_ids
) -> None:
    with pytest.raises(ValidationError):
        merge_service.preview(duplicates["keep"], source_ids)


def test_a_blocked_merge_changes_nothing_at_all(
    merge_service: MergeService,
    purchase_service: PurchaseService,
    ingredient_service: IngredientService,
    price_service: PriceService,
    duplicates: dict[str, int],
) -> None:
    """⚠️ 部分成功比失败更坏：老板会看到一份既不是合并前、也不是合并后的食材库。"""
    purchase_id = _buy(purchase_service, duplicates["dupe"], "GRND BEEF")
    price_service.save_pack_spec(
        food_item_id=duplicates["keep"],
        purchase_unit="case",
        pack_size="30",
        pack_unit="lb",
    )
    price_service.save_pack_spec(
        food_item_id=duplicates["dupe"],
        purchase_unit="case",
        pack_size="44.1",
        pack_unit="lb",
    )

    with pytest.raises(ConflictError):
        merge_service.merge(duplicates["keep"], [duplicates["dupe"]])

    assert ingredient_service.get_food_item(duplicates["dupe"]).id == duplicates["dupe"]
    assert purchase_service.get_purchase(purchase_id).food_item.id == duplicates["dupe"]


# --- 记住了什么（10.6）------------------------------------------------------


def test_the_old_name_still_finds_the_item_after_a_merge(
    merge_service: MergeService,
    suggestion_service: SuggestionService,
    duplicates: dict[str, int],
) -> None:
    """⚠️ 这是 10.6 的「保存过的纠正能够辅助下一次匹配」里最硬的一条。

    不记住旧叫法的话，老板下次按那个叫法搜就什么都搜不到，然后再建一条重复的
    ——正好回到合并要解决的问题。
    """
    merge_service.merge(duplicates["keep"], [duplicates["dupe"]])

    result = suggestion_service.suggest_food_items(query="Grnd Beef 80/20")

    assert [item.food_item.id for item in result.matches] == [duplicates["keep"]]
    assert result.matches[0].match_reason == REASON_MERGED_FROM
    assert result.matches[0].merged_from == "Grnd Beef 80/20"
    # 界面不说清楚的话，老板会以为自己记错了名字。
    assert any("已经被合并到" in note for note in result.notes)


def test_the_old_name_is_free_for_a_new_item_and_can_be_merged_again(
    merge_service: MergeService,
    ingredient_service: IngredientService,
    suggestion_service: SuggestionService,
    duplicates: dict[str, int],
) -> None:
    """⚠️ 所以 `food_item_merges.name_normalized` 不能做成唯一索引。

    合并之后名字就释放了；做成唯一索引会让第二次合并直接失败。
    """
    merge_service.merge(duplicates["keep"], [duplicates["dupe"]])
    again = ingredient_service.create_food_item(
        name="Grnd Beef 80/20", category_id=MEAT, base_unit="lb"
    )

    # 名字又被占着，所以精确命中的是新建的那条，不是合并日志。
    result = suggestion_service.suggest_food_items(query="Grnd Beef 80/20")
    assert result.matches[0].food_item.id == again.id

    merge_service.merge(duplicates["keep"], [again.id])

    after = suggestion_service.suggest_food_items(query="Grnd Beef 80/20")
    assert [item.food_item.id for item in after.matches] == [duplicates["keep"]]


def test_a_chinese_old_name_is_remembered_too(
    merge_service: MergeService,
    ingredient_service: IngredientService,
    suggestion_service: SuggestionService,
    duplicates: dict[str, int],
) -> None:
    dupe = ingredient_service.create_food_item(
        name="Beef Mince", name_zh="碎牛肉", category_id=MEAT, base_unit="lb"
    )

    merge_service.merge(duplicates["keep"], [dupe.id])

    result = suggestion_service.suggest_food_items(query="碎牛肉")

    assert [item.food_item.id for item in result.matches] == [duplicates["keep"]]
    assert result.matches[0].match_reason == REASON_MERGED_FROM


def test_description_memory_survives_the_merge(
    merge_service: MergeService,
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    duplicates: dict[str, int],
) -> None:
    """「这句描述以前确认到哪个食材」在合并后指向保留的那一条。

    采购记录没删、描述没改，所以 05.6 的精确命中直接就对了——这正是把别名放在
    采购历史上（而不是别名表里）的好处。
    """
    _buy(purchase_service, duplicates["dupe"], "GRND BEEF 80/20")

    merge_service.merge(duplicates["keep"], [duplicates["dupe"]])

    candidates = invoice_service.match_candidates_for("GRND BEEF 80/20")
    assert [c.food_item.id for c in candidates] == [duplicates["keep"]]
    assert candidates[0].match_reason == "description_exact"
