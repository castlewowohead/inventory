"""应用工厂：创建应用、加载配置、组装依赖、注册路由与错误处理。

依赖直接组装，不引入 DI 容器。配置与三个基础设施依赖（数据库、文件存储、识别器）
都可以由调用方注入；未注入时按配置创建默认实现。测试因此可以用临时目录的数据库和
存储、用假识别器，而不碰本机的 `var/` 与任何外部服务。

组装规则：

- **构造依赖不产生外部副作用。** 创建 `Database` 不连数据库（Engine 懒创建），
  创建 `LocalFileStorage` 不建目录，创建识别器不发网络请求。因此
  `python app.py` 启动完成时，既没有连库也没有连任何 OCR 服务。
  真实 OCR 适配器在步骤 08 加入时必须守同一条规则。
- **依赖只经注入传递。** 业务代码从注入拿到依赖，不读模块级全局量，
  也不在运行时直接读环境变量。
"""

from __future__ import annotations

import io
import logging
import sys
from dataclasses import dataclass

from flask import Flask, Response, request

from src.api import register_blueprints
from src.config import Config
from src.db import Database
from src.errors import register_error_handlers
from src.integrations.file_storage import LocalFileStorage
from src.integrations.ocr import Recognizer, build_recognizer
from src.services.extraction import ExtractionService
from src.services.invoices import InvoiceService

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# 一个请求里最多几个上传文件（用于算请求体上限）。手机一次选几页单据，4 个够用；
# 更多文件可以分几次传，`POST /api/invoices/<id>/files` 支持补传。
MAX_UPLOAD_FILES_PER_REQUEST = 4

# 应用日志使用的 logger 名。Flask(__name__) 让 app.logger 取的就是这个 logger，
# 入口因此可以在创建应用之前先把输出配好，启动摘要也能用统一格式打出来。
LOGGER_NAME = __name__

# 应用配置对象在 app.config 中的键。视图经由它取配置，不读模块级全局量。
CONFIG_KEY = "APP_CONFIG"

# 基础设施依赖在 app.config 中的键。
DEPENDENCIES_KEY = "APP_DEPENDENCIES"


@dataclass(frozen=True)
class Dependencies:
    """一个应用实例用到的基础设施依赖。

    Service 从这里取数据库、文件存储与识别器；API 层只把它们转交给 Service。
    """

    database: Database
    file_storage: LocalFileStorage
    recognizer: Recognizer

    @classmethod
    def from_config(cls, config: Config) -> "Dependencies":
        """按配置创建默认实现。此时不连库、不建目录、不访问外部服务。"""
        return cls(
            database=Database.from_config(config),
            file_storage=LocalFileStorage(config.file_storage_dir),
            recognizer=build_recognizer(config),
        )


def create_app(
    config: Config | None = None,
    *,
    dependencies: Dependencies | None = None,
    database: Database | None = None,
    file_storage: LocalFileStorage | None = None,
    recognizer: Recognizer | None = None,
) -> Flask:
    """创建并装配 Flask 应用。

    config 为 None 时从环境变量解析；配置不合法会抛 ConfigError，应用不会创建成功。
    dependencies 为 None 时按配置创建；单独传 database / file_storage / recognizer
    可只替换其中一个（测试常用），其余仍按配置创建。
    日志输出位置不在这里决定，见 configure_log_output()。
    """
    config = config or Config.from_env()
    dependencies = _assemble(config, dependencies, database, file_storage, recognizer)

    # 不使用模板与静态目录：本后端只提供 JSON 接口，前端另行交付。
    app = Flask(__name__, static_folder=None, template_folder=None)
    app.config[CONFIG_KEY] = config
    app.config[DEPENDENCIES_KEY] = dependencies
    app.config["SECRET_KEY"] = config.secret_key
    app.config["TESTING"] = config.is_testing
    # 请求体上限。发票上传一次可能带多个文件（多页单据），所以给单文件上限留几倍
    # 余量；超过时 Flask 抛 413，由统一错误处理器变成 JSON（见 src/errors.py 的
    # HTTP_MESSAGES）。没有这个上限的话，一个超大请求会先被整份读进内存，
    # 服务层的大小校验根本来不及生效。
    app.config["MAX_CONTENT_LENGTH"] = config.upload_max_bytes * MAX_UPLOAD_FILES_PER_REQUEST
    # 测试与生产都要求异常经统一处理器变成响应，而不是向上抛出；
    # 开发环境保留 Flask 默认行为，便于使用调试器。
    if config.is_testing or config.is_production:
        app.config["PROPAGATE_EXCEPTIONS"] = False
    # 中文消息按原样输出，不转成 \uXXXX 转义。
    app.json.ensure_ascii = False

    _configure_log_level(config)
    register_error_handlers(app)
    register_blueprints(app)
    _register_request_logging(app)

    summary = config.safe_summary()
    app.logger.info(
        "app created env=%s log_level=%s db=%s storage=%s ocr=%s host=%s port=%s",
        summary["app_env"],
        summary["log_level"],
        summary["database_url"],
        summary["file_storage_dir"],
        summary["ocr_provider"],
        summary["host"],
        summary["port"],
    )
    # 明确记录「此刻还没有连库，识别器是不是假数据实现」，便于核对启动行为。
    app.logger.info(
        "dependencies assembled db=%s db_connected=%s storage=%s ocr=%s ocr_fake=%s",
        dependencies.database.safe_url,
        dependencies.database.engine_created,
        dependencies.file_storage.root,
        dependencies.recognizer.provider,
        dependencies.recognizer.is_fake,
    )
    return app


def _assemble(
    config: Config,
    dependencies: Dependencies | None,
    database: Database | None,
    file_storage: LocalFileStorage | None,
    recognizer: Recognizer | None,
) -> Dependencies:
    """确定最终依赖：整体注入优先，其次逐项替换，最后按配置创建默认实现。"""
    if dependencies is not None:
        if database is not None or file_storage is not None or recognizer is not None:
            raise ValueError(
                "dependencies 与 database / file_storage / recognizer 不要同时传："
                "无法判断哪一个生效"
            )
        return dependencies
    if database is None and file_storage is None and recognizer is None:
        return Dependencies.from_config(config)
    return Dependencies(
        database=database or Database.from_config(config),
        file_storage=file_storage or LocalFileStorage(config.file_storage_dir),
        recognizer=recognizer or build_recognizer(config),
    )


def get_config(app: Flask) -> Config:
    """取出注入的配置对象。"""
    return app.config[CONFIG_KEY]


def get_dependencies(app: Flask) -> Dependencies:
    """取出注入的基础设施依赖。"""
    return app.config[DEPENDENCIES_KEY]


def get_database(app: Flask) -> Database:
    return get_dependencies(app).database


def get_file_storage(app: Flask) -> LocalFileStorage:
    return get_dependencies(app).file_storage


def get_recognizer(app: Flask) -> Recognizer:
    return get_dependencies(app).recognizer


def build_services(app: Flask) -> tuple[InvoiceService, ExtractionService]:
    """造出一对装好的单据服务与提取服务（步骤 08）。

    两者互相需要：提取服务要调 `InvoiceService.apply_extraction()` 写草稿，
    上传要触发提取。构造时解不开这个环，所以**先各造一个、再把钩子装上**。
    这段装配只在这里和 `src/worker.py` 出现，业务代码拿到的是装好的对象。

    服务本身无状态，按请求创建没有问题（与步骤 05 的做法一致）。
    """
    config = get_config(app)
    dependencies = get_dependencies(app)
    invoices = InvoiceService(
        dependencies.database,
        file_storage=dependencies.file_storage,
        recognizer=dependencies.recognizer,
        upload_max_bytes=config.upload_max_bytes,
    )
    extraction = ExtractionService(
        dependencies.database,
        file_storage=dependencies.file_storage,
        recognizer=dependencies.recognizer,
        invoice_service=invoices,
        config=config,
    )
    invoices.attach_extraction(extraction)
    return invoices, extraction


def configure_log_output(
    config: Config, stream: io.TextIOBase | None = None
) -> logging.Handler:
    """配置日志输出位置与格式，由启动入口调用。

    工厂只决定日志级别，输出接到哪里由进程入口决定：这样测试进程不会在全局
    logger 上累积处理器，也不会把日志写进已被接管的输出流。
    重复调用是幂等的——先清掉已有处理器（含 Flask 默认处理器）再挂自己的。
    """
    logger = logging.getLogger(LOGGER_NAME)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    handler = logging.StreamHandler(stream if stream is not None else _utf8_stderr())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, config.log_level))
    return handler


def _configure_log_level(config: Config) -> None:
    """只设级别，不挂处理器。

    记录的内容始终是结构化键值，不含 SECRET_KEY 与连接串密码（见 Config.safe_summary）。
    """
    logging.getLogger(LOGGER_NAME).setLevel(getattr(logging, config.log_level))


class _Utf8Stream(io.TextIOBase):
    """把文本按 UTF-8 写入底层二进制流，不接管该流的生命周期。

    不用 io.TextIOWrapper：它在被回收时会关闭所包装的流，于是连带关掉进程的
    stderr（在测试进程里表现为 "lost sys.stderr"）。本类的 close() 故意是空操作。
    """

    def __init__(self, buffer: io.BufferedIOBase) -> None:
        self._buffer = buffer

    def write(self, text: str) -> int:
        self._buffer.write(text.encode("utf-8", errors="backslashreplace"))
        self._buffer.flush()
        return len(text)

    def flush(self) -> None:
        self._buffer.flush()

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        """不关闭底层流：它属于调用方（通常就是进程的 stderr）。"""


def _utf8_stderr() -> io.TextIOBase:
    """按 UTF-8 写日志的 stderr 包装。

    Windows 控制台默认编码不是 UTF-8，中文路径与中文异常消息直接写出会乱码，
    在非中文代码页上还会抛 UnicodeEncodeError 导致日志丢失。
    """
    buffer = getattr(sys.stderr, "buffer", None)
    if buffer is None:
        return sys.stderr
    return _Utf8Stream(buffer)


def _register_request_logging(app: Flask) -> None:
    """基本访问日志。只记录方法、路径与状态码，不记录请求体与查询串。"""

    @app.after_request
    def log_request(response: Response) -> Response:
        app.logger.info("%s %s -> %s", request.method, request.path, response.status_code)
        return response
