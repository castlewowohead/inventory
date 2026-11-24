"""M06 classification queries and move journal. Sessions belong to the service."""

from __future__ import annotations

from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from src.models import (
    IntakeBatchFile,
    IntakeItem,
    IntakeMove,
    IntakeMoveEntry,
    INTAKE_FILE_STATUS_DISCARDED,
)
from src.repositories.intake import _item_options


class IntakeMoveRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def by_key(self, batch_id: int, request_key: str) -> IntakeMove | None:
        return self.session.scalars(
            select(IntakeMove).options(selectinload(IntakeMove.entries)).where(
                IntakeMove.batch_id == batch_id, IntakeMove.request_key == request_key
            )
        ).one_or_none()

    def latest(self, batch_id: int) -> IntakeMove | None:
        return self.session.scalars(
            select(IntakeMove).options(selectinload(IntakeMove.entries)).where(
                IntakeMove.batch_id == batch_id, IntakeMove.undone_at.is_(None),
                IntakeMove.entries.any(),
            ).order_by(IntakeMove.id.desc()).limit(1)
        ).one_or_none()

    def forget_items(self, item_ids: list[int]) -> int:
        """把撤销日志里指向这些内容的条目删掉，空掉的那几次移动一并删。

        移除文件时用（第 16 节）：内容都没了，留着一条**什么都恢复不了**的「撤销
        最近一次移动」是个陷阱——点下去不报错，也什么都不会变。只影响这些内容的
        条目，同一次移动里别的内容照常可撤。
        """
        if not item_ids:
            return 0
        touched = set(self.session.scalars(
            select(IntakeMoveEntry.move_id).where(IntakeMoveEntry.item_id.in_(item_ids))
        ).all())
        removed = self.session.execute(
            delete(IntakeMoveEntry).where(IntakeMoveEntry.item_id.in_(item_ids))
        ).rowcount or 0
        self.session.flush()
        for move_id in touched:
            left = self.session.scalar(
                select(func.count()).select_from(IntakeMoveEntry)
                .where(IntakeMoveEntry.move_id == move_id)
            ) or 0
            if left == 0:
                self.session.execute(delete(IntakeMove).where(IntakeMove.id == move_id))
        self.session.flush()
        return int(removed)

    def get(self, move_id: int) -> IntakeMove | None:
        return self.session.scalars(
            select(IntakeMove).options(selectinload(IntakeMove.entries)).where(IntakeMove.id == move_id)
        ).one_or_none()

    def list_groups(self, batch_id: int, *, limit: int, offset: int):
        grouped = (
            select(
                IntakeItem.decided_purchase_date.label("purchase_date"),
                IntakeItem.decided_source_name_normalized.label("source_key"),
                func.max(IntakeItem.decided_source_name).label("source_name"),
                func.count(IntakeItem.id).label("item_count"),
            )
            .join(IntakeBatchFile, IntakeBatchFile.id == IntakeItem.batch_file_id)
            .where(IntakeBatchFile.batch_id == batch_id, IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED)
            .group_by(IntakeItem.decided_purchase_date, IntakeItem.decided_source_name_normalized)
            .subquery()
        )
        total = self.session.scalar(select(func.count()).select_from(grouped)) or 0
        rows = self.session.execute(
            select(grouped).order_by(
                grouped.c.purchase_date.is_(None), grouped.c.purchase_date,
                grouped.c.source_key.is_(None), grouped.c.source_key,
            ).limit(limit).offset(offset)
        ).all()
        return rows, int(total)

    def list_items(self, batch_id: int, *, purchase_date: date | None,
                   source_key: str | None, limit: int, offset: int):
        conditions = [
            IntakeBatchFile.batch_id == batch_id,
            IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
            IntakeItem.decided_purchase_date == purchase_date if purchase_date is not None
            else IntakeItem.decided_purchase_date.is_(None),
            IntakeItem.decided_source_name_normalized == source_key if source_key is not None
            else IntakeItem.decided_source_name_normalized.is_(None),
        ]
        base = select(IntakeItem).join(IntakeBatchFile, IntakeBatchFile.id == IntakeItem.batch_file_id).where(*conditions)
        total = self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = self.session.scalars(
            base.options(*_item_options()).order_by(
                IntakeBatchFile.upload_order, IntakeItem.page_no, IntakeItem.row_no, IntakeItem.id
            ).limit(limit).offset(offset)
        ).unique().all()
        return rows, int(total)
