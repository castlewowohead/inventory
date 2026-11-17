"""配置加载与校验。

配置从一个映射读取（默认 os.environ），解析结果是不可变的 Config 对象，
由应用工厂注入。业务代码通过注入拿到配置，不在运行时直接读环境变量，
测试因此可以注入独立配置而不污染进程环境。

校验失败抛 ConfigError，由启动入口报出：宁可启动失败，也不带着错误配置运行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.errors import ConfigError

# backend/ 目录。相对路径配置一律相对本目录解析，与从哪里执行命令无关。
BASE_DIR = Path(__file__).resolve().parent.parent

VALID_APP_ENVS = ("development", "testing", "production")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# .env.example 里只允许占位密钥；生产环境必须换成真实值。
PLACEHOLDER_SECRETS = frozenset(
    {"change-me", "changeme", "replace-me", "secret", "password"}
)

# 已实现的识别器。`fake` 是明确标识的假数据实现（没有凭据也能启动与跑测试）；
# `dashscope` 是步骤 08 接入的阿里云 Model Studio 适配器，型号由步骤 07 的实测选出。
OCR_PROVIDER_FAKE = "fake"
OCR_PROVIDER_DASHSCOPE = "dashscope"
IMPLEMENTED_OCR_PROVIDERS = (OCR_PROVIDER_FAKE, OCR_PROVIDER_DASHSCOPE)

# 步骤 07 选出的生产配置：主模型 + 第二意见，**并行跑这 2 个**。
# 顺序有意义：第一个是主模型（印刷件字段准确率 96.6%），其后是交叉验证用的第二意见。
# ⚠️ 改这份名单等于改一个被测配置，准确率结论不能沿用，要重跑 tools/eval_adapter.py。
DEFAULT_OCR_MODELS = ("qwen3.5-ocr", "qwen3-vl-plus")

# 落选与不可用的型号，配置里写了就直接拒绝启动，免得"配了却更差"而没人发现。
# 数据见 docs/impl_record.md 步骤 07：`qwen-vl-ocr` 印刷件字段准确率 48.9%，且是唯一一个
# 把 15 行空表格填满的模型；`vanchin/deepseek-ocr` 78 次调用全部「产品未开通」。
REJECTED_OCR_MODELS: dict[str, str] = {
    "qwen-vl-ocr": "步骤 07 落选：印刷件字段准确率 48.9%，且会把空表格行格填满",
    "vanchin/deepseek-ocr": "步骤 07 实测「产品未开通」，需先在百炼控制台单独开通产品",
}

# 提取任务的执行方式。
# - `thread`：应用内后台线程池跑，提取**不挂在 HTTP 请求里**（步骤 08.3g 的要求）。
# - `inline`：在当前请求里直接跑完。给测试与「本地即时路径」用。
# ⚠️ 两种方式跑的是**同一份提取代码**，只差一个调度器；任务行一样落库，于是
# `GET /api/invoices/<id>/extraction` 在两种方式下都能查到状态。
OCR_WORKER_THREAD = "thread"
OCR_WORKER_INLINE = "inline"
OCR_WORKER_MODES = (OCR_WORKER_THREAD, OCR_WORKER_INLINE)

MASK = "***"

DEFAULTS: dict[str, str] = {
    "APP_ENV": "development",
    "LOG_LEVEL": "INFO",
    "SECRET_KEY": "change-me",
    "DATABASE_URL": "sqlite:///var/inventory.db",
    "FILE_STORAGE_DIR": "var/uploads",
    "UPLOAD_MAX_BYTES": str(20 * 1024 * 1024),
    "OCR_PROVIDER": OCR_PROVIDER_FAKE,
    "OCR_MODELS": ",".join(DEFAULT_OCR_MODELS),
    "OCR_BASE_URL": "",
    "OCR_API_KEY": "",
    "OCR_TIMEOUT_SECONDS": "240",
    "OCR_MAX_ATTEMPTS": "3",
    "OCR_WORKER_MODE": OCR_WORKER_THREAD,
    "OCR_WORKER_THREADS": "2",
    "HOST": "127.0.0.1",
    "PORT": "5000",
}

# 凭据的兼容变量名：步骤 07 的评测脚本用的是 `DASHSCOPE_API_KEY` /
# `DASHSCOPE_BASE_URL`，本机 `.env` 里已经有了。适配器自己的变量名是 `OCR_API_KEY` /
# `OCR_BASE_URL`（与厂商无关，换厂商不用改变量名）；两者都读，专用名优先，
# 这样既不必让人把 `.env` 里的值再抄一遍，也不把厂商名字焊进配置契约。
OCR_API_KEY_FALLBACKS = ("DASHSCOPE_API_KEY",)
OCR_BASE_URL_FALLBACKS = ("DASHSCOPE_BASE_URL",)

# 老域名自 2026-09-30 起不再支持新功能，因此**不设默认 endpoint**：要用真实适配器
# 就必须显式给出工作空间专属域名（百炼控制台「业务空间管理」页的 API Host 列）。
# 猜一个默认域名的代价是「连上了但用的是将被弃用的那条路」，而且不报错。


def mask_database_url(url: str) -> str:
    """隐去连接串里的密码，用于日志与诊断输出。"""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    userinfo, host = rest.rsplit("@", 1)
    if ":" in userinfo:
        user, _password = userinfo.split(":", 1)
        userinfo = f"{user}:{MASK}"
    return f"{scheme}://{userinfo}@{host}"


@dataclass(frozen=True)
class Config:
    """一次解析完成的运行配置。"""

    app_env: str
    log_level: str
    secret_key: str
    database_url: str
    file_storage_dir: Path
    upload_max_bytes: int
    ocr_provider: str
    ocr_models: tuple[str, ...]
    ocr_base_url: str
    ocr_api_key: str
    ocr_timeout_seconds: int
    ocr_max_attempts: int
    ocr_worker_mode: str
    ocr_worker_threads: int
    host: str
    port: int

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_testing(self) -> bool:
        return self.app_env == "testing"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def ocr_is_fake(self) -> bool:
        """当前识别器是否为明确标识的假数据实现。"""
        return self.ocr_provider == OCR_PROVIDER_FAKE

    @property
    def has_ocr_credentials(self) -> bool:
        """真实适配器的凭据是否齐备。

        **缺凭据不是启动失败**：步骤 08 的验收条件之一是「无密钥时手工和表格通路
        仍可用」。所以配置解析只在**显式选了真实厂商**时才要求凭据；选 `fake` 时
        这里为 False，上传与审核照常工作，只是不会有真实提取。
        """
        return bool(self.ocr_api_key and self.ocr_base_url)

    @property
    def ocr_primary_model(self) -> str | None:
        """主模型。交叉验证的第二意见是其后那些。"""
        return self.ocr_models[0] if self.ocr_models else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        """从映射解析配置。env 为 None 时读进程环境变量，否则只读传入的映射。"""
        source: Mapping[str, str] = os.environ if env is None else env

        def value(name: str) -> str:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return DEFAULTS[name]
            return raw.strip()

        def value_with_fallbacks(name: str, fallbacks: Sequence[str]) -> str:
            """取配置值；本项目自己的变量名为空时，再试兼容变量名。"""
            own = value(name)
            if own:
                return own
            for alias in fallbacks:
                raw = source.get(alias)
                if raw is not None and raw.strip():
                    return raw.strip()
            return ""

        def positive_int(name: str, *, minimum: int, maximum: int) -> int:
            raw = value(name)
            try:
                parsed = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name}={raw!r} 不是整数") from exc
            if not minimum <= parsed <= maximum:
                raise ConfigError(f"{name}={parsed} 超出 {minimum}—{maximum}")
            return parsed

        app_env = value("APP_ENV").lower()
        if app_env not in VALID_APP_ENVS:
            raise ConfigError(
                f"APP_ENV={app_env!r} 无效，可选值：{', '.join(VALID_APP_ENVS)}"
            )

        log_level = value("LOG_LEVEL").upper()
        if log_level not in VALID_LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL={log_level!r} 无效，可选值：{', '.join(VALID_LOG_LEVELS)}"
            )

        secret_key = value("SECRET_KEY")
        if app_env == "production" and secret_key.lower() in PLACEHOLDER_SECRETS:
            raise ConfigError("生产环境的 SECRET_KEY 仍是占位值；请设置本机生成的真实密钥。")

        ocr_provider = value("OCR_PROVIDER").lower()
        if ocr_provider not in IMPLEMENTED_OCR_PROVIDERS:
            raise ConfigError(
                f"OCR_PROVIDER={ocr_provider!r} 尚未实现。当前支持 "
                f"{', '.join(IMPLEMENTED_OCR_PROVIDERS)}"
                f"（{OCR_PROVIDER_FAKE} 是明确标识的假数据，"
                f"{OCR_PROVIDER_DASHSCOPE} 是阿里云 Model Studio）。"
            )

        ocr_models = tuple(
            name.strip() for name in value("OCR_MODELS").split(",") if name.strip()
        )
        for model in ocr_models:
            reason = REJECTED_OCR_MODELS.get(model)
            if reason is not None:
                raise ConfigError(f"OCR_MODELS 里的 {model!r} 不可用：{reason}")
        if len(set(ocr_models)) != len(ocr_models):
            raise ConfigError(f"OCR_MODELS 里有重复型号：{value('OCR_MODELS')!r}")

        ocr_api_key = value_with_fallbacks("OCR_API_KEY", OCR_API_KEY_FALLBACKS)
        ocr_base_url = value_with_fallbacks("OCR_BASE_URL", OCR_BASE_URL_FALLBACKS)

        if ocr_provider == OCR_PROVIDER_DASHSCOPE:
            # 选了真实厂商却没凭据：这是**配置矛盾**，不是「凭据还没到」。
            # 启动失败比静默退回假数据好——后者会让人以为真实识别已经接通。
            missing = [
                name
                for name, present in (
                    ("OCR_API_KEY（或 DASHSCOPE_API_KEY）", bool(ocr_api_key)),
                    ("OCR_BASE_URL（或 DASHSCOPE_BASE_URL）", bool(ocr_base_url)),
                )
                if not present
            ]
            if missing:
                raise ConfigError(
                    f"OCR_PROVIDER={ocr_provider!r} 缺少 {'、'.join(missing)}。"
                    "没有凭据时请把 OCR_PROVIDER 设回 "
                    f"{OCR_PROVIDER_FAKE}——手工录入与表格导入两条通路不需要凭据。"
                )
            if not ocr_models:
                raise ConfigError(
                    f"OCR_PROVIDER={ocr_provider!r} 时 OCR_MODELS 不能为空；"
                    f"步骤 07 选出的生产配置是 {','.join(DEFAULT_OCR_MODELS)}。"
                )

        ocr_worker_mode = value("OCR_WORKER_MODE").lower()
        if ocr_worker_mode not in OCR_WORKER_MODES:
            raise ConfigError(
                f"OCR_WORKER_MODE={ocr_worker_mode!r} 无效，"
                f"可选值：{', '.join(OCR_WORKER_MODES)}"
            )

        ocr_timeout_seconds = positive_int(
            "OCR_TIMEOUT_SECONDS", minimum=5, maximum=1800
        )
        ocr_max_attempts = positive_int("OCR_MAX_ATTEMPTS", minimum=1, maximum=10)
        ocr_worker_threads = positive_int("OCR_WORKER_THREADS", minimum=1, maximum=16)

        raw_port = value("PORT")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigError(f"PORT={raw_port!r} 不是整数") from exc
        if not 1 <= port <= 65535:
            raise ConfigError(f"PORT={port} 超出 1—65535")

        storage_dir = Path(value("FILE_STORAGE_DIR"))
        if not storage_dir.is_absolute():
            storage_dir = BASE_DIR / storage_dir

        # 单个上传文件的上限（步骤 05 的发票上传）。手机拍的单据照片通常 2—6 MB，
        # 多页 PDF 可能到十几 MB，默认 20 MB 留足余量。它同时被用来设置 Flask 的
        # MAX_CONTENT_LENGTH（见 src/factory.py）——没有请求体上限的话，一个超大
        # 上传会先把内存吃掉，再谈不上校验。
        raw_upload_max = value("UPLOAD_MAX_BYTES")
        try:
            upload_max_bytes = int(raw_upload_max)
        except ValueError as exc:
            raise ConfigError(
                f"UPLOAD_MAX_BYTES={raw_upload_max!r} 不是整数"
            ) from exc
        if upload_max_bytes < 1024:
            raise ConfigError(
                f"UPLOAD_MAX_BYTES={upload_max_bytes} 太小（至少 1024 字节）："
                "连一张最小的单据照片都传不进来。"
            )

        return cls(
            app_env=app_env,
            log_level=log_level,
            secret_key=secret_key,
            database_url=value("DATABASE_URL"),
            file_storage_dir=storage_dir,
            upload_max_bytes=upload_max_bytes,
            ocr_provider=ocr_provider,
            ocr_models=ocr_models,
            ocr_base_url=ocr_base_url,
            ocr_api_key=ocr_api_key,
            ocr_timeout_seconds=ocr_timeout_seconds,
            ocr_max_attempts=ocr_max_attempts,
            ocr_worker_mode=ocr_worker_mode,
            ocr_worker_threads=ocr_worker_threads,
            host=value("HOST"),
            port=port,
        )

    def safe_summary(self) -> dict[str, Any]:
        """可写入日志的配置摘要：不含密钥，连接串中的密码已隐去。

        ⚠️ `ocr_api_key` 只报「有没有」，连前几位都不给——日志会被复制进工单与
        聊天记录，前缀足以泄露 Key 所属账号。endpoint 不是秘密，照原样给出，
        因为「连到了哪个域名」是排查时第一个要看的东西。
        """
        return {
            "app_env": self.app_env,
            "log_level": self.log_level,
            "database_url": mask_database_url(self.database_url),
            "file_storage_dir": str(self.file_storage_dir),
            "upload_max_bytes": self.upload_max_bytes,
            "ocr_provider": self.ocr_provider,
            "ocr_models": list(self.ocr_models),
            "ocr_base_url": self.ocr_base_url,
            "ocr_api_key_present": bool(self.ocr_api_key),
            "ocr_timeout_seconds": self.ocr_timeout_seconds,
            "ocr_max_attempts": self.ocr_max_attempts,
            "ocr_worker_mode": self.ocr_worker_mode,
            "ocr_worker_threads": self.ocr_worker_threads,
            "host": self.host,
            "port": self.port,
        }

    def __repr__(self) -> str:
        fields: dict[str, Any] = self.safe_summary()
        fields["secret_key"] = MASK
        fields["ocr_api_key"] = MASK if self.ocr_api_key else ""
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(fields.items()))
        return f"Config({rendered})"
