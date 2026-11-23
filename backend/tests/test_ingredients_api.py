"""食材与分类接口的测试（步骤 03）。

关注的是 HTTP 这一层：状态码、响应体结构、错误码与 `details`、查询参数解析。
业务口径（规范化、重复判定、删除守卫）在 test_ingredients_service.py。

应用使用 `migrated_client`：数据库由真实迁移建出，初始分类已写入。
"""

from __future__ import annotations

from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from src.config import Config
from src.db import Database
from src.factory import create_app
from src.integrations.file_storage import LocalFileStorage

MEAT = "Meat & Poultry"
VEGETABLES = "Vegetables"


def category_id(client: FlaskClient, name: str = MEAT) -> int:
    items = client.get("/api/categories").get_json()["items"]
    return next(item["id"] for item in items if item["name"] == name)


def create_item(client: FlaskClient, **payload: Any) -> dict[str, Any]:
    body = {"base_unit": "lb", **payload}
    response = client.post("/api/food-items", json=body)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def error_of(response: Any) -> dict[str, Any]:
    payload = response.get_json()
    assert set(payload) == {"error"}, payload
    return payload["error"]


# --- 分类接口 ---


def test_list_categories_returns_the_seeded_ones(migrated_client: FlaskClient) -> None:
    response = migrated_client.get("/api/categories")

    assert response.status_code == 200
    items = response.get_json()["items"]
    assert len(items) == 8
    assert MEAT in {item["name"] for item in items}


def test_category_payload_shape(migrated_client: FlaskClient) -> None:
    item = migrated_client.get("/api/categories").get_json()["items"][0]

    assert set(item) == {"id", "name", "name_zh", "created_at", "updated_at"}


def test_timestamps_are_utc_with_an_offset(migrated_client: FlaskClient) -> None:
    """没有偏移量的时间串会被浏览器按本地时区解释，所以必须带 +00:00。"""
    item = migrated_client.get("/api/categories").get_json()["items"][0]

    assert item["created_at"].endswith("+00:00")


def test_chinese_is_not_escaped_in_responses(migrated_client: FlaskClient) -> None:
    response = migrated_client.get("/api/categories")

    assert "肉类与禽类" in response.get_data(as_text=True)


def test_create_category(migrated_client: FlaskClient) -> None:
    response = migrated_client.post(
        "/api/categories", json={"name": "Baking", "name_zh": "烘焙"}
    )

    assert response.status_code == 201
    assert response.get_json()["name"] == "Baking"
    assert len(migrated_client.get("/api/categories").get_json()["items"]) == 9


def test_create_category_without_a_name_is_rejected(
    migrated_client: FlaskClient,
) -> None:
    response = migrated_client.post("/api/categories", json={"name_zh": "烘焙"})

    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert error["details"]["missing_fields"] == ["name"]


def test_duplicate_category_is_a_conflict(migrated_client: FlaskClient) -> None:
    response = migrated_client.post("/api/categories", json={"name": "seafood"})

    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "conflict"
    assert error["details"]["existing"]["name"] == "Seafood"


def test_update_category(migrated_client: FlaskClient) -> None:
    target = category_id(migrated_client)

    response = migrated_client.patch(f"/api/categories/{target}", json={"name": "Meat"})

    assert response.status_code == 200
    assert response.get_json()["name"] == "Meat"


def test_update_category_with_an_empty_body_is_rejected(
    migrated_client: FlaskClient,
) -> None:
    target = category_id(migrated_client)

    response = migrated_client.patch(f"/api/categories/{target}", json={})

    assert response.status_code == 400
    assert error_of(response)["code"] == "validation_error"


def test_update_unknown_category_is_404(migrated_client: FlaskClient) -> None:
    response = migrated_client.patch("/api/categories/9999", json={"name": "x"})

    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_delete_unused_category_returns_204(migrated_client: FlaskClient) -> None:
    target = category_id(migrated_client)

    response = migrated_client.delete(f"/api/categories/{target}")

    assert response.status_code == 204
    assert response.get_data() == b""
    assert len(migrated_client.get("/api/categories").get_json()["items"]) == 7


def test_delete_category_in_use_is_a_conflict(migrated_client: FlaskClient) -> None:
    target = category_id(migrated_client)
    create_item(migrated_client, name="Ground Beef", category_id=target)

    response = migrated_client.delete(f"/api/categories/{target}")

    assert response.status_code == 409
    assert error_of(response)["details"]["food_item_count"] == 1


def test_non_integer_path_id_is_404(migrated_client: FlaskClient) -> None:
    """路由的 int 转换器先拦一道，脏 id 不会进 Service。"""
    response = migrated_client.get("/api/food-items/abc")

    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


# --- 请求体格式 ---


def test_body_must_be_json(migrated_client: FlaskClient) -> None:
    response = migrated_client.post(
        "/api/categories", data="name=Baking", content_type="text/plain"
    )

    assert response.status_code == 400
    assert error_of(response)["code"] == "validation_error"


def test_body_must_be_an_object_not_a_list(migrated_client: FlaskClient) -> None:
    response = migrated_client.post("/api/categories", json=["Baking"])

    assert response.status_code == 400
    assert error_of(response)["code"] == "validation_error"


def test_malformed_json_is_rejected(migrated_client: FlaskClient) -> None:
    response = migrated_client.post(
        "/api/categories", data="{not json", content_type="application/json"
    )

    assert response.status_code == 400


def test_unknown_fields_are_rejected(migrated_client: FlaskClient) -> None:
    """写错字段名（nameZh）必须报错，否则用户填的中文名被静默丢掉。"""
    response = migrated_client.post(
        "/api/categories", json={"name": "Baking", "nameZh": "烘焙"}
    )

    assert response.status_code == 400
    assert error_of(response)["details"]["unknown_fields"] == ["nameZh"]


def test_wrong_method_is_405(migrated_client: FlaskClient) -> None:
    response = migrated_client.put("/api/categories")

    assert response.status_code == 405
    assert error_of(response)["code"] == "method_not_allowed"


# --- 快速新增 ---


def test_quick_add_needs_only_three_fields(migrated_client: FlaskClient) -> None:
    payload = create_item(
        migrated_client, name="Chicken Breast", category_id=category_id(migrated_client)
    )

    assert payload["name"] == "Chicken Breast"
    assert payload["name_zh"] is None
    assert payload["specification"] is None
    assert payload["base_unit"] == "lb"
    assert payload["category"]["name"] == MEAT


def test_food_item_payload_shape(migrated_client: FlaskClient) -> None:
    payload = create_item(
        migrated_client, name="Chicken Breast", category_id=category_id(migrated_client)
    )

    assert set(payload) == {
        "id",
        "name",
        "name_zh",
        "specification",
        "base_unit",
        "category",
        "needs_review",
        "created_at",
        "updated_at",
    }
    assert set(payload["category"]) == {"id", "name", "name_zh"}
    # 老板自己在界面上建的食材是他亲手填的，不需要再确认一次。
    assert payload["needs_review"] is False


def test_create_food_item_with_all_fields(migrated_client: FlaskClient) -> None:
    payload = create_item(
        migrated_client,
        name="Ground Beef",
        category_id=category_id(migrated_client),
        base_unit="LBS",
        name_zh="牛肉末",
        specification="80/20",
    )

    assert payload["base_unit"] == "lb"
    assert payload["name_zh"] == "牛肉末"


@pytest.mark.parametrize(
    "missing",
    [
        {"category_id": 1, "base_unit": "lb"},
        {"name": "x", "base_unit": "lb"},
        {"name": "x", "category_id": 1},
    ],
)
def test_quick_add_required_fields_are_enforced(
    migrated_client: FlaskClient, missing: dict[str, Any]
) -> None:
    response = migrated_client.post("/api/food-items", json=missing)

    assert response.status_code == 400
    assert error_of(response)["details"]["missing_fields"]


def test_packaging_base_unit_is_rejected_with_a_readable_message(
    migrated_client: FlaskClient,
) -> None:
    response = migrated_client.post(
        "/api/food-items",
        json={
            "name": "Beef Case",
            "category_id": category_id(migrated_client),
            "base_unit": "case",
        },
    )

    assert response.status_code == 400
    error = error_of(response)
    assert "包装单位" in error["message"]
    assert error["details"]["field"] == "base_unit"


def test_unknown_category_id_is_400_not_404(migrated_client: FlaskClient) -> None:
    response = migrated_client.post(
        "/api/food-items",
        json={"name": "Mystery", "category_id": 9999, "base_unit": "lb"},
    )

    assert response.status_code == 400
    assert error_of(response)["details"]["field"] == "category_id"


def test_duplicate_food_item_is_a_conflict_pointing_at_the_existing_one(
    migrated_client: FlaskClient,
) -> None:
    first = create_item(
        migrated_client, name="Ground Beef", category_id=category_id(migrated_client)
    )

    response = migrated_client.post(
        "/api/food-items",
        json={
            "name": "  ground   beef ",
            "category_id": category_id(migrated_client),
            "base_unit": "lb",
        },
    )

    assert response.status_code == 409
    assert error_of(response)["details"]["existing"]["id"] == first["id"]


# --- 搜索 ---


@pytest.fixture
def seeded_items(migrated_client: FlaskClient) -> dict[str, int]:
    meat = category_id(migrated_client, MEAT)
    vegetables = category_id(migrated_client, VEGETABLES)
    beef = create_item(
        migrated_client,
        name="Ground Beef",
        category_id=meat,
        name_zh="牛肉末",
        specification="80/20",
    )
    chicken = create_item(
        migrated_client,
        name="Chicken Breast",
        category_id=meat,
        name_zh="鸡胸肉",
    )
    tomato = create_item(
        migrated_client, name="Tomato", category_id=vegetables, name_zh="番茜"
    )
    return {"beef": beef["id"], "chicken": chicken["id"], "tomato": tomato["id"]}


def test_search_page_shape(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    payload = migrated_client.get("/api/food-items").get_json()

    assert set(payload) == {"items", "total", "limit", "offset"}
    assert payload["total"] == 3
    assert payload["limit"] == 20
    assert payload["offset"] == 0


@pytest.mark.parametrize(
    "query",
    ["Ground Beef", "ground beef", "beef", "牛肉末", "牛肉", "80/20"],
)
def test_english_and_chinese_queries_return_the_same_single_item(
    migrated_client: FlaskClient, seeded_items: dict[str, int], query: str
) -> None:
    """验收条件：中英文都指向同一条食材。"""
    payload = migrated_client.get("/api/food-items", query_string={"q": query}).get_json()

    assert [item["id"] for item in payload["items"]] == [seeded_items["beef"]]
    assert payload["total"] == 1


def test_search_filters_by_category(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    payload = migrated_client.get(
        "/api/food-items",
        query_string={"category_id": category_id(migrated_client, VEGETABLES)},
    ).get_json()

    assert [item["id"] for item in payload["items"]] == [seeded_items["tomato"]]


def test_search_pagination(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    payload = migrated_client.get(
        "/api/food-items", query_string={"limit": 2, "offset": 2}
    ).get_json()

    assert len(payload["items"]) == 1
    assert payload["total"] == 3


@pytest.mark.parametrize("limit", ["0", "101", "abc", "-1"])
def test_invalid_limit_is_rejected(migrated_client: FlaskClient, limit: str) -> None:
    response = migrated_client.get("/api/food-items", query_string={"limit": limit})

    assert response.status_code == 400
    assert error_of(response)["details"]["field"] == "limit"


def test_blank_query_parameters_fall_back_to_defaults(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    payload = migrated_client.get(
        "/api/food-items", query_string={"q": "  ", "limit": "", "category_id": ""}
    ).get_json()

    assert payload["total"] == 3
    assert payload["limit"] == 20


# --- 单条读取、编辑与删除 ---


def test_get_food_item(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    response = migrated_client.get(f"/api/food-items/{seeded_items['beef']}")

    assert response.status_code == 200
    assert response.get_json()["name"] == "Ground Beef"


def test_get_unknown_food_item_is_404(migrated_client: FlaskClient) -> None:
    response = migrated_client.get("/api/food-items/9999")

    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_patch_changes_only_the_given_fields(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    response = migrated_client.patch(
        f"/api/food-items/{seeded_items['beef']}", json={"base_unit": "kg"}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["base_unit"] == "kg"
    assert payload["name"] == "Ground Beef"
    assert payload["specification"] == "80/20"


def test_patch_can_clear_an_optional_field(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    """显式 null 表示清空；未出现的字段保持原值。"""
    response = migrated_client.patch(
        f"/api/food-items/{seeded_items['beef']}", json={"name_zh": None}
    )

    assert response.get_json()["name_zh"] is None


def test_patch_rejects_unknown_fields(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    response = migrated_client.patch(
        f"/api/food-items/{seeded_items['beef']}", json={"baseUnit": "kg"}
    )

    assert response.status_code == 400
    assert error_of(response)["details"]["unknown_fields"] == ["baseUnit"]


def test_patch_rejects_a_name_another_item_uses(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    response = migrated_client.patch(
        f"/api/food-items/{seeded_items['beef']}", json={"name": "Tomato"}
    )

    assert response.status_code == 409
    assert error_of(response)["details"]["existing"]["id"] == seeded_items["tomato"]


def test_delete_food_item(
    migrated_client: FlaskClient, seeded_items: dict[str, int]
) -> None:
    response = migrated_client.delete(f"/api/food-items/{seeded_items['beef']}")

    assert response.status_code == 204
    assert (
        migrated_client.get(f"/api/food-items/{seeded_items['beef']}").status_code == 404
    )


def test_delete_unknown_food_item_is_404(migrated_client: FlaskClient) -> None:
    assert migrated_client.delete("/api/food-items/9999").status_code == 404


# --- 与骨架其余部分的关系 ---


def test_health_is_unchanged(migrated_client: FlaskClient) -> None:
    """新增业务接口不影响健康检查，也不让它声称检查了数据库。"""
    payload = migrated_client.get("/health").get_json()

    assert set(payload) == {"status", "app_env", "version"}


def test_unknown_api_path_is_404(migrated_client: FlaskClient) -> None:
    response = migrated_client.get("/api/suppliers")

    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_every_error_response_has_the_same_structure(
    migrated_client: FlaskClient,
) -> None:
    responses = [
        migrated_client.get("/api/food-items/9999"),
        migrated_client.post("/api/categories", json={}),
        migrated_client.post("/api/categories", json={"name": "Seafood"}),
        migrated_client.put("/api/categories"),
    ]

    for response in responses:
        error = error_of(response)
        assert set(error) >= {"code", "message"}
        assert isinstance(error["code"], str) and error["code"]
        assert isinstance(error["message"], str) and error["message"]


def test_data_survives_restarting_the_application(
    migrated_client: FlaskClient,
    migrated_database: Database,
    config: Config,
    tmp_path: Any,
) -> None:
    """验收条件：重启后数据保留。

    这里用「关掉连接池、按同一个数据库文件重新创建应用」模拟重启。
    """
    created = create_item(
        migrated_client,
        name="Ground Beef",
        category_id=category_id(migrated_client),
        name_zh="牛肉末",
    )
    url = str(migrated_database.url)
    migrated_database.dispose()

    reopened = Database(url)
    try:
        restarted: Flask = create_app(
            config,
            database=reopened,
            file_storage=LocalFileStorage(tmp_path / "uploads-restart"),
        )
        response = restarted.test_client().get(f"/api/food-items/{created['id']}")
    finally:
        reopened.dispose()

    assert response.status_code == 200
    assert response.get_json()["name_zh"] == "牛肉末"


# --- 建议接口与「从没买过」筛选（步骤 10）---------------------------------


def test_suggest_returns_matches_with_a_reason_and_a_score(
    migrated_client: FlaskClient,
) -> None:
    meat = category_id(migrated_client)
    item = create_item(migrated_client, name="Country Crock", category_id=meat)

    response = migrated_client.get("/api/food-items/suggest?q=CNTRY%20CROCK")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"query", "matches", "recent", "frequent", "notes"}
    assert payload["query"] == "cntry crock"
    assert [row["food_item"]["id"] for row in payload["matches"]] == [item["id"]]
    assert payload["matches"][0]["match_reason"] == "fuzzy"
    assert 0.7 <= payload["matches"][0]["score"] <= 1.0
    # 名称匹配那三档不去查采购历史：候选框每敲一个字都要查一次，而那两列在这个
    # 场景里也不影响排序。
    assert payload["matches"][0]["purchase_count"] is None
    assert payload["recent"] == []
    assert payload["frequent"] == []


def test_suggest_without_a_query_returns_recent_and_frequent_plus_notes(
    migrated_client: FlaskClient,
) -> None:
    meat = category_id(migrated_client)
    item = create_item(migrated_client, name="Beef Stomach", category_id=meat)
    bought = migrated_client.post(
        "/api/purchases",
        json={
            "food_item_id": item["id"],
            "quantity": "10",
            "purchase_unit": "lb",
            "unit_price": "6.00",
            "line_amount": "60.00",
            "purchase_date": "2026-11-14",
        },
    )
    assert bought.status_code == 201, bought.get_json()

    payload = migrated_client.get("/api/food-items/suggest").get_json()

    assert payload["query"] is None
    assert payload["matches"] == []
    assert [row["food_item"]["id"] for row in payload["recent"]] == [item["id"]]
    assert payload["recent"][0]["match_reason"] == "recent"
    assert payload["recent"][0]["purchase_count"] == 1
    assert payload["recent"][0]["last_purchase_date"] == "2026-11-14"
    # 「常用」窗口排除了什么，必须跟着列表一起读（同步骤 09 的 notes 口径）。
    assert any("未填采购日期" in note for note in payload["notes"])


def test_suggest_rejects_a_bad_limit_rather_than_clamping_it(
    migrated_client: FlaskClient,
) -> None:
    response = migrated_client.get("/api/food-items/suggest?limit=500")

    assert response.status_code == 400
    assert error_of(response)["details"]["field"] == "limit"


def test_suggest_is_not_swallowed_by_the_integer_path_route(
    migrated_client: FlaskClient,
) -> None:
    """`/food-items/suggest` 与 `/food-items/<int:id>` 共用前缀，不能被抢走。"""
    assert migrated_client.get("/api/food-items/suggest").status_code == 200
    assert migrated_client.get("/api/food-items/not-an-id").status_code == 404


def test_the_food_item_list_is_unchanged_by_the_suggest_endpoint(
    migrated_client: FlaskClient,
) -> None:
    """⚠️ 食材库那一页刻意**不**做模糊匹配。

    老板在那里是找确定的某一条然后改它或删它，"这条到底在不在库里"必须有确定
    答案；搜 `CNTRY CROCK` 返回 0 条在那个接口上是正确行为。
    """
    meat = category_id(migrated_client)
    create_item(migrated_client, name="Country Crock", category_id=meat)

    payload = migrated_client.get("/api/food-items?q=CNTRY%20CROCK").get_json()

    assert payload["total"] == 0
    assert payload["items"] == []


def test_never_purchased_query_param(migrated_client: FlaskClient) -> None:
    meat = category_id(migrated_client)
    orphan = create_item(migrated_client, name="Typo Item", category_id=meat)

    payload = migrated_client.get(
        "/api/food-items?never_purchased=true"
    ).get_json()

    assert [row["id"] for row in payload["items"]] == [orphan["id"]]


def test_never_purchased_rejects_a_value_it_cannot_read(
    migrated_client: FlaskClient,
) -> None:
    """宽松解释会让人以为筛过了，实际看到的是全量数据。"""
    response = migrated_client.get("/api/food-items?never_purchased=maybe")

    assert response.status_code == 400
    assert error_of(response)["details"]["field"] == "never_purchased"


# --- 合并接口（步骤 10.3）--------------------------------------------------


def test_merge_preview_then_merge(migrated_client: FlaskClient) -> None:
    meat = category_id(migrated_client)
    keep = create_item(migrated_client, name="Ground Beef", category_id=meat)
    dupe = create_item(migrated_client, name="Grnd Beef 80/20", category_id=meat)

    preview = migrated_client.post(
        f"/api/food-items/{keep['id']}/merge-preview",
        json={"source_ids": [dupe["id"]]},
    )
    assert preview.status_code == 200
    plan = preview.get_json()
    assert plan["can_merge"] is True
    assert plan["blockers"] == []
    assert plan["target"]["id"] == keep["id"]
    assert [row["food_item"]["id"] for row in plan["sources"]] == [dupe["id"]]
    assert any("无法撤销" in note for note in plan["notes"])
    # 预览是只读的：那条食材还在。
    assert migrated_client.get(f"/api/food-items/{dupe['id']}").status_code == 200

    done = migrated_client.post(
        f"/api/food-items/{keep['id']}/merge", json={"source_ids": [dupe["id"]]}
    )
    assert done.status_code == 200
    assert done.get_json()["can_merge"] is True
    assert migrated_client.get(f"/api/food-items/{dupe['id']}").status_code == 404

    # 10.6：按旧叫法还搜得到，给出的是合并后的那一条。
    suggestion = migrated_client.get(
        "/api/food-items/suggest?q=Grnd%20Beef%2080%2F20"
    ).get_json()
    assert [row["food_item"]["id"] for row in suggestion["matches"]] == [keep["id"]]
    assert suggestion["matches"][0]["match_reason"] == "merged_from"
    assert suggestion["matches"][0]["merged_from"] == "Grnd Beef 80/20"


def test_merge_with_incompatible_units_is_a_conflict(
    migrated_client: FlaskClient,
) -> None:
    oils = category_id(migrated_client, "Oils & Condiments")
    keep = create_item(
        migrated_client, name="Olive Oil", category_id=oils, base_unit="gallon"
    )
    dupe = create_item(
        migrated_client, name="Olive Oil Block", category_id=oils, base_unit="lb"
    )

    response = migrated_client.post(
        f"/api/food-items/{keep['id']}/merge", json={"source_ids": [dupe["id"]]}
    )

    assert response.status_code == 409
    blockers = error_of(response)["details"]["blockers"]
    assert [item["code"] for item in blockers] == ["incompatible_base_unit"]
    # 拦住了就什么都没动。
    assert migrated_client.get(f"/api/food-items/{dupe['id']}").status_code == 200


def test_merge_requires_source_ids(migrated_client: FlaskClient) -> None:
    meat = category_id(migrated_client)
    keep = create_item(migrated_client, name="Ground Beef", category_id=meat)

    response = migrated_client.post(f"/api/food-items/{keep['id']}/merge", json={})

    assert response.status_code == 400


def test_merge_into_an_unknown_item_is_404(migrated_client: FlaskClient) -> None:
    response = migrated_client.post(
        "/api/food-items/9999/merge-preview", json={"source_ids": [1]}
    )

    assert response.status_code == 404
