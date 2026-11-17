"""健康检查接口的测试（步骤 01）。"""

from __future__ import annotations

from flask import Flask
from flask.testing import FlaskClient

from src import __version__
from src.api.health import health_payload
from src.config import Config


def test_health_returns_ok(client: FlaskClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == {
        "status": "ok",
        "app_env": "testing",
        "version": __version__,
    }


def test_health_does_not_claim_checks_it_did_not_run(client: FlaskClient) -> None:
    """当前没有数据库与 OCR 连通性检查，响应里就不能出现相关字段。"""
    payload = response_json(client)

    assert set(payload) == {"status", "app_env", "version"}


def test_health_response_contains_no_secret(client: FlaskClient, config: Config) -> None:
    response = client.get("/health")

    assert config.secret_key not in response.get_data(as_text=True)


def test_health_payload_needs_no_request_context() -> None:
    """响应体由纯函数构造，不依赖 Flask 全局 request，可脱离请求上下文调用。"""
    assert health_payload(app_env="development", version="1.2.3") == {
        "status": "ok",
        "app_env": "development",
        "version": "1.2.3",
    }


def test_health_rejects_other_methods(client: FlaskClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405
    assert response.get_json()["error"]["code"] == "method_not_allowed"


def test_app_exposes_only_the_implemented_routes(app: Flask) -> None:
    """未实现的业务不注册路由，也就不存在返回假成功的接口。

    当前有健康检查（步骤 01）、食材/分类接口（步骤 03）、采购记录接口（步骤 04）
    与发票审核（05）、表格导入导出（06）、真实 OCR 提取（08）、价格统计（09）。
    搜索增强与食材合并（10）尚未实现。
    """
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert rules == {
        # --- 步骤 09：标准化价格、月度与来源统计、涨价提醒 ---
        "/api/prices/units",
        "/api/prices/food-items/<int:food_item_id>/history",
        "/api/prices/food-items/<int:food_item_id>/monthly",
        "/api/prices/food-items/<int:food_item_id>/sources",
        "/api/prices/alerts",
        "/api/prices/alert-settings",
        "/api/prices/pack-specs",
        "/api/prices/pack-specs/pending",
        "/api/prices/pack-specs/<int:pack_spec_id>",
        # --- 步骤 08：提取任务与供应商路由 ---
        "/api/invoices/<int:invoice_id>/extraction",
        "/api/invoices/<int:invoice_id>/supplier-candidates",
        "/api/ocr-routes",
        "/health",
        "/api/categories",
        "/api/categories/<int:category_id>",
        "/api/food-items",
        "/api/food-items/suggest",
        "/api/food-items/<int:food_item_id>",
        "/api/food-items/<int:food_item_id>/merge",
        "/api/food-items/<int:food_item_id>/merge-preview",
        "/api/purchases",
        "/api/purchases/export",
        "/api/imports",
        "/api/imports/preview",
        "/api/imports/template",
        "/api/purchases/<int:purchase_id>",
        "/api/purchases/<int:purchase_id>/revisions",
        "/api/invoices",
        "/api/invoices/match",
        "/api/invoices/<int:invoice_id>",
        "/api/invoices/<int:invoice_id>/complete",
        "/api/invoices/<int:invoice_id>/confirm",
        "/api/invoices/<int:invoice_id>/files",
        "/api/invoices/<int:invoice_id>/files/<int:file_id>",
        "/api/invoices/<int:invoice_id>/files/<int:file_id>/content",
        "/api/invoices/<int:invoice_id>/lines",
        "/api/invoices/<int:invoice_id>/lines/<int:line_id>",
        "/api/invoices/<int:invoice_id>/lines/<int:line_id>/unconfirm",
    }


def response_json(client: FlaskClient) -> dict[str, str]:
    return client.get("/health").get_json()
