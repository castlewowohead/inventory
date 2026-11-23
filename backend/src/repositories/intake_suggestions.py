"""商家记忆与相似堆的数据访问（多文件计划 M07）。

约定与 `repositories/intake.py` 相同：只接收已有 Session，自己不提交、不回滚、
不做业务判断；返回 ORM 行或原始元组，由 Service 转成只读视图。

四处需要说明的地方：

1. **「这一家叫什么」是查出来的，不是存出来的。** `confirmed_intake_sources()` 与
   `confirmed_purchase_sources()` 各是一句 `GROUP BY`，键是规范化来源名。展示写法
   取**最近一次人工确认**的那一个——与 M02 `group_rows()` 里「取字典序最大」那个
   稳定但任意的选择不同，这里有依据：人最后一次写的就是他现在想看到的。
2. **别名表是唯一落库的记忆。** 它存的是人的决定（「纸面这样写 = 这一家」），
   推不出来也必须可更正（docs/multifiles.md 第 5 节）。
3. **版式记忆同样是查出来的**：`layout_memory()` 问的是「同一个指纹下，有哪些页
   已经被人确认成哪一家」。没有「版式→商家」表，所以不会出现「表说是 A、页上
   确认的是 B」两份打架的真相；要更正就改那一页的确认。
4. **读取一律排除已回收的文件**（与 `repositories/intake.py` 第 2 条相同）：
   用户已经把那个文件撤出批次了，它的页不该再出现在建议与堆里。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models import (
    INTAKE_FILE_STATUS_DISCARDED,
    INTAKE_ITEM_KIND_PAGE,
    IntakeBatchFile,
    IntakeItem,
    IntakeSourceAlias,
    Purchase,
)
from src.repositories.intake import _item_options


class IntakeSourceMemoryRepository:
    """确认过的来源、别名映射与版式记忆的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- 确认过的来源（派生，不落表） ---------------------------------------

    def confirmed_intake_sources(
        self, *, limit: int
    ) -> list[tuple[str, str, datetime | None, int]]:
        """整理阶段人工确认过的来源：`(规范化键, 最近写法, 最近确认时刻, 内容数)`。

        展示写法取**最近一次确认**的那一个：同一个键下的两种写法由 Python 侧按
        `decided_at` 挑，不在 SQL 里用 `max(name)`——那取的是字典序，与「人最后
        一次是怎么写的」没有关系。
        """
        counts = dict(
            self._session.execute(
                select(IntakeItem.decided_source_name_normalized, func.count())
                .join(IntakeItem.batch_file)
                .where(
                    IntakeItem.decided_source_name_normalized.is_not(None),
                    IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
                )
                .group_by(IntakeItem.decided_source_name_normalized)
            ).all()
        )
        rows = self._session.execute(
            select(
                IntakeItem.decided_source_name_normalized,
                IntakeItem.decided_source_name,
                IntakeItem.decided_at,
            )
            .join(IntakeItem.batch_file)
            .where(
                IntakeItem.decided_source_name_normalized.is_not(None),
                IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
            )
            .order_by(
                IntakeItem.decided_source_name_normalized,
                IntakeItem.decided_at.desc().nulls_last(),
                IntakeItem.id.desc(),
            )
        ).all()
        latest: list[tuple[str, str, datetime | None, int]] = []
        seen: set[str] = set()
        for key, name, decided_at in rows:
            if key in seen:
                continue
            seen.add(key)
            latest.append((key, name or key, decided_at, counts.get(key, 0)))
            if len(latest) >= limit:
                break
        return latest

    def confirmed_purchase_sources(
        self, *, limit: int
    ) -> list[tuple[str, str, datetime | None, int]]:
        """已入账采购记录里用过的来源，同一个形状。

        为什么把它算进「历史来源」：`purchases.source_name` 是**人确认过**的采购
        来源（步骤 04），与整理阶段的确认是同一种事实。只读它不写它——采购记录的
        来源由采购纠错流程改，不由建议层改。
        """
        rows = self._session.execute(
            select(
                Purchase.source_name_normalized,
                func.max(Purchase.created_at),
                func.count(),
            )
            .where(Purchase.source_name_normalized.is_not(None))
            .group_by(Purchase.source_name_normalized)
            .order_by(func.max(Purchase.created_at).desc())
            .limit(limit)
        ).all()
        if not rows:
            return []
        names = dict(
            self._session.execute(
                select(Purchase.source_name_normalized, func.max(Purchase.source_name))
                .where(Purchase.source_name_normalized.is_not(None))
                .group_by(Purchase.source_name_normalized)
            ).all()
        )
        return [
            (key, names.get(key) or key, last_used, int(count))
            for key, last_used, count in rows
        ]

    # --- 别名表（唯一落库的记忆） -------------------------------------------

    def alias_for(self, alias_key: str) -> IntakeSourceAlias | None:
        return self._session.scalars(
            select(IntakeSourceAlias).where(IntakeSourceAlias.alias_key == alias_key)
        ).one_or_none()

    def list_aliases(self, *, limit: int, offset: int = 0) -> list[IntakeSourceAlias]:
        return list(
            self._session.scalars(
                select(IntakeSourceAlias)
                .order_by(IntakeSourceAlias.source_key, IntakeSourceAlias.alias_key)
                .limit(limit)
                .offset(offset)
            )
        )

    def get_alias(self, alias_id: int) -> IntakeSourceAlias | None:
        return self._session.get(IntakeSourceAlias, alias_id)

    def add_alias(self, alias: IntakeSourceAlias) -> IntakeSourceAlias:
        self._session.add(alias)
        self._session.flush()
        return alias

    def delete_alias(self, alias: IntakeSourceAlias) -> None:
        self._session.delete(alias)
        self._session.flush()

    # --- 批次内的内容 -------------------------------------------------------

    def batch_items(
        self, batch_id: int, *, limit: int, offset: int
    ) -> tuple[list[IntakeItem], int]:
        """这个批次的内容，按稳定顺序分页（与 M02 的列表同一个排序）。"""
        conditions = (
            IntakeBatchFile.batch_id == batch_id,
            IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
        )
        total = int(
            self._session.scalars(
                select(func.count())
                .select_from(IntakeItem)
                .join(IntakeItem.batch_file)
                .where(*conditions)
            ).one()
        )
        rows = list(
            self._session.scalars(
                select(IntakeItem)
                .options(*_item_options())
                .join(IntakeItem.batch_file)
                .where(*conditions)
                .order_by(
                    IntakeBatchFile.upload_order,
                    IntakeItem.page_no,
                    IntakeItem.row_no,
                    IntakeItem.id,
                )
                .limit(limit)
                .offset(offset)
            ).unique()
        )
        return rows, total

    def confirmed_in_file(self, batch_file_id: int) -> list[IntakeItem]:
        """同一份文件里**已经被人确认过商家**的页（第 3.2 节的第二层线索）。"""
        return list(
            self._session.scalars(
                select(IntakeItem)
                .where(
                    IntakeItem.batch_file_id == batch_file_id,
                    IntakeItem.decided_source_name_normalized.is_not(None),
                )
                .order_by(IntakeItem.page_no, IntakeItem.row_no, IntakeItem.id)
            )
        )

    def set_layout_profile(
        self, item: IntakeItem, *, fingerprint: str | None, features: str | None
    ) -> None:
        """写下这一页量出来的版式指纹（本地测量的缓存，不是业务决定）。"""
        item.layout_fingerprint = fingerprint
        item.layout_features = features
        self._session.flush()

    # --- 版式记忆（派生，不落表） -------------------------------------------

    def layout_memory(
        self, fingerprints: list[str]
    ) -> list[tuple[str, str, str, datetime | None, int]]:
        """同一指纹下人确认过的商家：`(指纹, 规范化键, 最近写法, 最近确认, 页数)`。

        ⚠️ 一个指纹**可能对应好几家**——不同商家用同一个模板是真实存在的情况
        （docs/multifiles.md 第 2 节）。本方法把它们**全部**返回，由 Service 原样
        摆出来让人挑；在这里"按页数最多的那一家"选一个，就是把相似度当成了事实。
        """
        if not fingerprints:
            return []
        counts = self._session.execute(
            select(
                IntakeItem.layout_fingerprint,
                IntakeItem.decided_source_name_normalized,
                func.count(),
                func.max(IntakeItem.decided_at),
            )
            .join(IntakeItem.batch_file)
            .where(
                IntakeItem.layout_fingerprint.in_(fingerprints),
                IntakeItem.decided_source_name_normalized.is_not(None),
                IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
            )
            .group_by(
                IntakeItem.layout_fingerprint,
                IntakeItem.decided_source_name_normalized,
            )
        ).all()
        if not counts:
            return []
        names = dict(
            self._session.execute(
                select(
                    IntakeItem.decided_source_name_normalized,
                    func.max(IntakeItem.decided_source_name),
                )
                .where(IntakeItem.decided_source_name_normalized.is_not(None))
                .group_by(IntakeItem.decided_source_name_normalized)
            ).all()
        )
        return [
            (fingerprint, key, names.get(key) or key, last_at, int(count))
            for fingerprint, key, count, last_at in counts
        ]

    def pile_rows(
        self, batch_id: int
    ) -> list[tuple[str | None, int, bool, bool, str | None]]:
        """堆视图要的最小事实：`(指纹, 内容 id, 已排除, 已确认商家, 特征 JSON)`。

        只取**页**内容：表格行没有版式（它的来源写在单元格里，第 3.2 节的第一层
        已经处理）。
        """
        rows = self._session.execute(
            select(
                IntakeItem.layout_fingerprint,
                IntakeItem.id,
                IntakeItem.pile_excluded,
                IntakeItem.decided_source_name_normalized,
                IntakeItem.layout_features,
            )
            .join(IntakeItem.batch_file)
            .where(
                IntakeBatchFile.batch_id == batch_id,
                IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
                IntakeItem.kind == INTAKE_ITEM_KIND_PAGE,
            )
            .order_by(
                IntakeBatchFile.upload_order,
                IntakeItem.page_no,
                IntakeItem.id,
            )
        ).all()
        return [
            (fingerprint, int(item_id), bool(excluded), decided is not None, features)
            for fingerprint, item_id, excluded, decided, features in rows
        ]


__all__ = ["IntakeSourceMemoryRepository"]
