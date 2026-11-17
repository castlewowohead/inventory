"""业务异常与统一错误响应。

业务代码只抛本模块的异常类型，不自己构造 HTTP 响应、不设置状态码；
异常到 HTTP 的映射集中在 register_error_handlers()，由应用工厂注册。

统一错误响应体：

    {"error": {"code": "validation_error", "message": "...", "details": {...}}}

details 只在确实有字段级信息时出现。响应里不包含异常堆栈与内部实现细节。
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, has_request_context, jsonify, request
from werkzeug.exceptions import HTTPException


class ConfigError(RuntimeError):
    """启动期配置错误。

    不映射为 HTTP 响应：配置不合法时应用不应该启动，更不应该带着错误配置对外服务。
    """


class AppError(Exception):
    """业务异常基类。子类通过 status_code 与 code 决定对外表现。"""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


class ValidationError(AppError):
    """请求数据不合法：缺字段、数值无效、单位无法解析等。"""

    status_code = 400
    code = "validation_error"


class AmountMismatchError(ValidationError):
    """数量 × 单价 与行金额对不上，且差额超过容差（步骤 04）。

    独立的错误码让前端认得出这一种 400：它不是「字段写错了」，而是「单据上的数字
    就是这样，要不要照原样入账」。带 `accept_amount_mismatch=true` 重发即可，
    那一行会按行金额入账并标记为金额异常。
    """

    code = "amount_mismatch"


class NotFoundError(AppError):
    """请求的资源不存在。"""

    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    """与当前状态冲突：重复确认、重复提交、版本不一致等。"""

    status_code = 409
    code = "conflict"


class FeatureNotImplementedError(AppError):
    """能力尚未实现。

    用于明确告知调用方「这件事现在做不到」，替代返回假成功。
    """

    status_code = 501
    code = "not_implemented"


# 框架级错误的对外文案。与业务异常保持同一种语言，且不回显请求 URL 与内部细节。
HTTP_MESSAGES: dict[int, str] = {
    400: "请求格式不正确。",
    401: "需要身份验证。",
    403: "没有访问权限。",
    404: "请求的资源不存在。",
    405: "该路径不支持此 HTTP 方法。",
    413: "上传内容超过大小限制。",
    415: "不支持的请求内容类型。",
    429: "请求过于频繁，请稍后再试。",
    500: "服务器内部错误，请查看服务端日志。",
}


def _current_path() -> str:
    return request.path if has_request_context() else "-"


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return jsonify({"error": error}), status_code


def _http_exception_code(exc: HTTPException) -> str:
    """把 werkzeug 的异常名转成稳定的错误码，如 Not Found -> not_found。"""
    name = (exc.name or "").strip().lower()
    if not name:
        return "http_error"
    return "_".join(name.replace("-", " ").split())


def register_error_handlers(app: Flask) -> None:
    """注册异常到 HTTP 响应的映射。所有错误都返回统一的 JSON 结构。"""

    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError) -> tuple[Response, int]:
        if exc.status_code >= 500:
            app.logger.error(
                "app error status=%s code=%s path=%s",
                exc.status_code,
                exc.code,
                _current_path(),
                exc_info=exc,
            )
        else:
            app.logger.info(
                "request rejected status=%s code=%s path=%s",
                exc.status_code,
                exc.code,
                _current_path(),
            )
        return jsonify(exc.to_payload()), exc.status_code

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException) -> tuple[Response, int]:
        status_code = exc.code or 500
        if status_code >= 500:
            app.logger.error(
                "http error status=%s path=%s",
                status_code,
                _current_path(),
                exc_info=exc,
            )
        else:
            app.logger.info("http error status=%s path=%s", status_code, _current_path())
        return _error_response(
            status_code,
            _http_exception_code(exc),
            HTTP_MESSAGES.get(status_code) or "请求无法处理。",
        )

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception) -> tuple[Response, int]:
        # 未预期异常：完整堆栈只进日志，响应里不暴露内部细节。
        app.logger.exception("unhandled error path=%s", _current_path())
        return _error_response(500, "internal_error", "服务器内部错误，请查看服务端日志。")
