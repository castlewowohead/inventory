"""重复食材合并（步骤 10.3）。

### 这件事为什么危险

合并会**删掉一条食材**，并把它名下的采购记录、发票草稿行与箱规全部改指向另一条。
采购记录就是价格历史，所以合并错了等于把两种货的价格搅进同一个加权均价——
不报错、不可察觉，正是本项目反复防的那一类。因此本模块的口径是：

1. **先预览，再执行。** `preview()` 是只读的，把"会搬动多少东西、有什么拦路的
   冲突、合并之后哪些数字会变"全部摆出来。界面可以直接显示它。
2. **拦得住的一律拦住，不自动取舍。** 两条食材的箱规互相矛盾、基础单位不是同一
   类计量——这些都**拒绝合并**并说清楚是哪一条冲突，而不是挑一个留下。
   「一箱等于多少」是人确认过一次的事实，系统替他丢掉一条，历史价格会**静默**
   变成另一个数（步骤 09 实测：44.1 lb 被 30 lb 盖掉差 47%）。
3. **一次事务，要么全成要么全不成。** 搬到一半失败会留下一条没有采购记录的食材
   和一批指向已删除食材的外键。

### 为什么不在 `services/ingredients.py` 里

合并要判断两个基础单位是不是同一类计量，那是 `services/units.py` 的事；而
`units.py` 自己 import `ingredients.py`（单位词表在那边），反过来再 import 就成了
循环。依赖方向与 `prices.py` / `suggestions.py` 一致：**上层能力依赖词表，
词表不依赖它们。**

### 合并**不可逆**

被并掉的那条食材行在合并那一刻就删掉了。`food_item_merges` 记下它的名字与这次
搬动的条数，供"按旧叫法还能搜到"（10.6）与事后核对用，但它**不是回滚日志**：
采购记录搬过去之后，系统已经分不清哪几条原本属于哪一边。这件事必须在界面上说
清楚，所以 `MergePlan.notes` 里固定带一条。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.models import FoodItemMerge
from src.repositories.ingredients import FoodItemMergeRepository, FoodItemRepository
from src.repositories.invoices import InvoiceLineRepository
from src.repositories.prices import PackSpecRepository
from src.repositories.purchases import PurchaseRepository
from src.services.ingredients import FoodItemView, normalize_name
from src.services.units import dimension_of

MAX_MERGE_SOURCES: Final[int] = 10
"""一次最多并掉几条。

不是性能上限，是**手滑上限**：合并不可逆，一次把十几条搅在一起之后没有退路。
真要清理一大批重复，分几次做反而看得清每一次动了什么。
"""

# 拦路冲突的代码。界面按 code 决定怎么说，不去解析 message。
BLOCK_SELF_MERGE: Final[str] = "self_merge"
BLOCK_INCOMPATIBLE_BASE_UNIT: Final[str] = "incompatible_base_unit"
BLOCK_PACK_SPEC_CONFLICT: Final[str] = "pack_spec_conflict"

IRREVERSIBLE_NOTE: Final[str] = (
    "合并后无法撤销。原来的食材会被删除，相关采购记录和单据明细会归到保留的食材名下。"
)


@dataclass(frozen=True, slots=True)
class MergeBlocker:
    """一条拦住合并的冲突。`details` 给界面定位到具体是哪一条箱规 / 哪个单位。"""

    code: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MergeSourcePlan:
    """一条将被并掉的食材，以及它名下有多少东西要搬。"""

    food_item: FoodItemView
    purchase_count: int
    invoice_line_count: int
    pack_spec_count: int
    duplicate_pack_spec_count: int
    """与目标**完全相同**的箱规有几条。这些不搬，直接丢掉——同一个事实存两份
    没有意义，而且会撞 `pack_specs` 的唯一索引。⚠️ 只有连 `pack_size` 与
    `pack_unit` 都一样才算"相同"；只要有一个不同，那是冲突，不是重复。"""


@dataclass(frozen=True, slots=True)
class MergePlan:
    """一次合并的完整计划。预览与执行返回同一个结构，于是两边说的是同一件事。"""

    target: FoodItemView
    sources: tuple[MergeSourcePlan, ...]
    blockers: tuple[MergeBlocker, ...]
    notes: tuple[str, ...]

    @property
    def can_merge(self) -> bool:
        return not self.blockers

    @property
    def moved_purchase_count(self) -> int:
        return sum(item.purchase_count for item in self.sources)

    @property
    def moved_invoice_line_count(self) -> int:
        return sum(item.invoice_line_count for item in self.sources)

    @property
    def moved_pack_spec_count(self) -> int:
        return sum(
            item.pack_spec_count - item.duplicate_pack_spec_count
            for item in self.sources
        )


class MergeService:
    """重复食材的预览与合并。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- 预览 ---

    def preview(self, target_id: int, source_ids: Any) -> MergePlan:
        """只读：这次合并会搬动什么、有什么拦着。不写任何东西。"""
        checked = self._check_ids(target_id, source_ids)
        with self._database.unit_of_work() as session:
            return self._plan(session, target_id, checked)

    # --- 执行 ---

    def merge(self, target_id: int, source_ids: Any) -> MergePlan:
        """一次事务里完成合并。有任何拦路冲突就整个拒绝，不做"能并多少并多少"。

        ⚠️ **部分成功比失败更坏**：并掉一半之后老板看到的是一份既不是合并前、
        也不是合并后的食材库，而他无从知道差在哪几条。
        """
        checked = self._check_ids(target_id, source_ids)
        with self._database.unit_of_work() as session:
            plan = self._plan(session, target_id, checked)
            if plan.blockers:
                raise ConflictError(
                    "这次合并被拦住了：" + "；".join(b.message for b in plan.blockers),
                    details={
                        "blockers": [
                            {"code": b.code, "message": b.message, "details": b.details}
                            for b in plan.blockers
                        ]
                    },
                )

            food_items = FoodItemRepository(session)
            purchases = PurchaseRepository(session)
            lines = InvoiceLineRepository(session)
            pack_specs = PackSpecRepository(session)
            merges = FoodItemMergeRepository(session)

            target = food_items.get(target_id)
            assert target is not None  # _plan 已经加载过它
            target_specs = {
                self._spec_key(spec): spec
                for spec in pack_specs.list_for_food_items([target_id])
            }

            for source_plan in plan.sources:
                source_id = source_plan.food_item.id
                source = food_items.get(source_id)
                if source is None:  # pragma: no cover - 同一事务里不会消失
                    raise NotFoundError(f"食材 {source_id} 不存在。")

                moved_purchases = purchases.repoint_food_item(
                    from_food_item_id=source_id, to_food_item_id=target_id
                )
                moved_lines = lines.repoint_food_item(
                    from_food_item_id=source_id, to_food_item_id=target_id
                )
                moved_specs = 0
                for spec in pack_specs.list_for_food_items([source_id]):
                    if self._spec_key(spec) in target_specs:
                        # 完全相同的箱规：丢掉重复的那条，不是丢掉一个事实。
                        pack_specs.delete(spec)
                        continue
                    spec.food_item_id = target_id
                    target_specs[self._spec_key(spec)] = spec
                    moved_specs += 1

                merges.add(
                    FoodItemMerge(
                        target_food_item_id=target_id,
                        merged_food_item_id=source_id,
                        name=source.name,
                        name_normalized=source.name_normalized,
                        name_zh=source.name_zh,
                        name_zh_normalized=source.name_zh_normalized,
                        specification=source.specification,
                        base_unit=source.base_unit,
                        moved_purchase_count=moved_purchases,
                        moved_invoice_line_count=moved_lines,
                        moved_pack_spec_count=moved_specs,
                    )
                )
                # 先 flush 掉上面的改动再删：否则 SQLAlchemy 可能把 DELETE 排在
                # UPDATE 前面，撞上外键。
                session.flush()
                food_items.delete(source)

            session.flush()
            session.refresh(target)
            return MergePlan(
                target=FoodItemView.of(target),
                sources=plan.sources,
                blockers=(),
                notes=plan.notes,
            )

    # --- 计划 ---

    def _plan(self, session, target_id: int, source_ids: tuple[int, ...]) -> MergePlan:
        food_items = FoodItemRepository(session)
        purchases = PurchaseRepository(session)
        lines = InvoiceLineRepository(session)
        pack_specs = PackSpecRepository(session)

        target = food_items.get(target_id)
        if target is None:
            raise NotFoundError(f"食材 {target_id} 不存在。")

        blockers: list[MergeBlocker] = []
        notes: list[str] = [IRREVERSIBLE_NOTE]
        target_specs = {
            self._spec_key(spec): spec
            for spec in pack_specs.list_for_food_items([target_id])
        }

        source_plans: list[MergeSourcePlan] = []
        for source_id in source_ids:
            if source_id == target_id:
                blockers.append(
                    MergeBlocker(
                        code=BLOCK_SELF_MERGE,
                        message="不能把一条食材合并到它自己。",
                        details={"food_item_id": source_id},
                    )
                )
                continue
            source = food_items.get(source_id)
            if source is None:
                raise NotFoundError(f"食材 {source_id} 不存在。")

            self._check_units(target, source, blockers, notes)

            specs = pack_specs.list_for_food_items([source_id])
            duplicates = 0
            for spec in specs:
                existing = target_specs.get(self._spec_key(spec))
                if existing is None:
                    continue
                if (
                    existing.pack_size == spec.pack_size
                    and existing.pack_unit == spec.pack_unit
                ):
                    duplicates += 1
                    continue
                blockers.append(
                    MergeBlocker(
                        code=BLOCK_PACK_SPEC_CONFLICT,
                        message=(
                            "这两条食材留有不同的旧单位换算记录，暂时不能合并。"
                        ),
                        details={
                            "food_item_id": source_id,
                            "source_name": spec.source_name,
                            "purchase_unit": spec.purchase_unit_normalized,
                            "target_pack_spec_id": existing.id,
                            "source_pack_spec_id": spec.id,
                        },
                    )
                )

            source_plans.append(
                MergeSourcePlan(
                    food_item=FoodItemView.of(source),
                    purchase_count=purchases.count_for_food_item(source_id),
                    invoice_line_count=lines.count_for_food_item(source_id),
                    pack_spec_count=len(specs),
                    duplicate_pack_spec_count=duplicates,
                )
            )

        if any(item.duplicate_pack_spec_count for item in source_plans):
            notes.append(
                "两边有**完全相同**的箱规（来源、包装单位、规格数量、基础单位都一样），"
                "重复的那份会被丢掉——丢的是一份副本，不是一个事实。"
            )
        return MergePlan(
            target=FoodItemView.of(target),
            sources=tuple(source_plans),
            blockers=tuple(blockers),
            notes=tuple(notes),
        )

    @staticmethod
    def _check_units(target, source, blockers: list[MergeBlocker], notes: list[str]) -> None:
        """基础单位的可合并性。

        三种情况：

        - **同一个单位** → 什么都不用说；
        - **同一类计量、不同单位**（lb 与 kg）→ 允许，但要说出来：合并之后这些
          历史采购会按目标食材的基础单位重新标准化，价格数字会变（变的是单位，
          不是钱）；
        - **不同类计量**（lb 与 gallon）→ **拒绝**。没有密度就不能把体积换成重量
          （步骤 09 的硬规则），强行合并会让一半的历史永远算不出标准化价格，
          而界面上只会显示成"暂无数据"。
        """
        if source.base_unit == target.base_unit:
            return
        source_dimension = dimension_of(source.base_unit)
        target_dimension = dimension_of(target.base_unit)
        if source_dimension != target_dimension:
            blockers.append(
                MergeBlocker(
                    code=BLOCK_INCOMPATIBLE_BASE_UNIT,
                    message=(
                        f"{source.name} 使用 {source.base_unit}，{target.name} 使用"
                        f" {target.base_unit}。这两种单位不能直接换算，因此不能合并。"
                    ),
                    details={
                        "food_item_id": source.id,
                        "from_unit": source.base_unit,
                        "to_unit": target.base_unit,
                        "from_dimension": source_dimension,
                        "to_dimension": target_dimension,
                    },
                )
            )
            return
        note = (
            f"{source.name} 原来按 {source.base_unit} 比价；合并后会按"
            f" {target.base_unit} 比价。历史采购金额不变。"
        )
        if note not in notes:
            notes.append(note)

    @staticmethod
    def _spec_key(spec) -> tuple[str | None, str]:
        """箱规的唯一键，与 `pack_specs` 的两条局部唯一索引同口径。"""
        return (spec.source_name_normalized, spec.purchase_unit_normalized)

    # --- 输入校验 ---

    @staticmethod
    def _check_ids(target_id: Any, source_ids: Any) -> tuple[int, ...]:
        if not isinstance(source_ids, (list, tuple)):
            raise ValidationError(
                "source_ids 必须是整数数组。", details={"field": "source_ids"}
            )
        if not source_ids:
            raise ValidationError(
                "至少要指定一条被合并的食材。", details={"field": "source_ids"}
            )
        if len(source_ids) > MAX_MERGE_SOURCES:
            raise ValidationError(
                f"一次最多合并 {MAX_MERGE_SOURCES} 条。合并不可逆，分几次做更看得清"
                "每一次动了什么。",
                details={"field": "source_ids", "max": MAX_MERGE_SOURCES},
            )
        checked: list[int] = []
        for value in source_ids:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValidationError(
                    "source_ids 里只能是整数。", details={"field": "source_ids"}
                )
            if value not in checked:
                checked.append(value)
        return tuple(checked)


def merged_name_lookup(session, query_normalized: str) -> FoodItemMerge | None:
    """按旧叫法查「这个名字现在归谁」，取最近一次合并（10.6）。

    放在模块级而不是 `MergeService` 里：调用方是 `services/suggestions.py`，
    它在自己的事务里查，不需要（也不该）为此开第二个事务。
    """
    if not query_normalized:
        return None
    rows = FoodItemMergeRepository(session).find_by_old_name(query_normalized)
    return rows[0] if rows else None


def normalized_old_name(value: str) -> str:
    """供调用方把用户输入折叠成与日志里一致的形式。"""
    return normalize_name(value)


__all__ = [
    "BLOCK_INCOMPATIBLE_BASE_UNIT",
    "BLOCK_PACK_SPEC_CONFLICT",
    "BLOCK_SELF_MERGE",
    "IRREVERSIBLE_NOTE",
    "MAX_MERGE_SOURCES",
    "MergeBlocker",
    "MergePlan",
    "MergeService",
    "MergeSourcePlan",
    "merged_name_lookup",
    "normalized_old_name",
]
