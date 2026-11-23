"""食材、分类的数据访问（步骤 03）。

本层的全部约定：

- **只接收已有 Session**：自己不创建 Session、不提交、不回滚。事务边界由 Service
  用 `Database.unit_of_work()` 决定（见 src/db.py 与 README 第 10 节）。
- **不做业务校验、不抛业务异常**：输入的字符串已经由 Service 规范化过，本层只负责
  "怎么查、怎么写"。重复名称、分类是否存在这类判断属于 Service。
- **返回 ORM 对象**：Service 在事务内把它们转成自己的只读视图再交给 API 层，
  因此 ORM 对象不会以游离（detached）状态流到表示层。

`flush()` 的位置是有意的：写入后立刻 flush，一是拿到自增主键，二是让唯一约束的
`IntegrityError` 在 Service 的 try 块里就抛出来，而不是等到事务提交时才炸。
"""

from __future__ import annotations

from typing import NamedTuple, Sequence

from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session, joinedload

from src.models import (
    LINE_STATUS_CONFIRMED,
    Category,
    FoodItem,
    FoodItemMerge,
    InvoiceLine,
    PackSpec,
    Purchase,
)

# 搜索结果的排序档位。精确命中排在前缀命中之前，前缀命中排在"仅出现在某处"之前；
# 同档内按规范化名称排序，保证分页结果稳定（否则同一个 offset 两次请求可能不同）。
RANK_EXACT = 0
RANK_PREFIX = 1
RANK_CONTAINS = 2


class SearchableRow(NamedTuple):
    """模糊匹配扫描出来的一行：只有打分需要的两列。

    刻意**不**返回 ORM 对象：模糊层要把整库过一遍打分，而最终只有十几条会被展示。
    为了打分而实例化几千个 `FoodItem`（还各带一次分类的 joinedload）是纯浪费，
    真正该付的代价是「打完分再按 id 取回那十几条」。
    """

    id: int
    search_text: str


class CategoryRepository:
    """分类的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_all(self) -> list[Category]:
        statement = select(Category).order_by(Category.name_normalized)
        return list(self._session.scalars(statement))

    def get(self, category_id: int) -> Category | None:
        return self._session.get(Category, category_id)

    def find_by_normalized_name(self, name_normalized: str) -> Category | None:
        statement = select(Category).where(
            Category.name_normalized == name_normalized
        )
        return self._session.scalars(statement).one_or_none()

    def count_food_items(self, category_id: int) -> int:
        """该分类下的食材条数。删除分类前用它判断是否还有引用。"""
        statement = (
            select(func.count())
            .select_from(FoodItem)
            .where(FoodItem.category_id == category_id)
        )
        return int(self._session.scalars(statement).one())

    def add(self, category: Category) -> Category:
        self._session.add(category)
        self._session.flush()
        return category

    def delete(self, category: Category) -> None:
        self._session.delete(category)
        self._session.flush()


class FoodItemRepository:
    """标准食材的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, food_item_id: int) -> FoodItem | None:
        statement = (
            select(FoodItem)
            .options(joinedload(FoodItem.category))
            .where(FoodItem.id == food_item_id)
        )
        return self._session.scalars(statement).one_or_none()

    def find_by_normalized_name(self, name_normalized: str) -> FoodItem | None:
        statement = (
            select(FoodItem)
            .options(joinedload(FoodItem.category))
            .where(FoodItem.name_normalized == name_normalized)
        )
        return self._session.scalars(statement).one_or_none()

    def find_by_normalized_name_zh(self, name_zh_normalized: str) -> FoodItem | None:
        """按中文名查。调用方必须传非空值：中文名为空的食材不该被"空值"命中。"""
        if not name_zh_normalized:
            raise ValueError("name_zh_normalized 不能为空")
        statement = (
            select(FoodItem)
            .options(joinedload(FoodItem.category))
            .where(FoodItem.name_zh_normalized == name_zh_normalized)
        )
        return self._session.scalars(statement).one_or_none()

    def get_many(self, food_item_ids: Sequence[int]) -> list[FoodItem]:
        """按 id 批量取回，**保持传入顺序**。

        顺序由调用方决定是刻意的：模糊层已经按分数排好了，数据库按主键返回会把那个
        顺序打乱，而「最像的排第一」正是候选列表唯一的价值。
        """
        if not food_item_ids:
            return []
        statement = (
            select(FoodItem)
            .options(joinedload(FoodItem.category))
            .where(FoodItem.id.in_(list(food_item_ids)))
        )
        by_id = {row.id: row for row in self._session.scalars(statement)}
        return [by_id[item_id] for item_id in food_item_ids if item_id in by_id]

    def scan_searchable(
        self, *, category_id: int | None = None, limit: int
    ) -> list[SearchableRow]:
        """取出供模糊打分用的全部检索文本，最多 `limit` 行。

        **为什么是整库扫描而不是建索引表**：模糊匹配要容忍缩写与拼写差异，
        `CHIX-BRST` 与 `CHIX BRST` 之间没有可以用 SQL `LIKE` 命中的公共子串，
        而 SQLite 没有自带的三元组索引。一家餐厅的食材库是几百条量级，扫一遍
        几百行短字符串比维护一张随写入同步的索引表便宜得多——索引表的真正代价
        不是空间，是它和主表**不同步时不会报错**。

        `limit` 是上限而不是分页：调用方必须能区分「扫完了」与「扫到上限」，
        后者意味着模糊层给出的候选不完整，这件事要说出来（见
        `services/ingredients.py` 的 `FUZZY_SCAN_LIMIT`）。
        """
        statement = select(FoodItem.id, FoodItem.search_text)
        if category_id is not None:
            statement = statement.where(FoodItem.category_id == category_id)
        statement = statement.order_by(FoodItem.id).limit(limit)
        return [
            SearchableRow(id=int(row.id), search_text=row.search_text)
            for row in self._session.execute(statement)
        ]

    def search(
        self,
        *,
        query_normalized: str | None = None,
        category_id: int | None = None,
        never_purchased: bool | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[FoodItem], int]:
        """返回（当页食材, 符合条件的总条数）。

        `query_normalized` 对 `search_text` 做一次包含匹配，于是中文名、英文名与
        规格用同一个条件命中，一条食材也只会出现一次（单表查询，无需去重）。
        `%`、`_` 与转义符本身由 SQLAlchemy 的 `autoescape=True` 处理，用户搜
        "100%" 不会变成通配符。

        模糊匹配不在这里：它需要把检索文本取回内存打分，走
        `scan_searchable()` + `get_many()`（步骤 10 的建议接口）。**本方法保持
        "包含匹配 + 稳定分页"不变**——食材库那一页是老板用来找确定的某一条、
        然后改它或删它的地方，在那里多给几条"差不多像"的会让"这条到底在不在库里"
        变得说不清。

        `never_purchased=True` 筛出**一条采购记录都没有引用过**的食材（10.15，
        用来回收孤立食材）。⚠️ 判据刻意**包含已作废的采购记录**：作废是软删，那条
        记录仍然是一个外键引用，带着它的食材删不掉——把它算成"从没买过"只会让
        老板在删除时撞 409。
        """
        conditions = []
        if query_normalized:
            conditions.append(FoodItem.search_text.contains(query_normalized, autoescape=True))
        if category_id is not None:
            conditions.append(FoodItem.category_id == category_id)
        if never_purchased is not None:
            referenced = (
                select(Purchase.id)
                .where(Purchase.food_item_id == FoodItem.id)
                .exists()
            )
            conditions.append(~referenced if never_purchased else referenced)

        total_statement = select(func.count()).select_from(FoodItem).where(*conditions)
        total = int(self._session.scalars(total_statement).one())

        statement: Select[tuple[FoodItem]] = (
            select(FoodItem)
            .options(joinedload(FoodItem.category))
            .where(*conditions)
            .order_by(*self._ordering(query_normalized))
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total

    @staticmethod
    def _ordering(query_normalized: str | None) -> tuple:
        """排序表达式：无关键字时按名称；有关键字时精确 → 前缀 → 其余。"""
        if not query_normalized:
            return (FoodItem.name_normalized,)
        rank = case(
            (FoodItem.name_normalized == query_normalized, RANK_EXACT),
            (FoodItem.name_zh_normalized == query_normalized, RANK_EXACT),
            (
                FoodItem.name_normalized.startswith(query_normalized, autoescape=True),
                RANK_PREFIX,
            ),
            (
                FoodItem.name_zh_normalized.startswith(
                    query_normalized, autoescape=True
                ),
                RANK_PREFIX,
            ),
            else_=RANK_CONTAINS,
        )
        return (rank, FoodItem.name_normalized)

    def add(self, food_item: FoodItem) -> FoodItem:
        self._session.add(food_item)
        self._session.flush()
        return food_item

    def delete(self, food_item: FoodItem) -> None:
        self._session.delete(food_item)
        self._session.flush()

    def reference_counts(self, food_item_id: int) -> dict[str, int]:
        """Count direct references before deciding whether deletion is allowed."""

        def count(model, *conditions) -> int:
            statement = select(func.count()).select_from(model).where(*conditions)
            return int(self._session.scalar(statement) or 0)

        return {
            "purchases": count(Purchase, Purchase.food_item_id == food_item_id),
            "manual_purchases": count(
                Purchase,
                Purchase.food_item_id == food_item_id,
                Purchase.entry_source == "manual",
            ),
            "voided_purchases": count(Purchase, Purchase.food_item_id == food_item_id, Purchase.voided_at.is_not(None)),
            "invoice_linked_purchases": count(
                Purchase,
                Purchase.food_item_id == food_item_id,
                Purchase.invoice_line_id.is_not(None),
            ),
            "invoice_lines": count(InvoiceLine, InvoiceLine.food_item_id == food_item_id),
            "confirmed_invoice_lines": count(
                InvoiceLine,
                InvoiceLine.food_item_id == food_item_id,
                InvoiceLine.status == LINE_STATUS_CONFIRMED,
            ),
            "pack_specs": count(PackSpec, PackSpec.food_item_id == food_item_id),
            "merge_history": count(FoodItemMerge, FoodItemMerge.target_food_item_id == food_item_id),
        }

    def clear_unlinked_history(self, food_item_id: int) -> None:
        """Remove standalone history and release draft references before deletion."""
        for line in self._session.scalars(
            select(InvoiceLine).where(InvoiceLine.food_item_id == food_item_id)
        ):
            if line.status != LINE_STATUS_CONFIRMED:
                line.food_item_id = None
                line.version += 1

        for purchase in self._session.scalars(
            select(Purchase).where(
                Purchase.food_item_id == food_item_id,
                Purchase.invoice_line_id.is_(None),
            )
        ):
            self._session.delete(purchase)

        for spec in self._session.scalars(
            select(PackSpec).where(PackSpec.food_item_id == food_item_id)
        ):
            self._session.delete(spec)

        for merge in self._session.scalars(
            select(FoodItemMerge).where(FoodItemMerge.target_food_item_id == food_item_id)
        ):
            self._session.delete(merge)

        self._session.flush()


class FoodItemMergeRepository:
    """重复食材合并日志的数据访问（步骤 10.3）。

    这张表只追加、不修改，所以这里没有 `update` 也没有 `delete`。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, merge: FoodItemMerge) -> FoodItemMerge:
        self._session.add(merge)
        self._session.flush()
        return merge

    def find_by_old_name(self, name_normalized: str) -> list[FoodItemMerge]:
        """按被合并掉的旧叫法查，最近的合并排在前面。

        **可能有多条**：同一个名字可以被合并过不止一次（合并之后名字就释放了，
        老板可以再建一条同名食材、再合并一次）。返回全部并按时间倒序，由服务层
        决定取哪一条——最近那次才是现在有效的归属。
        """
        if not name_normalized:
            return []
        statement = (
            select(FoodItemMerge)
            .where(
                (FoodItemMerge.name_normalized == name_normalized)
                | (FoodItemMerge.name_zh_normalized == name_normalized)
            )
            .order_by(FoodItemMerge.id.desc())
        )
        return list(self._session.scalars(statement))

    def list_for_target(self, target_food_item_id: int) -> list[FoodItemMerge]:
        """这条食材吞并过哪些叫法。界面上用来说明"它现在还代表着哪些旧名字"。"""
        statement = (
            select(FoodItemMerge)
            .where(FoodItemMerge.target_food_item_id == target_food_item_id)
            .order_by(FoodItemMerge.id.desc())
        )
        return list(self._session.scalars(statement))
