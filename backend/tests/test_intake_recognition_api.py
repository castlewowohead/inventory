"""日期/商家识别的 HTTP 边界（多文件计划 M05）。

验证的是**请求与响应**：驱动一次识别队列返回什么、`model_calls` 报得准不准、
商家没确认时重新解析会不会被拦住、路径里的两个 id 对不上会不会漏出别的批次的
内容。业务规则本身在 `test_intake_recognition.py`。

⚠️ 这里刻意用**带文字层的合成 PDF**：它走的是零成本的文字层路径，所以这些用例
一次网络请求都不会发，而 `model_calls` 恰好可以断言成 0——那正是「不对同一页重复
收费」在接口上的样子。真实扫描件的识别结果另见 `docs/multifiles.md` M05 的验证
记录。
"""

from __future__ import annotations

import io

from flask.testing import FlaskClient

from src.services.intake import IntakeService
from tests.test_intake_recognition import (
    INVOICE_PAGE,
    SECOND_PAGE,
    scan_pdf,
    text_pdf,
)

INTAKE = "/api/intake-batches"


def new_batch(client: FlaskClient) -> int:
    response = client.post(INTAKE)
    assert response.status_code == 201
    return response.get_json()["id"]


def upload(client: FlaskClient, batch_id: int, name: str, content: bytes):
    return client.post(
        f"{INTAKE}/{batch_id}/files",
        data={"file": (io.BytesIO(content), name)},
        content_type="multipart/form-data",
    )


def items_of(client: FlaskClient, batch_id: int) -> list[dict]:
    response = client.get(f"{INTAKE}/{batch_id}/items")
    assert response.status_code == 200
    return response.get_json()["items"]


# --- 驱动识别队列 -----------------------------------------------------------


def test_recognising_a_batch_returns_candidates_and_reports_zero_model_calls(
    migrated_client: FlaskClient,
) -> None:
    """数字 PDF 的两页各自拿到候选，而 `model_calls` 是 0——文字层不花钱。"""
    batch_id = new_batch(migrated_client)
    assert upload(
        migrated_client, batch_id, "mixed.pdf", text_pdf([INVOICE_PAGE, SECOND_PAGE])
    ).status_code == 201

    response = migrated_client.post(f"{INTAKE}/{batch_id}/recognize")

    assert response.status_code == 200
    body = response.get_json()
    assert body["model_calls"] == 0
    assert [entry["outcome"] for entry in body["results"]] == ["recognized"] * 2
    assert [entry["source_candidates"] for entry in body["results"]] == [
        ["Costco Wholesale"],
        ["Hua Feng Market"],
    ]
    assert body["results"][0]["provider"] == "text-layer"


def test_the_item_payload_tells_apart_not_read_yet_from_read_but_nothing_there(
    migrated_client: FlaskClient,
) -> None:
    """卡片上的 `recognition.status` 要能说出三句不同的话。

    还没读是 `null`；读完了是 `done`（哪怕一条候选都没有）；读失败是 `failed` 带
    原因。一律显示「未知」会让人不知道该等还是该自己填。
    """
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "invoice.pdf", text_pdf([INVOICE_PAGE]))

    before = items_of(migrated_client, batch_id)[0]
    assert before["recognition"]["status"] == "pending"
    assert before["recognition"]["recognized_at"] is None

    migrated_client.post(f"{INTAKE}/{batch_id}/recognize")

    after = items_of(migrated_client, batch_id)[0]
    assert after["recognition"]["status"] == "done"
    assert after["recognition"]["recognized_at"] is not None
    assert after["recognition"]["provider"] == "text-layer"
    assert after["recognition"]["prompt_sha12"] is None


def test_an_ambiguous_date_is_reported_as_two_readings_on_the_card(
    migrated_client: FlaskClient,
) -> None:
    """`6/11/2026` 在卡片上就是**两种可能**，而且带着原文。

    前端据此说「这一天定不死，确认商家之后可以定」，而不是先显示一个 6 月 11 日
    让人以为已经读准了。
    """
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "invoice.pdf", text_pdf([INVOICE_PAGE]))
    migrated_client.post(f"{INTAKE}/{batch_id}/recognize")

    card = items_of(migrated_client, batch_id)[0]

    recognition = card["recognition"]
    assert recognition["date_raw_text"] == "6/11/2026"
    assert recognition["date_ambiguous"] is True
    assert sorted(entry["value"] for entry in recognition["date_readings"]) == [
        "2026-06-11",
        "2026-11-06",
    ]
    assert recognition["date_order"] == "strict"
    assert recognition["date_order_from"] == "default_strict"
    # 候选在响应里是单独一处，而且带依据——与人工决定分得清。
    dates = [
        entry for entry in card["candidates"] if entry["field"] == "purchase_date"
    ]
    assert {entry["value"] for entry in dates} == {"2026-06-11", "2026-11-06"}
    assert {entry["basis"] for entry in dates} == {"paper"}
    assert card["decided_purchase_date"] is None


def test_driving_the_queue_again_returns_nothing_to_do(
    migrated_client: FlaskClient,
) -> None:
    """重复驱动安全且免费：第二次队列是空的，`model_calls` 还是 0。"""
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "invoice.pdf", text_pdf([INVOICE_PAGE]))
    assert migrated_client.post(f"{INTAKE}/{batch_id}/recognize").status_code == 200

    again = migrated_client.post(f"{INTAKE}/{batch_id}/recognize")

    assert again.status_code == 200
    assert again.get_json()["results"] == []
    assert again.get_json()["model_calls"] == 0


def test_a_page_that_cannot_be_read_comes_back_200_with_the_reason_on_the_item(
    migrated_client: FlaskClient,
) -> None:
    """读不出来的页是**正常结果**，不是请求失败：200 + 这一页的原因。

    与 `/process`、批量确认同一条：「十页读好八页、两页读不出」该让人看见那两页是
    哪两页，而不是整个请求 500。
    """
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "blank.pdf", scan_pdf(2))

    response = migrated_client.post(f"{INTAKE}/{batch_id}/recognize")

    assert response.status_code == 200
    body = response.get_json()
    assert [entry["outcome"] for entry in body["results"]] == ["failed"] * 2
    assert body["results"][0]["error"]["code"] == "page_image_missing"
    # 原件照常可回看：失败写在内容上，不动字节。
    card = items_of(migrated_client, batch_id)[0]
    original = migrated_client.get(
        f"{INTAKE}/{batch_id}/items/{card['id']}/original"
    )
    assert original.status_code == 200
    assert original.headers["X-Intake-Page"] == "1"


def test_retry_failed_is_opt_in_and_validated(
    migrated_client: FlaskClient,
) -> None:
    batch_id = new_batch(migrated_client)

    bad = migrated_client.post(
        f"{INTAKE}/{batch_id}/recognize", json={"retry_failed": "yes"}
    )
    assert bad.status_code == 400

    unknown = migrated_client.post(
        f"{INTAKE}/{batch_id}/recognize", json={"force": True}
    )
    assert unknown.status_code == 400

    assert migrated_client.post(
        f"{INTAKE}/{batch_id}/recognize", json={"retry_failed": True}
    ).status_code == 200


def test_recognising_an_unknown_batch_is_a_404(migrated_client: FlaskClient) -> None:
    assert migrated_client.post(f"{INTAKE}/4242/recognize").status_code == 404


# --- 重新解析日期 -----------------------------------------------------------


def test_reparsing_without_a_confirmed_supplier_is_a_400(
    migrated_client: FlaskClient,
) -> None:
    """商家还没人工确认 → 400，而不是悄悄按某家的习惯读一个日期。"""
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "invoice.pdf", text_pdf([INVOICE_PAGE]))
    migrated_client.post(f"{INTAKE}/{batch_id}/recognize")
    card = items_of(migrated_client, batch_id)[0]

    response = migrated_client.post(
        f"{INTAKE}/{batch_id}/items/{card['id']}/reparse-date"
    )

    assert response.status_code == 400
    assert "商家还没人工确认" in response.get_json()["error"]["message"]


def test_reparsing_after_confirming_the_supplier_resolves_the_date_for_free(
    migrated_client: FlaskClient, intake_service: IntakeService
) -> None:
    """确认商家 + 这家设了 `date_order` → 歧义消失，`model_calls` 是 0。

    ⚠️ 确认商家这一步**没有 HTTP 入口**：分类命令是 M06 的范围，所以这里直接用
    M02 的 Service 写下那个决定，不提前造一个会返工的接口。
    """
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "invoice.pdf", text_pdf([INVOICE_PAGE]))
    migrated_client.post(f"{INTAKE}/{batch_id}/recognize")
    card = items_of(migrated_client, batch_id)[0]
    assert migrated_client.put(
        "/api/ocr-routes",
        json={
            "source_name": "Costco Wholesale",
            "doc_class": "digital_pdf",
            "date_order": "mdy",
        },
    ).status_code in (200, 201)
    intake_service.classify_item(card["id"], source_name="Costco Wholesale")

    response = migrated_client.post(
        f"{INTAKE}/{batch_id}/items/{card['id']}/reparse-date"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["model_calls"] == 0
    dates = [
        entry
        for entry in body["item"]["candidates"]
        if entry["field"] == "purchase_date"
    ]
    assert [entry["value"] for entry in dates] == ["2026-06-11"]
    assert body["item"]["recognition"]["date_ambiguous"] is False
    assert body["item"]["recognition"]["date_order"] == "mdy"
    # 候选没有变成决定。
    assert body["item"]["decided_purchase_date"] is None


def test_reparsing_an_item_from_another_batch_is_a_404(
    migrated_client: FlaskClient,
) -> None:
    """批次 A 的 URL 不该能动批次 B 的卡片——「换个批次号试试」不是一条可用的读法。"""
    first = new_batch(migrated_client)
    upload(migrated_client, first, "invoice.pdf", text_pdf([INVOICE_PAGE]))
    migrated_client.post(f"{INTAKE}/{first}/recognize")
    card = items_of(migrated_client, first)[0]
    other = new_batch(migrated_client)

    response = migrated_client.post(
        f"{INTAKE}/{other}/items/{card['id']}/reparse-date"
    )

    assert response.status_code == 404
