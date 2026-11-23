"""Alembic 迁移环境的测试（步骤 02）。

每个用例都在**新建的临时数据库**上执行真实的 `alembic upgrade`，不是检查文件存在。
"""

from __future__ import annotations

import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from src.config import BASE_DIR
from src.db import Database, check_sqlite_foreign_keys, set_sqlite_foreign_keys
from src.models import Base
from src.services.matching import UNCATEGORIZED_NAME_NORMALIZED

ALEMBIC_INI = BASE_DIR / "alembic.ini"
VERSIONS_DIR = BASE_DIR / "migrations" / "versions"
BASELINE_REVISION = "73ebb18fcafe"


def alembic_config(url: str | None = None, *, x_argument: str | None = None) -> AlembicConfig:
    """构造程序化调用用的 Alembic 配置。

    configure_logger=False：env.py 据此跳过 fileConfig，不接管 pytest 的日志配置。
    """
    config = AlembicConfig(str(ALEMBIC_INI))
    config.attributes["configure_logger"] = False
    if url is not None:
        config.set_main_option("sqlalchemy.url", url)
    if x_argument is not None:
        # 等价于命令行的 `-x key=value`。
        config.cmd_opts = Namespace(x=[x_argument])
    return config


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    finally:
        connection.close()
    return {name for (name,) in rows}


def category_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("select name from categories").fetchall()
    finally:
        connection.close()
    return {name for (name,) in rows}


def stamped_revisions(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("select version_num from alembic_version").fetchall()
    finally:
        connection.close()
    return [version for (version,) in rows]


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


# --- 迁移能在新临时数据库执行 ---


def test_upgrade_head_runs_on_a_fresh_database(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "inventory.db"
    assert not target.exists()

    command.upgrade(alembic_config(sqlite_url(target)), "head")

    assert target.exists(), "迁移应创建数据库文件及其上级目录"
    assert stamped_revisions(target) == [head_revision()]


def test_upgrade_creates_exactly_the_expected_tables(tmp_path: Path) -> None:
    """当前修订链建出步骤 03—10 的十二张业务表，加多文件计划 M02 的五张。

    断言写成"恰好等于"：每一步新增的表都必须在这里被显式承认一次，否则某个
    Feature 偷偷建了表也没人察觉。导入导出（06）不加表；步骤 08 加两张
    （`extraction_jobs` 提取任务状态、`ocr_routes` 供应商 → 提取路径）；
    步骤 09 加两张（`pack_specs` 箱规、`price_alert_settings` 涨价阈值）；
    步骤 10 加一张（`food_item_merges` 重复食材合并日志）。

    ⚠️ `ocr_routes` **不是供应商表**——步骤 05 刻意没建供应商主表，这一条仍然
    成立：那张表里只有「规范化来源名 → 走哪条提取路径」一个映射。`pack_specs`
    同样不是：它只有「这家这种货一箱等于多少基础单位」这一个事实。
    ⚠️ `food_item_merges` **也不是别名表**（步骤 03 的决定不变）：它记的是
    **发生过的合并**，只在合并真的发生时才长一行，没人能往里手工添叫法。

    ⚠️ **步骤 09 没有给 `purchases` 加标准化价格列**，所以这里的表数量变了、
    列没变。标准化价格是查询时算的派生值（docs/stru_dev.md 09.5）：落一列回填
    的话，箱规一改那列就悄悄过期，而它看起来仍然像个可信的数字。

    多文件计划 M02 加五张（`intake_*`）：混合上传的整理批次、按内容摘要一行一份的
    原件字节、批次对原件的引用、可归类内容（PDF 的一页 / 表格的一行）与分类候选。
    ⚠️ **这里没有「日期/商家组」表**——组由内容上的人工决定值派生，另存一张会与
    内容打架（和"单据状态由行状态派生"同一条）。⚠️ **`invoices` / `invoice_files`
    一个字都没改**：那边 `sha256` 全局唯一是步骤 05「完全相同的文件不许重复上传」
    的落点，整理批次另立一套表而不是放宽它。
    """
    target = tmp_path / "inventory.db"

    command.upgrade(alembic_config(sqlite_url(target)), "head")

    assert table_names(target) == {
        "alembic_version",
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
        # M07 的商家记忆：**只有别名映射落表**。「这一家现在叫什么」与「这个版式
        # 属于哪一家」都是派生值（最近一次人工确认 / 指纹 + 那页的确认），另存一张
        # 就会出现两份能互相矛盾的真相。
        "intake_source_aliases",
    }


def test_upgrade_seeds_the_initial_categories(tmp_path: Path) -> None:
    """初始分类由 seed 修订写入，新库装好就能直接选分类。

    七个是步骤 03 的初始分类（`docs/imp_notes.md` Feature 1）；第八个
    `Uncategorized` 是步骤 10.8 另写一条修订加的——`food_items.category_id` 是
    `NOT NULL` 外键，而 10.C 的关键词猜不出分类时必须有地方可落，否则「分类认不出」
    会在发票逐行确认那一屏把入账整个挡住。
    """
    target = tmp_path / "inventory.db"

    command.upgrade(alembic_config(sqlite_url(target)), "head")

    assert category_names(target) == {
        "Meat & Poultry",
        "Seafood",
        "Vegetables",
        "Dairy & Eggs",
        "Dry Goods",
        "Frozen Food",
        "Oils & Condiments",
        "Uncategorized",
    }


def test_the_uncategorized_anchor_matches_the_application_constant(
    tmp_path: Path,
) -> None:
    """⚠️ 判定锚点是 `name_normalized`，不是显示名。

    迁移不 import 应用代码（它是历史快照），所以两边各写了一次
    `'uncategorized'`；这条断言是它们之间唯一的连接。对不上的话，老板把
    「Uncategorized」改个名之后，10.C 会找不到这条分类——而表现只是"自动建食材
    突然不工作了"，不报错。
    """
    target = tmp_path / "inventory.db"
    command.upgrade(alembic_config(sqlite_url(target)), "head")

    connection = sqlite3.connect(target)
    try:
        rows = {
            row[0]
            for row in connection.execute("select name_normalized from categories")
        }
    finally:
        connection.close()

    assert UNCATEGORIZED_NAME_NORMALIZED in rows


def test_seeding_again_does_not_duplicate_categories(tmp_path: Path) -> None:
    """seed 修订可重复执行：回退再升级不会插出第二套分类。"""
    target = tmp_path / "inventory.db"
    config = alembic_config(sqlite_url(target))
    command.upgrade(config, "head")

    command.downgrade(config, "-1")
    command.upgrade(config, "head")

    assert len(category_names(target)) == 8


def test_models_match_migrations(tmp_path: Path) -> None:
    """模型与迁移不能漂移。

    改了 src/models.py 却忘记写迁移（或反过来）时，这个用例会失败，而不是等到
    某个查询在生产库上报「no such column」。
    """
    target = tmp_path / "inventory.db"
    command.upgrade(alembic_config(sqlite_url(target)), "head")

    database = Database(sqlite_url(target))
    try:
        with database.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        database.dispose()

    assert differences == []


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "inventory.db"
    config = alembic_config(sqlite_url(target))

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert stamped_revisions(target) == [head_revision()]


def test_downgrade_to_base_removes_the_version(tmp_path: Path) -> None:
    target = tmp_path / "inventory.db"
    config = alembic_config(sqlite_url(target))
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    assert stamped_revisions(target) == []


def test_x_argument_overrides_the_url(tmp_path: Path) -> None:
    """文档里的 `-x db_url=...` 通路要真的生效。"""
    target = tmp_path / "by-x-argument.db"

    command.upgrade(
        alembic_config(x_argument=f"db_url={sqlite_url(target)}"), "head"
    )

    assert stamped_revisions(target) == [head_revision()]


def test_relative_sqlite_url_resolves_against_backend_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在别的目录执行迁移，仍然操作 backend/var 下的同一个数据库。"""
    relative = "var/test-migration-cwd-check.db"
    expected = BASE_DIR / relative
    expected.unlink(missing_ok=True)
    monkeypatch.chdir(tmp_path)

    try:
        command.upgrade(
            alembic_config(x_argument=f"db_url=sqlite:///{relative}"), "head"
        )

        assert expected.exists(), "相对路径应按 backend/ 解析"
        assert not (tmp_path / relative).exists(), "不应在当前工作目录下另建数据库"
        assert stamped_revisions(expected) == [head_revision()]
    finally:
        expected.unlink(missing_ok=True)


# --- 修订链本身 ---


def test_single_head_revision() -> None:
    """只有一条迁移链：出现多个 head 说明分支没合，升级结果会取决于执行顺序。"""
    script = ScriptDirectory.from_config(alembic_config())

    assert len(script.get_heads()) == 1


def test_baseline_is_the_root_of_the_chain() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    baseline = script.get_revision(BASELINE_REVISION)

    assert baseline.down_revision is None
    assert script.get_base() == BASELINE_REVISION


def test_every_revision_file_is_reachable() -> None:
    """versions/ 下的脚本数量与修订链长度一致：没有游离文件。"""
    script = ScriptDirectory.from_config(alembic_config())
    files = [
        path
        for path in VERSIONS_DIR.glob("*.py")
        if not path.name.startswith("__")
    ]

    assert len(files) == len(list(script.walk_revisions()))


# --- 配置文件自身的约束 ---


def test_alembic_ini_is_ascii_only() -> None:
    """Alembic 用 encoding="locale" 读 alembic.ini（见 alembic/util/compat.py）。

    非 ASCII 内容在非 UTF-8 的本地环境（本项目开发机的 GBK）会直接
    UnicodeDecodeError，所有 alembic 命令都无法执行。中文说明放在
    migrations/README、migrations/env.py 与 backend/README.md。
    """
    raw = ALEMBIC_INI.read_bytes()

    assert raw.decode("ascii"), "alembic.ini 必须是纯 ASCII"


def test_alembic_ini_holds_no_connection_string() -> None:
    """连接串由 env.py 从 DATABASE_URL 读取；写在 ini 里会把密码带进源码交付。"""
    text = ALEMBIC_INI.read_text(encoding="ascii")
    uncommented = [
        line for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]

    assert not any(line.startswith("sqlalchemy.url") for line in uncommented)


def test_script_location_and_sys_path_do_not_depend_on_cwd() -> None:
    text = ALEMBIC_INI.read_text(encoding="ascii")

    assert "script_location = %(here)s/migrations" in text
    assert "prepend_sys_path = %(here)s" in text


# --- 批处理改表与外键（78a4af6ad755 踩过的坑）-------------------------------


def purchases_columns(path: Path) -> dict[str, tuple]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[1]: row for row in connection.execute("pragma table_info(purchases)")
        }
    finally:
        connection.close()


def needs_review_column(connection: sqlite3.Connection) -> str:
    """步骤 10.13 之后 `food_items` 多了一列 `needs_review`（NOT NULL，无默认值）。

    与下面 `line_kind` 的处理同一个道理：这个辅助函数要能在**两个版本的结构**上
    工作，所以先看列在不在，再决定要不要填。
    """
    present = any(
        row[1] == "needs_review"
        for row in connection.execute("pragma table_info(food_items)")
    )
    return ", needs_review" if present else ""


def needs_review_value(connection: sqlite3.Connection) -> str:
    return ", 0" if needs_review_column(connection) else ""


def seed_a_purchase_with_a_revision(path: Path) -> None:
    """造一条采购记录**和一条指向它的修改日志**。

    修改日志这一条是关键：`purchases` 被 `purchase_revisions` 引用，而批处理改表
    要先 DROP 旧表。没有引用方的话，下面那个用例根本碰不到外键问题。

    这个辅助函数要能在**两个版本的结构**上工作：520863805194（还没有 `line_kind`）
    与 head（有，且 NOT NULL 又没有 server_default——那个默认值只在迁移里用于回填，
    回填完就撤掉了）。所以先看列在不在，再决定要不要填。
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute("pragma foreign_keys=ON")
        connection.execute(
            "insert into food_items (name, name_normalized, base_unit, category_id,"
            " search_text, created_at, updated_at" + needs_review_column(connection)
            + ") values"
            " ('Probe', 'probe', 'lb', 1, 'probe', '2026-11-08', '2026-11-08'"
            + needs_review_value(connection) + ")"
        )
        has_line_kind = any(
            row[1] == "line_kind"
            for row in connection.execute("pragma table_info(purchases)")
        )
        columns = [
            "food_item_id",
            "quantity",
            "purchase_unit",
            "unit_price",
            "line_amount",
            "currency",
            "amount_mismatch",
            "entry_source",
            "created_at",
            "updated_at",
        ]
        values: list[object] = [
            1,
            100000,
            "lb",
            60000,
            6000,
            "USD",
            0,
            "manual",
            "2026-11-08",
            "2026-11-08",
        ]
        if has_line_kind:
            columns.append("line_kind")
            values.append("purchase")
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"insert into purchases ({', '.join(columns)}) values ({placeholders})",
            values,
        )
        connection.execute(
            "insert into purchase_revisions (purchase_id, changed_at, action,"
            " changed_fields) values (1, '2026-11-08', 'update', '{}')"
        )
        connection.commit()
    finally:
        connection.close()


def test_upgrade_rebuilds_purchases_even_when_rows_reference_it(
    tmp_path: Path,
) -> None:
    """批处理改表在「有引用方的行」存在时也要能跑完。

    这是 78a4af6ad755 实际踩到的坑：批处理模式改 `purchases.food_item_id` 的可空性
    靠「建新表—搬数据—DROP 旧表—改名」，而 DROP 一张被 `purchase_revisions` 引用的
    表在外键打开时直接 `FOREIGN KEY constraint failed`。修法是迁移期间关外键、
    迁移后再打开并校验（见 migrations/env.py 与 src/db.py）。

    没有这个用例，把那段逻辑删掉后别的测试仍然全过——因为它们都在**空库**上迁移，
    碰不到引用方。
    """
    target = tmp_path / "inventory.db"
    config = alembic_config(sqlite_url(target))
    # 先升到加表那一版，塞进带引用的数据，再升到 head（触发批处理重建）。
    command.upgrade(config, "520863805194")
    seed_a_purchase_with_a_revision(target)

    command.upgrade(config, "head")

    columns = purchases_columns(target)
    assert "line_kind" in columns
    assert columns["food_item_id"][3] == 0, "food_item_id 应已放宽为可空"

    connection = sqlite3.connect(target)
    try:
        assert connection.execute("select count(*) from purchases").fetchone()[0] == 1
        assert (
            connection.execute("select count(*) from purchase_revisions").fetchone()[0]
            == 1
        ), "修改日志不能在重建 purchases 时丢掉"
        assert (
            connection.execute("select line_kind from purchases").fetchone()[0]
            == "purchase"
        ), "既有行应回填为 purchase"
        assert connection.execute("pragma foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_foreign_keys_are_on_again_after_migrating(tmp_path: Path) -> None:
    """迁移期间关外键是手段，不是终点：迁移后必须恢复。

    忘记恢复的后果是整个应用的外键变成注释，而这个项目刻意在连接时打开它
    （见 README 第 10 节）。
    """
    target = tmp_path / "inventory.db"
    command.upgrade(alembic_config(sqlite_url(target)), "head")

    database = Database(sqlite_url(target))
    try:
        with database.engine.connect() as connection:
            assert bool(
                connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
            ), "新连接上外键必须是打开的"
    finally:
        database.dispose()


def test_foreign_key_check_rejects_broken_references(tmp_path: Path) -> None:
    """`check_sqlite_foreign_keys` 是「迁移期间关外键」的配套检查，必须真的会报错。

    少了它，关外键就等于把约束变成注释。这里造一处真实的孤儿引用，确认它抛错。
    """
    target = tmp_path / "inventory.db"
    command.upgrade(alembic_config(sqlite_url(target)), "head")

    connection = sqlite3.connect(target)
    try:
        connection.execute("pragma foreign_keys=OFF")
        connection.execute(
            "insert into purchase_revisions (purchase_id, changed_at, action,"
            " changed_fields) values (424242, '2026-11-08', 'update', '{}')"
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(sqlite_url(target))
    try:
        with database.engine.connect() as conn:
            with pytest.raises(RuntimeError, match="外键违规"):
                check_sqlite_foreign_keys(conn)
    finally:
        database.dispose()


def test_foreign_key_check_passes_on_a_healthy_database(tmp_path: Path) -> None:
    """反面：数据完好时不能误报，否则每次迁移都会失败。"""
    target = tmp_path / "inventory.db"
    command.upgrade(alembic_config(sqlite_url(target)), "head")
    seed_a_purchase_with_a_revision(target)

    database = Database(sqlite_url(target))
    try:
        with database.engine.connect() as conn:
            check_sqlite_foreign_keys(conn)  # 不抛即通过
    finally:
        database.dispose()


def test_setting_the_pragma_is_verified_not_assumed(tmp_path: Path) -> None:
    """`set_sqlite_foreign_keys` 必须读回确认，而不是设完就当成设好了。

    PRAGMA 在真有事务时是静默无效的。这里先用一条 DML 让 pysqlite 真的 BEGIN，
    再尝试切换——此时必须抛错，而不是假装切换成功然后在批处理改表时才炸。
    """
    target = tmp_path / "inventory.db"
    command.upgrade(alembic_config(sqlite_url(target)), "head")

    database = Database(sqlite_url(target))
    try:
        with database.engine.connect() as connection:
            # 干净连接上可以正常切换。
            set_sqlite_foreign_keys(connection, False)
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0

            # 发一条真正的 DML，让 pysqlite 开启事务。
            connection.exec_driver_sql(
                "insert into categories (name, name_normalized, created_at,"
                " updated_at) values ('Probe', 'probe', '2026-11-08', '2026-11-08')"
            )
            with pytest.raises(RuntimeError, match="已经有打开的事务"):
                set_sqlite_foreign_keys(connection, True)
    finally:
        database.dispose()
