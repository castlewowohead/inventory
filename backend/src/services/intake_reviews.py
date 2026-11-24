"""M08: hand classified contents to the existing invoice review workflow."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.models import (
    AMOUNT_SCALE, DESCRIPTION_MAX_LENGTH, INTAKE_FILE_STATUS_READY,
    INVOICE_NUMBER_MAX_LENGTH, LINE_KIND_ADJUSTMENT, LINE_KIND_PURCHASE, LINE_STATUS_PENDING,
    PURCHASE_UNIT_MAX_LENGTH, QUANTITY_SCALE, UNIT_PRICE_SCALE,
    ExtractionJob, IntakeBatch, IntakeBatchFile, IntakeItem, IntakeReviewLink,
    Invoice, InvoiceLine, LINE_STATUS_CONFIRMED, utcnow,
)
from src.repositories.intake_reviews import IntakeReviewRepository
from src.services.ingredients import normalize_name
from src.services.invoices import InvoiceService, _active_purchase
from src.services.intake_moves import IntakeMoveService
from src.services.intake_projection import project_items
from src.services.purchases import PurchaseService, parse_decimal


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
            # 读不出单号就一页一张，与 `handoff_selection` 同一条规则。连号的相邻页
            # 曾经被合成一条建议，但「同一个文件里挨着」不能证明「是同一张发票」：
            # 一份 PDF 本来就是几十张独立发票扫在一起的。
            for _, ident in rows:
                suggestions.append({"item_ids": [ident], "basis": "single_page",
                                    "invoice_number_normalized": None})
        for row in page.items:
            if row.kind == "row" and row.id not in linked:
                suggestions.append({"item_ids": [row.id], "basis": "spreadsheet_row",
                                    "invoice_number_normalized": None})
        return {"suggestions": suggestions, "total_items": page.total,
                "limit": page.limit, "offset": page.offset,
                "note": "这些是次级归属建议；只有明确选择送审才建立单据。"}

    def handoff(self, batch_id: int, *, items: Any,
                target_invoice_id: int | None = None,
                suggested_group: Any = None, session=None) -> dict[str, Any]:
        """One explicit selection creates one draft; a group alone never does.

        An existing draft may receive another page while all of its lines are
        pending. The unique item link, item version update and invoice insert
        commit together. A retry returns the original draft.
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValidationError("items 必须是 1—20 条内容 ID 与版本。")
        if target_invoice_id is not None and (type(target_invoice_id) is not int or target_invoice_id < 1):
            raise ValidationError("target_invoice_id 必须是已有草稿的正整数 ID。")
        accepted_date = None
        accepted_source = None
        if suggested_group is not None:
            if not isinstance(suggested_group, dict) or set(suggested_group) != {"purchase_date", "source_name"}:
                raise ValidationError("suggested_group 必须包含 purchase_date 和 source_name。")
            try:
                accepted_date = date.fromisoformat(suggested_group["purchase_date"])
            except (TypeError, ValueError) as exc:
                raise ValidationError("suggested_group.purchase_date 必须是有效日期。") from exc
            raw_source = suggested_group["source_name"]
            if not isinstance(raw_source, str) or not raw_source.strip() or len(raw_source.strip()) > 120:
                raise ValidationError("suggested_group.source_name 必须是 1—120 字符的商家名。")
            accepted_source = raw_source.strip()
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

        with self.database.unit_of_work(session) as session:
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
                if suggested_group is None and (not item.decided_purchase_date or not item.decided_source_name):
                    raise ConflictError("请先人工确认采购日期与商家，再送入审核。",
                                        details={"item_id": ident})
                rows.append(item)
            if suggested_group is not None:
                batch_items = session.scalars(select(IntakeItem).join(IntakeBatchFile).where(
                    IntakeBatchFile.batch_id == batch_id,
                    IntakeBatchFile.status == INTAKE_FILE_STATUS_READY,
                ).options(selectinload(IntakeItem.candidates))).unique().all()
                projected = {value.item.id: value for value in project_items(batch_items)}
                if any(projected[row.id].purchase_date != accepted_date
                       or projected[row.id].source_key != normalize_name(accepted_source)
                       for row in rows):
                    raise ConflictError("建议分组已变化，请刷新后核对。")
            group_keys = {(accepted_date, normalize_name(accepted_source)) for _ in rows} if suggested_group is not None else {
                (r.decided_purchase_date, r.decided_source_name_normalized) for r in rows}
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
                invoice = Invoice(entry_source=entry_source, invoice_date=accepted_date or rows[0].decided_purchase_date,
                                  source_name=accepted_source or rows[0].decided_source_name,
                                  source_name_normalized=normalize_name(accepted_source) if accepted_source else rows[0].decided_source_name_normalized,
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
                classification = {}
                if suggested_group is not None and (row.decided_purchase_date is None or row.decided_source_name is None):
                    classification = {
                        "decided_purchase_date": accepted_date,
                        "decided_source_name": accepted_source,
                        "decided_source_name_normalized": normalize_name(accepted_source),
                        "decided_at": utcnow(),
                    }
                changed = session.execute(update(IntakeItem).where(
                    IntakeItem.id == row.id, IntakeItem.version == row.version,
                    IntakeItem.handed_off_at.is_(None)).values(
                        handed_off_at=utcnow(), version=row.version + 1,
                        **classification)).rowcount
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

    def handoff_selection(self, batch_id: int, *, items: Any,
                          suggested_group: Any = None) -> dict[str, Any]:
        """Open one review per printed invoice number, in one transaction.

        单据边界只认**纸面单号**（docs/multifiles.md 第 12 节）：

        1. 单号相同的页合成一张草稿——那是一张多页发票。
        2. **读不出单号的页，一页一张**，哪怕它们在同一个文件里连号。

        第 2 条是本节改掉的默认值。原先没有单号时整个选择合成一张，于是模型漏读
        单号（样本里确实发生过）就会把两张发票的金额加在一起，而「各行金额之和
        对不上单据总额」这个检查正好因此失效，看上去反倒很正常。拆错了在填表页
        一眼看得出来——第二张没有总额、行是接着上一张的——点一下「合并到上一张」
        就好；合错了很难发现。宁可多拆。

        表格行（`kind == "row"`）不走这条规则：一张 `entry_source="import"` 的草稿
        是**整份表格的审核容器**，不是一张发票，按行拆开会把一份 CSV 变成几十张
        假发票。
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValidationError("items 必须是 1—20 条内容 ID 与版本。")
        ids = [entry.get("id") for entry in items if isinstance(entry, dict)]
        if (len(ids) != len(items) or any(type(ident) is not int or ident < 1 for ident in ids)
                or len(set(ids)) != len(ids)):
            raise ValidationError("所选内容不能重复，且每项必须包含 id、version。")
        with self.database.unit_of_work() as session:
            rows = []
            for ident in ids:
                if type(ident) is not int or ident < 1:
                    raise ValidationError("内容 id 必须是正整数。")
                row = session.get(IntakeItem, ident)
                if row is None or row.batch_file.batch_id != batch_id:
                    raise NotFoundError("内容不在这个批次中。", details={"item_id": ident})
                if row.batch_file.status != INTAKE_FILE_STATUS_READY or row.batch_file.discarded_at:
                    raise ConflictError("文件尚未准备好或已移出批次。", details={"item_id": ident})
                rows.append(row)
            batch_items = session.scalars(select(IntakeItem).join(IntakeBatchFile).where(
                IntakeBatchFile.batch_id == batch_id,
                IntakeBatchFile.status == INTAKE_FILE_STATUS_READY,
            ).options(selectinload(IntakeItem.candidates))).unique().all()
            projected = {value.item.id: value for value in project_items(batch_items)}
            keys = {(projected[row.id].purchase_date, projected[row.id].source_key) for row in rows}
            if len(keys) != 1 or any(day is None or source is None for day, source in keys):
                raise ConflictError("一次开始填表只能选择同一天、同一商家的内容。")
            if len({row.kind for row in rows}) != 1:
                raise ConflictError("表格行与图片/PDF 页须分别填写。")
            numbers = [_paper_text(json.loads(row.recognition_payload or "{}"), "invoice_number")
                       for row in rows]
            groups: list[list[dict[str, Any]]] = []
            if rows[0].kind == "row":
                groups = [items]
            else:
                keyed: dict[str, list[dict[str, Any]]] = {}
                for entry, number in zip(items, numbers):
                    if number:
                        keyed.setdefault(normalize_name(number), []).append(entry)
                    else:
                        groups.append([entry])
                groups = list(keyed.values()) + groups
            results = [self.handoff(batch_id, items=group,
                                    suggested_group=suggested_group, session=session)
                       for group in groups]
            return {"invoice_ids": [result["invoice_id"] for result in results],
                    "item_ids": ids, "created": any(result["created"] for result in results)}

    def withdraw(self, batch_id: int, *, items: Any) -> dict[str, Any]:
        """撤回填表：丢掉这张草稿上填过的数据，内容回到「已分类、还没填表」。

        三条边界，都是用户在界面上看得见的后果：

        1. **撤回的单位是整张草稿**，不是一页。一张草稿是几页合起来的一张发票，
           只抽走其中一页会留下一张说不清自己是什么的半张单据。所以选中一页就连
           带它同一张草稿里的兄弟页一起回来，返回值把受影响的内容逐个列出来。
        2. **只清填表数据，不清分类**。日期和商家是整理阶段的人工决定，撤回的是
           「开始填表」这一步；要再往回走一步，用整理页原有的「移回待归类」。
        3. **已经入账的一行都不许这样撤**。确认过的行背后有采购记录与价格历史，
           那条路是单据里的撤销确认/作废（`void_invoice`）加采购修订，不能让撤回
           填表顺手把它们删掉。

        草稿本身是**真删**（`session.delete`）而不是作废：它从没产生过采购记录，
        原件字节在 `intake_files` 里一个字都不动，留一张看得见的作废单只会让人以为
        自己多做了一张单据。
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValidationError("items 必须是 1—20 条内容 ID 与版本。")
        selected: list[tuple[int, int]] = []
        for entry in items:
            if not isinstance(entry, dict) or set(entry) != {"id", "version"}:
                raise ValidationError("每项必须且只包含 id、version。")
            ident, version = entry["id"], entry["version"]
            if type(ident) is not int or ident < 1 or type(version) is not int or version < 1:
                raise ValidationError("内容 id、version 必须是正整数。")
            selected.append((ident, version))
        if len({ident for ident, _ in selected}) != len(selected):
            raise ValidationError("同一次撤回不能重复选择内容。")

        with self.database.unit_of_work() as session:
            if session.get(IntakeBatch, batch_id) is None:
                raise NotFoundError("找不到整理批次。", details={"batch_id": batch_id})
            repo = IntakeReviewRepository(session)
            for ident, version in selected:
                item = session.get(IntakeItem, ident)
                if item is None or item.batch_file.batch_id != batch_id:
                    raise NotFoundError("内容不在这个批次中。", details={"item_id": ident})
                if item.version != version:
                    raise ConflictError("内容版本已变化，请刷新。", details={
                        "item_id": ident, "current_version": item.version})
                if item.handed_off_at is None:
                    raise ConflictError("这一项还没有开始填表，不需要撤回。",
                                        details={"item_id": ident})
            links = repo.links_for_items([ident for ident, _ in selected])
            if len(links) != len(selected):
                linked = {link.item_id for link in links}
                raise ConflictError("有内容找不到对应的填表草稿，请刷新后重试。", details={
                    "item_ids": sorted({ident for ident, _ in selected} - linked)})

            invoice_ids = sorted({link.invoice_id for link in links})
            released: list[int] = []
            cleared_lines = 0
            for invoice_id in invoice_ids:
                invoice = session.get(Invoice, invoice_id)
                invoice_links = repo.links_for_invoice(invoice_id)
                if invoice is None or not invoice_links:
                    raise ConflictError("填表草稿已不存在，请刷新。",
                                        details={"invoice_id": invoice_id})
                booked = [line for line in invoice.lines
                          if line.status != LINE_STATUS_PENDING or line.purchases]
                # ⚠️ 已作废的草稿**可以**撤回：在单据库里删掉草稿之后，那几页否则会
                # 永远卡在「编辑中」，既回不去填表也移不动——正是这一节要消灭的死胡同。
                if invoice.voided_at is None:
                    if invoice.completed_at is not None:
                        raise ConflictError(
                            f"单据 #{invoice_id} 已经完成入库；请在单据里撤销确认或删除它，再回整理页。",
                            details={"invoice_id": invoice_id})
                    if booked:
                        raise ConflictError(
                            f"单据 #{invoice_id} 有 {len(booked)} 行已经确认入账；"
                            "请先在单据里撤销这些行的确认，再撤回填表。",
                            details={"invoice_id": invoice_id,
                                     "confirmed_line_nos": [line.line_no for line in booked]})
                if session.scalars(select(ExtractionJob).where(
                        ExtractionJob.invoice_id == invoice_id)).first() is not None:
                    raise ConflictError(
                        f"单据 #{invoice_id} 还有识别任务指着它，请等任务结束后再撤回。",
                        details={"invoice_id": invoice_id})
                # 作废单上的行可能还挂着**已作废的采购记录**，那是价格历史的凭据：
                # 这种单据留着当记录，只把页放回整理页。
                keep_invoice = any(line.purchases for line in invoice.lines)
                if not keep_invoice:
                    cleared_lines += len(invoice.lines)
                for link in invoice_links:
                    item = link.item
                    changed = session.execute(update(IntakeItem).where(
                        IntakeItem.id == item.id, IntakeItem.version == item.version,
                        IntakeItem.handed_off_at.is_not(None)).values(
                            handed_off_at=None, version=item.version + 1)).rowcount
                    if changed != 1:
                        raise ConflictError("内容同时被其他会话修改，请刷新。",
                                            details={"item_id": item.id})
                    released.append(item.id)
                    session.delete(link)
                session.flush()
                if not keep_invoice:
                    session.delete(invoice)
                session.flush()
            return {"item_ids": sorted(released), "invoice_ids": invoice_ids,
                    "cleared_lines": cleared_lines}

    def refill(self, batch_id: int, *, items: Any) -> dict[str, Any]:
        """重新填写：清掉这张单据填过的数据，为同一批页重新开一张空草稿。

        2026-11-24 反馈（第 20 节）。**这一条放宽了第 11 节 ③ 的边界 ③**：那时
        「已经确认入账的一行都不许这样撤」，已完成的单据在整理页上只剩一个「查看
        单据」，想重填只能去单据里逐行撤销确认再回来。用户的决定是：卡片上直接给
        一个「重新填写」，问一句「确认清空数据，重新填写吗？」就该能重来。

        所以**入账的行在这里被撤销**（而不是拦住）：每一条还有效的采购记录按现有的
        软删 + 修改日志作废，理由写明是哪张单据重填的。不撤就会重复记价——同一张纸
        填两遍，价格历史里就会出现两条。

        旧草稿的去留沿用 `withdraw` 的那条规则，没有第二套说法：

        - 行上挂过采购记录（哪怕已作废）→ **整张单据作废留着**。那些记录是价格历史
          的凭据，而采购记录指着行，行指着单据，删掉就只剩悬空的凭据。
        - 一条采购记录都没有 → 草稿**真删**，它从没产生过任何账。

        页本身一个字不动：`decided_*` 的日期和商家是整理阶段的人工决定，重填的是
        「填表」这一步。清完立刻用同一批页建一张新草稿（走的就是 `handoff`，所以
        识别结果照样预填进去），返回新草稿的 id 让界面直接落进去。

        **一次只重填一张单据**：一张草稿就是一张发票（第 12 节 ①），选中的页跨了两张
        草稿时拦住——否则「重新填写」要回答「重填成几张」，而那不是它该回答的问题。
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValidationError("items 必须是 1—20 条内容 ID 与版本。")
        selected: list[tuple[int, int]] = []
        for entry in items:
            if not isinstance(entry, dict) or set(entry) != {"id", "version"}:
                raise ValidationError("每项必须且只包含 id、version。")
            ident, version = entry["id"], entry["version"]
            if type(ident) is not int or ident < 1 or type(version) is not int or version < 1:
                raise ValidationError("内容 id、version 必须是正整数。")
            selected.append((ident, version))
        if len({ident for ident, _ in selected}) != len(selected):
            raise ValidationError("同一次重填不能重复选择内容。")

        with self.database.unit_of_work() as session:
            if session.get(IntakeBatch, batch_id) is None:
                raise NotFoundError("找不到整理批次。", details={"batch_id": batch_id})
            repo = IntakeReviewRepository(session)
            for ident, version in selected:
                item = session.get(IntakeItem, ident)
                if item is None or item.batch_file.batch_id != batch_id:
                    raise NotFoundError("内容不在这个批次中。", details={"item_id": ident})
                if item.version != version:
                    raise ConflictError("内容版本已变化，请刷新。", details={
                        "item_id": ident, "current_version": item.version})
                if item.handed_off_at is None:
                    raise ConflictError("这一项还没有开始填表，不需要重新填写。",
                                        details={"item_id": ident})
            links = repo.links_for_items([ident for ident, _ in selected])
            if len(links) != len(selected):
                linked = {link.item_id for link in links}
                raise ConflictError("有内容找不到对应的填表草稿，请刷新后重试。", details={
                    "item_ids": sorted({ident for ident, _ in selected} - linked)})
            invoice_ids = sorted({link.invoice_id for link in links})
            if len(invoice_ids) != 1:
                raise ConflictError("一次只能重新填写一张单据，请分别重填。",
                                    details={"invoice_ids": invoice_ids})
            invoice_id = invoice_ids[0]
            invoice = session.get(Invoice, invoice_id)
            invoice_links = repo.links_for_invoice(invoice_id)
            if invoice is None or not invoice_links:
                raise ConflictError("填表草稿已不存在，请刷新。",
                                    details={"invoice_id": invoice_id})
            # 补传过的原件挂在这张单据自己身上（`invoice_files.sha256` 全局唯一）。
            # 留着它作废，同一张照片就再也传不进新草稿；跟着删掉又会把用户自己补的
            # 证据顺手清了。两样都不该由「重新填写」顺带决定，所以拦在这里并指出路。
            if invoice.files:
                raise ConflictError(
                    f"单据 #{invoice_id} 补传过 {len(invoice.files)} 份原件；"
                    "请先在单据的「原件文件」里删掉它们，再重新填写。",
                    details={"invoice_id": invoice_id, "file_count": len(invoice.files)})
            if session.scalars(select(ExtractionJob).where(
                    ExtractionJob.invoice_id == invoice_id)).first() is not None:
                raise ConflictError(
                    f"单据 #{invoice_id} 还有识别任务指着它，请等任务结束后再重新填写。",
                    details={"invoice_id": invoice_id})

            purchases = PurchaseService(self.database)
            voided_purchases = 0
            for line in invoice.lines:
                if line.status != LINE_STATUS_CONFIRMED:
                    continue
                active = _active_purchase(line)
                if active is not None:
                    purchases.void_purchase(
                        active.id,
                        reason=f"重新填写单据 #{invoice.id}，撤销第 {line.line_no} 行入账",
                        session=session)
                    voided_purchases += 1
                line.status = LINE_STATUS_PENDING
                line.confirmed_at = None
                line.version += 1
            invoice.completed_at = None
            keep_invoice = any(line.purchases for line in invoice.lines)
            cleared_lines = 0 if keep_invoice else len(invoice.lines)
            if keep_invoice:
                invoice.voided_at = utcnow()

            released: list[int] = []
            for link in invoice_links:
                item = link.item
                changed = session.execute(update(IntakeItem).where(
                    IntakeItem.id == item.id, IntakeItem.version == item.version,
                    IntakeItem.handed_off_at.is_not(None)).values(
                        handed_off_at=None, version=item.version + 1)).rowcount
                if changed != 1:
                    raise ConflictError("内容同时被其他会话修改，请刷新。",
                                        details={"item_id": item.id})
                released.append(item.id)
                session.delete(link)
            session.flush()
            if not keep_invoice:
                session.delete(invoice)
            session.flush()
            # 版本号刚被 UPDATE 改过，ORM 手里那几个对象是旧的；下一步 handoff 要按
            # 当前版本号重新占用它们，所以这里必须让它重新读一次。
            session.expire_all()
            fresh = [session.get(IntakeItem, ident) for ident in sorted(released)]
            created = self.handoff(
                batch_id,
                items=[{"id": row.id, "version": row.version} for row in fresh],
                session=session)
            return {"invoice_id": created["invoice_id"], "previous_invoice_id": invoice_id,
                    "previous_invoice_kept": keep_invoice,
                    "item_ids": sorted(released), "cleared_lines": cleared_lines,
                    "voided_purchases": voided_purchases}

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
