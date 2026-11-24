"""M06 grouping uses synthetic content and migrated, temporary SQLite databases."""

from __future__ import annotations

import hashlib

import pytest

from src.errors import ConflictError
from src.models import IntakeItem, utcnow
from src.services.intake import IntakeService
from src.services.intake import CandidateInput
from src.services.intake_moves import IntakeMoveService


def _items(intake: IntakeService, count: int = 4):
    batch = intake.create_batch()
    ref = intake.register_file(batch.id, storage_key="synthetic/m06.pdf",
        sha256=hashlib.sha256(b"m06 synthetic bytes").hexdigest(), size=100,
        original_name="synthetic.pdf", content_type="application/pdf").ref
    return batch.id, intake.record_items(ref.id, pages=list(range(1, count + 1)))


def _selected(rows):
    return [{"id": row.id, "version": row.version} for row in rows]


def test_ledger_filters_before_pagination(migrated_client, migrated_database, intake_service):
    """M11: filters count the whole batch; a page is never filtered only in memory."""
    batch_id, rows = _items(intake_service, 6)
    moves = IntakeMoveService(migrated_database)
    moves.move(batch_id, request_key="ledger-group", action="group",
        items=_selected(rows[:2]), purchase_date="2026-11-08", source_name="Costco")
    moves.move(batch_id, request_key="ledger-date", action="date",
        items=_selected(rows[2:3]), purchase_date="2026-11-09")
    moves.move(batch_id, request_key="ledger-source", action="source",
        items=_selected(rows[3:4]), source_name="Market")
    url = f"/api/intake-batches/{batch_id}/items"
    def page(query):
        response = migrated_client.get(f"{url}?{query}")
        assert response.status_code == 200, response.json
        return response.json
    assert page("missing=both&limit=1")["total"] == 2
    assert page("missing=both&limit=1&offset=1")["items"][0]["id"] == rows[5].id
    assert [item["id"] for item in page("missing=date")["items"]] == [rows[3].id]
    assert [item["id"] for item in page("missing=source")["items"]] == [rows[2].id]
    assert page("purchase_date=2026-11-08&source_name=COSTCO")["total"] == 2
    assert page("source_name=missing")["total"] == 3
    assert page("missing=source_any")["total"] == 3
    assert page("missing=date_any")["total"] == 3
    assert page("missing=recognition_failed")["total"] == 0
    assert migrated_client.get(f"{url}?missing=wrong").status_code == 400
    assert migrated_client.get(f"{url}?purchase_date=2026-02-31").status_code == 400


def test_three_move_date_only_undo_and_persist(migrated_database, intake_service):
    batch_id, rows = _items(intake_service)
    moves = IntakeMoveService(migrated_database)
    first = moves.move(batch_id, request_key="three-to-costco", action="group",
        items=_selected(rows[:3]), purchase_date="2026-11-08", source_name="Costco")
    assert [item.version for item in first.items] == [2, 2, 2]
    assert [(g.purchase_date.isoformat() if g.purchase_date else None, g.source_name, g.item_count)
        for g in moves.groups(batch_id).groups] == [
            ("2026-11-08", "Costco", 3), (None, None, 1)]
    assert moves.group_items(batch_id, purchase_date="2026-11-08", source_name="COSTCO").total == 3
    assert moves.group_items(batch_id, purchase_date=None, source_name=None).total == 1
    # Reopening the service and replaying the same request does not create a second move.
    replay = IntakeMoveService(migrated_database).move(batch_id, request_key="three-to-costco",
        action="group", items=_selected(rows[:3]), purchase_date="2026-11-08", source_name="Costco")
    assert replay.id == first.id
    assert [item.version for item in replay.items] == [2, 2, 2]
    noop = moves.move(batch_id, request_key="already-there", action="group",
        items=[{"id": rows[0].id, "version": 2}], purchase_date="2026-11-08", source_name="Costco")
    assert noop.items[0].version == 2
    restored = moves.undo(batch_id, first.id)
    assert all(item.decided_purchase_date is None and item.decided_source_name is None for item in restored.items)
    assert all(item.version == 3 for item in restored.items)
    assert moves.group_items(batch_id, purchase_date=None, source_name=None).total == 4
    date_only = moves.move(batch_id, request_key="date-only", action="date",
        items=[{"id": rows[0].id, "version": 3}], purchase_date="2026-11-09")
    assert date_only.items[0].decided_source_name is None
    assert moves.group_items(batch_id, purchase_date="2026-11-09", source_name=None).total == 1


def test_conflict_rolls_back_all_selected_items(migrated_database, intake_service):
    batch_id, rows = _items(intake_service, 3)
    session_a = IntakeMoveService(migrated_database)
    session_b = IntakeMoveService(migrated_database)
    session_a.move(batch_id, request_key="session-a", action="date",
        items=_selected(rows[1:2]), purchase_date="2026-11-08")
    with pytest.raises(ConflictError):
        session_b.move(batch_id, request_key="session-b", action="group",
            items=_selected(rows), purchase_date="2026-11-09", source_name="Costco")
    assert intake_service.get_item(rows[0].id).decided_purchase_date is None
    assert intake_service.get_item(rows[1].id).decided_purchase_date.isoformat() == "2026-11-08"
    assert intake_service.get_item(rows[2].id).decided_purchase_date is None


def test_exclusion_clear_and_undo_guard(migrated_database, intake_service):
    batch_id, rows = _items(intake_service, 2)
    moves = IntakeMoveService(migrated_database)
    excluded = moves.move(batch_id, request_key="exception", action="exclude", items=_selected(rows[:1]))
    assert excluded.items[0].pile_excluded is True
    with pytest.raises(ConflictError):
        moves.move(batch_id, request_key="exception", action="clear", items=_selected(rows[:1]))
    moves.undo(batch_id, excluded.id)
    assert intake_service.get_item(rows[0].id).pile_excluded is False
    grouped = moves.move(batch_id, request_key="group", action="group",
        items=[{"id": rows[0].id, "version": 3}], purchase_date="2026-11-08", source_name="Costco")
    cleared = moves.move(batch_id, request_key="clear", action="clear",
        items=[{"id": rows[0].id, "version": grouped.items[0].version}])
    assert cleared.items[0].decided_purchase_date is None
    assert cleared.items[0].decided_source_name is None
    with pytest.raises(ConflictError):
        moves.undo(batch_id, grouped.id)


def test_handed_off_item_rejects_move_and_undo(migrated_database, intake_service):
    batch_id, rows = _items(intake_service, 1)
    moves = IntakeMoveService(migrated_database)
    op = moves.move(batch_id, request_key="before-review", action="date",
        items=_selected(rows), purchase_date="2026-11-08")
    with migrated_database.unit_of_work() as session:
        session.get(IntakeItem, rows[0].id).handed_off_at = utcnow()
    with pytest.raises(ConflictError):
        moves.undo(batch_id, op.id)
    with pytest.raises(ConflictError):
        moves.move(batch_id, request_key="after-review", action="clear",
            items=[{"id": rows[0].id, "version": 2}])
    with pytest.raises(ConflictError):
        intake_service.classify_item(rows[0].id, purchase_date="2026-11-09", version=2)


def test_http_board_move_and_undo(migrated_client, intake_service):
    batch_id, rows = _items(intake_service)
    intake_service.replace_candidates(rows[0].id, field="source_name", basis="paper",
        candidates=[CandidateInput(value="Costco", evidence={"page": 1, "text": "Costco"})])
    root = f"/api/intake-batches/{batch_id}"
    command = {"request_key": "http-three", "action": "group", "items": _selected(rows[:3]),
               "purchase_date": "2026-11-08", "source_name": "Costco"}
    response = migrated_client.post(f"{root}/moves", json=command)
    assert response.status_code == 200
    move_id = response.get_json()["move_id"]
    board = migrated_client.get(f"{root}/groups?limit=1&offset=0")
    assert board.status_code == 200
    assert board.get_json()["total"] == 2
    assert board.get_json()["groups"][0]["item_count"] == 3
    cards = migrated_client.get(f"{root}/groups/items?purchase_date=2026-11-08&source_name=Costco&limit=2")
    assert cards.status_code == 200
    assert cards.get_json()["total"] == 3
    assert len(cards.get_json()["items"]) == 2
    assert cards.get_json()["items"][0]["candidates"][0]["basis"] == "paper"
    assert cards.get_json()["items"][0]["decided_source_name"] == "Costco"
    assert migrated_client.post(f"{root}/moves", json={**command, "request_key": "bad", "action": []}).status_code == 400
    assert migrated_client.post(f"{root}/moves/{move_id}/undo").status_code == 200
    assert migrated_client.get(f"{root}/groups/items").get_json()["total"] == 4


def test_dates_remain_until_deleted_and_clear_overrides_suggestions(migrated_client, intake_service):
    batch_id, rows = _items(intake_service, 1)
    intake_service.replace_candidates(rows[0].id, field="purchase_date", basis="paper",
        candidates=[CandidateInput(value="2026-06-06", evidence={"raw_text": "6/6/2026"})])
    root = f"/api/intake-batches/{batch_id}"

    def groups():
        response = migrated_client.get(f"{root}/groups?suggested=true")
        assert response.status_code == 200
        return response.json["groups"]

    assert any(group["purchase_date"] == "2026-06-06" for group in groups())
    moved = migrated_client.post(f"{root}/moves", json={
        "request_key": "move-to-eight", "action": "date", "items": _selected(rows),
        "purchase_date": "2026-06-08"})
    assert moved.status_code == 200
    assert any(group["purchase_date"] == "2026-06-06" and group["item_count"] == 0
               for group in groups())
    current = moved.json["items"][0]
    cleared = migrated_client.post(f"{root}/moves", json={
        "request_key": "clear-to-pending", "action": "clear",
        "items": [{"id": current["id"], "version": current["version"]}]})
    assert cleared.status_code == 200
    assert migrated_client.get(f"{root}/groups/items?suggested=true").json["total"] == 1
    assert all(group["item_count"] == 0 for group in groups()
               if group["purchase_date"] is not None)

    created = migrated_client.post(f"{root}/date-groups", json={"purchase_date": "2026-06-10"})
    assert created.status_code == 201
    assert any(group["purchase_date"] == "2026-06-10" for group in groups())
    assert migrated_client.delete(f"{root}/date-groups/2026-06-10").status_code == 200
    assert not any(group["purchase_date"] == "2026-06-10" for group in groups())
    assert migrated_client.delete(f"{root}/date-groups/2026-06-06").status_code == 200
    assert not any(group["purchase_date"] == "2026-06-06" for group in groups())
