"""Alembic 运行环境。

连接串来源按优先级：

1. `-x db_url=...`（命令行临时覆盖，测试与临时数据库用它）；
2. `alembic.ini` 的 `sqlalchemy.url`（本项目留空，仅供程序化调用时写入）；
3. `backend/.env` 与进程环境变量里的 `DATABASE_URL`（正常使用的来源）。

迁移复用 `src.db.Database` 创建 Engine，而不是自己 `engine_from_config`：
这样迁移与应用用的是同一套连接设置——相对 SQLite 路径按 `backend/` 解析、
SQLite 外键约束打开——不会出现「应用连 A 库、迁移改 B 库」这类只在其中一条
通路出现的差异。

**SQLite 的外键在迁移期间刻意关掉，迁移完再打开并校验。** 原因是批处理模式改表的
实际做法是「建新表—搬数据—DROP 旧表—改名」，而 `DROP` 一张**被别的表引用**的表
在外键打开时必然失败（`FOREIGN KEY constraint failed`）——`purchases` 被
`purchase_revisions` 引用，所以 78a4af6ad755 改 `purchases.food_item_id` 的可空性时
撞上了这一点。SQLite 官方文档对这套改表流程的要求也正是先 `PRAGMA foreign_keys=OFF`。
关掉不等于不管：迁移结束后重新打开，并用 `PRAGMA foreign_key_check` 实际核对一遍，
有违规就让 `alembic upgrade` 直接失败，而不是留着坏数据继续跑。两个辅助函数放在
`src/db.py`（那里已经管着 SQLite 的其它细节），这样它们可以被直接测试——本文件是
alembic 的环境脚本，一导入就会执行迁移，没法在测试里 import。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy.engine import URL, Connection

from src.config import BASE_DIR, Config
from src.db import (
    Database,
    check_sqlite_foreign_keys,
    resolve_database_url,
    set_sqlite_foreign_keys,
)
from src.models import Base

config = context.config

# 程序化调用（如测试里 command.upgrade(...)）传入 configure_logger=False 时跳过：
# 否则 fileConfig 会接管调用方进程的日志配置。disable_existing_loggers=False
# 保证即使应用这套配置，也不会把应用自己的 logger 关掉。
if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 自动生成迁移的对比基准。业务表随 Feature 步骤加入 src/models.py 后会自动纳入对比。
target_metadata = Base.metadata


def database_url() -> URL:
    """按上述优先级确定连接串。"""
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if not override:
        override = config.get_main_option("sqlalchemy.url", None)
    if override:
        return resolve_database_url(override)

    # 文件缺失时静默跳过；已存在的进程环境变量优先，.env 不覆盖它们。
    load_dotenv(BASE_DIR / ".env")
    return resolve_database_url(Config.from_env().database_url)


def configure_context(**kwargs: object) -> None:
    """统一的 context.configure 参数。

    render_as_batch：SQLite 不支持大多数 ALTER，批处理模式改为「建新表—搬数据—
    换名」。它依赖约束有稳定名字，命名约定见 src/models.py。
    """
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations(connection: Connection) -> None:
    is_sqlite = connection.dialect.name == "sqlite"
    # 必须在 begin_transaction() 之前关：见 set_sqlite_foreign_keys 的说明。
    if is_sqlite:
        set_sqlite_foreign_keys(connection, False)

    configure_context(connection=connection)
    with context.begin_transaction():
        context.run_migrations()

    # SQLite 被当作「非事务性 DDL」，所以上面的 with 块并不提交；而批处理改表留下的
    # 写入让 pysqlite 真的处在事务中，此时 PRAGMA 会被静默忽略。先提交，再开外键。
    # 提交本身也是对的：`alembic upgrade` 返回时迁移就该是持久的。
    if connection.in_transaction():
        connection.commit()

    if is_sqlite:
        set_sqlite_foreign_keys(connection, True)
        check_sqlite_foreign_keys(connection)


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL，不连接数据库。

    这里**不**管外键开关：离线模式只是把 SQL 打出来，由执行它的人负责。批处理改表的
    SQL 在 SQLite 上需要外面套一层 `PRAGMA foreign_keys=OFF` / `=ON`，在线模式由
    run_migrations() 处理，离线模式请自行加上。
    """
    configure_context(
        url=database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：在实际数据库上执行迁移。

    调用方可以通过 `config.attributes["connection"]` 传入已有连接（内存数据库的
    测试需要如此，换连接就是换数据库）；否则按连接串自行创建 Engine。
    """
    existing: Connection | None = config.attributes.get("connection")
    if existing is not None:
        run_migrations(existing)
        return

    database = Database(database_url())
    try:
        with database.engine.connect() as connection:
            run_migrations(connection)
    finally:
        database.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
