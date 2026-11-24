"""发票接口的 HTTP 层测试（步骤 05）。

重点是三件只有在这一层才看得出来的事：

1. 上传走 multipart，文件字段名是 `files`；
2. **批量确认返回 200 并逐行给结果**——"三行确认两行、第三行报错"是正常结果，
   不是请求失败；
3. 数值一律是字符串，认不出来的格子是 `null` 而不是 `"0"`。
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from flask.testing import FlaskClient

PHOTO = b"\x89PNG\r\n\x1a\n fake photo bytes for api tests"
OTHER_PHOTO = b"\x89PNG\r\n\x1a\n another api photo"


def upload_data(
    content: bytes = PHOTO, name: str = "invoice.png", **extra: str
) -> dict[str, Any]:
    data: dict[str, Any] = {"files": (io.BytesIO(content), name, "image/png")}
    data.update(extra)
    return data


def post_invoice(client: FlaskClient, **kwargs: Any) -> dict[str, Any]:
    response = client.post(
        "/api/invoices", data=upload_data(**kwargs), content_type="multipart/form-data"
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def create_food_item(client: FlaskClient, name: str = "Rice") -> int:
    response = client.post(
        "/api/food-items",
        json={"name": name, "category_id": 5, "base_unit": "lb"},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["id"]


# --- 上传 -------------------------------------------------------------------


def test_upload_returns_a_draft(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)

    assert payload["status"] == "draft"
    assert len(payload["files"]) == 1
    assert payload["files"][0]["original_name"] == "invoice.png"
    assert payload["recognition"]["provider"] == "fake"
    assert payload["recognition"]["is_fake"] is True
    assert len(payload["lines"]) == 2


def test_numbers_are_strings_and_unreadable_values_are_null(
    migrated_client: FlaskClient,
) -> None:
    """`"60.00"` 而不是 60.0；认不出来的单价是 null 而不是 `"0"`。"""
    payload = post_invoice(migrated_client)
    first, second = payload["lines"]

    assert first["quantity"] == "10"
    assert first["unit_price"] == "6.00"
    assert first["line_amount"] == "60.00"
    assert second["unit_price"] is None
    assert second["raw_fields"]["fields"]["unit_price"]["status"] == "unreadable"
    assert second["missing_fields"] == ["unit_price", "food_item_id"]


def test_upload_without_files_is_rejected(migrated_client: FlaskClient) -> None:
    response = migrated_client.post(
        "/api/invoices", data={}, content_type="multipart/form-data"
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "files"


def test_unsupported_type_is_rejected_through_the_api(
    migrated_client: FlaskClient,
) -> None:
    response = migrated_client.post(
        "/api/invoices",
        data={"files": (io.BytesIO(b"hello"), "notes.txt", "text/plain")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "pdf" in response.get_json()["error"]["details"]["allowed_extensions"]


def test_recognize_false_only_stores_the_file(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client, recognize="false")

    assert payload["lines"] == []
    assert payload["recognition"]["provider"] is None


def test_recognize_flag_must_be_a_boolean_word(migrated_client: FlaskClient) -> None:
    response = migrated_client.post(
        "/api/invoices",
        data=upload_data(recognize="maybe"),
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "recognize"


def test_a_too_large_request_is_refused_as_json(migrated_client: FlaskClient) -> None:
    """请求体上限由 Flask 挡下（413），但错误结构仍然是本项目统一的那一种。"""
    response = migrated_client.post(
        "/api/invoices",
        data=upload_data(content=b"x" * (400 * 1024)),
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "request_entity_too_large"


def test_the_same_file_twice_is_a_conflict(migrated_client: FlaskClient) -> None:
    first = post_invoice(migrated_client)

    response = migrated_client.post(
        "/api/invoices",
        data=upload_data(name="again.png"),
        content_type="multipart/form-data",
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["details"]["invoice_id"] == first["id"]


# --- 文件 -------------------------------------------------------------------


def test_file_content_comes_back_byte_for_byte(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    file_id = payload["files"][0]["id"]

    response = migrated_client.get(
        f"/api/invoices/{payload['id']}/files/{file_id}/content"
    )

    assert response.status_code == 200
    assert response.data == PHOTO
    assert response.mimetype == "image/png"


def test_adding_and_deleting_files(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    invoice_id = payload["id"]

    added = migrated_client.post(
        f"/api/invoices/{invoice_id}/files",
        data=upload_data(content=OTHER_PHOTO, name="page2.png"),
        content_type="multipart/form-data",
    )
    assert added.status_code == 201
    second_id = added.get_json()["files"][1]["id"]

    deleted = migrated_client.delete(f"/api/invoices/{invoice_id}/files/{second_id}")
    assert deleted.status_code == 200
    assert len(deleted.get_json()["files"]) == 1


def test_the_last_file_cannot_be_deleted_through_the_api(
    migrated_client: FlaskClient,
) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.delete(
        f"/api/invoices/{payload['id']}/files/{payload['files'][0]['id']}"
    )

    assert response.status_code == 409
    assert "最后一个文件" in response.get_json()["error"]["message"]


# --- 单据头与行 -------------------------------------------------------------


def test_patching_the_header(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.patch(
        f"/api/invoices/{payload['id']}",
        json={
            "source_name": "Restaurant Depot",
            "invoice_date": "2026-11-08",
            "invoice_total": "286.00",
            "currency": "USD",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["source_name"] == "Restaurant Depot"
    assert body["invoice_date"] == "2026-11-08"
    assert body["invoice_total"] == "286.00"


def test_patching_an_existing_invoice_accepts_clear_ocr_date_text(
    migrated_client: FlaskClient,
) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.patch(
        f"/api/invoices/{payload['id']}", json={"invoice_date": "6/29/26"}
    )

    assert response.status_code == 200
    assert response.get_json()["invoice_date"] == "2026-06-29"

    ambiguous = migrated_client.patch(
        f"/api/invoices/{payload['id']}", json={"invoice_date": "6/11/26"}
    )
    assert ambiguous.status_code == 400
    assert ambiguous.get_json()["error"]["details"]["field"] == "invoice_date"


def test_unknown_fields_are_rejected(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.patch(
        f"/api/invoices/{payload['id']}", json={"supplier": "Costco"}
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["unknown_fields"] == ["supplier"]


def test_adding_a_line_by_hand(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client, recognize="false")
    food_item_id = create_food_item(migrated_client)

    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/lines",
        json={
            "description": "手工补的一行",
            "quantity": "3",
            "purchase_unit": "lb",
            "unit_price": "4.00",
            "line_amount": "12.00",
            "food_item_id": food_item_id,
        },
    )

    assert response.status_code == 201
    body = response.get_json()
    assert (body["line_no"], body["status"], body["missing_fields"]) == (1, "pending", [])


def test_json_floats_are_rejected_on_lines(migrated_client: FlaskClient) -> None:
    """与步骤 04 同一个口径：JSON 小数在序列化前已经过了 double，只能拒绝。"""
    payload = post_invoice(migrated_client, recognize="false")

    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/lines", json={"quantity": 12.345}
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "quantity"


def test_skipping_and_reopening_a_line(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    line_id = payload["lines"][1]["id"]

    skipped = migrated_client.patch(
        f"/api/invoices/{payload['id']}/lines/{line_id}",
        json={"status": "skipped", "skip_reason": "手写备注，不是食材"},
    )
    assert skipped.status_code == 200
    assert skipped.get_json()["skip_reason"] == "手写备注，不是食材"

    reopened = migrated_client.patch(
        f"/api/invoices/{payload['id']}/lines/{line_id}", json={"status": "pending"}
    )
    assert reopened.get_json()["skip_reason"] is None


def test_deleting_a_manual_line(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client, recognize="false")
    created = migrated_client.post(
        f"/api/invoices/{payload['id']}/lines", json={"description": "加错了"}
    ).get_json()

    response = migrated_client.delete(
        f"/api/invoices/{payload['id']}/lines/{created['id']}"
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "deleted"


def test_a_line_from_another_invoice_is_not_found(migrated_client: FlaskClient) -> None:
    first = post_invoice(migrated_client)
    second = post_invoice(migrated_client, content=OTHER_PHOTO, name="b.png")

    response = migrated_client.patch(
        f"/api/invoices/{first['id']}/lines/{second['lines'][0]['id']}",
        json={"quantity": "1"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_unknown_invoice_is_not_found(migrated_client: FlaskClient) -> None:
    response = migrated_client.get("/api/invoices/99999")

    assert response.status_code == 404


# --- 确认 -------------------------------------------------------------------


def test_partial_confirmation_returns_200_with_per_line_results(
    migrated_client: FlaskClient,
) -> None:
    """三行确认两行是**正常结果**：HTTP 200，失败的那行在 results 里说明原因。"""
    payload = post_invoice(migrated_client)
    food_item_id = create_food_item(migrated_client)
    first, second = payload["lines"]

    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={
            "lines": [
                {"line_id": first["id"], "food_item_id": food_item_id},
                {"line_id": second["id"], "food_item_id": food_item_id},
            ]
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert (body["confirmed"], body["failed"]) == (1, 1)
    assert body["results"][0]["ok"] is True
    assert body["results"][0]["purchase_id"] is not None
    assert body["results"][1]["error"]["details"]["missing_fields"] == ["unit_price"]
    assert body["invoice"]["status"] == "in_review"


def test_confirm_all(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    food_item_id = create_food_item(migrated_client)
    for line in payload["lines"]:
        migrated_client.patch(
            f"/api/invoices/{payload['id']}/lines/{line['id']}",
            json={"food_item_id": food_item_id},
        )
    migrated_client.patch(
        f"/api/invoices/{payload['id']}/lines/{payload['lines'][1]['id']}",
        json={"unit_price": "113.00"},
    )

    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm", json={"all": True}
    )

    assert response.status_code == 200
    assert response.get_json()["confirmed"] == 2
    assert response.get_json()["invoice"]["status"] == "ready_to_complete"


@pytest.mark.parametrize(
    "body", [{}, {"all": True, "lines": [{"line_id": 1}]}, {"lines": []}]
)
def test_confirm_needs_exactly_one_way_to_pick_lines(
    migrated_client: FlaskClient, body: dict[str, Any]
) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm", json=body
    )

    assert response.status_code == 400


def test_amount_mismatch_comes_back_with_its_own_code(
    migrated_client: FlaskClient,
) -> None:
    payload = post_invoice(migrated_client)
    food_item_id = create_food_item(migrated_client)
    line_id = payload["lines"][0]["id"]
    migrated_client.patch(
        f"/api/invoices/{payload['id']}/lines/{line_id}", json={"line_amount": "75.00"}
    )

    refused = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={"lines": [{"line_id": line_id, "food_item_id": food_item_id}]},
    ).get_json()
    accepted = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={
            "lines": [
                {
                    "line_id": line_id,
                    "food_item_id": food_item_id,
                    "accept_amount_mismatch": True,
                }
            ]
        },
    ).get_json()

    assert refused["results"][0]["error"]["code"] == "amount_mismatch"
    assert refused["results"][0]["error"]["details"]["expected"] == "60.00"
    assert accepted["confirmed"] == 1


def test_a_new_food_item_can_be_created_while_confirming(
    migrated_client: FlaskClient,
) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={
            "lines": [
                {
                    "line_id": payload["lines"][0]["id"],
                    "new_food_item": {
                        "name": "Jasmine Rice",
                        "category_id": 5,
                        "base_unit": "lb",
                    },
                }
            ]
        },
    )

    assert response.get_json()["confirmed"] == 1
    found = migrated_client.get("/api/food-items?q=Jasmine Rice").get_json()
    assert found["total"] == 1


def test_confirm_line_payload_is_validated(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={"lines": [{"line_id": "1"}]},
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "line_id"


def test_unconfirming_through_the_api(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    food_item_id = create_food_item(migrated_client)
    line_id = payload["lines"][0]["id"]
    confirmed = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={"lines": [{"line_id": line_id, "food_item_id": food_item_id}]},
    ).get_json()
    purchase_id = confirmed["results"][0]["purchase_id"]

    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/lines/{line_id}/unconfirm",
        json={"reason": "食材认错了"},
    )

    assert response.status_code == 200
    assert response.get_json()["line"]["status"] == "pending"
    purchase = migrated_client.get(f"/api/purchases/{purchase_id}").get_json()
    assert purchase["voided_at"] is not None


# --- 追溯、收口与作废 -------------------------------------------------------


def test_a_purchase_links_back_to_its_invoice(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    food_item_id = create_food_item(migrated_client)
    confirmed = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={
            "lines": [
                {"line_id": payload["lines"][0]["id"], "food_item_id": food_item_id}
            ]
        },
    ).get_json()

    purchase = migrated_client.get(
        f"/api/purchases/{confirmed['results'][0]['purchase_id']}"
    ).get_json()

    assert purchase["source_invoice"]["invoice_id"] == payload["id"]
    assert purchase["source_invoice"]["line_no"] == 1
    assert purchase["entry_source"] == "invoice"
    # 顺着这个 id 就能回到原件
    assert (
        migrated_client.get(
            f"/api/invoices/{purchase['source_invoice']['invoice_id']}"
        ).status_code
        == 200
    )


def test_completing_reports_the_counts(migrated_client: FlaskClient) -> None:
    """收口确认要把数字摆出来：这是漏行唯一会被拦住的时刻。"""
    payload = post_invoice(migrated_client)
    migrated_client.patch(f"/api/invoices/{payload['id']}", json={"invoice_date": "2026-11-08"})
    food_item_id = create_food_item(migrated_client)
    migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={
            "lines": [
                {"line_id": payload["lines"][0]["id"], "food_item_id": food_item_id}
            ]
        },
    )
    migrated_client.patch(
        f"/api/invoices/{payload['id']}/lines/{payload['lines'][1]['id']}",
        json={"status": "skipped", "skip_reason": "认不出"},
    )

    response = migrated_client.post(f"/api/invoices/{payload['id']}/complete")

    assert response.status_code == 200
    body = response.get_json()
    assert "已确认 1 行" in body["message"]
    assert "跳过 1 行" in body["message"]
    assert "1 个原始文件" in body["message"]
    assert body["invoice"]["status"] == "completed"

    reopened = migrated_client.post(f"/api/invoices/{payload['id']}/reopen")
    assert reopened.status_code == 200
    assert reopened.get_json()["status"] == "ready_to_complete"
    assert reopened.get_json()["completed_at"] is None
    assert reopened.get_json()["lines"][0]["status"] == "confirmed"
    assert migrated_client.post(f"/api/invoices/{payload['id']}/reopen").status_code == 409
    assert migrated_client.post(f"/api/invoices/{payload['id']}/complete").status_code == 200


def test_reviewed_header_fields_are_saved_separately_from_original_evidence(
    migrated_client: FlaskClient,
) -> None:
    payload = post_invoice(migrated_client)
    original = payload["recognition"]["raw_document_fields"]
    response = migrated_client.patch(
        f"/api/invoices/{payload['id']}",
        json={"invoice_date": "2026-11-08", "invoice_total": "60.00"},
    )

    assert response.status_code == 200
    assert response.get_json()["header_reviewed_fields"] == ["invoice_date", "invoice_total"]
    assert response.get_json()["recognition"]["raw_document_fields"] == original


def test_completing_with_pending_lines_is_refused(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.post(f"/api/invoices/{payload['id']}/complete")

    assert response.status_code == 409
    assert response.get_json()["error"]["details"]["pending_line_nos"] == [1, 2]


def test_completed_invoice_source_correction_updates_booked_source(
    migrated_client: FlaskClient,
) -> None:
    payload = post_invoice(migrated_client)
    invoice_id = payload["id"]
    migrated_client.patch(f"/api/invoices/{invoice_id}", json={"source_name": "Original", "invoice_date": "2026-11-08"})
    food_item_id = create_food_item(migrated_client)
    migrated_client.post(f"/api/invoices/{invoice_id}/confirm", json={"lines": [
        {"line_id": payload["lines"][0]["id"], "food_item_id": food_item_id}
    ]})
    migrated_client.patch(
        f"/api/invoices/{invoice_id}/lines/{payload['lines'][1]['id']}",
        json={"status": "skipped", "skip_reason": "unreadable"},
    )
    assert migrated_client.post(f"/api/invoices/{invoice_id}/complete").status_code == 200

    response = migrated_client.patch(
        f"/api/invoices/{invoice_id}",
        json={"source_name": "Renamed", "invoice_date": "2026-11-09"},
    )
    assert response.status_code == 200
    assert response.get_json()["source_name"] == "Renamed"
    purchase = migrated_client.get("/api/purchases").get_json()["items"][0]
    assert purchase["source_name"] == "Renamed"
    assert purchase["purchase_date"] == "2026-11-09"
    history = migrated_client.get(f"/api/prices/food-items/{food_item_id}/history").get_json()
    assert history["items"][0]["purchase"]["purchase_date"] == "2026-11-09"


def test_voiding_an_invoice(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)

    response = migrated_client.delete(f"/api/invoices/{payload['id']}")

    assert response.status_code == 200
    assert response.get_json()["status"] == "voided"
    assert migrated_client.get("/api/invoices").get_json()["total"] == 0
    assert (
        migrated_client.get("/api/invoices?include_voided=true").get_json()["total"] == 1
    )


def test_an_invoice_with_confirmed_lines_cannot_be_voided(
    migrated_client: FlaskClient,
) -> None:
    payload = post_invoice(migrated_client)
    food_item_id = create_food_item(migrated_client)
    migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={
            "lines": [
                {"line_id": payload["lines"][0]["id"], "food_item_id": food_item_id}
            ]
        },
    )

    response = migrated_client.delete(f"/api/invoices/{payload['id']}")

    assert response.status_code == 409
    assert response.get_json()["error"]["details"]["confirmed_line_nos"] == [1]


def test_first_purchase_creates_food_item_from_line_unit(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={"lines": [{"line_id": payload["lines"][0]["id"], "new_food_item": {
            "name": "First Purchase", "category_id": 5, "use_purchase_unit_as_base": True,
        }}]},
    )
    assert response.status_code == 200
    assert response.get_json()["results"][0]["ok"] is True
    item = migrated_client.get("/api/food-items?q=First%20Purchase").get_json()["items"][0]
    assert item["base_unit"] == "lb"


def test_first_purchase_with_case_keeps_original_unit_without_inventing_a_price(
    migrated_client: FlaskClient,
) -> None:
    payload = post_invoice(migrated_client)
    line_id = payload["lines"][0]["id"]
    migrated_client.patch(
        f"/api/invoices/{payload['id']}/lines/{line_id}",
        json={"purchase_unit": "case"},
    )
    response = migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={"lines": [{"line_id": line_id, "new_food_item": {
            "name": "First Case", "category_id": 5, "use_purchase_unit_as_base": True,
        }}]},
    )
    assert response.get_json()["results"][0]["ok"] is True
    item = migrated_client.get("/api/food-items?q=First%20Case").get_json()["items"][0]
    assert item["base_unit"] == "case"
    history = migrated_client.get(f"/api/prices/food-items/{item['id']}/history")
    assert history.status_code == 200
    assert history.get_json()["items"][0]["conversion"]["status"] == "pending_pack_spec"


def test_delete_confirmed_invoice_requires_matching_line_ids(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    line_id = payload["lines"][0]["id"]
    food_item_id = create_food_item(migrated_client)
    migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={"lines": [{"line_id": line_id, "food_item_id": food_item_id}]},
    )
    url = f"/api/invoices/{payload['id']}"
    assert migrated_client.delete(url, json={"confirmed_line_ids": []}).status_code == 409
    assert migrated_client.delete(url, json={"confirmed_line_ids": [True]}).status_code == 400

    response = migrated_client.delete(url, json={"confirmed_line_ids": [line_id]})

    assert response.status_code == 200
    assert response.get_json()["invoice"]["status"] == "voided"
    assert migrated_client.get("/api/purchases").get_json()["total"] == 0
    assert migrated_client.get("/api/purchases?include_voided=true").get_json()["total"] == 1


# --- 列表与匹配 -------------------------------------------------------------


def test_the_list_paginates(migrated_client: FlaskClient) -> None:
    for index in range(3):
        post_invoice(migrated_client, content=f"photo {index}".encode(), name=f"p{index}.png")

    body = migrated_client.get("/api/invoices?limit=2").get_json()

    assert (len(body["items"]), body["total"], body["limit"]) == (2, 3, 2)


def _dated(client: FlaskClient, *, day: str | None, source: str, index: int) -> int:
    """建一张带日期与来源的单据，返回它的 id。"""
    invoice_id = post_invoice(
        client, content=f"photo {index}".encode(), name=f"p{index}.png"
    )["id"]
    response = client.patch(
        f"/api/invoices/{invoice_id}",
        json={"invoice_date": day, "source_name": source},
    )
    assert response.status_code == 200, response.get_json()
    return invoice_id


def test_the_list_filters_by_purchase_date_range(migrated_client: FlaskClient) -> None:
    """某天 / 某周 / 某月都是同一个闭区间参数的不同宽度。"""
    june_03 = _dated(migrated_client, day="2026-06-03", source="Costco", index=0)
    june_08 = _dated(migrated_client, day="2026-06-08", source="Costco", index=1)
    july_01 = _dated(migrated_client, day="2026-07-01", source="Costco", index=2)

    one_day = migrated_client.get("/api/invoices?date_from=2026-06-08&date_to=2026-06-08")
    assert [item["id"] for item in one_day.get_json()["items"]] == [june_08]

    one_month = migrated_client.get("/api/invoices?date_from=2026-06-01&date_to=2026-06-30")
    assert {item["id"] for item in one_month.get_json()["items"]} == {june_03, june_08}
    assert one_month.get_json()["total"] == 2

    open_end = migrated_client.get("/api/invoices?date_from=2026-07-01")
    assert [item["id"] for item in open_end.get_json()["items"]] == [july_01]


def test_a_date_range_excludes_invoices_without_a_date(
    migrated_client: FlaskClient,
) -> None:
    """没有日期的单据落在任何区间之外——这是期望行为，不是漏行。"""
    dated = _dated(migrated_client, day="2026-06-03", source="Costco", index=0)
    post_invoice(migrated_client, content=b"no date", name="nodate.png")

    june = migrated_client.get("/api/invoices?date_from=2026-06-01&date_to=2026-06-30")
    assert [item["id"] for item in june.get_json()["items"]] == [dated]

    assert migrated_client.get("/api/invoices?date_from=06/08/2026").status_code == 400
    assert migrated_client.get(
        "/api/invoices?date_from=2026-07-01&date_to=2026-06-01"
    ).status_code == 400


def test_the_two_list_orders_answer_two_different_questions(
    migrated_client: FlaskClient,
) -> None:
    """录入次序与采购日期次序**故意**相反，这样两种排法才区分得开。

    六月一日补录一张五月二十二日的单子，两者差着十天；用一个键冒充另一个，
    「最近录了什么」和「这批货是哪天进的」就会是同一份名单。
    """
    newest_purchase = _dated(migrated_client, day="2026-06-08", source="Costco", index=0)
    middle = _dated(migrated_client, day="2026-06-03", source="Costco", index=1)
    oldest_purchase = _dated(migrated_client, day="2026-05-22", source="Costco", index=2)
    undated = post_invoice(migrated_client, content=b"no date", name="nodate.png")["id"]

    # 默认（待办清单）：刚录进来的在最前，与单据上的日期无关。
    by_creation = [item["id"] for item in migrated_client.get("/api/invoices").get_json()["items"]]
    assert by_creation == [undated, oldest_purchase, middle, newest_purchase]
    assert by_creation == [item["id"] for item in
                           migrated_client.get("/api/invoices?order=created_at").get_json()["items"]]

    # 按采购日期：日期待填排最前，其余按单据上的日期倒序。
    by_date = [item["id"] for item in
               migrated_client.get("/api/invoices?order=invoice_date").get_json()["items"]]
    assert by_date == [undated, newest_purchase, middle, oldest_purchase]

    assert migrated_client.get("/api/invoices?order=uploaded_at").status_code == 400


def test_match_endpoint_returns_candidates(migrated_client: FlaskClient) -> None:
    payload = post_invoice(migrated_client)
    food_item_id = create_food_item(migrated_client)
    description = payload["lines"][0]["description"]
    migrated_client.post(
        f"/api/invoices/{payload['id']}/confirm",
        json={
            "lines": [
                {"line_id": payload["lines"][0]["id"], "food_item_id": food_item_id}
            ]
        },
    )

    body = migrated_client.get(
        "/api/invoices/match", query_string={"description": description}
    ).get_json()

    assert [item["food_item"]["id"] for item in body["candidates"]] == [food_item_id]
    assert body["candidates"][0]["purchase_count"] == 1


def test_match_needs_a_description(migrated_client: FlaskClient) -> None:
    response = migrated_client.get("/api/invoices/match")

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "description"


def test_candidates_show_up_on_a_later_draft(migrated_client: FlaskClient) -> None:
    """同一批供应商单据第二次录入时，行上就带着候选了（**不自动确认**）。

    ⚠️ **步骤 10.C 改了这里的口径**：以前第二次录入只给候选、食材格子留空，
    现在「老板确认过的那句描述」会**直接选中**（零点击）。没变的是：行仍然是
    `pending`，确认请求还得老板自己点——与 05.6 的「预填候选可以，替老板点确认
    不行」一致。
    """
    first = post_invoice(migrated_client)
    food_item_id = create_food_item(migrated_client)
    migrated_client.post(
        f"/api/invoices/{first['id']}/confirm",
        json={
            "lines": [{"line_id": first["lines"][0]["id"], "food_item_id": food_item_id}]
        },
    )

    second = post_invoice(migrated_client, content=OTHER_PHOTO, name="b.png")
    line = second["lines"][0]

    assert line["food_item"]["id"] == food_item_id
    assert line["status"] == "pending"
    assert [c["food_item"]["id"] for c in line["match_candidates"]] == [food_item_id]


def test_a_confirmed_description_outranks_the_keyword_guess(
    migrated_client: FlaskClient,
) -> None:
    """⚠️ 这一条拦的是一个**静默的错**（步骤 10.C 实现时发现的）。

    第一张单据会按关键词自动建一条食材（Fake 识别器那行描述里的关键词）。
    老板随后把同一句描述确认成了**另一条**食材——那是他亲手做的判断。如果关键词
    匹配排在已确认映射之前，第二张单据就会替他选回那条自动建的，而界面上看起来
    就像是他自己选的。`imp_notes.md` 4.8 的原话也是「优先复用已确认的映射」。
    """
    first = post_invoice(migrated_client)
    auto_created = first["lines"][0]["food_item"]
    assert auto_created is not None, "第一张单据应当按关键词自动建出一条食材"
    assert auto_created["needs_review"] is True

    chosen = create_food_item(migrated_client, name="Rice")
    assert chosen != auto_created["id"]
    migrated_client.post(
        f"/api/invoices/{first['id']}/confirm",
        json={"lines": [{"line_id": first["lines"][0]["id"], "food_item_id": chosen}]},
    )

    second = post_invoice(migrated_client, content=OTHER_PHOTO, name="b.png")

    assert second["lines"][0]["food_item"]["id"] == chosen
