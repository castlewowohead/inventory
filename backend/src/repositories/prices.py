"""箱规与涨价提醒设置的数据访问（步骤 09）。

本层的约定与其它 Repository 相同：只接收已有 Session，自己不提交、不回滚、
不做业务校验；返回 ORM 对象，由 Service 在事务内转成只读视图。

**为什么单独一个文件而不塞进 `repositories/purchases.py`**：那边是采购记录本身的
读写（步骤 04 起就是），这里的两张表都是**换算与提醒的配置**，生命周期和采购记录
无关。价格统计要读的采购记录仍然走 `PurchaseRepository`，不在这里复制一份查询。

⚠️ **箱规查找的顺序是业务规则，不是实现细节**：先精确匹配来源，匹配不到再退到
「不限来源」那条。写在 `index_for_lookup()` 的注释里，因为顺序颠倒的后果是
——某家供应商的特殊箱规被通用箱规盖掉，价格差好几倍，而且不报错。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from src.models import (
    PRICE_ALERT_SETTINGS_ID,
    PackSpec,
    PriceAlertSettings,
)

# 箱规查找键：(食材 id, 规范化来源名或 None, 包装单位 token)
PackSpecKey = tuple[int, str | None, str]


class PackSpecRepository:
    """箱规的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, pack_spec_id: int) -> PackSpec | None:
        statement = (
            select(PackSpec)
            .options(joinedload(PackSpec.food_item))
            .where(PackSpec.id == pack_spec_id)
        )
        return self._session.scalars(statement).unique().one_or_none()

    def find_exact(
        self,
        *,
        food_item_id: int,
        source_name_normalized: str | None,
        purchase_unit_normalized: str,
    ) -> PackSpec | None:
        """按完整键找一条。`source_name_normalized=None` 找的是「不限来源」那条。

        `IS NULL` 不能写成 `== None` 之外的形式：SQL 里 `x = NULL` 永远不成立，
        所以这里显式分支，否则"不限来源"那条永远查不到。
        """
        conditions = [
            PackSpec.food_item_id == food_item_id,
            PackSpec.purchase_unit_normalized == purchase_unit_normalized,
        ]
        if source_name_normalized is None:
            conditions.append(PackSpec.source_name_normalized.is_(None))
        else:
            conditions.append(
                PackSpec.source_name_normalized == source_name_normalized
            )
        statement = (
            select(PackSpec)
            .options(joinedload(PackSpec.food_item))
            .where(*conditions)
        )
        return self._session.scalars(statement).unique().one_or_none()

    def list_for_food_items(self, food_item_ids: list[int]) -> list[PackSpec]:
        """一次取出若干食材的全部箱规，供批量统计用。

        统计要对每一行采购记录找箱规，逐行查会变成 N 次查询；条数本来很少
        （一家供应商一种货一条），一次取回来在内存里建索引更简单也更快。
        """
        if not food_item_ids:
            return []
        statement = (
            select(PackSpec)
            .options(joinedload(PackSpec.food_item))
            .where(PackSpec.food_item_id.in_(food_item_ids))
            .order_by(PackSpec.food_item_id, PackSpec.id)
        )
        return list(self._session.scalars(statement).unique())

    def list_all(
        self, *, food_item_id: int | None = None, limit: int, offset: int
    ) -> tuple[list[PackSpec], int]:
        conditions = []
        if food_item_id is not None:
            conditions.append(PackSpec.food_item_id == food_item_id)
        total = int(
            self._session.scalars(
                select(func.count()).select_from(PackSpec).where(*conditions)
            ).one()
        )
        statement = (
            select(PackSpec)
            .options(joinedload(PackSpec.food_item))
            .where(*conditions)
            # 稳定排序：同一个 offset 两次请求必须返回同一页。
            .order_by(PackSpec.food_item_id, PackSpec.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement).unique()), total

    def add(self, pack_spec: PackSpec) -> PackSpec:
        self._session.add(pack_spec)
        self._session.flush()
        return pack_spec

    def delete(self, pack_spec: PackSpec) -> None:
        """硬删。

        箱规是**配置**而不是业务事实：删掉它只是让这家这种货重新变成"算不出
        标准化价格"，采购记录一条都不动。所以这里不做软删——留着一条作废的箱规
        只会让"到底哪条生效"变得说不清。
        """
        self._session.delete(pack_spec)
        self._session.flush()


def index_for_lookup(pack_specs: list[PackSpec]) -> dict[PackSpecKey, PackSpec]:
    """把箱规列表变成查找索引。键是 (食材, 来源或 None, 包装单位)。"""
    return {
        (
            spec.food_item_id,
            spec.source_name_normalized,
            spec.purchase_unit_normalized,
        ): spec
        for spec in pack_specs
    }


def resolve_from_index(
    index: dict[PackSpecKey, PackSpec],
    *,
    food_item_id: int,
    source_name_normalized: str | None,
    purchase_unit_normalized: str,
) -> PackSpec | None:
    """按既定顺序找箱规：**先精确来源，再不限来源**。

    ⚠️ 顺序不能颠倒。某家供应商的一箱是 44.1 lb、通用箱规写的是 30 lb 时，
    顺序反了就会用 30 lb 去算这家的价格——差 47%，不报错，也看不出来。
    """
    if source_name_normalized is not None:
        exact = index.get(
            (food_item_id, source_name_normalized, purchase_unit_normalized)
        )
        if exact is not None:
            return exact
    return index.get((food_item_id, None, purchase_unit_normalized))


class PriceAlertSettingsRepository:
    """涨价提醒设置的数据访问（单行表）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> PriceAlertSettings | None:
        return self._session.get(PriceAlertSettings, PRICE_ALERT_SETTINGS_ID)

    def add(self, settings: PriceAlertSettings) -> PriceAlertSettings:
        self._session.add(settings)
        self._session.flush()
        return settings
