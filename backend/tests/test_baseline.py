"""步骤 00 的基线自检。

本文件不测试业务规则，只校验「工程基线」这一步骤 00 的交付物本身是否自洽：
Python 版本下限、依赖声明与实际安装版本一致、骨架目录存在、运行期数据与密钥
被忽略规则覆盖、配置示例里没有真实密钥。
"""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent

# 依赖声明中记录的下限；实际验证组合见 README。
MIN_PYTHON = (3, 12)

REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")

SKELETON_DIRS = (
    "src",
    "src/api",
    "src/services",
    "src/repositories",
    "src/integrations",
    "tests",
)

# 步骤 02 的基础设施交付物。migrations/ 由 `alembic init` 创建，不是 Python 包。
STEP02_PATHS = (
    "alembic.ini",
    "migrations/env.py",
    "migrations/script.py.mako",
    "migrations/versions",
    "src/db.py",
    "src/models.py",
    "src/contracts.py",
    "src/integrations/file_storage.py",
    "src/integrations/ocr.py",
)

# 运行期产物与本机密钥必须被忽略，不能进入源码交付。
REQUIRED_IGNORE_PATTERNS = (
    ".venv/",
    "__pycache__/",
    ".env",
    "var/*",
    "*.db",
    "samples/real/",
)

SECRET_NAME_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")

# 配置示例中允许出现的占位值（小写比较）。
ALLOWED_SECRET_PLACEHOLDERS = {"", "change-me", "fake", "replace-me"}


def read_text(relative_path: str) -> str:
    return (BACKEND_DIR / relative_path).read_text(encoding="utf-8")


def parse_pins(relative_path: str) -> dict[str, str]:
    """解析 `name==version` 形式的依赖声明，忽略注释与 -r 引用。"""
    pins: dict[str, str] = {}
    for raw_line in read_text(relative_path).splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        assert "==" in line, f"{relative_path} 中的依赖未固定版本: {line}"
        name, version = line.split("==", 1)
        pins[name.strip()] = version.strip()
    return pins


def all_pins() -> dict[str, str]:
    merged: dict[str, str] = {}
    for name in REQUIREMENT_FILES:
        merged.update(parse_pins(name))
    return merged


def test_python_version_meets_declared_floor() -> None:
    assert sys.version_info[:2] >= MIN_PYTHON, (
        f"当前 Python {sys.version.split()[0]} 低于声明下限 "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    )


def test_requirement_files_pin_every_dependency() -> None:
    pins = all_pins()
    assert pins, "依赖声明为空"
    expected = {"Flask", "SQLAlchemy", "alembic", "python-dotenv", "pytest"}
    assert expected <= set(pins), f"缺少基线依赖声明: {sorted(expected - set(pins))}"


@pytest.mark.parametrize("package", sorted(all_pins()))
def test_installed_version_matches_pin(package: str) -> None:
    pinned = all_pins()[package]
    try:
        installed = metadata.version(package)
    except metadata.PackageNotFoundError:  # pragma: no cover - 环境未按声明安装
        pytest.fail(f"{package} 未安装；请先执行 python -m pip install -r requirements-dev.txt")
    assert installed == pinned, (
        f"{package} 实际安装 {installed}，依赖声明为 {pinned}；"
        "请同步 requirements 与 README 的验证组合"
    )


@pytest.mark.parametrize("relative_path", SKELETON_DIRS)
def test_skeleton_directory_exists(relative_path: str) -> None:
    assert (BACKEND_DIR / relative_path).is_dir(), f"缺少骨架目录 {relative_path}"


@pytest.mark.parametrize("relative_path", SKELETON_DIRS[:-1])
def test_skeleton_packages_are_importable(relative_path: str) -> None:
    assert (BACKEND_DIR / relative_path / "__init__.py").is_file(), (
        f"{relative_path} 不是 Python 包"
    )


@pytest.mark.parametrize("relative_path", STEP02_PATHS)
def test_infrastructure_files_are_delivered(relative_path: str) -> None:
    assert (BACKEND_DIR / relative_path).exists(), f"缺少步骤 02 的交付物 {relative_path}"


def test_migrations_dir_is_not_a_python_package() -> None:
    """alembic 通过 prepend_sys_path 找到 env.py，不需要也不应该把它变成包。"""
    assert not (BACKEND_DIR / "migrations" / "__init__.py").exists()


@pytest.mark.parametrize("pattern", REQUIRED_IGNORE_PATTERNS)
def test_ignore_rules_cover_runtime_artifacts(pattern: str) -> None:
    lines = {line.strip() for line in read_text(".gitignore").splitlines()}
    assert pattern in lines, f".gitignore 缺少忽略规则 {pattern}"


def test_env_example_exists_and_env_is_ignored() -> None:
    assert (BACKEND_DIR / ".env.example").is_file(), "缺少配置示例 .env.example"
    assert not (BACKEND_DIR / ".env").exists() or ".env" in {
        line.strip() for line in read_text(".gitignore").splitlines()
    }, "本机 .env 存在但未被忽略"


def test_env_example_contains_no_real_secrets() -> None:
    offenders = []
    for raw_line in read_text(".env.example").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if any(hint in name.upper() for hint in SECRET_NAME_HINTS):
            if value.strip().lower() not in ALLOWED_SECRET_PLACEHOLDERS:
                offenders.append(name.strip())
    assert not offenders, f"配置示例中疑似写入真实密钥: {offenders}"


def test_no_old_project_paths_in_delivered_config() -> None:
    """骨架交付物不得引用两个旧项目。"""
    forbidden = ("kitchen-ledger", "restaurant-erp")
    checked = [
        "requirements.txt",
        "requirements-dev.txt",
        ".env.example",
        "pytest.ini",
        ".gitignore",
        "alembic.ini",
    ]
    for relative_path in checked:
        text = read_text(relative_path).lower()
        for token in forbidden:
            assert token not in text, f"{relative_path} 引用了旧项目 {token}"
