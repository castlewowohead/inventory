"""启动入口 app.py 的测试（步骤 01）。

入口必须保持「薄」：只加载 .env、解析配置、创建应用，不定义路由、不放业务代码，
也不在导入时就开始监听端口。
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterator

import dotenv
import pytest
from flask import Flask

from src.config import BASE_DIR
from src.factory import LOGGER_NAME, get_config

ENTRY_FILE = BASE_DIR / "app.py"


@pytest.fixture
def entry_module(
    monkeypatch: pytest.MonkeyPatch, test_env: dict[str, str]
) -> Iterator[ModuleType]:
    """用注入的环境变量导入入口模块，屏蔽本机 .env 的影响。

    入口会配置进程级日志输出，因此这里保存并还原 logger 状态：
    否则指向 pytest 捕获流的处理器会留在全局 logger 上，影响后续测试。
    """
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for key, value in test_env.items():
        monkeypatch.setenv(key, value)

    logger = logging.getLogger(LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level

    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    try:
        yield module
    finally:
        sys.modules.pop("app", None)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        for handler in saved_handlers:
            logger.addHandler(handler)
        logger.setLevel(saved_level)


def test_entry_exposes_a_flask_app(entry_module: ModuleType) -> None:
    assert isinstance(entry_module.app, Flask)


def test_entry_app_uses_the_process_configuration(entry_module: ModuleType) -> None:
    config = get_config(entry_module.app)

    assert config.app_env == "testing"
    assert config.port == 5001


def test_entry_registers_only_the_implemented_routes(
    entry_module: ModuleType,
) -> None:
    """入口拿到的应用与工厂装出来的一致：健康检查 + 食材/分类 + 采购记录 + 发票
    与提取接口 + 价格统计。"""
    rules = {rule.rule for rule in entry_module.app.url_map.iter_rules()}

    assert rules == {
        "/health",
        # --- 步骤 09：标准化价格与价格统计 ---
        "/api/prices/units",
        "/api/prices/food-items/<int:food_item_id>/history",
        "/api/prices/food-items/<int:food_item_id>/monthly",
        "/api/prices/food-items/<int:food_item_id>/sources",
        "/api/prices/alerts",
        "/api/prices/alert-settings",
        "/api/prices/pack-specs",
        "/api/prices/pack-specs/pending",
        "/api/prices/pack-specs/<int:pack_spec_id>",
        # --- 步骤 08 ---
        "/api/invoices/<int:invoice_id>/extraction",
        "/api/invoices/<int:invoice_id>/supplier-candidates",
        "/api/ocr-routes",
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


def test_entry_health_check_answers(entry_module: ModuleType) -> None:
    response = entry_module.app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_entry_defines_no_routes_or_business_logic() -> None:
    source = ENTRY_FILE.read_text(encoding="utf-8")

    for token in ("Blueprint(", ".route(", "@app.get", "@app.post", "sqlalchemy"):
        assert token not in source, f"入口文件不应出现 {token}"


def test_entry_serves_only_under_main_guard() -> None:
    """导入 app.py 不会启动服务器，app.run() 只在 __main__ 分支里调用。"""
    source = ENTRY_FILE.read_text(encoding="utf-8")

    guard_index = source.index('if __name__ == "__main__":')
    assert source.count("app.run(") == 1
    assert source.index("app.run(") > guard_index


def test_entry_file_stays_thin() -> None:
    code_lines = [
        line
        for line in ENTRY_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert len(code_lines) <= 40, "入口文件变厚了，装配逻辑应该放进 src/factory.py"


def test_entry_file_is_at_backend_root() -> None:
    assert ENTRY_FILE.is_file()
    assert ENTRY_FILE.parent == Path(BASE_DIR)
