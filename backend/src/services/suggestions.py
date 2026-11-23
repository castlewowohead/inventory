"""食材建议：模糊匹配、最近与常用（步骤 10.1 / 10.2）。

### 为什么不在 `services/ingredients.py` 里

那个模块是**单位与名称词表的 owner**，`services/units.py` 与
`services/matching.py` 都 import 它。模糊匹配需要 `matching.py`，写在
`ingredients.py` 里就成了循环 import。依赖方向与 `units.py` / `prices.py` 一致：
**派生的读路径依赖词表，词表不依赖它们。**

### 和步骤 03 的搜索是什么关系

步骤 03 的 `GET /api/food-items` 保持原样不动，它是**食材库那一页**：分页浏览、
分类筛选、找到确定的某一条然后改它或删它。在那里把"差不多像"的也列出来是帮倒忙
——老板接下来可能要删它，而"这条到底在不在库里"必须有确定答案。所以
`搜 `_live` 返回 0 条` 在那个接口上是**正确行为**，本模块不去改它。

本模块是**选食材那一屏**用的：发票逐行确认时在候选框里打几个字。那里要的是
"容忍缩写与拼写差异、把最可能的排前面"，而且每条候选都要能说出**为什么命中**
——理由和分数一起返回，与步骤 09 每个统计响应都带 `notes` 是同一条原则。

### 三条来源，分开返回

| 来源 | 什么时候有 | 怎么来的 |
| --- | --- | --- |
| `matches` | 带了关键字 | 精确 → 前缀 → 包含 → 模糊，四档 |
| `recent` | 没带关键字 | 按**最近一次入账**排（`MAX(purchases.id)`） |
| `frequent` | 没带关键字 | 最近 `FREQUENT_WINDOW_DAYS` 天内按采购次数排 |

分开返回而不是合成一张榜：三者的含义不同，合在一起老板就分不清眼前这条是"刚买过"
还是"打的字像"。前端要并排摆还是分段摆，是前端的事。

⚠️ **本模块只给候选，一条都不替老板选中。** 05.6 定下的口径「预填候选可以，
替老板点确认不行」在这里继续有效；10.C 的"零点击选中"走的是**关键词精确命中**
那一条路，不吃本模块的模糊分数——`pork` 与 `park` 的模糊比值是 0.75，当候选没问题，
拿来自动选中就会把一条采购记录静默挂到错误的食材上。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from src.db import Database
from src.errors import ValidationError
from src.repositories.ingredients import CategoryRepository, FoodItemRepository
from src.repositories.purchases import PurchaseRepository
from src.services import matching
from src.services.ingredients import (
    MAX_QUERY_LENGTH,
    FoodItemView,
    normalize_name,
)
from src.services.merging import merged_name_lookup
from src.services.validation import require_limit

DEFAULT_SUGGEST_LIMIT: Final[int] = 10
MAX_SUGGEST_LIMIT: Final[int] = 50
"""建议列表的上限比步骤 03 的列表（100）小得多：这是一个"扫一眼就选"的列表，
给 100 条等于没给建议。"""

FREQUENT_WINDOW_DAYS: Final[int] = 90
"""「常用」的统计窗口。

为什么是 90 天而不是"全部历史"：餐厅的菜单会换季。半年前每周买的那样东西，
今天排在榜首只会挡住现在真正常买的。为什么不是 30 天：步骤 09 的真实样本里
**全部采购集中在 2026 年 6 月一个月**，30 天的窗口在真实数据上经常是空的，
而一个总是空的入口等于没有这个入口。

⚠️ 这是一个**可以调的产品参数，不是算出来的阈值**。真实数据不足以把它量出来，
所以它写成常量、写明理由，而不是假装有依据。
"""

FUZZY_SCAN_LIMIT: Final[int] = 5000
"""模糊层一次最多比较多少条食材。

一家餐厅的食材库是几百条量级，5000 已经很宽裕。⚠️ **扫到上限时要在 `notes` 里
说出来**，不能悄悄给一份不完整的候选——「候选里没有」和「候选只看了前 5000 条」
对老板的下一步动作完全不同（前者该新建，后者该换个关键词再搜）。
精确与包含那两层走数据库索引，不受这个上限影响。
"""

# 四档命中的排序优先级。排序先按档位、再按分数：分数只表示"有多像"，
# 不负责把精确命中压到模糊命中后面去。
_TIERS: Final[dict[str, int]] = {
    matching.REASON_EXACT: 0,
    # 被合并掉的旧叫法排在前缀命中之前：合并是老板**亲手做过的判断**，
    # 比"名字开头一样"确定得多。
    matching.REASON_MERGED_FROM: 1,
    matching.REASON_PREFIX: 2,
    matching.REASON_CONTAINS: 3,
    matching.REASON_FUZZY: 4,
}


@dataclass(frozen=True, slots=True)
class FoodItemSuggestion:
    """一条候选食材，**带着它是怎么来的**。

    `merged_from` 只有 `match_reason == "merged_from"` 时有值：老板打的那个名字
    已经被合并掉了，这一列告诉界面"他打的是哪个旧叫法"。不说清楚的话他会以为
    自己记错了，然后再建一条重复的——正好回到合并要解决的问题。

    `score` 是 `matching.coverage()` 的单向相似度（"我打的这些字，这条食材里有
    多少"），`match_reason` 是档位。两个都给，是因为单看分数解释不了排序：
    精确命中和包含命中都可能是 1.0 分，排序靠的是档位。

    `purchase_count` / `last_purchase_date` 只有「最近 / 常用」两条来源有值——
    名称匹配那三档不去查采购历史（候选框每敲一个字都要查一次，而那两列在
    名称匹配的场景里也不影响排序）。
    """

    food_item: FoodItemView
    match_reason: str
    score: float | None
    purchase_count: int | None = None
    last_purchase_date: date | None = None
    merged_from: str | None = None


@dataclass(frozen=True, slots=True)
class FoodItemSuggestions:
    """一次建议查询的结果。`notes` 是**必须跟着列表一起读**的话。"""

    query: str | None
    matches: tuple[FoodItemSuggestion, ...]
    recent: tuple[FoodItemSuggestion, ...]
    frequent: tuple[FoodItemSuggestion, ...]
    notes: tuple[str, ...]


class SuggestionService:
    """食材建议的只读服务。无状态，可按请求创建。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    def suggest_food_items(
        self,
        *,
        query: str | None = None,
        category_id: int | None = None,
        limit: int = DEFAULT_SUGGEST_LIMIT,
        today: date | None = None,
    ) -> FoodItemSuggestions:
        """带关键字就做四档名称匹配；不带关键字就给「最近 + 常用」。"""
        checked_limit = require_limit(limit, max_limit=MAX_SUGGEST_LIMIT)
        normalized_query = self._normalize_query(query)
        notes: list[str] = []

        with self._database.unit_of_work() as session:
            if category_id is not None:
                self._require_category(session, category_id)
            if normalized_query is None:
                recent, frequent = self._usage_lists(
                    session,
                    category_id=category_id,
                    limit=checked_limit,
                    today=today or date.today(),
                    notes=notes,
                )
                return FoodItemSuggestions(
                    query=None,
                    matches=(),
                    recent=recent,
                    frequent=frequent,
                    notes=tuple(notes),
                )
            matches = self._name_matches(
                session,
                normalized_query=normalized_query,
                category_id=category_id,
                limit=checked_limit,
                notes=notes,
            )
            return FoodItemSuggestions(
                query=normalized_query,
                matches=matches,
                recent=(),
                frequent=(),
                notes=tuple(notes),
            )

    # --- 名称匹配 ---

    def _name_matches(
        self,
        session,
        *,
        normalized_query: str,
        category_id: int | None,
        limit: int,
        notes: list[str],
    ) -> tuple[FoodItemSuggestion, ...]:
        repository = FoodItemRepository(session)
        rows, _total = repository.search(
            query_normalized=normalized_query,
            category_id=category_id,
            limit=limit,
            offset=0,
        )
        prepared_query = matching.prepare(normalized_query)
        found: dict[int, FoodItemSuggestion] = {}
        for row in rows:
            found[row.id] = FoodItemSuggestion(
                food_item=FoodItemView.of(row),
                match_reason=self._precise_reason(row, normalized_query),
                score=round(
                    matching.coverage(prepared_query, matching.prepare(row.search_text)),
                    4,
                ),
            )

        # 被合并掉的旧叫法（10.3 / 10.6）。放在模糊层**之前**：那是一次老板亲手
        # 做过的纠正，比"看起来像"确定得多。
        merged = self._merged_match(
            session,
            normalized_query=normalized_query,
            category_id=category_id,
            exclude=set(found),
        )
        if merged is not None:
            found[merged.food_item.id] = merged
            notes.append(
                f"「{merged.merged_from}」已经被合并到「{merged.food_item.name}」，"
                "所以给出的是合并后的那一条。"
            )

        # 模糊层**只在精确层没填满时才扫库**。精确层已经给出 `limit` 条包含关键字的
        # 食材时，那些就是答案——为了可能更像的几条再把整库过一遍不值得，而且会让
        # 候选框每敲一个字都扫一次。
        if len(found) < limit:
            found.update(
                self._fuzzy_matches(
                    session,
                    prepared_query=prepared_query,
                    category_id=category_id,
                    exclude=set(found),
                    limit=limit - len(found),
                    notes=notes,
                )
            )

        ordered = sorted(
            found.values(),
            key=lambda item: (
                _TIERS[item.match_reason],
                -(item.score or 0.0),
                item.food_item.name,
            ),
        )
        return tuple(ordered[:limit])

    @staticmethod
    def _precise_reason(row, normalized_query: str) -> str:
        """包含匹配命中的那一条属于哪一档。与 `FoodItemRepository._ordering()` 同口径。"""
        if normalized_query in (row.name_normalized, row.name_zh_normalized):
            return matching.REASON_EXACT
        if row.name_normalized.startswith(normalized_query) or (
            row.name_zh_normalized or ""
        ).startswith(normalized_query):
            return matching.REASON_PREFIX
        return matching.REASON_CONTAINS

    def _merged_match(
        self,
        session,
        *,
        normalized_query: str,
        category_id: int | None,
        exclude: set[int],
    ) -> FoodItemSuggestion | None:
        """老板打的是一个**被合并掉的旧叫法**吗（10.3 / 10.6）。

        这是「保存过的纠正能够辅助下一次匹配」里最硬的那一条：合并是老板亲手做过
        的判断，不是算出来的相似度。所以它排在模糊层之前，而且**不给分数**——
        它不是比出来的。

        目标食材已经在结果里（他打的字同时也命中了新名字）就不重复给一条。
        """
        merge = merged_name_lookup(session, normalized_query)
        if merge is None or merge.target_food_item_id in exclude:
            return None
        food_item = FoodItemRepository(session).get(merge.target_food_item_id)
        if food_item is None:  # pragma: no cover - 外键保证它在
            return None
        if category_id is not None and food_item.category_id != category_id:
            return None
        return FoodItemSuggestion(
            food_item=FoodItemView.of(food_item),
            match_reason=matching.REASON_MERGED_FROM,
            score=None,
            merged_from=merge.name,
        )

    def _fuzzy_matches(
        self,
        session,
        *,
        prepared_query: matching.Prepared,
        category_id: int | None,
        exclude: set[int],
        limit: int,
        notes: list[str],
    ) -> dict[int, FoodItemSuggestion]:
        repository = FoodItemRepository(session)
        rows = repository.scan_searchable(
            category_id=category_id, limit=FUZZY_SCAN_LIMIT
        )
        if len(rows) >= FUZZY_SCAN_LIMIT:
            notes.append(
                "食材较多，搜索建议可能不完整。找不到时请试试更准确的名称。"
            )
        scored: list[tuple[float, int]] = []
        for row in rows:
            if row.id in exclude:
                continue
            score = matching.coverage(prepared_query, matching.prepare(row.search_text))
            if score >= matching.SUGGEST_MIN_SCORE:
                scored.append((score, row.id))
        if not scored:
            return {}
        # 分数高的在前；同分按 id 升序，保证两次相同查询的结果一致
        # （顺序不稳定的候选列表会让老板以为自己点错了）。
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        top = scored[:limit]
        items = repository.get_many([item_id for _score, item_id in top])
        by_id = {item.id: item for item in items}
        result: dict[int, FoodItemSuggestion] = {}
        for score, item_id in top:
            row = by_id.get(item_id)
            if row is None:  # pragma: no cover - 同一个事务里不会消失
                continue
            result[item_id] = FoodItemSuggestion(
                food_item=FoodItemView.of(row),
                match_reason=matching.REASON_FUZZY,
                score=round(score, 4),
            )
        return result

    # --- 最近 / 常用 ---

    def _usage_lists(
        self,
        session,
        *,
        category_id: int | None,
        limit: int,
        today: date,
        notes: list[str],
    ) -> tuple[tuple[FoodItemSuggestion, ...], tuple[FoodItemSuggestion, ...]]:
        purchases = PurchaseRepository(session)
        food_items = FoodItemRepository(session)

        recent_rows = purchases.food_item_usage(
            category_id=category_id, limit=limit, order="recent"
        )
        since = today - timedelta(days=FREQUENT_WINDOW_DAYS - 1)
        frequent_rows = purchases.food_item_usage(
            since=since, category_id=category_id, limit=limit, order="frequent"
        )
        notes.append(
            f"「常用」只统计 {since.isoformat()} 起（最近 {FREQUENT_WINDOW_DAYS} 天）"
            "的采购。⚠️ **未填采购日期的记录进不了这个榜**——它们算不进任何一个月，"
            "与步骤 09 的月度均价同一条口径。那些记录仍然出现在「最近」里。"
        )
        return (
            self._usage_suggestions(food_items, recent_rows, matching.REASON_RECENT),
            self._usage_suggestions(
                food_items, frequent_rows, matching.REASON_FREQUENT
            ),
        )

    @staticmethod
    def _usage_suggestions(
        repository: FoodItemRepository, rows, reason: str
    ) -> tuple[FoodItemSuggestion, ...]:
        if not rows:
            return ()
        items = repository.get_many([row.food_item_id for row in rows])
        by_id = {item.id: item for item in items}
        result: list[FoodItemSuggestion] = []
        for row in rows:
            food_item = by_id.get(row.food_item_id)
            if food_item is None:  # pragma: no cover - 外键保证它在
                continue
            result.append(
                FoodItemSuggestion(
                    food_item=FoodItemView.of(food_item),
                    match_reason=reason,
                    score=None,
                    purchase_count=row.purchase_count,
                    last_purchase_date=row.last_purchase_date,
                )
            )
        return tuple(result)

    # --- 输入校验 ---

    @staticmethod
    def _normalize_query(query) -> str | None:
        """与步骤 03 的 `_normalize_query` 同口径：空白等于不筛选，超长直接拒绝。"""
        if query is None:
            return None
        if not isinstance(query, str):
            raise ValidationError("q 必须是字符串。", details={"field": "q"})
        if len(query) > MAX_QUERY_LENGTH:
            raise ValidationError(
                f"搜索关键字不能超过 {MAX_QUERY_LENGTH} 个字符。",
                details={"field": "q", "max_length": MAX_QUERY_LENGTH},
            )
        return normalize_name(query) or None

    @staticmethod
    def _require_category(session, category_id) -> None:
        if not isinstance(category_id, int) or isinstance(category_id, bool):
            raise ValidationError(
                "category_id 必须是整数。", details={"field": "category_id"}
            )
        if CategoryRepository(session).get(category_id) is None:
            raise ValidationError(
                f"分类 {category_id} 不存在。", details={"field": "category_id"}
            )


__all__ = [
    "DEFAULT_SUGGEST_LIMIT",
    "FREQUENT_WINDOW_DAYS",
    "FUZZY_SCAN_LIMIT",
    "FoodItemSuggestion",
    "FoodItemSuggestions",
    "MAX_SUGGEST_LIMIT",
    "SuggestionService",
]
