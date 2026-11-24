"""M06: atomic manual grouping, paged board reads and last-move undo.

M07 新增一个 action：`source` 只写来源、保留日期。相似堆里的几张卡片往往各属不同
日期，而「一次给整堆命名」是 M07 的验收条件；用 `group` 会逼着替它们猜一个共同
日期。写法与 `date`（只写日期、保留来源）完全对称，走的仍是这一条命令、这一个
事务、同一套版本检查与撤销记录——整理页上的三种操作（拖拽、多选批量、点选目标）
执行同一条后端命令，不另开一条写路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from sqlalchemy import select, update

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.models import (
    INTAKE_FILE_STATUS_DISCARDED, SOURCE_NAME_MAX_LENGTH, IntakeBatchFile, IntakeDateGroup, IntakeItem,
    IntakeMove, IntakeMoveEntry, utcnow,
)
from src.repositories.intake import IntakeBatchRepository, IntakeItemRepository, _item_options
from src.repositories.intake_moves import IntakeMoveRepository
from src.services.ingredients import normalize_name
from src.services.intake import IntakeGroupView, IntakeItemPage, IntakeItemView, MAX_ITEMS_PER_PAGE
from src.services.intake_projection import ProjectedItem, project_items
from src.services.validation import require_limit, require_offset, require_text


@dataclass(frozen=True)
class MoveResult:
    id: int
    request_key: str
    undone: bool
    items: tuple[IntakeItemView, ...]


@dataclass(frozen=True)
class GroupPage:
    groups: tuple[IntakeGroupView, ...]
    total: int
    limit: int
    offset: int


class IntakeMoveService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _active_batch(session, batch_id: int) -> None:
        batch = IntakeBatchRepository(session).get(batch_id)
        if batch is None:
            raise NotFoundError("找不到整理批次。", details={"batch_id": batch_id})
        if batch.closed_at is not None or batch.discarded_at is not None:
            raise ConflictError("批次已收口或作废，不能再移动内容。", details={"batch_id": batch_id})

    @staticmethod
    def _existing_batch(session, batch_id: int) -> None:
        if IntakeBatchRepository(session).get(batch_id) is None:
            raise NotFoundError("找不到整理批次。", details={"batch_id": batch_id})

    @staticmethod
    def _pin_dates(session, batch_id: int, dates: set[date]) -> None:
        if not dates:
            return
        existing = set(session.scalars(select(IntakeDateGroup.purchase_date).where(
            IntakeDateGroup.batch_id == batch_id,
            IntakeDateGroup.purchase_date.in_(dates))).all())
        session.add_all(IntakeDateGroup(batch_id=batch_id, purchase_date=value)
                        for value in dates - existing)

    def create_date_group(self, batch_id: int, purchase_date: str) -> None:
        value = self._date(purchase_date)
        with self.database.unit_of_work() as session:
            self._active_batch(session, batch_id)
            self._pin_dates(session, batch_id, {value})

    def delete_date_group(self, batch_id: int, purchase_date: str) -> None:
        value = self._date(purchase_date)
        with self.database.unit_of_work() as session:
            self._active_batch(session, batch_id)
            group = session.scalar(select(IntakeDateGroup).where(
                IntakeDateGroup.batch_id == batch_id, IntakeDateGroup.purchase_date == value))
            if group is None:
                raise NotFoundError("找不到这个日期类别。")
            if any(row.purchase_date == value for row in self._projected(session, batch_id)):
                raise ConflictError("请先把这个日期里的内容移走，再删除类别。")
            session.delete(group)

    def groups(self, batch_id: int, *, limit: int = 50, offset: int = 0) -> GroupPage:
        limit = require_limit(limit, max_limit=MAX_ITEMS_PER_PAGE)
        offset = require_offset(offset)
        with self.database.unit_of_work() as session:
            self._existing_batch(session, batch_id)
            repo = IntakeMoveRepository(session)
            _, total = repo.list_groups(batch_id, limit=1, offset=0)
            rows, _ = repo.list_groups(batch_id, limit=max(total, 1), offset=0)
            grouped = [IntakeGroupView(purchase_date=row.purchase_date, source_name=row.source_name,
                source_name_normalized=row.source_key, item_count=row.item_count) for row in rows]
            dates = {row.purchase_date for row in grouped}
            for value in session.scalars(select(IntakeDateGroup.purchase_date).where(
                    IntakeDateGroup.batch_id == batch_id)).all():
                if value not in dates:
                    grouped.append(IntakeGroupView(purchase_date=value, source_name=None,
                        source_name_normalized=None, item_count=0))
            grouped.sort(key=lambda group: (
                group.purchase_date is None, group.purchase_date or date.min,
                group.source_name_normalized is None, group.source_name_normalized or ""))
            return GroupPage(
                tuple(grouped[offset:offset + limit]), len(grouped), limit, offset,
            )

    def group_items(self, batch_id: int, *, purchase_date: str | None, source_name: str | None,
                    limit: int = 50, offset: int = 0) -> IntakeItemPage:
        limit = require_limit(limit, max_limit=MAX_ITEMS_PER_PAGE)
        offset = require_offset(offset)
        parsed_date = self._date(purchase_date) if purchase_date is not None else None
        source_key = normalize_name(require_text(source_name, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH)) if source_name is not None else None
        with self.database.unit_of_work() as session:
            self._existing_batch(session, batch_id)
            rows, total = IntakeMoveRepository(session).list_items(
                batch_id, purchase_date=parsed_date, source_key=source_key, limit=limit, offset=offset
            )
            return IntakeItemPage(tuple(IntakeItemView.of(item) for item in rows), total, limit, offset)

    @staticmethod
    def _projected(session, batch_id: int) -> list[ProjectedItem]:
        rows = session.scalars(
            select(IntakeItem).join(IntakeBatchFile, IntakeBatchFile.id == IntakeItem.batch_file_id)
            .where(IntakeBatchFile.batch_id == batch_id,
                   IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED)
            .options(*_item_options())
            .order_by(IntakeBatchFile.upload_order, IntakeItem.page_no,
                      IntakeItem.row_no, IntakeItem.id)
        ).unique().all()
        return project_items(rows)

    def suggested_groups(self, batch_id: int, *, limit: int = 50, offset: int = 0) -> GroupPage:
        """The organizer's read-only projection; no suggestion becomes a decision."""
        limit = require_limit(limit, max_limit=MAX_ITEMS_PER_PAGE)
        offset = require_offset(offset)
        with self.database.unit_of_work() as session:
            self._existing_batch(session, batch_id)
            grouped: dict[tuple[date | None, str | None], IntakeGroupView] = {}
            for row in self._projected(session, batch_id):
                key = row.purchase_date, row.source_key
                prior = grouped.get(key)
                grouped[key] = IntakeGroupView(
                    purchase_date=row.purchase_date,
                    source_name=prior.source_name if prior else row.source_name,
                    source_name_normalized=row.source_key,
                    item_count=(prior.item_count if prior else 0) + 1,
                    suggested_count=(prior.suggested_count if prior else 0) + int(row.suggested),
                )
            for value in session.scalars(select(IntakeDateGroup.purchase_date).where(
                    IntakeDateGroup.batch_id == batch_id)).all():
                if not any(key[0] == value for key in grouped):
                    grouped[(value, None)] = IntakeGroupView(
                        purchase_date=value, source_name=None, source_name_normalized=None,
                        item_count=0)
            ordered = sorted(grouped.values(), key=lambda group: (
                group.purchase_date is None, group.purchase_date or date.min,
                group.source_name_normalized is None, group.source_name_normalized or "",
            ))
            return GroupPage(tuple(ordered[offset:offset + limit]), len(ordered), limit, offset)

    def suggested_group_items(self, batch_id: int, *, purchase_date: str | None,
                              source_name: str | None, limit: int = 50,
                              offset: int = 0) -> IntakeItemPage:
        limit = require_limit(limit, max_limit=MAX_ITEMS_PER_PAGE)
        offset = require_offset(offset)
        parsed_date = self._date(purchase_date) if purchase_date is not None else None
        source_key = normalize_name(require_text(
            source_name, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH
        )) if source_name is not None else None
        with self.database.unit_of_work() as session:
            self._existing_batch(session, batch_id)
            selected = [row for row in self._projected(session, batch_id)
                        if row.purchase_date == parsed_date and row.source_key == source_key]
            page = selected[offset:offset + limit]
            return IntakeItemPage(tuple(IntakeItemView.of(row.item) for row in page),
                                  len(selected), limit, offset)

    @staticmethod
    def _date(raw: Any) -> date:
        if not isinstance(raw, str):
            raise ValidationError("purchase_date 必须是 ISO 日期文本。", details={"field": "purchase_date"})
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise ValidationError("purchase_date 必须是合法的 ISO 日期。", details={"field": "purchase_date"}) from exc

    @staticmethod
    def _validated_moves(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
            raise ValidationError("items 必须是 1 到 200 个内容。", details={"field": "items"})
        result = []
        ids = set()
        for value in raw:
            if not isinstance(value, Mapping) or set(value) != {"id", "version"}:
                raise ValidationError("每个内容必须只含 id 和 version。", details={"field": "items"})
            item_id, version = value["id"], value["version"]
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (item_id, version)):
                raise ValidationError("id 和 version 必须是正整数。", details={"field": "items"})
            if item_id in ids:
                raise ValidationError("同一个内容不能在一次操作中重复出现。", details={"item_id": item_id})
            ids.add(item_id)
            result.append({"id": item_id, "version": version})
        return result

    def move(self, batch_id: int, *, request_key: str, action: str, items: Any,
             purchase_date: Any = None, source_name: Any = None) -> MoveResult:
        request_key = require_text(request_key, field="request_key", max_length=80)
        if not isinstance(action, str) or action not in {"date", "group", "source", "clear", "exclude", "include"}:
            raise ValidationError("action 必须是 date、group、source、clear、exclude 或 include。")
        selected = self._validated_moves(items)
        target_date = self._date(purchase_date) if action in {"date", "group"} else None
        target_source = require_text(source_name, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH) if action in {"group", "source"} else None
        if action in {"date", "group"} and purchase_date is None:
            raise ValidationError("移动到日期或商家组时必须提供 purchase_date。")
        if action in {"group", "source"} and source_name is None:
            raise ValidationError("移动到商家组时必须提供 source_name。")
        if (action not in {"group", "source"} and source_name is not None) or (action not in {"date", "group"} and purchase_date is not None):
            raise ValidationError("当前 action 不接受这些目标字段。")
        signature = json.dumps({"action": action, "items": selected, "purchase_date":
            target_date.isoformat() if target_date else None, "source_name": target_source},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.database.unit_of_work() as session:
            self._active_batch(session, batch_id)
            repo = IntakeMoveRepository(session)
            prior = repo.by_key(batch_id, request_key)
            if prior is not None:
                if prior.request_signature != signature:
                    raise ConflictError("request_key 已用于不同的操作。", details={"request_key": request_key})
                return self._result(session, prior, selected_ids=[row["id"] for row in selected])

            # All IDs and versions are checked before the first write. A later failure rolls
            # back the entire transaction, including the journal and preceding item updates.
            rows = []
            for selected_item in selected:
                item = IntakeItemRepository(session).get(selected_item["id"])
                if item is None or item.batch_file.batch_id != batch_id:
                    raise NotFoundError("内容不在这个批次中。", details={"item_id": selected_item["id"]})
                self._writable(item)
                if item.version != selected_item["version"]:
                    raise ConflictError("内容版本已改变，请刷新后重试。", details={
                        "item_id": item.id, "expected_version": selected_item["version"], "current_version": item.version})
                rows.append(item)
            # Preserve a date header when the last content is moved away.
            projected_before = {row.item.id: row.purchase_date
                                for row in self._projected(session, batch_id)}
            dates_to_pin = {projected_before[item.id] for item in rows
                            if projected_before[item.id] is not None}
            if target_date is not None:
                dates_to_pin.add(target_date)
            self._pin_dates(session, batch_id, dates_to_pin)
            operation = IntakeMove(batch_id=batch_id, request_key=request_key, request_signature=signature)
            session.add(operation)
            session.flush()
            for item in rows:
                before = (item.decided_purchase_date, item.decided_source_name, item.decided_at, item.pile_excluded)
                new_date, new_source, new_excluded = before[0], before[1], before[3]
                if action in {"date", "group"}:
                    new_date = target_date
                if action in {"group", "source"}:
                    # `source` 只写来源、**不碰日期**（M07）：相似堆里的六张卡片
                    # 常常各是不同的一天，逼着一起定一个日期就会把日期猜错——
                    # 而 M05 已经把「日期与商家可以各自待定」做成了结构。
                    new_source = target_source
                if action == "clear":
                    new_date = new_source = None
                if action in {"exclude", "include"}:
                    new_excluded = action == "exclude"
                if (new_date, new_source, new_excluded) == (before[0], before[1], before[3]) and not (
                    action == "clear" and item.decided_at is None
                ):
                    continue
                now = utcnow()
                expected_version = item.version
                changed = session.execute(
                    update(IntakeItem).where(IntakeItem.id == item.id, IntakeItem.version == expected_version,
                        IntakeItem.handed_off_at.is_(None)).values(
                        decided_purchase_date=new_date, decided_source_name=new_source,
                        decided_source_name_normalized=normalize_name(new_source) if new_source else None,
                        decided_at=now if action not in {"exclude", "include"} else before[2],
                        pile_excluded=new_excluded, version=expected_version + 1,
                    )
                ).rowcount
                if changed != 1:
                    raise ConflictError("内容刚被其他会话修改，请刷新后重试。", details={"item_id": item.id})
                session.add(IntakeMoveEntry(move_id=operation.id, item_id=item.id,
                    before_date=before[0], before_source=before[1], before_decided_at=before[2],
                    before_pile_excluded=before[3], after_version=expected_version + 1))
                session.refresh(item)
            session.flush()
            return self._result(session, operation, selected_ids=[value["id"] for value in selected])

    @staticmethod
    def _writable(item: IntakeItem) -> None:
        if item.batch_file.status == INTAKE_FILE_STATUS_DISCARDED:
            raise ConflictError("文件已移出批次，不能分类。", details={"item_id": item.id})
        if item.handed_off_at is not None:
            raise ConflictError("内容已进入审核，请通过审核修改流程更正。", details={"item_id": item.id})

    @staticmethod
    def _result(session, operation: IntakeMove, selected_ids: Sequence[int] | None = None) -> MoveResult:
        ids = selected_ids if selected_ids is not None else [row["id"] for row in json.loads(operation.request_signature)["items"]]
        item_repo = IntakeItemRepository(session)
        return MoveResult(operation.id, operation.request_key, operation.undone_at is not None,
            tuple(IntakeItemView.of(item_repo.get(item_id)) for item_id in ids))

    def undo(self, batch_id: int, move_id: int) -> MoveResult:
        with self.database.unit_of_work() as session:
            self._active_batch(session, batch_id)
            repo = IntakeMoveRepository(session)
            operation = repo.get(move_id)
            if operation is None or operation.batch_id != batch_id:
                raise NotFoundError("找不到这次移动。", details={"move_id": move_id})
            if operation.undone_at is not None:
                return self._result(session, operation)
            if not operation.entries:
                operation.undone_at = utcnow()
                session.flush()
                return self._result(session, operation)
            latest = repo.latest(batch_id)
            if latest is None or latest.id != move_id:
                raise ConflictError("只能撤销最近一次尚未撤销的移动。", details={"move_id": move_id})
            item_repo = IntakeItemRepository(session)
            for entry in operation.entries:
                item = item_repo.get(entry.item_id)
                self._writable(item)
                if item.version != entry.after_version:
                    raise ConflictError("内容已再次改变，不能直接撤销。", details={"item_id": item.id,
                        "expected_version": entry.after_version, "current_version": item.version})
            for entry in operation.entries:
                item = item_repo.get(entry.item_id)
                changed = session.execute(update(IntakeItem).where(
                    IntakeItem.id == item.id, IntakeItem.version == entry.after_version,
                    IntakeItem.handed_off_at.is_(None)).values(
                    decided_purchase_date=entry.before_date,
                    decided_source_name=entry.before_source,
                    decided_source_name_normalized=normalize_name(entry.before_source) if entry.before_source else None,
                    decided_at=entry.before_decided_at,
                    pile_excluded=entry.before_pile_excluded,
                    version=entry.after_version + 1,
                )).rowcount
                if changed != 1:
                    raise ConflictError("内容刚被其他会话修改，请刷新后重试。", details={"item_id": item.id})
                session.refresh(item)
            operation.undone_at = utcnow()
            session.flush()
            return self._result(session, operation)
