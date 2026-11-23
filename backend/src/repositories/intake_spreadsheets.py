"""Database access for M04 spreadsheet intake rows."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.errors import ConflictError, NotFoundError
from src.models import (
    INTAKE_FILE_STATUS_FAILED, INTAKE_FILE_STATUS_PENDING, INTAKE_FILE_STATUS_READY,
    INTAKE_ITEM_KIND_ROW, IntakeBatchFile, IntakeItem,
)
from src.services.spreadsheet_parsing import ParsedTable


class IntakeSpreadsheetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_rows(self, batch_file_id: int, parsed: ParsedTable, metadata: dict) -> int:
        ref = self.session.get(IntakeBatchFile, batch_file_id)
        if ref is None:
            raise NotFoundError("批次文件不存在。", details={"batch_file_id": batch_file_id})
        if ref.batch.closed_at or ref.batch.discarded_at:
            raise ConflictError("批次已经关闭或作废，不能再导入表格。")
        if ref.status not in (INTAKE_FILE_STATUS_PENDING, INTAKE_FILE_STATUS_FAILED):
            raise ConflictError("文件已处理，不能重复生成表格行。", details={"batch_file_id": batch_file_id})
        if ref.items:
            raise ConflictError("文件已有可归类内容，不能重复生成表格行。")
        ref.sheet_name = parsed.table.sheet if parsed.table.sheet != "CSV" else None
        ref.table_metadata = json.dumps(metadata, ensure_ascii=False)
        for row in parsed.rows:
            self.session.add(IntakeItem(
                batch_file_id=ref.id, kind=INTAKE_ITEM_KIND_ROW, row_no=row.number,
                row_data=json.dumps({"values": row.values, "raw_fields": row.evidence,
                                     "review_issues": row.review["issues"],
                                     "overrides": row.review["overrides"]}, ensure_ascii=False),
            ))
        ref.item_count = len(parsed.rows)
        ref.status = INTAKE_FILE_STATUS_READY
        ref.error_code = None
        ref.error_message = None
        self.session.flush()
        return len(parsed.rows)

    def page(self, batch_id: int, *, limit: int, offset: int) -> tuple[list[IntakeItem], int]:
        conditions = (
            IntakeBatchFile.batch_id == batch_id,
            IntakeBatchFile.status != "discarded",
            IntakeItem.kind == INTAKE_ITEM_KIND_ROW,
        )
        total = self.session.scalar(select(func.count()).select_from(IntakeItem)
                                    .join(IntakeBatchFile).where(*conditions)) or 0
        rows = list(self.session.scalars(select(IntakeItem).join(IntakeBatchFile)
            .where(*conditions).order_by(IntakeBatchFile.upload_order, IntakeItem.row_no, IntakeItem.id)
            .limit(limit).offset(offset)))
        return rows, int(total)

    def get_row(self, batch_id: int, item_id: int) -> IntakeItem:
        row = self.session.scalar(select(IntakeItem).join(IntakeBatchFile).where(
            IntakeBatchFile.batch_id == batch_id, IntakeItem.id == item_id,
            IntakeItem.kind == INTAKE_ITEM_KIND_ROW,
            IntakeBatchFile.status != "discarded",
        ))
        if row is None:
            raise NotFoundError("表格行不存在。", details={"item_id": item_id})
        return row

    def groups(self, batch_id: int) -> list[tuple[date | None, str | None, str | None, int]]:
        rows = self.session.execute(select(
            IntakeItem.decided_purchase_date,
            IntakeItem.decided_source_name_normalized,
            func.max(IntakeItem.decided_source_name), func.count(),
        ).join(IntakeBatchFile).where(
            IntakeBatchFile.batch_id == batch_id,
            IntakeBatchFile.status != "discarded",
            IntakeItem.kind == INTAKE_ITEM_KIND_ROW,
        ).group_by(IntakeItem.decided_purchase_date, IntakeItem.decided_source_name_normalized)
         .order_by(IntakeItem.decided_purchase_date.is_(None), IntakeItem.decided_purchase_date,
                   IntakeItem.decided_source_name_normalized.is_(None),
                   IntakeItem.decided_source_name_normalized))
        return [(d, n, s, int(count)) for d, n, s, count in rows]
