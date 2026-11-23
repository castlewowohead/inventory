"""商家记忆与相似堆的 HTTP 边界（多文件计划 M07）。

验证的是**请求与响应**：重算一次建议返回什么、`model_calls` 报得准不准、堆里的
例外有没有被单独列出来、别名能不能改指向与撤销、整堆命名走的是不是 M06 那一条
`/moves` 命令。业务规则本身在 `test_intake_suggestions.py`。

⚠️ 这些用例一次网络请求都不会发：M07 的三层线索全是本地计算与数据库查询，所以
`model_calls` 恰好可以断言成 0。真实原件上的实测覆盖范围另见
`docs/multifiles.md` M07 的验证记录。
"""

from __future__ import annotations

import io

from flask.testing import FlaskClient

from tests.test_intake_suggestions import form_png, photo_jpeg, scan_pdf

INTAKE = "/api/intake-batches"
MEMORY = "/api/source-memory"


def new_batch(client: FlaskClient) -> int:
    response = client.post(INTAKE)
    assert response.status_code == 201
    return response.get_json()["id"]


def upload(client: FlaskClient, batch_id: int, name: str, content: bytes) -> dict:
    response = client.post(
        f"{INTAKE}/{batch_id}/files",
        data={"file": (io.BytesIO(content), name)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def cards(client: FlaskClient, batch_id: int) -> list[dict]:
    response = client.get(f"{INTAKE}/{batch_id}/items")
    assert response.status_code == 200
    return response.get_json()["items"]


def move(client: FlaskClient, batch_id: int, payload: dict) -> dict:
    response = client.post(f"{INTAKE}/{batch_id}/moves", json=payload)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def suggest(client: FlaskClient, batch_id: int) -> dict:
    response = client.post(f"{INTAKE}/{batch_id}/suggest-sources")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_suggesting_reports_piles_and_zero_model_calls(
    migrated_client: FlaskClient,
) -> None:
    batch_id = new_batch(migrated_client)
    for index in range(3):
        upload(migrated_client, batch_id, f"scan{index}.png", form_png(tag=str(index)))

    payload = suggest(migrated_client, batch_id)

    assert payload["model_calls"] == 0
    assert payload["layout_profiled"] == 3
    assert payload["layout_pending"] == 0
    assert payload["total"] == 3
    pile = payload["piles"][0]
    assert pile["item_count"] == 3
    assert pile["basis_detail"] == "layout_template"
    assert pile["template_evidence"] is True
    assert pile["features"]["ruled_rows"] == 12
    assert pile["confirmed_sources"] == []
    # 这一轮没有给出商家名，所以卡片上一条商家候选都没有（堆只是待办）。
    assert all(card["candidates"] == [] for card in cards(migrated_client, batch_id))


def test_pile_board_is_read_only_and_lists_the_exception_separately(
    migrated_client: FlaskClient,
) -> None:
    batch_id = new_batch(migrated_client)
    for index in range(4):
        upload(migrated_client, batch_id, f"scan{index}.png", form_png(tag=str(index)))
    suggest(migrated_client, batch_id)
    rows = cards(migrated_client, batch_id)
    exception = rows[3]
    move(
        migrated_client,
        batch_id,
        {
            "request_key": "exclude-one",
            "action": "exclude",
            "items": [{"id": exception["id"], "version": exception["version"]}],
        },
    )

    response = migrated_client.get(f"{INTAKE}/{batch_id}/piles")

    assert response.status_code == 200
    board = response.get_json()
    assert board["batch_id"] == batch_id
    assert board["piles"][0]["item_count"] == 3
    assert board["piles"][0]["excluded_item_ids"] == [exception["id"]]
    assert exception["id"] not in board["piles"][0]["item_ids"]


def test_naming_a_whole_pile_uses_the_m06_move_command(
    migrated_client: FlaskClient,
) -> None:
    """整堆命名**只写来源**：日期仍然待定，因为堆里的卡片常常各属不同的一天。"""
    batch_id = new_batch(migrated_client)
    for index in range(3):
        upload(migrated_client, batch_id, f"scan{index}.png", form_png(tag=str(index)))
    suggest(migrated_client, batch_id)
    rows = cards(migrated_client, batch_id)

    named = move(
        migrated_client,
        batch_id,
        {
            "request_key": "name-the-pile",
            "action": "source",
            "items": [{"id": row["id"], "version": row["version"]} for row in rows],
            "source_name": "Kunkee Trading",
        },
    )

    assert [item["decided_source_name"] for item in named["items"]] == [
        "Kunkee Trading"
    ] * 3
    assert all(item["decided_purchase_date"] is None for item in named["items"])
    # 组视图里这三张在同一个「日期待定 / Kunkee Trading」组里。
    groups = migrated_client.get(f"{INTAKE}/{batch_id}/groups").get_json()["groups"]
    assert [(row["purchase_date"], row["source_name"], row["item_count"]) for row in groups] == [
        (None, "Kunkee Trading", 3)
    ]
    # 整堆命名之后这一堆就空了，`/piles` 不再返回它。
    assert migrated_client.get(f"{INTAKE}/{batch_id}/piles").get_json()["piles"] == []


def test_source_memory_lists_confirmed_sources_and_their_aliases(
    migrated_client: FlaskClient,
) -> None:
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "one.pdf", scan_pdf(1))
    card = cards(migrated_client, batch_id)[0]
    move(
        migrated_client,
        batch_id,
        {
            "request_key": "confirm",
            "action": "source",
            "items": [{"id": card["id"], "version": card["version"]}],
            "source_name": "Costco",
        },
    )

    created = migrated_client.post(
        f"{MEMORY}/aliases",
        json={"alias_text": "COSTCO WHOLESALE #1162", "source_name": "Costco"},
    )
    assert created.status_code == 200
    alias = created.get_json()["alias"]
    assert alias["alias_key"] == "costco wholesale #1162"
    assert alias["source_key"] == "costco"
    assert alias["basis"] == "user"

    listed = migrated_client.get(MEMORY)
    assert listed.status_code == 200
    sources = listed.get_json()["sources"]
    assert [item["source_key"] for item in sources] == ["costco"]
    assert sources[0]["source_name"] == "Costco"
    assert sources[0]["confirmed_items"] == 1
    assert [item["alias_key"] for item in sources[0]["aliases"]] == [
        "costco wholesale #1162"
    ]

    aliases = migrated_client.get(f"{MEMORY}/aliases").get_json()["aliases"]
    assert len(aliases) == 1
    removed = migrated_client.delete(f"{MEMORY}/aliases/{alias['id']}")
    assert removed.status_code == 200
    assert migrated_client.get(f"{MEMORY}/aliases").get_json()["aliases"] == []
    # 删别名不动已确认的分类。
    assert cards(migrated_client, batch_id)[0]["decided_source_name"] == "Costco"


def test_alias_errors_are_json_not_crashes(migrated_client: FlaskClient) -> None:
    same = migrated_client.post(
        f"{MEMORY}/aliases", json={"alias_text": "COSTCO", "source_name": "costco"}
    )
    assert same.status_code == 400
    assert "别名" in same.get_json()["error"]["message"]
    unknown_field = migrated_client.post(
        f"{MEMORY}/aliases",
        json={"alias_text": "HF", "source_name": "Hua Feng", "supplier_id": 7},
    )
    assert unknown_field.status_code == 400
    missing = migrated_client.delete(f"{MEMORY}/aliases/4242")
    assert missing.status_code == 404


def test_history_suggestion_appears_on_the_card_with_its_basis(
    migrated_client: FlaskClient,
) -> None:
    """建议落在卡片的 `candidates` 里，`basis` 与 `evidence` 都看得见。"""
    first = new_batch(migrated_client)
    upload(migrated_client, first, "one.png", form_png(tag="one"))
    suggest(migrated_client, first)
    known = cards(migrated_client, first)[0]
    move(
        migrated_client,
        first,
        {
            "request_key": "confirm",
            "action": "source",
            "items": [{"id": known["id"], "version": known["version"]}],
            "source_name": "Kunkee Trading",
        },
    )

    second = new_batch(migrated_client)
    upload(migrated_client, second, "two.png", form_png(tag="two"))
    payload = suggest(migrated_client, second)

    assert payload["model_calls"] == 0
    card = cards(migrated_client, second)[0]
    assert [item["basis"] for item in card["candidates"]] == ["similar_layout"]
    candidate = card["candidates"][0]
    assert candidate["value"] == "Kunkee Trading"
    assert candidate["evidence"]["basis_detail"] == "layout_memory"
    # 候选不是决定：卡片仍然在「商家待定」里。
    assert card["decided_source_name"] is None
    assert payload["items"][0]["suggestions"][0]["score"] == "0.40"


def test_unprofiled_photos_are_reported_without_a_source_candidate(
    migrated_client: FlaskClient,
) -> None:
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "p1.jpg", photo_jpeg(tint=231))
    upload(migrated_client, batch_id, "p2.jpg", photo_jpeg(tint=232))

    payload = suggest(migrated_client, batch_id)

    pile = payload["piles"][0]
    assert pile["basis_detail"] == "layout_unknown"
    assert pile["template_evidence"] is False
    assert pile["confirmed_sources"] == []
    assert all(card["candidates"] == [] for card in cards(migrated_client, batch_id))


def test_unknown_batch_and_unknown_fields_are_rejected(
    migrated_client: FlaskClient,
) -> None:
    assert migrated_client.post(f"{INTAKE}/4242/suggest-sources").status_code == 404
    assert migrated_client.get(f"{INTAKE}/4242/piles").status_code == 404
    batch_id = new_batch(migrated_client)
    rejected = migrated_client.post(
        f"{INTAKE}/{batch_id}/suggest-sources", json={"supplier": "Costco"}
    )
    assert rejected.status_code == 400


def test_source_action_still_refuses_a_date(migrated_client: FlaskClient) -> None:
    """`source` 只接受来源：给它日期是请求错误，而不是悄悄把日期也写了。"""
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "one.pdf", scan_pdf(1))
    card = cards(migrated_client, batch_id)[0]

    response = migrated_client.post(
        f"{INTAKE}/{batch_id}/moves",
        json={
            "request_key": "source-with-date",
            "action": "source",
            "items": [{"id": card["id"], "version": card["version"]}],
            "source_name": "Costco",
            "purchase_date": "2026-11-08",
        },
    )

    assert response.status_code == 400
    assert cards(migrated_client, batch_id)[0]["decided_source_name"] is None
