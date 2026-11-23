"""价格统计接口测试（步骤 09）。

与 `test_prices_service.py` 的分工：那边验证业务规则与算例，这边验证 HTTP 口径
——状态码、错误码、字段形状，以及三条输出约定：

1. 数值在 JSON 里是**字符串**（沿用步骤 04）；
2. 算不出来的值是 `null`，**不是 0**；
3. 每个统计响应都带 `excluded` 与 `notes`。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from flask.testing import FlaskClient

from src.services.prices import month_start, previous_month_start, week_start


def this_month_day_one() -> str:
    return month_start(date.today()).isoformat()


def last_month_day_one() -> str:
    return previous_month_start(date.today()).isoformat()


@pytest.fixture
def item_id(migrated_client: FlaskClient) -> int:
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


def post_purchase(client: FlaskClient, item_id: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "food_item_id": item_id,
        "quantity": "10",
        "purchase_unit": "lb",
        "unit_price": "6.00",
        "line_amount": "60.00",
        "purchase_date": this_month_day_one(),
    }
    payload.update(overrides)
    response = client.post("/api/purchases", json=payload)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


# --- 单位目录 ---------------------------------------------------------------


def test_units_endpoint_states_the_system_and_the_rules(
    migrated_client: FlaskClient,
) -> None:
    response = migrated_client.get("/api/prices/units")

    assert response.status_code == 200
    body = response.get_json()
    assert body["volume_system"] == "US"
    assert {entry["name"] for entry in body["dimensions"]} == {
        "mass",
        "volume",
        "count",
    }
    assert "case" in body["packaging_units"]


# --- 价格历史 ---------------------------------------------------------------


def test_history_returns_string_numbers_and_the_normalized_price(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(migrated_client, item_id)

    response = migrated_client.get(f"/api/prices/food-items/{item_id}/history")

    assert response.status_code == 200
    body = response.get_json()
    conversion = body["items"][0]["conversion"]
    assert conversion["status"] == "available"
    assert conversion["normalized_unit_price"] == "6.00"
    assert conversion["base_quantity"] == "10"
    assert conversion["base_unit"] == "lb"
    # 采购记录本身仍按步骤 04 的形状返回，字段没有被改动。
    assert body["items"][0]["purchase"]["line_amount"] == "60.00"
    assert body["excluded"]["total"] == 0
    assert body["notes"]


def test_history_reports_null_not_zero_when_the_price_cannot_be_derived(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """⚠️ 算不出来是 `null`。写成 0 会让「没算出来」和「真的免费」变成同一个数。"""
    post_purchase(
        migrated_client,
        item_id,
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
    )

    body = migrated_client.get(
        f"/api/prices/food-items/{item_id}/history"
    ).get_json()
    conversion = body["items"][0]["conversion"]

    assert conversion["status"] == "pending_pack_spec"
    assert conversion["normalized_unit_price"] is None
    assert conversion["base_quantity"] is None
    assert body["excluded"]["pending_pack_spec"] == 1
    assert body["convertible_count"] == 0


def test_history_paginates(migrated_client: FlaskClient, item_id: int) -> None:
    for _ in range(3):
        post_purchase(migrated_client, item_id)

    body = migrated_client.get(
        f"/api/prices/food-items/{item_id}/history?limit=2&offset=2"
    ).get_json()

    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["limit"] == 2 and body["offset"] == 2


def test_history_for_a_missing_food_item_is_404(migrated_client: FlaskClient) -> None:
    response = migrated_client.get("/api/prices/food-items/9999/history")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


# --- 月度统计 ---------------------------------------------------------------


def test_weekly_endpoint_returns_weeks_and_keeps_monthly_endpoint(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client, item_id,
        purchase_date=week_start(date.today()).isoformat(),
    )
    weekly = migrated_client.get(
        f"/api/prices/food-items/{item_id}/weekly?weeks=6"
    )
    monthly = migrated_client.get(
        f"/api/prices/food-items/{item_id}/monthly?months=6"
    )
    assert weekly.status_code == 200
    assert monthly.status_code == 200
    assert len(weekly.get_json()["weeks"]) == 6
    assert weekly.get_json()["weeks"][-1]["weighted_average_unit_price"] == "6.00"
    assert len(monthly.get_json()["months"]) == 6


def test_weekly_alerts_compare_with_previous_week(
    migrated_client: FlaskClient, item_id: int
) -> None:
    this_week = week_start(date.today())
    post_purchase(migrated_client, item_id,
                  purchase_date=(this_week - timedelta(weeks=1)).isoformat(),
                  quantity="10", unit_price="4", line_amount="40")
    post_purchase(migrated_client, item_id,
                  purchase_date=this_week.isoformat(),
                  quantity="10", unit_price="6", line_amount="60")
    response = migrated_client.get("/api/prices/alerts?period=week")
    assert response.status_code == 200
    body = response.get_json()
    assert body["current_period"]["date_from"] == this_week.isoformat()
    assert body["alerts"][0]["comparison"]["change_percent"] == "50.00"


def test_source_comparison_can_use_recent_twelve_weeks(
    migrated_client: FlaskClient, item_id: int
) -> None:
    start = week_start(date.today()) - timedelta(weeks=11)
    post_purchase(migrated_client, item_id,
                  purchase_date=(start - timedelta(days=1)).isoformat(),
                  source_name="Earlier")
    post_purchase(migrated_client, item_id,
                  purchase_date=start.isoformat(), source_name="Recent")
    response = migrated_client.get(
        f"/api/prices/food-items/{item_id}/sources?period=week"
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["period"]["date_from"] == start.isoformat()
    assert [source["source_name"] for source in body["sources"]] == ["Recent"]


def test_monthly_returns_the_series_and_the_comparison(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client,
        item_id,
        purchase_date=last_month_day_one(),
        unit_price="5.00",
        line_amount="50.00",
    )
    post_purchase(migrated_client, item_id)

    body = migrated_client.get(
        f"/api/prices/food-items/{item_id}/monthly?months=2"
    ).get_json()

    assert len(body["months"]) == 2
    assert body["comparison"]["status"] == "available"
    assert body["comparison"]["change_amount"] == "1.00"
    assert body["comparison"]["change_percent"] == "20.00"
    assert body["comparison"]["current"]["weighted_average_unit_price"] == "6.00"
    assert body["comparison"]["current"]["purchase_count"] == 1
    assert body["comparison"]["current"]["period"]["basis"] in {
        "month_to_date",
        "full_month",
    }


def test_monthly_change_percent_is_null_without_a_base_period(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """⚠️ 缺基期不显示 0%（09.11）。"""
    post_purchase(migrated_client, item_id)

    body = migrated_client.get(
        f"/api/prices/food-items/{item_id}/monthly"
    ).get_json()

    assert body["comparison"]["status"] == "no_base_data"
    assert body["comparison"]["change_percent"] is None
    assert body["comparison"]["change_amount"] is None


def test_monthly_refuses_to_mix_currencies(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(migrated_client, item_id, currency="USD")
    post_purchase(migrated_client, item_id, currency="CNY")

    response = migrated_client.get(f"/api/prices/food-items/{item_id}/monthly")

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["available"] == ["CNY", "USD"]


def test_monthly_accepts_an_explicit_currency(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(migrated_client, item_id, currency="USD")
    post_purchase(migrated_client, item_id, currency="CNY")

    body = migrated_client.get(
        f"/api/prices/food-items/{item_id}/monthly?currency=CNY"
    ).get_json()

    assert body["currency"] == "CNY"
    assert body["excluded"]["other_currency"] == 1


def test_monthly_rejects_a_bad_month_count(
    migrated_client: FlaskClient, item_id: int
) -> None:
    response = migrated_client.get(f"/api/prices/food-items/{item_id}/monthly?months=0")

    assert response.status_code == 400


# --- 来源比价 ---------------------------------------------------------------


def test_sources_ranks_by_price_and_reports_the_spread(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client, item_id, source_name="Sysco", unit_price="5.80",
        line_amount="58.00"
    )
    post_purchase(
        migrated_client, item_id, source_name="Costco", unit_price="5.50",
        line_amount="55.00"
    )

    body = migrated_client.get(
        f"/api/prices/food-items/{item_id}/sources"
    ).get_json()

    assert [source["source_name"] for source in body["sources"]] == [
        "Costco",
        "Sysco",
    ]
    assert body["sources"][0]["weighted_average_unit_price"] == "5.50"
    assert body["spread_amount"] == "0.30"
    assert body["spread_percent"] == "5.45"
    assert body["cheapest_source_normalized"] == "costco"


def test_sources_window_can_be_narrowed(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(migrated_client, item_id, source_name="Sysco")

    body = migrated_client.get(
        f"/api/prices/food-items/{item_id}/sources"
        f"?date_from={this_month_day_one()}&date_to={this_month_day_one()}"
    ).get_json()

    assert body["period"]["date_from"] == this_month_day_one()
    assert len(body["sources"]) == 1


# --- 箱规 -------------------------------------------------------------------


def post_pack_spec(
    client: FlaskClient, item_id: int, **overrides: Any
) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = {
        "food_item_id": item_id,
        "purchase_unit": "case",
        "pack_size": "40",
        "pack_unit": "lb",
    }
    payload.update(overrides)
    response = client.post("/api/prices/pack-specs", json=payload)
    return response.status_code, response.get_json()


def test_creating_a_pack_spec_returns_201_with_the_affected_count(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client,
        item_id,
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
    )

    status, body = post_pack_spec(migrated_client, item_id, spec_text="40 lb / case")

    assert status == 201
    assert body["created"] is True
    assert body["pack_size"] == "40"
    assert body["pack_unit"] == "lb"
    assert body["spec_text"] == "40 lb / case"
    # ⚠️ 改箱规会让历史均价跟着变，所以响应必须把影响面说出来。
    assert body["affected_purchase_count"] == 1
    assert "标准化价格" in body["notice"]


def test_posting_the_same_key_again_updates_and_returns_200(
    migrated_client: FlaskClient, item_id: int
) -> None:
    first_status, first = post_pack_spec(migrated_client, item_id)
    second_status, second = post_pack_spec(migrated_client, item_id, pack_size="30")

    assert first_status == 201 and second_status == 200
    assert second["created"] is False
    assert second["id"] == first["id"]
    assert second["pack_size"] == "30"


def test_pack_spec_rejects_unknown_fields(
    migrated_client: FlaskClient, item_id: int
) -> None:
    status, body = post_pack_spec(migrated_client, item_id, packSize="40")

    assert status == 400
    assert body["error"]["code"] == "validation_error"


def test_pack_spec_requires_the_core_fields(migrated_client: FlaskClient) -> None:
    response = migrated_client.post("/api/prices/pack-specs", json={"pack_size": "40"})

    assert response.status_code == 400


def test_pack_spec_rejects_a_measurement_unit(
    migrated_client: FlaskClient, item_id: int
) -> None:
    status, body = post_pack_spec(migrated_client, item_id, purchase_unit="lb")

    assert status == 400
    assert "不需要箱规" in body["error"]["message"]


def test_pack_spec_list_and_delete(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client,
        item_id,
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
    )
    _, created = post_pack_spec(migrated_client, item_id)

    listing = migrated_client.get("/api/prices/pack-specs").get_json()
    assert listing["total"] == 1
    assert listing["items"][0]["food_item"]["id"] == item_id

    deleted = migrated_client.delete(f"/api/prices/pack-specs/{created['id']}")
    assert deleted.status_code == 200
    assert deleted.get_json()["affected_purchase_count"] == 1

    after = migrated_client.get(
        f"/api/prices/food-items/{item_id}/history"
    ).get_json()
    assert after["items"][0]["conversion"]["status"] == "pending_pack_spec"
    # 采购记录本身没有被改动。
    assert after["items"][0]["purchase"]["line_amount"] == "220.00"


def test_deleting_a_missing_pack_spec_is_404(migrated_client: FlaskClient) -> None:
    response = migrated_client.delete("/api/prices/pack-specs/9999")

    assert response.status_code == 404


def test_pending_list_carries_a_suggestion_flagged_as_needing_confirmation(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client,
        item_id,
        purchase_unit="case",
        quantity="1",
        unit_price="220.00",
        line_amount="220.00",
        original_description="Ground Beef 2lb x 20",
    )

    body = migrated_client.get("/api/prices/pack-specs/pending").get_json()

    assert body["total"] == 1
    row = body["items"][0]
    assert row["purchase_unit"] == "case"
    assert row["total_amount"] == "220.00"
    assert row["suggestion"]["pack_size"] == "40"
    assert row["suggestion"]["pack_unit"] == "lb"
    # ⚠️ 候选必须人确认：接口里把这一点写成字段，不靠文档。
    assert row["suggestion"]["confirmation_required"] is True


# --- 涨价提醒 ---------------------------------------------------------------


def test_alert_settings_round_trip(migrated_client: FlaskClient) -> None:
    initial = migrated_client.get("/api/prices/alert-settings").get_json()
    assert initial["enabled"] is True
    assert initial["threshold_percent"] == "10.00"

    updated = migrated_client.put(
        "/api/prices/alert-settings", json={"threshold_percent": "15.5"}
    )
    assert updated.status_code == 200
    assert updated.get_json()["threshold_percent"] == "15.50"

    assert (
        migrated_client.get("/api/prices/alert-settings").get_json()[
            "threshold_percent"
        ]
        == "15.50"
    )


def test_alert_settings_reject_unknown_fields(migrated_client: FlaskClient) -> None:
    response = migrated_client.put(
        "/api/prices/alert-settings", json={"threshold": "15"}
    )

    assert response.status_code == 400


def test_alert_settings_reject_a_json_float(migrated_client: FlaskClient) -> None:
    """数值口径沿用步骤 04：JSON 小数会丢精度，直接拒绝。"""
    response = migrated_client.put(
        "/api/prices/alert-settings", json={"threshold_percent": 15.5}
    )

    assert response.status_code == 400


def test_alerts_list_the_items_above_the_threshold(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client,
        item_id,
        purchase_date=last_month_day_one(),
        unit_price="5.00",
        line_amount="50.00",
    )
    post_purchase(migrated_client, item_id)

    body = migrated_client.get("/api/prices/alerts").get_json()

    assert len(body["alerts"]) == 1
    alert = body["alerts"][0]
    assert alert["food_item"]["id"] == item_id
    assert alert["comparison"]["change_percent"] == "20.00"
    assert alert["comparison"]["current"]["purchase_ids"]
    assert body["threshold_percent"] == "10.00"
    assert body["current_period"]["days"] == body["base_period"]["days"]


def test_alerts_accept_a_threshold_override_without_saving_it(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client,
        item_id,
        purchase_date=last_month_day_one(),
        unit_price="5.90",
        line_amount="59.00",
    )
    post_purchase(migrated_client, item_id)

    body = migrated_client.get("/api/prices/alerts?threshold=1").get_json()

    assert len(body["alerts"]) == 1
    assert body["threshold_percent"] == "1.00"
    assert body["settings"]["threshold_percent"] == "10.00"


def test_alerts_report_nothing_when_disabled(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(
        migrated_client,
        item_id,
        purchase_date=last_month_day_one(),
        unit_price="5.00",
        line_amount="50.00",
    )
    post_purchase(migrated_client, item_id)
    migrated_client.put("/api/prices/alert-settings", json={"enabled": False})

    body = migrated_client.get("/api/prices/alerts").get_json()

    assert body["alerts"] == []
    assert any("已关闭" in note for note in body["notes"])


def test_alerts_skip_reasons_are_not_silent(
    migrated_client: FlaskClient, item_id: int
) -> None:
    post_purchase(migrated_client, item_id)

    body = migrated_client.get("/api/prices/alerts").get_json()

    assert body["alerts"] == []
    assert body["skipped"] == {"no_base_data": 1}


# --- 路由边界 ---------------------------------------------------------------


def test_unknown_price_route_is_404_not_an_empty_result(
    migrated_client: FlaskClient,
) -> None:
    """未实现的能力不提供返回假成功的接口（步骤 01 的口径）。"""
    response = migrated_client.get("/api/prices/forecast")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_price_endpoints_do_not_write_purchases(
    migrated_client: FlaskClient, item_id: int
) -> None:
    """本模块只读采购记录。改数量/单位/金额要走 PATCH /api/purchases（有修改日志）。"""
    purchase = post_purchase(migrated_client, item_id)

    migrated_client.get(f"/api/prices/food-items/{item_id}/history")
    migrated_client.get(f"/api/prices/food-items/{item_id}/monthly")
    migrated_client.get("/api/prices/alerts")

    after = migrated_client.get(f"/api/purchases/{purchase['id']}").get_json()
    assert after == purchase
    assert after["revision_count"] == 0
