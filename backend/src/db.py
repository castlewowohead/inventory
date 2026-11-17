"""数据库连接、会话与事务边界。

分工：

- 本模块提供 Engine、Session 工厂和一个事务上下文管理器。
- **事务边界由 Service 决定**：Service 用 `Database.unit_of_work()` 包住一次业务
  操作，其中的多次写入共同提交或共同回滚；Repository 只接收已有 Session 做读写，
  自己不提交、不回滚、不创建 Session。API 层不碰 Session。
- 嵌套调用（Service 调 Service）要把外层 Session 传进 `unit_of_work(session)`，
  内层只加入外层事务，不提前提交；提交与回滚始终由最外层那一个作用域决定。

两条刻意的设计：

1. **Engine 懒创建。** 构造 `Database` 不连接数据库，也不导入方言驱动，所以
   `python app.py` 正常启动时不会连库，连接串写了未安装驱动的数据库也不会在启动
   时炸掉无关功能；连接串本身的格式错误仍在构造时就报出来。
2. **相对 SQLite 路径按 `backend/` 解析。** `sqlite:///var/inventory.db` 在 SQLAlchemy
   里是相对当前工作目录的，于是从不同目录执行 `python app.py` 与
   `python -m alembic upgrade head` 会操作两个不同的文件。本模块统一把这类相对路径
   改写成 `backend/` 下的绝对路径，应用与迁移因此总是指向同一个数据库。

数值精度（Numeric / Decimal 在 SQLite 下的实际存储行为）属于步骤 04 的任务，本模块
不声称已解决：这里没有任何数值列，也没有注册数值类型适配。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import BASE_DIR, Config
from src.errors import ConfigError

logger = logging.getLogger(__name__)

# SQLAlchemy 用这些 database 值表示内存数据库，不是磁盘上的相对路径。
IN_MEMORY_DATABASES = frozenset({None, "", ":memory:"})

# SQLite 写锁的等待秒数。只允许一个写入者，撞上时默认是立刻抛
# 「database is locked」；给它一段等待时间，后台 worker 与前台请求同时写的那一瞬
# 就不会变成一次失败。⚠️ 它是等待，不是并发控制（详见 `_create_engine`）。
SQLITE_BUSY_TIMEOUT_SECONDS = 15.0


def set_sqlite_foreign_keys(connection: Connection, enabled: bool) -> None:
    """开/关 SQLite 的外键校验，并**读回来确认真的生效**。

    迁移要用它：批处理模式改表的做法是「建新表—搬数据—DROP 旧表—改名」，而 DROP 一张
    **被别的表引用**的表在外键打开时必然失败（`FOREIGN KEY constraint failed`）。
    SQLite 官方文档对这套改表流程的要求也正是先关掉外键。

    `PRAGMA foreign_keys` 在 SQLite 真的开着事务时是**静默无效**的，所以不能设完就
    当成设好了。这里不能用 `connection.in_transaction()` 判断：SQLAlchemy 2.x 会
    autobegin 一个「逻辑」事务，连接一拿到手它就返回 True，但 pysqlite 要等到第一条
    DML 才真正发 `BEGIN`——那个值与 PRAGMA 到底生不生效无关。唯一可靠的判断是把值
    读回来比一次。
    """
    wanted = "ON" if enabled else "OFF"
    connection.exec_driver_sql(f"PRAGMA foreign_keys={wanted}")
    actual = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar())
    if actual is not enabled:
        raise RuntimeError(
            f"无法把 SQLite 外键校验切换为 {wanted}：连接上已经有打开的事务，"
            "PRAGMA 被静默忽略了。批处理改表会因此失败，"
            "先确认操作是在干净连接上执行的。"
        )


def check_sqlite_foreign_keys(connection: Connection) -> None:
    """实际核对外键完整性，有违规就抛 RuntimeError。

    这是 `set_sqlite_foreign_keys(conn, False)` 的配套检查：关掉外键是为了能 DROP
    被引用的表，不是为了容忍坏数据。少了这一步，关外键就等于把约束变成注释。
    `PRAGMA foreign_key_check` 返回的每一行都是一处违规（表名、rowid、被引用的表、
    外键序号）。
    """
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        shown = [tuple(row) for row in violations[:10]]
        raise RuntimeError(
            f"存在外键违规，数据库已不一致：{shown}"
            + ("（仅显示前 10 处）" if len(violations) > 10 else "")
        )


def resolve_database_url(url: str | URL) -> URL:
    """解析连接串，并把相对的 SQLite 文件路径改写成 `backend/` 下的绝对路径。

    只做字符串与路径处理：不加载方言驱动，也不建立连接。连接串格式不合法时抛
    ConfigError——这属于启动期配置错误，不应该被当成运行期的业务异常。
    """
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise ConfigError(f"DATABASE_URL={url!r} 不是合法的连接串：{exc}") from exc

    # make_url 已经拒绝了缺少 scheme 的串（如 "not-a-url"、"sqlite"），这里不再重复判断。
    if parsed.get_backend_name() != "sqlite":
        return parsed

    database = parsed.database
    if database in IN_MEMORY_DATABASES:
        return parsed

    path = Path(database)
    if path.is_absolute():
        return parsed
    # as_posix()：Windows 下反斜杠路径虽可用，但渲染进日志和 alembic.ini 时
    # 容易被当成转义字符，正斜杠在三个平台的 sqlite3 里都接受。
    return parsed.set(database=(BASE_DIR / path).resolve().as_posix())


def sqlite_file_path(url: URL) -> Path | None:
    """返回 SQLite 数据库文件路径；内存数据库或其它数据库返回 None。"""
    if url.get_backend_name() != "sqlite" or url.database in IN_MEMORY_DATABASES:
        return None
    return Path(url.database)


class Database:
    """Engine 与 Session 的持有者，同时提供 Service 使用的事务边界。"""

    def __init__(self, url: str | URL, *, echo: bool = False) -> None:
        self._url = resolve_database_url(url)
        self._echo = echo
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None

    @classmethod
    def from_config(cls, config: Config) -> "Database":
        """按配置创建。此时不连接数据库，见模块文档。"""
        # echo 会把 SQL 语句原样写进日志；参数里可能含业务数据，默认关闭。
        return cls(config.database_url, echo=False)

    @property
    def url(self) -> URL:
        """已解析的连接串（SQLite 相对路径已改写为绝对路径）。"""
        return self._url

    @property
    def safe_url(self) -> str:
        """可写进日志的连接串：密码已隐去。

        SQLite 走单独一条分支：`render_as_string()` 会把文件路径百分号编码
        （`E:` → `E%3A`，中文目录整段转义），日志里完全读不出指向哪个文件；
        SQLite 的连接串里又没有密码可泄露，所以直接给出路径本身。
        """
        if not self.is_sqlite:
            return self._url.render_as_string(hide_password=True)
        path = sqlite_file_path(self._url)
        if path is not None:
            location = f":///{path.as_posix()}"
        elif self._url.database:
            location = f":///{self._url.database}"  # :memory:
        else:
            location = "://"
        query = "".join(
            f"{separator}{key}={value}"
            for separator, (key, value) in zip(
                ["?"] + ["&"] * len(self._url.query), self._url.query.items()
            )
        )
        return f"{self._url.drivername}{location}{query}"

    @property
    def is_sqlite(self) -> bool:
        return self._url.get_backend_name() == "sqlite"

    @property
    def is_in_memory(self) -> bool:
        return self.is_sqlite and self._url.database in IN_MEMORY_DATABASES

    @property
    def engine_created(self) -> bool:
        """Engine 是否已经创建过。用于验证「正常启动不连库」。"""
        return self._engine is not None

    @property
    def engine(self) -> Engine:
        """首次访问时创建 Engine。创建 Engine 本身仍不建立连接。"""
        if self._engine is None:
            self._engine = self._create_engine()
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            self._session_factory = sessionmaker(
                bind=self.engine,
                # 提交后不过期：Service 提交完还要把对象交给 API 层序列化，
                # 过期会在 Session 关闭后触发 DetachedInstanceError。
                expire_on_commit=False,
                autoflush=True,
                future=True,
            )
        return self._session_factory

    def _create_engine(self) -> Engine:
        kwargs: dict[str, Any] = {"echo": self._echo, "future": True}
        if self.is_sqlite:
            # Flask 开发服务器是多线程的，连接池里的连接会跨线程复用；
            # sqlite3 默认禁止这种复用，这里显式放开（每个 Session 独占一条连接）。
            #
            # `timeout` 是**等锁的秒数**，不是查询超时：SQLite 只允许一个写入者，
            # 默认（5 秒，但 sqlite3 模块按 `timeout` 参数走）写冲突时直接抛
            # 「database is locked」。后台 worker 在写草稿的同一时刻，老板可能正在
            # 确认另一张单据的某一行——这种撞车本来就会发生，而且会越来越多
            # （步骤 10.C 又在提取路径里加了"自动建食材"这一次写）。
            #
            # ⚠️ **这是等待，不是并发控制**：它只把"立刻失败"换成"等一会儿再说"。
            # 真正的互斥仍然靠步骤 08 的进程内锁（`src/worker.py`）与各处的事务
            # 边界；拆成多进程时要换成 `BEGIN IMMEDIATE`，那条结论不变。
            kwargs["connect_args"] = {
                "check_same_thread": False,
                "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
            }
            if self.is_in_memory:
                # 内存库的数据属于单条连接：换连接就是换数据库，
                # 必须固定复用同一条，否则迁移建的表在下一条连接里不存在。
                kwargs["poolclass"] = StaticPool
            else:
                self._ensure_parent_dir()

        engine = create_engine(self._url, **kwargs)
        if self.is_sqlite:
            _enable_sqlite_foreign_keys(engine)
        logger.debug("engine created url=%s", self.safe_url)
        return engine

    def _ensure_parent_dir(self) -> None:
        """SQLite 不会自动创建上级目录，缺目录时报的是「无法打开数据库文件」。"""
        path = sqlite_file_path(self._url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def unit_of_work(self, session: Session | None = None) -> Iterator[Session]:
        """一次业务操作的事务边界：正常退出提交，异常回滚并原样抛出异常。

        传入 session 时只加入外层事务：不提交也不回滚，由最外层作用域决定结果。
        这样 Service 之间可以互相调用而不会提前提交半个业务操作。

        提交本身失败时会尝试回滚；回滚也失败则记录「结果未知」后抛出原始异常，
        不对外声称已经回滚干净。
        """
        if session is not None:
            yield session
            return

        new_session = self.session_factory()
        try:
            yield new_session
            new_session.commit()
        except Exception:
            try:
                new_session.rollback()
            except Exception:  # pragma: no cover - 需要连接中断才能触发
                # 不能声称已回滚：此时事务的实际结果未知，交由上层按未知处理。
                logger.exception("rollback failed; transaction outcome unknown")
            raise
        finally:
            new_session.close()

    def create_all(self) -> None:
        """按当前模型建表。只供临时数据库与试验使用。

        正式环境与测试的结构变更一律走 Alembic 迁移（`python -m alembic upgrade head`）：
        用 create_all 建表会绕开迁移，让「迁移是否真的能跑」失去验证。
        """
        from src.models import Base

        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        """关闭连接池。进程退出前或测试结束时调用。"""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite 默认不校验外键，必须每条连接显式打开。

    不打开的后果是外键约束只是注释：删掉被引用的食材也不会报错，发票行会指向
    不存在的记录。这类引用完整性在步骤 03 起的业务表上开始生效。
    """

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
