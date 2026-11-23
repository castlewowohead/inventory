"""M08: hand classified contents to the existing invoice review workflow."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select, update

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.models import (
    AMOUNT_SCALE, DESCRIPTION_MAX_LENGTH, INTAKE_FILE_STATUS_READY,
    INVOICE_NUMBER_MAX_LENGTH, LINE_KIND_ADJUSTMENT, LINE_KIND_PURCHASE, LINE_STATUS_PENDING,
    PURCHASE_UNIT_MAX_LENGTH, QUANTITY_SCALE, UNIT_PRICE_SCALE,
    IntakeBatch, IntakeItem, IntakeReviewLink, Invoice, InvoiceLine, utcnow,
)
from src.repositories.intake_reviews import IntakeReviewRepository
from src.services.ingredients import normalize_name
from src.services.invoices import InvoiceService
from src.services.intake_moves import IntakeMoveService
from src.services.purchases import parse_decimal


def _decimal(value: Any, *, field: str, scale: int, allow_negative: bool = False):
    if value is None or value == "":
        return None
    try:
        return parse_decimal(value, field=field, scale=scale,
                             allow_zero=True, allow_negative=allow_negative)
    except ValidationError:
        return None  # The original value and issue remain in raw_fields.


def _paper_text(payload: dict, name: str) -> str | None:
    field = (payload.get("fields") or {}).get(name) or {}
    raw = field.get("raw_text")
    return raw.strip() if field.get("status") == "recognized" and isinstance(raw, str) and raw.strip() else None


class IntakeReviewService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def suggestions(self, batch_id: int, *, purchase_date: str | None,
                    source_name: str | None, limit: int = 200,
                    offset: int = 0) -> dict[str, Any]:
        """Display-only invoice membership hints within one confirmed group."""
        if purchase_date is None or source_name is None:
            raise ValidationError("请指定已确认组的 purchase_date 和 source_name。")
        page = IntakeMoveService(self.database).group_items(
            batch_id, purchase_date=purchase_date, source_name=source_name,
            limit=limit, offset=offset)
        with self.database.unit_of_work() as session:
            linked = {link.item_id for link in IntakeReviewRepository(session).links_for_items(
                [row.id for row in page.items])}
        open_items = [row for row in page.items if row.kind == "page"
                      and row.decided_purchase_date and row.decided_source_name
                      and row.id not in linked]
        numbered: dict[str, list[int]] = {}
        without_number: dict[int, list[tuple[int, int]]] = {}
        suggestions: list[dict[str, Any]] = []
        for row in open_items:
            raw = _paper_text(row.recognition_payload or {}, "invoice_number")
            if raw:
                numbered.setdefault(normalize_name(raw), []).append(row.id)
            else:
                without_number.setdefault(row.batch_file_id, []).append((row.page_no or 1, row.id))
        for key, ids in numbered.items():
            suggestions.append({"item_ids": ids, "basis": "same_printed_invoice_number",
                                "invoice_number_normalized": key})
        for rows in without_number.values():
            rows.sort()
            run: list[tuple[int, int]] = []
            for number, ident in rows:
                if run and number != run[-1][0] + 1:
                    suggestions.append({"item_ids": [item_id for _, item_id in run],
                                        "basis": "same_file_consecutive_pages" if len(run) > 1 else "single_page",
                                        "invoice_number_normalized": None})
                    run = []
                run.append((number, ident))
            if run:
                suggestions.append({"item_ids": [item_id for _, item_id in run],
                                    "basis": "same_file_consecutive_pages" if len(run) > 1 else "single_page",
                                    "invoice_number_normalized": None})
        for row in page.items:
            if row.kind == "row" and row.id not in linked:
                suggestions.append({"item_ids": [row.id], "basis": "spreadsheet_row",
                                    "invoice_number_normalized": None})
        return {"suggestions": suggestions, "total_items": page.total,
                "limit": page.limit, "offset": page.offset,
                "note": "这些是次级归属建议；只有明确选择送审才建立单据。"}

    def handoff(self, batch_id: int, *, items: Any,
                target_invoice_id: int | None = None) -> dict[str, Any]:
        """One explicit selection creates one draft; a group alone never does.

        An existing draft may receive another page while all of its lines are
        pending. The unique item link, item version update and invoice insert
        commit together. A retry returns the original draft.
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValidationError("items 必须是 1—20 条内容 ID 与版本。")
        if target_invoice_id is not None and (type(target_invoice_id) is not int or target_invoice_id < 1):
            raise ValidationError("target_invoice_id 必须是已有草稿的正整数 ID。")
        selected: list[tuple[int, int]] = []
        for entry in items:
            if not isinstance(entry, dict) or set(entry) != {"id", "version"}:
                raise ValidationError("每项必须且只包含 id、version。")
            ident, version = entry["id"], entry["version"]
            if type(ident) is not int or ident < 1 or type(version) is not int or version < 1:
                raise ValidationError("内容 id、version 必须是正整数。")
            selected.append((ident, version))
        ids = [ident for ident, _ in selected]
        if len(set(ids)) != len(ids):
            raise ValidationError("同一次送审不能重复选择内容。")

        with self.database.unit_of_work() as session:
            batch = session.get(IntakeBatch, batch_id)
            if batch is None:
                raise NotFoundError("找不到整理批次。", details={"batch_id": batch_id})
            repo = IntakeReviewRepository(session)
            prior = repo.links_for_items(ids)
            if prior:
                invoice_ids = {link.invoice_id for link in prior}
                if len(prior) == len(ids) and len(invoice_ids) == 1 and (
                    target_invoice_id is None or target_invoice_id in invoice_ids
                ):
                    return {"invoice_id": next(iter(invoice_ids)), "item_ids": ids,
                            "created": False}
                raise ConflictError("所选内容已有不同的审核归属，请刷新后分开处理。",
                                    details={"linked_item_ids": [link.item_id for link in prior]})
            if batch.closed_at or batch.discarded_at:
                raise ConflictError("批次已关闭或作废，不能送审。")

            rows: list[IntakeItem] = []
            for ident, version in selected:
                item = session.get(IntakeItem, ident)
                if item is None or item.batch_file.batch_id != batch_id:
                    raise NotFoundError("内容不在这个批次中。", details={"item_id": ident})
                if item.batch_file.status != INTAKE_FILE_STATUS_READY or item.batch_file.discarded_at:
                    raise ConflictError("文件尚未准备好或已移出批次。", details={"item_id": ident})
                if item.version != version or item.handed_off_at is not None:
                    raise ConflictError("内容版本或送审状态已变化，请刷新。", details={
                        "item_id": ident, "current_version": item.version})
                if not item.decided_purchase_date or not item.decided_source_name:
                    raise ConflictError("请先人工确认采购日期与商家，再送入审核。",
                                        details={"item_id": ident})
                rows.append(item)
            group_keys = {(r.decided_purchase_date, r.decided_source_name_normalized) for r in rows}
            if len(group_keys) != 1:
                raise ConflictError("一次送审只能选择同日期、同商家的内容。")
            if len({r.kind for r in rows}) != 1:
                raise ConflictError("表格行与图片/PDF 页须分别建立审核草稿。")
            entry_source = "import" if rows[0].kind == "row" else "invoice"

            payloads = [json.loads(row.recognition_payload) if row.recognition_payload else {} for row in rows]
            numbers = {_paper_text(p, "invoice_number") for p in payloads} - {None}
            if len(numbers) > 1:
                raise ConflictError("所选页出现不同单号，请分别送审或在审核中合并。",
                                    details={"invoice_numbers": sorted(numbers)})
            totals = {_paper_text(p, "invoice_total") for p in payloads} - {None}
            if len(totals) > 1:
                raise ConflictError("所选页出现不同单据总额，请分别送审。")
            number = next(iter(numbers), None)
            if number:
                number = number[:INVOICE_NUMBER_MAX_LENGTH]
            total = _decimal(next(iter(totals), None), field="invoice_total", scale=AMOUNT_SCALE)

            if target_invoice_id is None:
                providers = {p.get("provider") for p in payloads if p.get("provider")}
                invoice = Invoice(entry_source=entry_source, invoice_date=rows[0].decided_purchase_date,
                                  source_name=rows[0].decided_source_name,
                                  source_name_normalized=rows[0].decided_source_name_normalized,
                                  invoice_number=number,
                                  invoice_number_normalized=normalize_name(number) if number else None,
                                  invoice_total=total, currency="USD",
                                  header_reviewed_fields='["invoice_date", "source_name"]',
                                  recognition_provider=next(iter(providers)) if len(providers) == 1 else (
                                      "intake-mixed" if providers else None),
                                  recognition_is_fake=any(bool(p.get("is_fake")) for p in payloads),
                                  recognized_at=max((r.recognized_at for r in rows if r.recognized_at), default=None),
                                  raw_document_fields=json.dumps({"fields": payloads[0].get("fields", {}),
                                      "intake_items": [
                                      {"item_id": row.id, "fields": payload.get("fields", {}),
                                       "purpose": payload.get("purpose"), "rounds": payload.get("rounds")}
                                      for row, payload in zip(rows, payloads)
                                  ]}, ensure_ascii=False))
                session.add(invoice)
                session.flush()
            else:
                invoice = session.get(Invoice, target_invoice_id)
                if invoice is None:
                    raise NotFoundError("目标审核草稿不存在。", details={"invoice_id": target_invoice_id})
                existing = repo.links_for_invoice(target_invoice_id)
                if not existing or any(link.item.batch_file.batch_id != batch_id for link in existing):
                    raise ConflictError("目标草稿不属于这个整理批次。")
                if invoice.entry_source != entry_source:
                    raise ConflictError("表格行与图片/PDF 页须分别审核。")
                if invoice.completed_at or invoice.voided_at or any(line.status != LINE_STATUS_PENDING for line in invoice.lines):
                    raise ConflictError("目标草稿已处理，不能再合并页。")
                if (invoice.invoice_date, invoice.source_name_normalized) != next(iter(group_keys)):
                    raise ConflictError("目标草稿日期或商家不同，请先在审核中更正。")
                if invoice.invoice_number and number and normalize_name(invoice.invoice_number) != normalize_name(number):
                    raise ConflictError("单号不同，请先核对是否为同一张单据。")
                if invoice.invoice_total and total and invoice.invoice_total != total:
                    raise ConflictError("单据总额不同，请先核对是否为同一张单据。")
                if not invoice.invoice_number and number:
                    invoice.invoice_number = number
                    invoice.invoice_number_normalized = normalize_name(number)
                if invoice.invoice_total is None:
                    invoice.invoice_total = total

            next_no = max((line.line_no for line in invoice.lines), default=0)
            for row, payload in zip(rows, payloads):
                changed = session.execute(update(IntakeItem).where(
                    IntakeItem.id == row.id, IntakeItem.version == row.version,
                    IntakeItem.handed_off_at.is_(None)).values(
                        handed_off_at=utcnow(), version=row.version + 1)).rowcount
                if changed != 1:
                    raise ConflictError("内容同时被其他会话修改，请刷新。", details={"item_id": row.id})
                repo.add(IntakeReviewLink(item_id=row.id, invoice_id=invoice.id))
                if row.kind == "row":
                    data = json.loads(row.row_data or "{}")
                    values = data.get("values") or {}
                    source_lines = [{"fields": data.get("raw_fields") or {},
                                     "raw_text": None, "index": row.row_no}]
                else:
                    data = {}
                    values = {}
                    source_lines = payload.get("lines") or []
                for raw_line in source_lines:
                    next_no += 1
                    evidence = {**((data.get("raw_fields") or {}) if row.kind == "row" else {}),
                                "fields": raw_line.get("fields") or {},
                                "intake_batch_id": batch_id, "intake_item_id": row.id,
                                "original_name": row.batch_file.original_name,
                                "page": row.page_no, "row": row.row_no,
                                "recognition_purpose": payload.get("purpose"),
                                "recognition_rounds": payload.get("rounds")}
                    # The grouping pass did not perform the handwritten structure and
                    # ink checks. Keep page OCR values as evidence, not confirmed inputs.
                    line = InvoiceLine(invoice_id=invoice.id, line_no=next_no,
                                       status=LINE_STATUS_PENDING,
                                       line_kind=(LINE_KIND_ADJUSTMENT if row.kind == "row" and
                                                  values.get("line_kind") == LINE_KIND_ADJUSTMENT
                                                  else LINE_KIND_PURCHASE),
                                       raw_fields=json.dumps(evidence, ensure_ascii=False),
                                       page=row.page_no,
                                       review_metadata=json.dumps({"issues": data.get("review_issues", []),
                                                                   "overrides": []}, ensure_ascii=False))
                    if row.kind == "row":
                        line.description = (values.get("description") or values.get("original_description") or None)
                        if line.description:
                            line.description = line.description[:DESCRIPTION_MAX_LENGTH]
                        line.quantity = _decimal(values.get("quantity"), field="quantity", scale=QUANTITY_SCALE,
                                                 allow_negative=line.line_kind == LINE_KIND_ADJUSTMENT)
                        line.purchase_unit = (values.get("purchase_unit") or None)
                        if line.purchase_unit:
                            line.purchase_unit = line.purchase_unit[:PURCHASE_UNIT_MAX_LENGTH]
                        line.unit_price = _decimal(values.get("unit_price"), field="unit_price", scale=UNIT_PRICE_SCALE)
                        line.line_amount = _decimal(values.get("line_amount"), field="line_amount", scale=AMOUNT_SCALE,
                                                    allow_negative=line.line_kind == LINE_KIND_ADJUSTMENT)
                        line.currency = values.get("currency") or None
                    session.add(line)
            session.flush()
            return {"invoice_id": invoice.id, "item_ids": ids,
                    "created": target_invoice_id is None}

    def revise_classification(self, batch_id: int, invoice_id: int, *,
                              items: Any, purchase_date: Any,
                              source_name: Any, scope: Any) -> dict[str, Any]:
        """Change the full review, its linked cards and inherited booked purchases.

        The caller must name every linked item and its current version, so the
        affected scope is explicit and stale tabs cannot silently rewrite a group.
        """
        if scope != "invoice":
            raise ValidationError("scope 必须明确指定为 invoice（整张草稿）。")
        if not isinstance(purchase_date, str):
            raise ValidationError("purchase_date 必须是 YYYY-MM-DD。")
        try:
            parsed_date = date.fromisoformat(purchase_date)
        except ValueError as exc:
            raise ValidationError("purchase_date 必须是有效的 YYYY-MM-DD 日期。") from exc
        if not isinstance(source_name, str) or not source_name.strip() or len(source_name.strip()) > 120:
            raise ValidationError("source_name 必须是 1—120 字符的商家名。")
        source_name = source_name.strip()
        if not isinstance(items, list) or not items:
            raise ValidationError("items 必须列出这张草稿的全部内容及版本。")
        supplied = {}
        for row in items:
            if (not isinstance(row, dict) or set(row) != {"id", "version"}
                    or type(row["id"]) is not int or type(row["version"]) is not int
                    or row["id"] in supplied):
                raise ValidationError("items 中的 id/version 必须是不重复的整数。")
            supplied[row["id"]] = row["version"]
        with self.database.unit_of_work() as session:
            repo = IntakeReviewRepository(session)
            links = repo.links_for_invoice(invoice_id)
            invoice = session.get(Invoice, invoice_id)
            if invoice is None or not links or any(
                link.item.batch_file.batch_id != batch_id for link in links
            ):
                raise NotFoundError("这张审核草稿不属于该批次。")
            if invoice.voided_at is not None:
                raise ConflictError("已作废单据不能修改分类。")
            actual = {link.item_id for link in links}
            if actual != set(supplied):
                raise ConflictError("影响范围已变化，请刷新后列出全部内容。",
                                    details={"item_ids": sorted(actual)})
            for link in links:
                item = link.item
                if item.version != supplied[item.id]:
                    raise ConflictError("内容版本已变化，请刷新后重试。", details={
                        "item_id": item.id, "current_version": item.version})
            old_header = {"purchase_date": invoice.invoice_date,
                          "source_name": invoice.source_name,
                          "currency": invoice.currency or "USD"}
            for link in links:
                item = link.item
                changed = session.execute(update(IntakeItem).where(
                    IntakeItem.id == item.id, IntakeItem.version == item.version).values(
                        decided_purchase_date=parsed_date,
                        decided_source_name=source_name,
                        decided_source_name_normalized=normalize_name(source_name),
                        decided_at=utcnow(), version=item.version + 1)).rowcount
                if changed != 1:
                    raise ConflictError("内容同时被其他会话修改，请刷新。", details={"item_id": item.id})
            invoice.invoice_date = parsed_date
            invoice.source_name = source_name
            invoice.source_name_normalized = normalize_name(source_name)
            InvoiceService._sync_confirmed_header_fields(session, invoice, old_header)
            session.flush()
            return {"invoice_id": invoice_id, "scope": "invoice",
                    "item_ids": sorted(actual), "purchase_date": purchase_date,
                    "source_name": source_name}

    def reassign(self, batch_id: int, invoice_id: int, *, items: Any,
                 target_invoice_id: int | None = None) -> dict[str, Any]:
        """Move selected pending pages/rows to a new or existing draft.

        This is the review-time merge/split action. Confirmed purchases cannot
        be moved behind the reviewer's back; they require the normal revision
        and unconfirm workflow.
        """
        if not isinstance(items, list) or not items:
            raise ValidationError("items 必须列出要移动的内容及版本。")
        selected = {}
        for row in items:
            if (not isinstance(row, dict) or set(row) != {"id", "version"}
                    or type(row["id"]) is not int or type(row["version"]) is not int
                    or row["id"] in selected):
                raise ValidationError("items 中的 id/version 必须是不重复的整数。")
            selected[row["id"]] = row["version"]
        if target_invoice_id is not None and (type(target_invoice_id) is not int or target_invoice_id < 1):
            raise ValidationError("target_invoice_id 必须是正整数。")
        with self.database.unit_of_work() as session:
            repo = IntakeReviewRepository(session)
            links = repo.links_for_invoice(invoice_id)
            source = session.get(Invoice, invoice_id)
            if not links or source is None or any(
                link.item.batch_file.batch_id != batch_id for link in links
            ):
                raise NotFoundError("审核草稿不属于该批次。")
            by_id = {link.item_id: link for link in links}
            if not set(selected) <= set(by_id):
                raise ConflictError("所选内容不在这张草稿中。")
            if source.completed_at or source.voided_at or any(
                line.status != LINE_STATUS_PENDING for line in source.lines
            ):
                raise ConflictError("草稿已有确认或已完成，请先走审核撤销与修订流程。")
            for ident, version in selected.items():
                if by_id[ident].item.version != version:
                    raise ConflictError("内容版本已变化，请刷新。", details={"item_id": ident})
            if target_invoice_id is None:
                if set(selected) == set(by_id):
                    raise ValidationError("拆分须保留至少一个内容在原草稿。")
                target = Invoice(entry_source=source.entry_source, invoice_date=source.invoice_date,
                                 source_name=source.source_name,
                                 source_name_normalized=source.source_name_normalized,
                                 currency=source.currency,
                                 header_reviewed_fields=source.header_reviewed_fields)
                session.add(target)
                session.flush()
            else:
                if target_invoice_id == invoice_id:
                    raise ValidationError("目标不能是原草稿。")
                target = session.get(Invoice, target_invoice_id)
                target_links = repo.links_for_invoice(target_invoice_id)
                if target is None or not target_links or any(
                    link.item.batch_file.batch_id != batch_id for link in target_links
                ):
                    raise ConflictError("目标草稿不属于该批次。")
                if target.entry_source != source.entry_source:
                    raise ConflictError("表格草稿与图片/PDF 草稿不能合并。")
                if target.completed_at or target.voided_at or any(
                    line.status != LINE_STATUS_PENDING for line in target.lines
                ):
                    raise ConflictError("目标草稿已处理，不能合并。")
                if (target.invoice_date, target.source_name_normalized) != (
                    source.invoice_date, source.source_name_normalized
                ):
                    raise ConflictError("两个草稿的日期或商家不同，请先明确修正分类。")
                if (target.invoice_number and source.invoice_number and
                    normalize_name(target.invoice_number) != normalize_name(source.invoice_number)):
                    raise ConflictError("单号不同，请核对后再合并。")
            moving_lines = [line for line in source.lines if line.raw_fields and
                            (json.loads(line.raw_fields).get("intake_item_id") in selected)]
            next_no = max((line.line_no for line in target.lines), default=0)
            for line in moving_lines:
                next_no += 1
                line.invoice_id = target.id
                line.line_no = next_no
            for ident in selected:
                link = by_id[ident]
                item = link.item
                changed = session.execute(update(IntakeItem).where(
                    IntakeItem.id == ident, IntakeItem.version == selected[ident]).values(
                        version=selected[ident] + 1)).rowcount
                if changed != 1:
                    raise ConflictError("内容同时被其他会话修改，请刷新。", details={"item_id": ident})
                link.invoice_id = target.id
            # A split makes both totals uncertain. Reviewers must verify each
            # invoice's own printed total before closeout; never divide a sum.
            source.invoice_total = None
            target.invoice_total = None
            if set(selected) == set(by_id):
                if any(line not in moving_lines for line in source.lines):
                    raise ConflictError("原草稿有手工行，请先处理这些行再整体合并。")
                source.voided_at = utcnow()
            session.flush()
            return {"source_invoice_id": invoice_id, "target_invoice_id": target.id,
                    "item_ids": sorted(selected), "created": target_invoice_id is None}
