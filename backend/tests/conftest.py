"""测试夹具。

三条约定：

1. **一律注入独立配置**：不读取本机 `.env`，也不依赖进程环境变量，因此在任何机器
   上结果一致，也不会把开发者的密钥带进测试。
2. **一律使用临时目录**：数据库文件与上传目录都落在 pytest 的 `tmp_path` 下，
   测试不写 `backend/var/`，互相之间也不共享状态。
3. **业务测试跑在真实迁移出来的库上**：`migrated_database` 及其衍生夹具的表结构由
   `alembic upgrade head` 建立，不用 `Base.metadata.create_all()`——否则"迁移到底
   能不能跑、和模型是否一致"就失去了验证（见 src/db.py 对 `create_all` 的说明）。
   迁移只在会话开始时执行一次，各用例复制那个文件，于是既真实又不慢。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from flask import Flask
from flask.testing import FlaskClient

from src.config import BASE_DIR, Config
from src.db import Database
from src.factory import create_app
from src.integrations.file_storage import LocalFileStorage
from src.integrations.ocr import FakeRecognizer
from src.services.ingredients import FoodItemView, IngredientService
from src.services.intake import IntakeService
from src.services.intake_ingest import IntakeIngestService
from src.services.intake_recognition import IntakeRecognitionService
from src.services.intake_suggestions import IntakeSuggestionService
from src.services.invoices import InvoiceService
from src.services.prices import PriceService
from src.services.purchases import PurchaseService
from src.services.suggestions import SuggestionService

# 测试专用配置。SECRET_KEY 是显式标注的测试值，不是真实密钥。
# 这里的 DATABASE_URL / FILE_STORAGE_DIR 用相对路径，是为了覆盖「相对路径按
# backend/ 解析」这条规则；真正读写磁盘的夹具改用 tmp_path，见下面的 database / storage。
# 测试里的单文件上限。比生产默认值（20 MB）小得多，于是"文件太大"这条规则可以用
# 几十 KB 的假数据验证，不必真造一个 20 MB 的文件。
UPLOAD_MAX_BYTES_FOR_TESTS = 64 * 1024

TEST_ENV: dict[str, str] = {
    "APP_ENV": "testing",
    "LOG_LEVEL": "INFO",
    "SECRET_KEY": "test-only-secret-not-a-real-key",
    "DATABASE_URL": "sqlite:///var/test-inventory.db",
    "FILE_STORAGE_DIR": "var/test-uploads",
    "UPLOAD_MAX_BYTES": str(UPLOAD_MAX_BYTES_FOR_TESTS),
    "OCR_PROVIDER": "fake",
    "HOST": "127.0.0.1",
    "PORT": "5001",
}


@pytest.fixture
def test_env() -> dict[str, str]:
    return dict(TEST_ENV)


@pytest.fixture
def config(test_env: dict[str, str]) -> Config:
    return Config.from_env(test_env)


@pytest.fixture
def tmp_env(test_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    """把数据库与存储目录指向临时目录的环境映射。"""
    return dict(
        test_env,
        DATABASE_URL=f"sqlite:///{(tmp_path / 'data' / 'inventory.db').as_posix()}",
        FILE_STORAGE_DIR=str(tmp_path / "uploads"),
    )


@pytest.fixture
def tmp_config(tmp_env: dict[str, str]) -> Config:
    return Config.from_env(tmp_env)


@pytest.fixture
def database(tmp_config: Config) -> Iterator[Database]:
    """临时目录里的 SQLite 文件数据库（**没有表**，供基础设施测试使用）。

    上级目录 `data/` 刻意不预先创建：顺带验证 Database 会自己建出来
    （SQLite 缺目录时报的是「无法打开数据库文件」）。
    """
    instance = Database.from_config(tmp_config)
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture
def memory_database() -> Iterator[Database]:
    """内存数据库。比文件库快，且不留痕迹；跨连接的行为见 Database 的 StaticPool 说明。"""
    instance = Database("sqlite://")
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """执行一次真实 `alembic upgrade head` 得到的数据库文件，供各用例复制。

    会话级：迁移只跑一次。各用例拿到的是它的副本，彼此互不影响。
    """
    target = tmp_path_factory.mktemp("migrated") / "template.db"
    config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    # env.py 据此跳过 fileConfig，不接管 pytest 的日志配置。
    config.attributes["configure_logger"] = False
    config.set_main_option("sqlalchemy.url", f"sqlite:///{target.as_posix()}")
    command.upgrade(config, "head")
    return target


@pytest.fixture
def migrated_database(migrated_template: Path, tmp_path: Path) -> Iterator[Database]:
    """迁移后的数据库（含业务表与初始分类），每个用例一份独立副本。"""
    target = tmp_path / "data" / "inventory.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(migrated_template, target)
    instance = Database(f"sqlite:///{target.as_posix()}")
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    """临时目录里的本地文件存储。根目录不预先创建，由首次保存时建出。"""
    return LocalFileStorage(tmp_path / "uploads")


@pytest.fixture
def recognizer() -> FakeRecognizer:
    return FakeRecognizer()


@pytest.fixture
def app(config: Config, database: Database, storage: LocalFileStorage) -> Flask:
    """装配好的应用（数据库里没有业务表）。

    数据库与文件存储注入临时目录的实现：请求测试因此不会写进 `backend/var/`。
    识别器按配置创建（`OCR_PROVIDER=fake`），不访问网络。
    """
    return create_app(config, database=database, file_storage=storage)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def migrated_app(
    config: Config, migrated_database: Database, storage: LocalFileStorage
) -> Flask:
    """装配好的应用，数据库已执行迁移——业务接口测试用它。"""
    return create_app(config, database=migrated_database, file_storage=storage)


@pytest.fixture
def migrated_client(migrated_app: Flask) -> FlaskClient:
    return migrated_app.test_client()


@pytest.fixture
def ingredient_service(migrated_database: Database) -> IngredientService:
    return IngredientService(migrated_database)


@pytest.fixture
def purchase_service(migrated_database: Database) -> PurchaseService:
    return PurchaseService(migrated_database)


@pytest.fixture
def price_service(migrated_database: Database) -> PriceService:
    """价格统计服务（步骤 09）。

    与 `purchase_service` / `ingredient_service` 共用同一个 `migrated_database`：
    那边录进去的采购记录这边才算得出标准化价格——统计是**只读**派生值，
    没有自己的数据来源。
    """
    return PriceService(migrated_database)


@pytest.fixture
def suggestion_service(migrated_database: Database) -> SuggestionService:
    """食材建议服务（步骤 10）。与 `ingredient_service` / `purchase_service` 共用
    同一个 `migrated_database`：那两边建的食材与采购记录，是这边"最近 / 常用"
    与模糊匹配的唯一数据来源。"""
    return SuggestionService(migrated_database)


@pytest.fixture
def invoice_service(
    migrated_database: Database, storage: LocalFileStorage, recognizer: FakeRecognizer
) -> InvoiceService:
    """发票服务。文件落在 tmp_path 下，识别器是明确标识的 Fake。

    与 `purchase_service` / `ingredient_service` 共用同一个 `migrated_database`，
    所以这里确认出来的采购记录那两个服务查得到——逐行确认的测试要的就是这一点。
    """
    return InvoiceService(
        migrated_database,
        file_storage=storage,
        recognizer=recognizer,
        upload_max_bytes=UPLOAD_MAX_BYTES_FOR_TESTS,
    )


@pytest.fixture
def intake_service(migrated_database: Database) -> IntakeService:
    """整理批次服务（多文件计划 M02）。

    与 `invoice_service` 共用同一个 `migrated_database`：M02 要验证的一件事正是
    「新加的五张表不影响旧发票照常回读」，两边用同一个库才看得出来。

    这里**不注入文件存储**：M02 的服务不保存字节，只登记「字节已经存好了」这个
    事实（谁保存、怎么切页是 M03）。
    """
    return IntakeService(migrated_database)


@pytest.fixture
def intake_ingest_service(
    intake_service: IntakeService, storage: LocalFileStorage
) -> IntakeIngestService:
    """整理批次的摄入服务（多文件计划 M03）。

    与 `intake_service` 共用同一个库、与 `invoice_service` 共用同一个 `storage`：
    「同一份字节在批次路径与旧单张上传路径各自怎么判重」要两边用同一个存储才看得出来。

    **调度器留空**，即 `InlineIngest`：切页当场跑完，测试结果确定，不起线程。
    后台线程另有专门的用例（见 `test_intake_ingest.py`）。
    """
    return IntakeIngestService(
        intake_service,
        file_storage=storage,
        upload_max_bytes=UPLOAD_MAX_BYTES_FOR_TESTS,
    )


@pytest.fixture
def intake_recognition_service(
    intake_service: IntakeService,
    migrated_database: Database,
    storage: LocalFileStorage,
    recognizer: FakeRecognizer,
) -> IntakeRecognitionService:
    """整理批次的日期/商家识别服务（多文件计划 M05）。

    与 `intake_service` / `intake_ingest_service` 共用同一个库和同一个 `storage`：
    「切出来的页能不能被识别、识别过的页会不会被再送一遍」要在同一套数据上才看
    得出来。

    **识别器是明确标识的 Fake**，所以这个夹具一次网络请求都不会发。要控制读出来的
    具体内容（歧义日期、读不出的商家、两个模型分歧）的用例自己换一个测试替身——
    Fake 识别器的产出是固定的，撑不起那些分支。
    """
    return IntakeRecognitionService(
        intake_service,
        database=migrated_database,
        file_storage=storage,
        recognizer=recognizer,
    )


@pytest.fixture
def intake_suggestion_service(
    intake_service: IntakeService,
    migrated_database: Database,
    storage: LocalFileStorage,
) -> IntakeSuggestionService:
    """商家记忆与相似堆（多文件计划 M07）。

    ⚠️ **没有识别器参数**，这不是漏写：M07 的三层线索（同文件参考、历史别名、
    版式相似）全是本地计算与数据库查询，服务结构上拿不到任何能发网络请求的对象。
    与 `intake_service` / `intake_ingest_service` 共用同一个库和 `storage`：
    版式要从真正落盘的那份字节上量出来。
    """
    return IntakeSuggestionService(
        intake_service, database=migrated_database, file_storage=storage
    )


@pytest.fixture
def food_item(ingredient_service: IngredientService) -> FoodItemView:
    """一条可供采购记录引用的标准食材。

    与 `purchase_service` / `migrated_database` 共用同一个数据库：两个 Service
    都由同一个 `migrated_database` 夹具构造，所以这里建出来的食材那边查得到。
    """
    return ingredient_service.create_food_item(
        name="Ground Beef",
        name_zh="牛肉末",
        specification="80/20",
        category_id=1,
        base_unit="lb",
    )


@pytest.fixture
def other_food_item(ingredient_service: IngredientService) -> FoodItemView:
    """第二条食材，用于验证「按食材筛选」与「食材选错了直接改」。"""
    return ingredient_service.create_food_item(
        name="Beef Tendon",
        name_zh="牛筋",
        category_id=1,
        base_unit="lb",
    )
