"""M06 classification queries and move journal. Sessions belong to the service."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.models import IntakeBatchFile, IntakeItem, IntakeMove, INTAKE_FILE_STATUS_DISCARDED
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
