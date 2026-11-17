"""采购记录接口测试（步骤 04）。

与 `test_purchases_service.py` 的分工：那边验证业务规则，这边验证 HTTP 口径——
状态码、错误码、字段形状，以及「数值在 JSON 里是字符串」这一条。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from flask.testing import FlaskClient

LINE: dict[str, Any] = {
    "quantity": "10",
    "purchase_unit": "lb",
    "unit_price": "6.00",
    "line_amount": "60.00",
    "purchase_date": "2026-11-08",
}


@pytest.fixture
def item_id(migrated_client: FlaskClient) -> int:
    """经接口建一条食材，拿到它的 id。"""
    response = migrated_client.post(
        "/api/food-items",
        json={
            "name": "Ground Beef",
            "name_zh": "牛肉末",
            "specification": "80/20",
            "category_id": 1,
            "base_unit": "lb",
        },
    )
    assert response.status_code == 201
    return response.get_json()["id"]


def post_purchase(
    client: FlaskClient, item_id: int, **overrides: Any
) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = {"food_item_id": item_id, **LINE}
    payload.update(overrides)
    response = client.post("/api/purchases", json=payload)
    return response.status_code, response.get_json()


# --- 录入 -------------------------------------------------------------------


def test_manual_entry_returns_the_saved_record(
    migrated_client: FlaskClient, item_id: int
) -> None:
    status, body = post_purchase(
        migrated_client,
        item_id,
        source_name="Costco",
        original_description="GRND BF 80/20",
    )

    assert status == 201
    assert body["food_item"]["id"] == item_id
    assert body["food_item"]["name_zh"] == "牛肉末"
    assert body["purchase_date"] == "2026-11-08"
    assert body["source_name"] == "Costco"
    assert body["original_description"] == "GRND BF 80/20"
    assert body["purchase_unit"] == "lb"
    assert body["purchase_unit_normalized"] == "lb"
    assert body["currency"] == "USD"
    assert body["amount_mismatch"] is False
    assert body["entry_source"] == "manual"
    assert body["voided_at"] is None
    assert body["revision_count"] == 0


def test_numbers_are_json_strings_not_floats(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """JSON 的数字是双精度浮点，`1234567.89` 经它一转就可能差一分钱。

    所以三个数值列在响应里是字符串——精确值一路走到界面上。
    """
    _, body = post_purchase(
        migrated_client,
        item_id,
        quantity="12.345",
        unit_price="0.07",
        line_amount="0.86",
    )

    for key in ("quantity", "unit_price", "line_amount"):
        assert isinstance(body[key], str), f"{key} 应该是字符串"
    assert body["quantity"] == "12.345"
    assert body["unit_price"] == "0.07"
    assert body["line_amount"] == "0.86"


def test_source_invoice_placeholder_is_present_and_null(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """发票关联列在步骤 05 才建；字段先占位，那一步就不用改响应结构。"""
    _, body = post_purchase(migrated_client, item_id)

    assert "source_invoice" in body
    assert body["source_invoice"] is None


def test_quick_entry_needs_only_five_fields(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """日期、来源、原始描述、币种都可以不填。"""
    response = migrated_client.post(
        "/api/purchases",
        json={
            "food_item_id": item_id,
            "quantity": "10",
            "purchase_unit": "lb",
            "unit_price": "6.00",
            "line_amount": "60.00",
        },
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["purchase_date"] is None
    assert body["source_name"] is None
    assert body["currency"] == "USD"


@pytest.mark.parametrize(
    "missing", ["quantity", "purchase_unit", "unit_price", "line_amount"]
)
def test_missing_required_fields_are_reported(
    migrated_client: FlaskClient, item_id: int, missing: str
) -> None:
    payload: dict[str, Any] = {"food_item_id": item_id, **LINE}
    payload.pop(missing)

    response = migrated_client.post("/api/purchases", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"]["code"] == "validation_error"
    assert missing in body["error"]["details"]["missing_fields"]


def test_purchase_line_without_a_food_item_is_rejected_by_the_service(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """`food_item_id` 不再是「形状」层的必填项——调整行可以不填。

    所以正常采购行漏填它时，报错来自 Service（它才知道 line_kind），
    走的是 `details.field` 而不是 `details.missing_fields`。
    """
    response = migrated_client.post("/api/purchases", json=dict(LINE))

    assert response.status_code == 400
    details = response.get_json()["error"]["details"]
    assert details["field"] == "food_item_id"
    assert "missing_fields" not in details


def test_unknown_fields_are_rejected(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """写错字段名时忽略，意味着老板填的东西被静默丢掉。"""
    status, body = post_purchase(migrated_client, item_id, unitPrice="6.00")

    assert status == 400
    assert body["error"]["details"]["unknown_fields"] == ["unitPrice"]


def test_json_float_is_rejected_through_the_api(
    migrated_client: FlaskClient, item_id: int
) -> None:
    status, body = post_purchase(migrated_client, item_id, quantity=12.345)

    assert status == 400
    assert "JSON 小数" in body["error"]["message"]


def test_unknown_food_item_is_a_400(
    migrated_client: FlaskClient, item_id: int
) -> None:
    status, body = post_purchase(migrated_client, item_id, food_item_id=99999)

    assert status == 400
    assert body["error"]["details"]["field"] == "food_item_id"


# --- 金额不符（04.4）--------------------------------------------------------


def test_amount_mismatch_has_its_own_error_code(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """独立错误码让前端分得清「字段写错了」和「单据就是这样，确认吗」。"""
    status, body = post_purchase(
        migrated_client, item_id, quantity="3", unit_price="10.00", line_amount="35.00"
    )

    assert status == 400
    assert body["error"]["code"] == "amount_mismatch"
    details = body["error"]["details"]
    assert details["expected"] == "30.00"
    assert details["line_amount"] == "35.00"
    assert details["difference"] == "5.00"
    assert details["tolerance"] == "0.05"


def test_confirming_the_mismatch_saves_the_invoice_amount(
    migrated_client: FlaskClient, item_id: int
) -> None:
    status, body = post_purchase(
        migrated_client,
        item_id,
        quantity="3",
        unit_price="10.00",
        line_amount="35.00",
        accept_amount_mismatch=True,
    )

    assert status == 201
    assert body["line_amount"] == "35.00"  # 行金额为权威，不反算成 30
    assert body["amount_mismatch"] is True


def test_accept_flag_must_be_a_boolean(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """不做宽松解释：`"true"` 这种写法容易把「确认过」和「没确认」搞混。"""
    status, body = post_purchase(
        migrated_client, item_id, accept_amount_mismatch="true"
    )

    assert status == 400
    assert body["error"]["details"]["field"] == "accept_amount_mismatch"


# --- 查询（04.5）------------------------------------------------------------


def test_history_lists_newest_first_with_pagination(
    migrated_client: FlaskClient, item_id: int
) -> None:
    # 日期都在今天之前：未来日期会被拒（见 test_future_dates_are_rejected）。
    # 行金额写成字面量而不是 price * 10 —— 这个模块测的就是不经浮点算钱。
    for day, price, amount in (
        ("01", "5.50", "55.00"),
        ("05", "5.80", "58.00"),
        ("08", "6.00", "60.00"),
    ):
        post_purchase(
            migrated_client,
            item_id,
            purchase_date=f"2026-11-{day}",
            unit_price=price,
            line_amount=amount,
        )

    response = migrated_client.get(f"/api/purchases?food_item_id={item_id}&limit=2")

    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [item["unit_price"] for item in body["items"]] == ["6.00", "5.80"]


def test_undated_filter_lists_the_records_to_fix(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(migrated_client, item_id)
    _, undated = post_purchase(migrated_client, item_id, purchase_date=None)

    response = migrated_client.get("/api/purchases?undated=true")

    body = response.get_json()
    assert [item["id"] for item in body["items"]] == [undated["id"]]


def test_date_range_filter_through_the_api(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(migrated_client, item_id, purchase_date="2026-09-30")
    _, inside = post_purchase(migrated_client, item_id, purchase_date="2026-10-15")

    response = migrated_client.get(
        "/api/purchases?date_from=2026-10-01&date_to=2026-10-31"
    )

    assert [item["id"] for item in response.get_json()["items"]] == [inside["id"]]


@pytest.mark.parametrize("raw", ["maybe", "2"])
def test_bad_boolean_query_params_are_rejected(
    migrated_client: FlaskClient, raw: str
) -> None:
    response = migrated_client.get(f"/api/purchases?undated={raw}")

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "undated"


def test_single_record_and_missing_record(
    migrated_client: FlaskClient, item_id: int
) -> None:
    _, created = post_purchase(migrated_client, item_id)

    found = migrated_client.get(f"/api/purchases/{created['id']}")
    missing = migrated_client.get("/api/purchases/99999")

    assert found.status_code == 200
    assert found.get_json()["id"] == created["id"]
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "not_found"


def test_non_integer_id_is_a_404_from_the_router(
    migrated_client: FlaskClient,
) -> None:
    """`<int:...>` 转换器拦在前面，脏 id 进不到 Service。"""
    assert migrated_client.get("/api/purchases/abc").status_code == 404


# --- 纠错与修改历史（04.6）--------------------------------------------------


def test_patch_updates_in_place_and_logs_the_change(
    migrated_client: FlaskClient, item_id: int
) -> None:
    _, created = post_purchase(
        migrated_client, item_id, unit_price="5.80", line_amount="58.00"
    )

    patched = migrated_client.patch(
        f"/api/purchases/{created['id']}",
        json={
            "unit_price": "8.50",
            "line_amount": "85.00",
            "reason": "单价看错了",
        },
    )

    assert patched.status_code == 200
    body = patched.get_json()
    assert body["line_amount"] == "85.00"
    assert body["revision_count"] == 1

    history = migrated_client.get(f"/api/purchases/{created['id']}/revisions")
    assert history.status_code == 200
    (revision,) = history.get_json()["items"]
    assert revision["action"] == "update"
    assert revision["reason"] == "单价看错了"
    assert revision["changed_fields"]["line_amount"] == {
        "before": "58.00",
        "after": "85.00",
    }


def test_patch_can_fix_the_matched_food_item(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """最常见的纠错：匹配错了食材，直接改。"""
    other = migrated_client.post(
        "/api/food-items",
        json={"name": "Beef Tendon", "category_id": 1, "base_unit": "lb"},
    ).get_json()["id"]
    _, created = post_purchase(migrated_client, item_id)

    patched = migrated_client.patch(
        f"/api/purchases/{created['id']}", json={"food_item_id": other}
    )

    assert patched.status_code == 200
    assert patched.get_json()["food_item"]["id"] == other


def test_empty_patch_body_is_rejected(
    migrated_client: FlaskClient, item_id: int
) -> None:
    _, created = post_purchase(migrated_client, item_id)

    response = migrated_client.patch(f"/api/purchases/{created['id']}", json={})

    assert response.status_code == 400


def test_revisions_of_a_missing_purchase_is_a_404(
    migrated_client: FlaskClient,
) -> None:
    assert migrated_client.get("/api/purchases/99999/revisions").status_code == 404


# --- 作废（04.6）-----------------------------------------------------------


def test_delete_voids_instead_of_removing(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """返回 200 而不是 204：记录没被删掉，只是改了状态。"""
    _, created = post_purchase(migrated_client, item_id)

    voided = migrated_client.delete(
        f"/api/purchases/{created['id']}", json={"reason": "重复录入"}
    )

    assert voided.status_code == 200
    assert voided.get_json()["status"] == "voided"

    assert migrated_client.get("/api/purchases").get_json()["total"] == 0
    assert (
        migrated_client.get("/api/purchases?include_voided=true").get_json()["total"]
        == 1
    )
    still_there = migrated_client.get(f"/api/purchases/{created['id']}")
    assert still_there.status_code == 200
    assert still_there.get_json()["voided_at"] is not None


def test_voiding_twice_is_a_409(
    migrated_client: FlaskClient, item_id: int
) -> None:
    _, created = post_purchase(migrated_client, item_id)
    migrated_client.delete(f"/api/purchases/{created['id']}")

    again = migrated_client.delete(f"/api/purchases/{created['id']}")

    assert again.status_code == 409
    assert again.get_json()["error"]["code"] == "conflict"


def test_patching_a_voided_record_is_a_409(
    migrated_client: FlaskClient, item_id: int
) -> None:
    _, created = post_purchase(migrated_client, item_id)
    migrated_client.delete(f"/api/purchases/{created['id']}")

    response = migrated_client.patch(
        f"/api/purchases/{created['id']}", json={"unit_price": "7.00"}
    )

    assert response.status_code == 409


def test_delete_without_a_body_works(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """作废原因是可选的，不带请求体也应该能作废。"""
    _, created = post_purchase(migrated_client, item_id)

    assert migrated_client.delete(f"/api/purchases/{created['id']}").status_code == 200


# --- 调整行（退货 / 折让）---------------------------------------------------

ADJUSTMENT: dict[str, Any] = {
    "line_kind": "adjustment",
    "quantity": "-2",
    "purchase_unit": "lb",
    "unit_price": "6.00",
    "line_amount": "-12.00",
    "purchase_date": "2026-11-08",
}


def post_adjustment(
    client: FlaskClient, item_id: int | None = None, **overrides: Any
) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = dict(ADJUSTMENT)
    if item_id is not None:
        payload["food_item_id"] = item_id
    payload.update(overrides)
    response = client.post("/api/purchases", json=payload)
    return response.status_code, response.get_json()


def test_return_line_is_accepted_with_negative_values(
    migrated_client: FlaskClient, item_id: int
) -> None:
    status, body = post_adjustment(migrated_client, item_id)

    assert status == 201
    assert body["line_kind"] == "adjustment"
    assert body["quantity"] == "-2"
    assert body["line_amount"] == "-12.00"
    assert body["amount_mismatch"] is False


def test_whole_invoice_discount_has_a_null_food_item(
    migrated_client: FlaskClient,
) -> None:
    """整单折让不指向任何一种食材，`food_item` 为 null。"""
    status, body = post_adjustment(
        migrated_client,
        None,
        quantity="1",
        unit_price="-20.00",
        line_amount="-20.00",
        original_description="EARLY PAY DISCOUNT 2%",
    )

    assert status == 201
    assert body["food_item"] is None
    assert body["line_amount"] == "-20.00"


def test_negative_values_on_a_purchase_line_point_at_adjustment(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """正常采购行收到负数时，错误信息要告诉人该怎么录，而不是只说「不行」。"""
    status, body = post_purchase(migrated_client, item_id, line_amount="-60.00")

    assert status == 400
    assert "adjustment" in body["error"]["message"]
    assert body["error"]["details"]["hint_line_kind"] == "adjustment"


def test_adjustments_are_hidden_from_the_default_listing(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """默认不返回调整行：混进价格列表会让人算出偏低的价格而不自知。"""
    _, bought = post_purchase(migrated_client, item_id)
    _, returned = post_adjustment(migrated_client, item_id)

    default = migrated_client.get("/api/purchases").get_json()
    only_adj = migrated_client.get("/api/purchases?kind=adjustment").get_json()
    everything = migrated_client.get("/api/purchases?kind=all").get_json()

    assert [i["id"] for i in default["items"]] == [bought["id"]]
    assert [i["id"] for i in only_adj["items"]] == [returned["id"]]
    assert {i["id"] for i in everything["items"]} == {bought["id"], returned["id"]}


def test_reconciliation_adds_up_with_kind_all(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """对账口径：kind=all 时各行相加等于单据总额（$60 − $12 = $48）。"""
    post_purchase(migrated_client, item_id)
    post_adjustment(migrated_client, item_id)

    body = migrated_client.get("/api/purchases?kind=all").get_json()
    total = sum(Decimal(item["line_amount"]) for item in body["items"])

    assert total == Decimal("48.00")


def test_bad_kind_query_param_is_rejected(migrated_client: FlaskClient) -> None:
    response = migrated_client.get("/api/purchases?kind=refunds")

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "kind"


def test_line_kind_cannot_be_patched(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """录错了类别请作废重录；`line_kind` 不在可改字段里。"""
    _, created = post_purchase(migrated_client, item_id)

    response = migrated_client.patch(
        f"/api/purchases/{created['id']}", json={"line_kind": "adjustment"}
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["unknown_fields"] == ["line_kind"]


def test_adjustment_can_be_voided_and_still_reconciled(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """作废的调整行默认不出现，但 include_voided 仍能查到（追溯不丢）。"""
    _, returned = post_adjustment(migrated_client, item_id)

    assert migrated_client.delete(f"/api/purchases/{returned['id']}").status_code == 200
    assert migrated_client.get("/api/purchases?kind=all").get_json()["total"] == 0
    assert (
        migrated_client.get(
            "/api/purchases?kind=all&include_voided=true"
        ).get_json()["total"]
        == 1
    )


# --- 与食材接口的衔接 -------------------------------------------------------


def test_food_item_referenced_by_a_purchase_cannot_be_deleted(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """步骤 03 写的「被采购记录引用时 409」现在真有引用方了。"""
    post_purchase(migrated_client, item_id)

    response = migrated_client.delete(f"/api/food-items/{item_id}")

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "conflict"
