"""健康检查接口。

只回答「应用进程起来了、配置解析成功了」。当前不检查数据库与外部服务连通性：
数据库边界在步骤 02 建立，真实 OCR 在步骤 08 接入，没做的检查不在响应里声称已做。
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify

from src import __version__

health_bp = Blueprint("health", __name__)


def health_payload(*, app_env: str, version: str) -> dict[str, str]:
    """构造健康检查响应体。

    纯函数：参数由调用方传入，不读 Flask 的全局 request，可脱离请求上下文测试。
    """
    return {"status": "ok", "app_env": app_env, "version": version}


@health_bp.get("/health")
def health() -> tuple[Response, int]:
    config = current_app.config["APP_CONFIG"]
    payload = health_payload(app_env=config.app_env, version=__version__)
    return jsonify(payload), 200
