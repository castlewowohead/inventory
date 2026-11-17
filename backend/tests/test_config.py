"""配置加载与校验的测试（步骤 01）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import (
    BASE_DIR,
    IMPLEMENTED_OCR_PROVIDERS,
    OCR_PROVIDER_DASHSCOPE,
    OCR_PROVIDER_FAKE,
    Config,
    mask_database_url,
)
from src.errors import ConfigError


def test_defaults_allow_local_start_without_env_file() -> None:
    """没有 .env 时也能解析出一套本地默认配置。"""
    config = Config.from_env({})

    assert config.app_env == "development"
    assert config.log_level == "INFO"
    assert config.database_url == "sqlite:///var/inventory.db"
    assert config.ocr_provider == "fake"
    assert config.host == "127.0.0.1"
    assert config.port == 5000


def test_injected_mapping_ignores_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """传入映射时不读 os.environ，开发者本机的环境变量不会漏进测试。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "machine-local-secret")

    config = Config.from_env({})

    assert config.app_env == "development"
    assert config.secret_key == "change-me"


def test_relative_storage_path_resolves_against_backend_dir(test_env: dict[str, str]) -> None:
    config = Config.from_env(test_env)

    assert config.file_storage_dir.is_absolute()
    assert config.file_storage_dir == BASE_DIR / "var" / "test-uploads"


def test_absolute_storage_path_is_kept(test_env: dict[str, str], tmp_path: Path) -> None:
    test_env["FILE_STORAGE_DIR"] = str(tmp_path)

    assert Config.from_env(test_env).file_storage_dir == tmp_path


def test_blank_value_falls_back_to_default(test_env: dict[str, str]) -> None:
    test_env["HOST"] = "   "

    assert Config.from_env(test_env).host == "127.0.0.1"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("APP_ENV", "staging"),
        ("LOG_LEVEL", "VERBOSE"),
        ("PORT", "not-a-number"),
        ("PORT", "0"),
        ("PORT", "70000"),
        ("OCR_PROVIDER", "qwen"),
    ],
)
def test_invalid_values_are_rejected(test_env: dict[str, str], key: str, value: str) -> None:
    test_env[key] = value

    with pytest.raises(ConfigError):
        Config.from_env(test_env)


def test_unimplemented_ocr_provider_error_lists_implemented_ones(
    test_env: dict[str, str],
) -> None:
    """配置了尚未实现的识别服务要直接失败，而不是静默退回 fake。

    报错里要把**已实现的**那几个列出来：这条错误通常出现在部署现场，看错误信息
    就该知道能填什么，而不是回头翻文档。
    """
    test_env["OCR_PROVIDER"] = "qwen"

    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(test_env)

    message = str(excinfo.value)
    for provider in IMPLEMENTED_OCR_PROVIDERS:
        assert provider in message


def test_real_provider_without_credentials_fails_fast(
    test_env: dict[str, str],
) -> None:
    """选了真实厂商却没凭据：宁可启动失败，也不静默退回假数据。

    步骤 08 的验收条件是「无密钥时手工和表格通路仍可用」——那条路是把
    `OCR_PROVIDER` 设回 `fake`，不是让真实适配器带着空 Key 跑。
    """
    test_env["OCR_PROVIDER"] = OCR_PROVIDER_DASHSCOPE

    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(test_env)

    message = str(excinfo.value)
    assert "OCR_API_KEY" in message
    # 要给出可执行的出路，而不只是说"缺东西"。
    assert OCR_PROVIDER_FAKE in message


def test_real_provider_accepts_step07_credentials_variable_names(
    test_env: dict[str, str],
) -> None:
    """步骤 07 评测脚本用的 `DASHSCOPE_*` 变量名要能直接复用。

    本机 `.env` 里已经有这两个值了。适配器自己的变量名是与厂商无关的
    `OCR_API_KEY` / `OCR_BASE_URL`，但不能逼人把同一个值抄两遍。
    """
    test_env["OCR_PROVIDER"] = OCR_PROVIDER_DASHSCOPE
    test_env["DASHSCOPE_API_KEY"] = "sk-test-not-a-real-key"
    test_env["DASHSCOPE_BASE_URL"] = "https://example-workspace.maas.aliyuncs.com/v1"

    config = Config.from_env(test_env)

    assert config.has_ocr_credentials
    assert config.ocr_api_key == "sk-test-not-a-real-key"
    assert config.ocr_base_url.endswith("/v1")
    # 专用名优先：两边都给时用本项目自己的变量名。
    config_with_own = Config.from_env(
        dict(test_env, OCR_API_KEY="sk-own-name", OCR_BASE_URL="https://own.example/v1")
    )
    assert config_with_own.ocr_api_key == "sk-own-name"
    assert config_with_own.ocr_base_url == "https://own.example/v1"


def test_rejected_models_are_refused_with_the_measured_reason(
    test_env: dict[str, str],
) -> None:
    """步骤 07 落选的型号配上去要报错，并说明落选理由。

    `qwen-vl-ocr` 印刷件字段准确率 48.9%，还是唯一一个把 15 行空表格填满的模型。
    允许它被静默配进去，等于让「配了却更差」这件事没人发现。
    """
    test_env["OCR_MODELS"] = "qwen3.5-ocr,qwen-vl-ocr"

    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(test_env)

    assert "qwen-vl-ocr" in str(excinfo.value)
    assert "48.9" in str(excinfo.value)


def test_duplicate_models_are_refused(test_env: dict[str, str]) -> None:
    """同一个型号写两遍会让「多模型一致」变成和自己比，必须拒绝。"""
    test_env["OCR_MODELS"] = "qwen3.5-ocr,qwen3.5-ocr"

    with pytest.raises(ConfigError, match="重复"):
        Config.from_env(test_env)


def test_api_key_never_appears_in_summary_or_repr(test_env: dict[str, str]) -> None:
    """密钥不进日志：摘要只报「有没有」，repr 里是掩码。

    连前几位都不给——日志会被复制进工单与聊天记录，前缀足以泄露 Key 所属账号。
    """
    secret = "sk-this-must-not-leak-anywhere"
    test_env["OCR_PROVIDER"] = OCR_PROVIDER_DASHSCOPE
    test_env["OCR_API_KEY"] = secret
    test_env["OCR_BASE_URL"] = "https://example-workspace.maas.aliyuncs.com/v1"

    config = Config.from_env(test_env)

    assert secret not in repr(config)
    assert secret not in str(config.safe_summary())
    assert config.safe_summary()["ocr_api_key_present"] is True
    # endpoint 不是秘密：排查时第一个要看的就是"连到了哪个域名"。
    assert config.safe_summary()["ocr_base_url"].startswith("https://")


def test_production_rejects_placeholder_secret(test_env: dict[str, str]) -> None:
    test_env["APP_ENV"] = "production"
    test_env["SECRET_KEY"] = "change-me"

    with pytest.raises(ConfigError, match="SECRET_KEY"):
        Config.from_env(test_env)


def test_production_accepts_real_secret(test_env: dict[str, str]) -> None:
    test_env["APP_ENV"] = "production"
    test_env["SECRET_KEY"] = "0f3a9c7d2b6e4f10a5c8d9e2b4f6a8c0"

    config = Config.from_env(test_env)

    assert config.is_production is True
    assert config.is_development is False


def test_repr_masks_secret_key(test_env: dict[str, str]) -> None:
    config = Config.from_env(test_env)

    rendered = repr(config)

    assert test_env["SECRET_KEY"] not in rendered
    assert "***" in rendered


def test_safe_summary_has_no_secret_and_masks_db_password(test_env: dict[str, str]) -> None:
    test_env["DATABASE_URL"] = "postgresql://erp_user:sup3r-s3cret@db.internal:5432/inventory"

    summary = Config.from_env(test_env).safe_summary()

    assert "secret_key" not in summary
    assert "sup3r-s3cret" not in summary["database_url"]
    assert summary["database_url"] == "postgresql://erp_user:***@db.internal:5432/inventory"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///var/inventory.db", "sqlite:///var/inventory.db"),
        ("postgresql://user@host/db", "postgresql://user@host/db"),
        ("postgresql://user:pw@host/db", "postgresql://user:***@host/db"),
        ("not-a-url", "not-a-url"),
    ],
)
def test_mask_database_url(url: str, expected: str) -> None:
    assert mask_database_url(url) == expected
