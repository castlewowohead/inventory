"""统一错误响应的测试（步骤 01）。

本文件里的 /__probe/* 路由只在测试内注册，不随应用交付：它们存在的目的是
验证「异常 → HTTP 响应」这层映射，而不是为未实现的业务提供可调用的接口。
"""

from __future__ import annotations

import logging

import pytest
from flask import Flask, Response, request
from flask.testing import FlaskClient

from src.errors import (
    AppError,
    ConflictError,
    FeatureNotImplementedError,
    NotFoundError,
    ValidationError,
)

INTERNAL_DETAIL = "连接池句柄 0x7ffd 内部状态异常"


@pytest.fixture
def probe_client(app: Flask) -> FlaskClient:
    """注册探针路由后的测试客户端。"""

    @app.get("/__probe/validation")
    def _validation() -> Response:
        raise ValidationError("数量必须大于 0", details={"field": "quantity"})

    @app.get("/__probe/not-found")
    def _not_found() -> Response:
        raise NotFoundError("食材不存在")

    @app.get("/__probe/conflict")
    def _conflict() -> Response:
        raise ConflictError("该行已确认")

    @app.get("/__probe/not-implemented")
    def _not_implemented() -> Response:
        raise FeatureNotImplementedError("发票识别尚未接入")

    @app.get("/__probe/base-error")
    def _base_error() -> Response:
        raise AppError("未分类的业务异常")

    @app.get("/__probe/boom")
    def _boom() -> Response:
        raise RuntimeError(INTERNAL_DETAIL)

    @app.post("/__probe/json")
    def _json() -> dict[str, object]:
        return {"received": request.get_json()}

    return app.test_client()


@pytest.mark.parametrize(
    ("path", "status_code", "code"),
    [
        ("/__probe/validation", 400, "validation_error"),
        ("/__probe/not-found", 404, "not_found"),
        ("/__probe/conflict", 409, "conflict"),
        ("/__probe/not-implemented", 501, "not_implemented"),
        ("/__probe/base-error", 500, "internal_error"),
    ],
)
def test_business_exceptions_map_to_status_and_code(
    probe_client: FlaskClient, path: str, status_code: int, code: str
) -> None:
    response = probe_client.get(path)

    assert response.status_code == status_code
    assert response.is_json
    assert response.get_json()["error"]["code"] == code


def test_validation_error_keeps_field_details(probe_client: FlaskClient) -> None:
    response = probe_client.get("/__probe/validation")

    error = response.get_json()["error"]
    assert error["message"] == "数量必须大于 0"
    assert error["details"] == {"field": "quantity"}


def test_error_without_details_omits_the_key(probe_client: FlaskClient) -> None:
    error = probe_client.get("/__probe/not-found").get_json()["error"]

    assert set(error) == {"code", "message"}


def test_unknown_route_returns_json_not_found(client: FlaskClient) -> None:
    response = client.get("/no-such-endpoint")

    assert response.status_code == 404
    assert response.is_json
    assert response.get_json()["error"]["code"] == "not_found"


def test_malformed_json_body_is_rejected(probe_client: FlaskClient) -> None:
    response = probe_client.post(
        "/__probe/json", data="{not json", content_type="application/json"
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "bad_request"


def test_wrong_content_type_is_rejected(probe_client: FlaskClient) -> None:
    response = probe_client.post("/__probe/json", data="quantity=1")

    assert response.status_code in (400, 415)
    assert response.get_json()["error"]["code"] in (
        "bad_request",
        "unsupported_media_type",
    )


def test_valid_json_body_passes_through(probe_client: FlaskClient) -> None:
    response = probe_client.post("/__probe/json", json={"quantity": 2})

    assert response.status_code == 200
    assert response.get_json() == {"received": {"quantity": 2}}


def test_unexpected_exception_returns_generic_500(probe_client: FlaskClient) -> None:
    response = probe_client.get("/__probe/boom")

    assert response.status_code == 500
    assert response.is_json
    body = response.get_data(as_text=True)
    assert INTERNAL_DETAIL not in body
    assert "Traceback" not in body
    assert response.get_json()["error"]["code"] == "internal_error"


def test_unexpected_exception_is_logged_with_traceback(
    probe_client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        probe_client.get("/__probe/boom")

    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert records, "未预期异常必须记入日志"
    assert any(record.exc_info for record in records), "日志里缺少异常堆栈"
    assert INTERNAL_DETAIL in caplog.text


def test_framework_errors_use_the_same_language_as_business_errors(
    client: FlaskClient,
) -> None:
    """404 / 405 这类框架错误也用本项目的文案，不直接回显 werkzeug 的英文描述。"""
    assert client.get("/no-such-endpoint").get_json()["error"]["message"] == "找不到这项内容，请刷新后重试。"
    assert client.post("/health").get_json()["error"]["message"] == "当前操作暂时无法完成，请刷新后重试。"


def test_framework_error_does_not_echo_request_path(client: FlaskClient) -> None:
    response = client.get("/internal-admin-console")

    assert "internal-admin-console" not in response.get_data(as_text=True)


def test_every_error_response_uses_the_same_envelope(probe_client: FlaskClient) -> None:
    paths = [
        "/__probe/validation",
        "/__probe/not-found",
        "/__probe/conflict",
        "/__probe/not-implemented",
        "/__probe/boom",
        "/no-such-endpoint",
    ]

    for path in paths:
        payload = probe_client.get(path).get_json()
        assert set(payload) == {"error"}, path
        assert {"code", "message"} <= set(payload["error"]), path
        assert isinstance(payload["error"]["message"], str), path
