"""数据库会话与事务边界的测试（步骤 02）。

验证的是「提交与回滚分得清」这件基础设施能力，不是业务规则。

**本文件不为测试创建业务表。** 下面的 `tx_probe` / `tx_parent` 两张表属于本测试
模块自己的 MetaData，不挂在 `src.models.Base` 上，因此既不会进入迁移，也不会被
误当成食材、采购或发票表（它们分别在步骤 03、04、05 设计）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    insert,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config import BASE_DIR, Config
from src.db import Database, resolve_database_url, sqlite_file_path
from src.errors import ConfigError, ValidationError
from src.models import Base, UtcDateTime

# 只在测试内使用的临时表，与业务模型完全隔离。
PROBE_METADATA = MetaData()

tx_probe = Table(
    "tx_probe",
    PROBE_METADATA,
    Column("id", Integer, primary_key=True),
    Column("note", String(50), nullable=False),
)

tx_parent = Table(
    "tx_parent",
    PROBE_METADATA,
    Column("id", Integer, primary_key=True),
)

tx_child = Table(
    "tx_child",
    PROBE_METADATA,
    Column("id", Integer, primary_key=True),
    Column("parent_id", Integer, ForeignKey("tx_parent.id"), nullable=False),
)


@pytest.fixture
def probe_db(memory_database: Database) -> Database:
    """建好临时表的内存数据库。"""
    PROBE_METADATA.create_all(memory_database.engine)
    return memory_database


def notes(database: Database) -> list[str]:
    """用一个独立会话读出当前已提交的内容。"""
    with database.unit_of_work() as session:
        return list(session.scalars(select(tx_probe.c.note).order_by(tx_probe.c.id)))


class ProbeService:
    """示范 Service 如何控制事务边界。

    Service 用一个 `unit_of_work()` 包住整次操作，其中的多次写入共同提交或共同
    回滚；Repository 位置上的代码只拿到 Session，自己不提交。步骤 05 的「写入失败
    不留下半条正式记录」依赖的就是这个结构。
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def record_pair(self, first: str, second: str, *, fail_between: bool = False) -> None:
        with self._database.unit_of_work() as session:
            self._append(session, first)
            if fail_between:
                raise ValidationError("第二条记录的数据不合法")
            self._append(session, second)

    def record_pair_nested(self, first: str, second: str, *, fail: bool = False) -> None:
        """外层 Service 调用内层 Service：事务边界仍只有最外面那一个。"""
        with self._database.unit_of_work() as session:
            self._append(session, first)
            self.append_within(session, second)
            if fail:
                raise ValidationError("整次操作失败")

    def append_within(self, session: Session, note: str) -> None:
        """被别的 Service 调用时加入对方的事务，不自己提交。"""
        with self._database.unit_of_work(session) as joined:
            self._append(joined, note)

    @staticmethod
    def _append(session: Session, note: str) -> None:
        session.execute(insert(tx_probe).values(note=note))


# --- 连接串解析 ---


def test_relative_sqlite_path_resolves_against_backend_dir() -> None:
    """否则从不同目录执行 app.py 与 alembic 会操作两个不同的数据库文件。"""
    url = resolve_database_url("sqlite:///var/inventory.db")

    assert sqlite_file_path(url) == BASE_DIR / "var" / "inventory.db"
    assert Path(url.database).is_absolute()


def test_absolute_sqlite_path_is_kept(tmp_path: Path) -> None:
    target = tmp_path / "data" / "inventory.db"

    url = resolve_database_url(f"sqlite:///{target.as_posix()}")

    assert sqlite_file_path(url) == target


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_in_memory_urls_are_not_treated_as_paths(url: str) -> None:
    assert sqlite_file_path(resolve_database_url(url)) is None


def test_other_databases_are_left_untouched() -> None:
    url = resolve_database_url("postgresql://user:pw@db.internal:5432/inventory")

    assert url.database == "inventory"
    assert sqlite_file_path(url) is None


@pytest.mark.parametrize("url", ["not-a-url", "", "sqlite", "://nowhere"])
def test_malformed_url_is_a_startup_error(url: str) -> None:
    """连接串写错属于配置错误：宁可启动失败，也不要带着错配置运行。"""
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        resolve_database_url(url)


def test_safe_url_hides_password() -> None:
    database = Database("postgresql://user:sup3r-s3cret@db.internal/inventory")

    assert "sup3r-s3cret" not in database.safe_url
    assert "***" in database.safe_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite://", "sqlite://"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
        ("postgresql://u@h:5432/db", "postgresql://u@h:5432/db"),
    ],
)
def test_safe_url_stays_readable(url: str, expected: str) -> None:
    assert Database(url).safe_url == expected


def test_safe_url_shows_the_sqlite_path_unescaped() -> None:
    """日志里要能直接看出用的是哪个文件。

    render_as_string() 会把 `E:` 与中文目录整段百分号编码，读不出指向哪里；
    SQLite 连接串里没有密码，因此按原样给出路径。
    """
    rendered = Database("sqlite:///var/inventory.db").safe_url

    assert "%" not in rendered
    assert rendered.endswith("/backend/var/inventory.db")
    assert rendered.startswith("sqlite:///")


# --- 懒创建：启动不连库 ---


def test_engine_is_not_created_until_used(tmp_config: Config) -> None:
    database = Database.from_config(tmp_config)

    assert database.engine_created is False
    assert sqlite_file_path(database.url) is not None
    assert not sqlite_file_path(database.url).exists()


def test_database_file_appears_only_on_first_use(database: Database) -> None:
    path = sqlite_file_path(database.url)
    assert path is not None and not path.exists()

    with database.unit_of_work() as session:
        session.execute(text("select 1"))

    assert path.exists(), "首次使用后应创建数据库文件及其上级目录"
    assert database.engine_created is True


def test_unsupported_driver_fails_only_when_used() -> None:
    """未安装驱动的连接串不影响构造与启动，只在真正要连库时才报错。"""
    database = Database("postgresql+psycopg2://user:pw@db.internal/inventory")

    assert database.engine_created is False
    with pytest.raises(Exception) as excinfo:
        _ = database.engine
    assert "psycopg2" in str(excinfo.value)


def test_dispose_releases_engine(probe_db: Database) -> None:
    assert probe_db.engine_created is True

    probe_db.dispose()

    assert probe_db.engine_created is False


# --- 事务边界 ---


def test_commit_persists_both_writes(probe_db: Database) -> None:
    ProbeService(probe_db).record_pair("第一条", "第二条")

    assert notes(probe_db) == ["第一条", "第二条"]


def test_failure_rolls_back_the_whole_operation(probe_db: Database) -> None:
    """失败时不留下半条记录：第一条写入也要一起回滚。"""
    service = ProbeService(probe_db)

    with pytest.raises(ValidationError):
        service.record_pair("第一条", "第二条", fail_between=True)

    assert notes(probe_db) == []


def test_exception_propagates_unchanged(probe_db: Database) -> None:
    """回滚不吞异常：上层要能看到原始失败原因。"""
    with pytest.raises(ValidationError, match="数据不合法"):
        ProbeService(probe_db).record_pair("a", "b", fail_between=True)


def test_nested_unit_of_work_shares_one_transaction(probe_db: Database) -> None:
    ProbeService(probe_db).record_pair_nested("外层", "内层")

    assert notes(probe_db) == ["外层", "内层"]


def test_nested_failure_discards_inner_writes_too(probe_db: Database) -> None:
    """内层 Service 已经写过，但整次操作失败时同样不能留下任何记录。"""
    with pytest.raises(ValidationError):
        ProbeService(probe_db).record_pair_nested("外层", "内层", fail=True)

    assert notes(probe_db) == []


def test_inner_scope_does_not_commit_early(probe_db: Database) -> None:
    """内层作用域退出时事务必须还开着，否则外层失败也无法回滚内层写入。"""
    service = ProbeService(probe_db)

    with probe_db.unit_of_work() as session:
        service.append_within(session, "内层")
        assert session.in_transaction() is True, "内层退出后事务被提前结束"

    assert notes(probe_db) == ["内层"]


def test_passed_in_session_is_not_closed_by_inner_scope(probe_db: Database) -> None:
    session = probe_db.session_factory()
    try:
        with probe_db.unit_of_work(session) as joined:
            joined.execute(insert(tx_probe).values(note="x"))

        assert session.is_active is True
        session.rollback()
    finally:
        session.close()

    assert notes(probe_db) == []


def test_session_is_closed_after_scope(probe_db: Database) -> None:
    with probe_db.unit_of_work() as session:
        session.execute(insert(tx_probe).values(note="x"))
        captured = session

    assert captured.in_transaction() is False


def test_sessions_are_independent(probe_db: Database) -> None:
    """未提交的写入不会出现在另一个会话里。"""
    with probe_db.unit_of_work() as first:
        first.execute(insert(tx_probe).values(note="已提交"))

    with probe_db.unit_of_work() as second:
        second.execute(insert(tx_probe).values(note="未提交"))
        second.rollback()

    assert notes(probe_db) == ["已提交"]


# --- SQLite 行为 ---


def test_sqlite_foreign_keys_are_enforced(probe_db: Database) -> None:
    """SQLite 默认不校验外键，必须每条连接显式打开，否则约束只是注释。"""
    with pytest.raises(IntegrityError):
        with probe_db.unit_of_work() as session:
            session.execute(insert(tx_child).values(id=1, parent_id=999))


def test_foreign_key_pragma_is_on_for_every_connection(probe_db: Database) -> None:
    with probe_db.unit_of_work() as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_valid_foreign_key_still_works(probe_db: Database) -> None:
    with probe_db.unit_of_work() as session:
        session.execute(insert(tx_parent).values(id=1))
        session.execute(insert(tx_child).values(id=1, parent_id=1))

    with probe_db.unit_of_work() as session:
        assert session.execute(select(tx_child.c.parent_id)).scalar_one() == 1


# --- 时间列的时区语义 ---


def test_utc_datetime_refuses_naive_values() -> None:
    """不带时区的时间含义不确定，宁可写入时就报错。"""
    with pytest.raises(ValueError, match="必须带时区"):
        UtcDateTime().process_bind_param(datetime(2026, 1, 1, 12, 0), None)


def test_utc_datetime_converts_to_utc_before_storing() -> None:
    """北京时间 12:00 存成 UTC 的 04:00，列里始终是同一个时区的值。"""
    value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    assert UtcDateTime().process_bind_param(value, None) == datetime(2026, 1, 1, 4, 0)


def test_utc_datetime_reads_back_with_a_timezone() -> None:
    """SQLite 读回来的是 naive 值，必须补上 UTC——否则序列化出去没有偏移量，
    浏览器会按本地时区解释它。"""
    restored = UtcDateTime().process_result_value(datetime(2026, 1, 1, 4, 0), None)

    assert restored == datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    assert restored.isoformat().endswith("+00:00")


def test_utc_datetime_passes_none_through() -> None:
    assert UtcDateTime().process_bind_param(None, None) is None
    assert UtcDateTime().process_result_value(None, None) is None


# --- 与业务模型的隔离 ---


def test_probe_tables_are_not_business_tables() -> None:
    """测试用的临时表不进业务模型，因此不会进迁移。"""
    assert set(PROBE_METADATA.tables) == {"tx_probe", "tx_parent", "tx_child"}
    assert not set(PROBE_METADATA.tables) & set(Base.metadata.tables)


def test_declared_business_tables_are_exactly_the_expected_ones() -> None:
    """到步骤 10 为止共十二张业务表，加多文件计划 M02 的五张。

    分类、标准食材（03），采购记录与它的修改日志（04），单据、单据文件与行项目
    （05），提取任务与供应商提取路径（08），箱规与涨价提醒设置（09），
    重复食材合并日志（10）。断言刻意
    写成「恰好等于」而不是「至少包含」：下一步要加表时必须在这里显式承认一次，
    提前凭想象建表必然返工（SQLite 下每次改约束都要批处理重建表）。

    步骤 06（表格导入导出）**没有加表**——它复用步骤 05 的草稿表，这一点由这条
    断言守着。

    ⚠️ 步骤 09 加了两张**配置**表，但**没有给 `purchases` 加标准化价格列**：
    标准化价格是查询时算的派生值（docs/stru_dev.md 09.5）。落一列回填的话，
    箱规一改那列就悄悄过期，而它看起来仍然像个可信的数字。

    ⚠️ 步骤 10 的 `food_item_merges` **不是别名表**（步骤 03 的决定不变）：
    它记的是**发生过的合并**，只在合并真的发生时才长一行，没人能往里手工添叫法。

    ⚠️ M07 的 `intake_source_aliases` **也不是供应商表**：它只有「纸面上这种写法 →
    人确认过的哪一家」一个映射，没有联系人、地址、账期。M07 的另两样记忆**都没有
    表**——「这一家现在叫什么」由最近一次人工确认聚合出来，「这个版式属于哪一家」
    由 `intake_items.layout_fingerprint` 加那一页上的确认派生。

    多文件计划 M02 加五张 `intake_*`：整理批次、按内容摘要一行一份的原件字节、
    批次对原件的引用、可归类内容（PDF 的一页 / 表格的一行）与分类候选。
    ⚠️ **没有「日期/商家组」表**——组由内容上的人工决定值派生，另存一张就会与
    内容打架。⚠️ **`invoice_files` 一个字都没改**：它的 `sha256` 全局唯一是
    步骤 05「完全相同的文件不许重复上传」的落点，整理批次另立一套表而不是放宽它。
    """
    assert set(Base.metadata.tables) == {
        "categories",
        "food_items",
        "purchases",
        "purchase_revisions",
        "invoices",
        "invoice_files",
        "invoice_lines",
        "extraction_jobs",
        "ocr_routes",
        "pack_specs",
        "price_alert_settings",
        "food_item_merges",
        "intake_batches",
        "intake_files",
        "intake_batch_files",
        "intake_items",
        "intake_item_candidates",
        "intake_moves",
        "intake_move_entries",
        "intake_review_links",
        "intake_source_aliases",
    }


def test_money_and_quantity_never_use_numeric_or_float() -> None:
    """金额与数量不经 `Numeric` / `Float`，而是按整数最小单位存。

    步骤 04 实测过：SQLite 下 `Numeric` 落成 REAL，超出 scale 的位数被静默改掉、
    有效位数多了回读就变、SQL 侧 `sum()` 是浮点运算。所以数值列一律是
    `DecimalAsInteger`（impl 为 `Integer`），这条断言防的是以后有人图省事改回
    `Numeric` —— 那会把精度问题悄悄带回来。
    """
    numeric_columns = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, (Numeric, Float))
    ]

    assert numeric_columns == []


def test_naming_convention_is_configured() -> None:
    """约束要有稳定名字，SQLite 的批处理迁移才能引用它们。"""
    convention = Base.metadata.naming_convention

    assert set(convention) >= {"pk", "fk", "uq", "ix", "ck"}
    assert "%(table_name)s" in convention["pk"]
