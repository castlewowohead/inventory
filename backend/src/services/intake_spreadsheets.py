"""M04: CSV/XLSX rows in a durable intake batch, without OCR or purchases."""

from __future__ import annotations

import hashlib
import json

from src.db import Database
from src.errors import ConflictError, NotFoundError
from src.integrations.file_storage import LocalFileStorage
from src.models import (INTAKE_FILE_STATUS_DISCARDED, INTAKE_FILE_STATUS_FAILED,
                        INTAKE_FILE_STATUS_PENDING, IntakeBatchFile)
from src.repositories.intake import IntakeBatchFileRepository, IntakeFileRepository
from src.repositories.intake_spreadsheets import IntakeSpreadsheetRepository
from src.repositories.invoices import InvoiceFileRepository
from src.services.intake import IntakeService
from src.services.invoices import UploadedFile
from src.services.spreadsheets import SpreadsheetService
from src.services.validation import require_limit, require_offset


def _row_payload(row) -> dict:
    ref = row.batch_file
    return {
        "id": row.id, "batch_file_id": ref.id, "source_row": row.row_no,
        "sheet": ref.sheet_name or "CSV", "original_name": ref.original_name,
        "version": row.version,
        "purchase_date": row.decided_purchase_date.isoformat() if row.decided_purchase_date else None,
        "source_name": row.decided_source_name,
        "row_data": json.loads(row.row_data) if row.row_data else None,
    }


class IntakeSpreadsheetService:
    def __init__(self, database: Database, storage: LocalFileStorage, *, max_bytes: int) -> None:
        self.database = database
        self.storage = storage
        self.max_bytes = max_bytes
        self.intake = IntakeService(database)

    def _parse(self, upload: UploadedFile, options: object):
        return SpreadsheetService.parse_upload(upload, options, max_bytes=self.max_bytes)

    def _duplicates(self, batch_id: int, digest: str) -> dict:
        self.intake.get_batch(batch_id)
        with self.database.unit_of_work() as session:
            known = IntakeFileRepository(session).find_by_sha256(digest)
            same_batch = (IntakeBatchFileRepository(session).find_in_batch(
                batch_id=batch_id, intake_file_id=known.id) if known else None)
            other_batches = (IntakeFileRepository(session).batch_ids_for(
                known.id, exclude_batch_id=batch_id) if known else [])
            legacy = InvoiceFileRepository(session).find_by_sha256(digest)
            return {
                "shared_with_batch_ids": other_batches,
                "same_bytes_invoice_ids": [legacy.invoice_id] if legacy else [],
                "existing_storage_key": known.storage_key if known else None,
                "existing_batch_file_id": same_batch.id if same_batch else None,
                "existing_batch_file_status": same_batch.status if same_batch else None,
            }

    def preview(self, batch_id: int, upload: UploadedFile, options: object,
                *, limit: int = 50, offset: int = 0) -> dict:
        parsed, normalized_options, header = self._parse(upload, options)
        limit = require_limit(limit, max_limit=200)
        offset = require_offset(offset)
        digest = hashlib.sha256(upload.content).hexdigest()
        duplicates = self._duplicates(batch_id, digest)
        return {
            "filename": upload.original_name, "sheets": list(parsed.table.sheets),
            "import_metadata": SpreadsheetService._metadata(parsed, normalized_options),
            "defaults": {key: str(value) if value is not None else None for key, value in header.items()},
            "total": len(parsed.rows), "limit": limit, "offset": offset,
            "rows": [{"source_row": row.number, "values": row.values,
                      "raw_fields": row.evidence, "review_issues": row.review["issues"]}
                     for row in parsed.rows[offset:offset + limit]],
            "duplicate_warning": {key: value for key, value in duplicates.items()
                                  if key in ("shared_with_batch_ids", "same_bytes_invoice_ids")},
        }

    def upload(self, batch_id: int, upload: UploadedFile, options: object,
               *, confirm_duplicate: bool = False) -> dict:
        parsed, normalized_options, _header = self._parse(upload, options)
        digest = hashlib.sha256(upload.content).hexdigest()
        duplicates = self._duplicates(batch_id, digest)
        if duplicates["existing_batch_file_status"] == "ready":
            raise ConflictError("这个文件已经在本批次里了。", details={
                "sha256": digest, "batch_id": batch_id,
                "batch_file_id": duplicates["existing_batch_file_id"],
            })
        if (duplicates["shared_with_batch_ids"] or duplicates["same_bytes_invoice_ids"]) and not confirm_duplicate:
            raise ConflictError("相同文件曾导入过；确认后才能加入新整理批次。", details={
                "sha256": digest,
                "shared_with_batch_ids": duplicates["shared_with_batch_ids"],
                "same_bytes_invoice_ids": duplicates["same_bytes_invoice_ids"],
            })
        key = duplicates["existing_storage_key"]
        stored_key = None
        if key is None:
            stored = self.storage.save(upload.content, original_name=upload.original_name,
                                       content_type=upload.content_type)
            key = stored.key
            stored_key = key
        elif not self.storage.exists(key):
            raise NotFoundError("已登记的表格原件不存在，无法复用。", details={"sha256": digest})
        retry_id = (duplicates["existing_batch_file_id"]
                    if duplicates["existing_batch_file_status"] in
                    (INTAKE_FILE_STATUS_FAILED, INTAKE_FILE_STATUS_PENDING) else None)
        if retry_id is None:
            try:
                registered = self.intake.register_file(
                    batch_id, storage_key=key, sha256=digest, size=len(upload.content),
                    original_name=upload.original_name, content_type=upload.content_type,
                )
            except Exception:
                if stored_key is not None:
                    self.storage.delete(stored_key)
                raise
            ref_id = registered.ref.id
            if registered.ref.status == "ready":
                return {"batch_file_id": ref_id, "status": "ready",
                        "item_count": registered.ref.item_count, "sheet": registered.ref.sheet_name or "CSV",
                        "sha256": digest, "reused_bytes": True,
                        "shared_with_batch_ids": list(registered.shared_with_batch_ids),
                        "same_bytes_invoice_ids": list(registered.same_bytes_invoice_ids)}
        else:
            ref_id = retry_id
        metadata = SpreadsheetService._metadata(parsed, normalized_options)
        try:
            with self.database.unit_of_work() as session:
                count = IntakeSpreadsheetRepository(session).save_rows(ref_id, parsed, metadata)
        except Exception:
            self.intake.fail_file(ref_id, error_code="table_parse_failed",
                                  error_message="表格行保存失败，请重试处理。")
            raise
        return {
            "batch_file_id": ref_id, "status": "ready", "item_count": count,
            "sheet": parsed.table.sheet, "sha256": digest,
            "reused_bytes": retry_id is not None or registered.reused_bytes,
            "shared_with_batch_ids": duplicates["shared_with_batch_ids"],
            "same_bytes_invoice_ids": duplicates["same_bytes_invoice_ids"],
        }

    def list_rows(self, batch_id: int, *, limit: int = 50, offset: int = 0) -> dict:
        self.intake.get_batch(batch_id)
        limit = require_limit(limit, max_limit=200)
        offset = require_offset(offset)
        with self.database.unit_of_work() as session:
            rows, total = IntakeSpreadsheetRepository(session).page(batch_id, limit=limit, offset=offset)
            return {"items": [_row_payload(row) for row in rows], "total": total,
                    "limit": limit, "offset": offset}

    def get_row(self, batch_id: int, item_id: int) -> dict:
        self.intake.get_batch(batch_id)
        with self.database.unit_of_work() as session:
            return _row_payload(IntakeSpreadsheetRepository(session).get_row(batch_id, item_id))

    def groups(self, batch_id: int) -> list[dict]:
        self.intake.get_batch(batch_id)
        with self.database.unit_of_work() as session:
            return [{"purchase_date": d.isoformat() if d else None,
                     "source_name_normalized": n, "source_name": s, "item_count": count}
                    for d, n, s, count in IntakeSpreadsheetRepository(session).groups(batch_id)]

    def original(self, batch_id: int, batch_file_id: int) -> tuple[str, str]:
        self.intake.get_batch(batch_id)
        with self.database.unit_of_work() as session:
            ref = session.get(IntakeBatchFile, batch_file_id)
            if ref is None or ref.batch_id != batch_id or ref.status == INTAKE_FILE_STATUS_DISCARDED or ref.table_metadata is None:
                raise NotFoundError("批次表格不存在。", details={"batch_file_id": batch_file_id})
            return ref.file.storage_key, ref.original_name

    def get_file(self, batch_id: int, batch_file_id: int) -> dict:
        self.intake.get_batch(batch_id)
        with self.database.unit_of_work() as session:
            ref = session.get(IntakeBatchFile, batch_file_id)
            if ref is None or ref.batch_id != batch_id or ref.status == INTAKE_FILE_STATUS_DISCARDED or ref.table_metadata is None:
                raise NotFoundError("批次表格不存在。", details={"batch_file_id": batch_file_id})
            return {"batch_file_id": ref.id, "original_name": ref.original_name,
                    "status": ref.status, "item_count": ref.item_count,
                    "sha256": ref.file.sha256, "import_metadata": json.loads(ref.table_metadata)}
