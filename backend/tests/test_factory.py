"""应用工厂、配置注入、依赖组装与日志的测试（步骤 01、02）。"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
from flask import Flask

from src.config import Config
from src.db import Database
from src.errors import ConfigError
from src.factory import (
    LOGGER_NAME,
    Dependencies,
    configure_log_output,
    create_app,
    get_config,
    get_database,
    get_dependencies,
    get_file_storage,
    get_recognizer,
)
from src.integrations.file_storage import LocalFileStorage
from src.integrations.ocr import FakeRecognizer


def test_injected_config_is_used(app: Flask, config: Config) -> None:
    assert get_config(app) is config
    assert app.config["TESTING"] is True
    assert app.config["SECRET_KEY"] == config.secret_key


def test_apps_do_not_share_configuration(test_env: dict[str, str]) -> None:
    """每次创建都是独立应用，不同配置之间不会互相影响。"""
    first = create_app(Config.from_env(test_env))

    other_env = dict(test_env, APP_ENV="development", PORT="5002")
    second = create_app(Config.from_env(other_env))

    assert get_config(first).app_env == "testing"
    assert get_config(second).app_env == "development"
    assert get_config(second).port == 5002


def test_create_app_falls_back_to_environment_and_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注入配置时读环境变量；配置不合法就不创建应用。"""
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ConfigError, match="APP_ENV"):
        create_app()


def test_startup_log_omits_secret(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        create_app(config)

    assert "app created" in caplog.text
    assert config.secret_key not in caplog.text


def test_startup_log_masks_database_password(
    test_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    test_env["DATABASE_URL"] = "postgresql://erp_user:sup3r-s3cret@db.internal/inventory"

    with caplog.at_level(logging.INFO):
        create_app(Config.from_env(test_env))

    assert "sup3r-s3cret" not in caplog.text
    assert "erp_user:***@db.internal" in caplog.text


def test_access_log_records_method_path_and_status(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(config)

    with caplog.at_level(logging.INFO):
        app.test_client().get("/health")

    assert "GET /health -> 200" in caplog.text


def test_access_log_does_not_record_query_string(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(config)

    with caplog.at_level(logging.INFO):
        app.test_client().get("/health?token=should-not-be-logged")

    assert "should-not-be-logged" not in caplog.text


def test_no_static_or_template_folder(app: Flask) -> None:
    """只提供 JSON 接口，不挂静态目录与模板目录。"""
    assert app.static_folder is None
    assert app.template_folder is None


def test_startup_log_states_that_nothing_is_connected_yet(
    tmp_config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """启动摘要要如实记录：此刻还没连库，识别器是假数据实现。"""
    with caplog.at_level(logging.INFO):
        create_app(tmp_config)

    assert "dependencies assembled" in caplog.text
    assert "db_connected=False" in caplog.text
    assert "ocr=fake ocr_fake=True" in caplog.text


# --- 依赖组装（步骤 02） ---


def test_dependencies_are_assembled_from_config(tmp_config: Config) -> None:
    app = create_app(tmp_config)

    dependencies = get_dependencies(app)

    assert dependencies.database.url == Database.from_config(tmp_config).url
    assert dependencies.file_storage.root == tmp_config.file_storage_dir
    assert dependencies.recognizer.provider == tmp_config.ocr_provider


def test_creating_the_app_touches_neither_database_nor_disk(
    tmp_config: Config, tmp_path: Path
) -> None:
    """组装依赖不产生外部副作用：不连库、不建上传目录。"""
    app = create_app(tmp_config)

    assert get_database(app).engine_created is False
    assert not (tmp_path / "data").exists()
    assert not get_file_storage(app).root.exists()


def test_each_dependency_can_be_injected_separately(
    config: Config, memory_database: Database, storage: LocalFileStorage
) -> None:
    recognizer = FakeRecognizer()

    app = create_app(
        config,
        database=memory_database,
        file_storage=storage,
        recognizer=recognizer,
    )

    assert get_database(app) is memory_database
    assert get_file_storage(app) is storage
    assert get_recognizer(app) is recognizer


def test_partial_injection_keeps_config_defaults_for_the_rest(
    tmp_config: Config, memory_database: Database
) -> None:
    app = create_app(tmp_config, database=memory_database)

    assert get_database(app) is memory_database
    assert get_file_storage(app).root == tmp_config.file_storage_dir


def test_whole_dependency_set_can_be_injected(
    config: Config, memory_database: Database, storage: LocalFileStorage
) -> None:
    bundle = Dependencies(
        database=memory_database, file_storage=storage, recognizer=FakeRecognizer()
    )

    app = create_app(config, dependencies=bundle)

    assert get_dependencies(app) is bundle


def test_mixing_bundle_and_single_injections_is_refused(
    config: Config, memory_database: Database, storage: LocalFileStorage
) -> None:
    """同时传整组和单项无法判断谁生效，直接拒绝，不猜。"""
    bundle = Dependencies(
        database=memory_database, file_storage=storage, recognizer=FakeRecognizer()
    )

    with pytest.raises(ValueError, match="不要同时传"):
        create_app(config, dependencies=bundle, database=memory_database)


def test_apps_do_not_share_dependencies(tmp_config: Config) -> None:
    first = create_app(tmp_config)
    second = create_app(tmp_config)

    assert get_dependencies(first) is not get_dependencies(second)
    assert get_database(first) is not get_database(second)


def test_log_output_is_utf8_and_idempotent(test_env: dict[str, str]) -> None:
    """日志按 UTF-8 写出，中文路径与中文消息不乱码；重复配置不会挂重复处理器。"""
    config = Config.from_env(test_env)
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="utf-8", line_buffering=True)
    logger = logging.getLogger(LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level

    try:
        configure_log_output(config, stream=stream)
        configure_log_output(config, stream=stream)

        assert len(logger.handlers) == 1

        logger.info("启动摘要 storage=%s", "E:/最近事务/inventory/backend/var")
        stream.flush()
        written = buffer.getvalue().decode("utf-8")

        assert "启动摘要" in written
        assert "最近事务" in written
        assert "INFO" in written
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        for handler in saved_handlers:
            logger.addHandler(handler)
        logger.setLevel(saved_level)
