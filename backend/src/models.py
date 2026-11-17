"""数据库表定义、声明基类与命名约定。

业务表随对应 Feature 步骤加入，不一次设计全部表（见 docs/stru_dev.md）：

- 步骤 03：`categories`、`food_items`。
- **步骤 04（本文件当前内容）**：`purchases`、`purchase_revisions`。
- **步骤 05（本文件当前内容）**：`invoices`、`invoice_files`、`invoice_lines`，
  以及 `purchases.invoice_line_id`（步骤 04 刻意没建的那一列）。
- **步骤 08（本文件当前内容）**：`extraction_jobs`、`ocr_routes`。
- **步骤 09（本文件当前内容）**：`pack_specs`（箱规：1 个包装单位 = 多少基础单位）、
  `price_alert_settings`（涨价阈值与开关）。⚠️ **标准化价格没有落列**：它是派生值，
  查询时算，不改采购记录里的原始数量、单位与金额（docs/stru_dev.md 09.5）。

命名约定不是风格偏好，而是迁移前提：SQLite 不支持大部分 ALTER，Alembic 要靠
批处理模式重建表才能修改约束，而重建时必须能按名字引用既有约束。约束由数据库
自动命名（如 `sqlite_autoindex_...`）会让后续迁移无法稳定引用，所以从基线修订
起就固定命名规则。

金额与数量的精确存储**已在步骤 04 解决**：三个数值列用 `DecimalAsInteger` 按整数
最小单位存，进出都是精确整数运算，不经 `float`。直接用 `Numeric` 被实测否决的原因写在
该类的文档字符串里。食材的 `base_unit` 仍然只是一个字符串标识，**不含换算能力**——
单位换算与标准化价格是步骤 09 的 `services/units.py`。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# %(column_0_N_name)s 覆盖多列约束，避免复合唯一约束落到同一个名字上。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 字段长度上限。与 src/services/ingredients.py 的输入校验共用，避免"校验放过、
# 入库被截断"这类两头不一致。
NAME_MAX_LENGTH = 120
CATEGORY_NAME_MAX_LENGTH = 80
SPECIFICATION_MAX_LENGTH = 200
BASE_UNIT_MAX_LENGTH = 16


def utcnow() -> datetime:
    """当前时间，带 UTC 时区。模型的时间默认值统一用它。"""
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator[datetime]):
    """带时区语义的时间列：写入要求 tz-aware，读出一定带 UTC 时区。

    SQLite 没有真正的时间类型，`DateTime` 存的是字符串、读回来的是 **naive**
    datetime。直接把这种值序列化成 `"2026-11-10T08:00:00"`（没有偏移量）交给前端，
    浏览器的 `new Date(...)` 会按**本地时区**解释它——同一条记录在纽约和上海显示
    成不同时刻。因此：

    - 写入：拒绝不带时区的值（这类值的含义本身就不确定），统一转成 UTC 后去掉
      tzinfo 存成干净的 naive UTC 字符串；
    - 读出：一律附加 UTC 时区，于是 `isoformat()` 带 `+00:00`，没有歧义。

    步骤 04（采购日期与录入时间）、步骤 05（草稿与确认时间）继续用这个类型。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "写入时间必须带时区（请用 models.utcnow()）："
                "不带时区的值无法判断它表示哪个时刻。"
            )
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """所有业务表的声明基类。

    Alembic 的 `target_metadata` 指向 `Base.metadata`；新增表只要继承本类并被
    `src/models.py` 导入，自动生成的迁移就能看到它
    （`tests/test_migrations.py::test_models_match_migrations` 会在模型与迁移
    不一致时失败）。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """创建与更新时间。步骤 03 的两张表与步骤 04 的 `purchases` 共用。"""

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class Category(Base, TimestampMixin):
    """食材分类。

    系统在迁移里写入 7 个常见初始分类，餐厅可以自行新增与修改
    （见 docs/imp_notes.md Feature 1：不强制所有餐厅采用同一套分类体系）。

    `name_normalized` 承担"身份"：同一个名字换个大小写或全角空格不能变成第二个
    分类。中文名 `name_zh` 只是展示标签，不设唯一约束。
    """

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(
        String(CATEGORY_NAME_MAX_LENGTH), nullable=False
    )
    name_normalized: Mapped[str] = mapped_column(
        String(CATEGORY_NAME_MAX_LENGTH), nullable=False, unique=True
    )
    name_zh: Mapped[Optional[str]] = mapped_column(String(CATEGORY_NAME_MAX_LENGTH))

    food_items: Mapped[list["FoodItem"]] = relationship(back_populates="category")

    def __repr__(self) -> str:
        return f"Category(id={self.id!r}, name={self.name!r})"


class FoodItem(Base, TimestampMixin):
    """标准食材（docs/imp_notes.md Feature 1 的 Food Item）。

    一条食材只有**一个标准英文名和一个中文名**，外加可选规格；本轮不做独立别名表
    （该决定见 docs/stru_dev.md 步骤 03）。供应商发票上的原始商品描述不存在这里，
    而是保存在步骤 04 的采购记录上并指向本表，于是"同一食材的不同叫法"由采购历史
    记录下来，而不是先建一张别名表。

    三个 `*_normalized` / `search_text` 列都由 `src/services/ingredients.py` 在写入时
    生成，不要手工拼：

    - `name_normalized` / `name_zh_normalized`：唯一约束的落点，防止同一个名字
      换个大小写或全角写法就变成两条食材（docs/imp_notes.md：不要让同一种食材
      因为中英文名称不同而产生两条记录）。中文名可以为空，SQLite 的唯一索引允许
      多个 NULL 并存，正是需要的语义。
    - `search_text`：英文名 + 中文名 + 规格的规范化拼接，基础搜索对它做一次 LIKE
      就能同时命中中英文与规格。模糊匹配、最近/常用在步骤 10 加入，
      落在 `services/suggestions.py`（本列不变，模糊层把这一列取回内存打分）。
    """

    __tablename__ = "food_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        String(NAME_MAX_LENGTH), nullable=False, unique=True
    )
    name_zh: Mapped[Optional[str]] = mapped_column(String(NAME_MAX_LENGTH))
    name_zh_normalized: Mapped[Optional[str]] = mapped_column(
        String(NAME_MAX_LENGTH), unique=True
    )
    specification: Mapped[Optional[str]] = mapped_column(
        String(SPECIFICATION_MAX_LENGTH)
    )
    base_unit: Mapped[str] = mapped_column(String(BASE_UNIT_MAX_LENGTH), nullable=False)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id"), nullable=False, index=True
    )
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 步骤 10.13：这条食材是系统按关键词自动建出来的，还没人确认过。
    # 名字是从供应商描述里剥的、分类是本地词表猜的——两样都可能不对，所以要在
    # 界面上标出来，而不是混在老板自己建的食材里看不出区别。
    # ⚠️ 不能留在前端内存里：10.D 之后新建食材是"填完即落库"，刷新一下浏览器
    # 标记就没了，而那条猜出来的分类还在。清除时机属于业务判断（老板改过它、
    # 或用它确认过任意一行），由 services 决定，不写进约束。
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    category: Mapped[Category] = relationship(back_populates="food_items")

    def __repr__(self) -> str:
        return f"FoodItem(id={self.id!r}, name={self.name!r}, base_unit={self.base_unit!r})"


class FoodItemMerge(Base):
    """一次重复食材合并的日志（步骤 10.3）。

    **为什么要落库，而不是合并完就算了**：10.3 的验收写的是「合并后**别名**、
    采购历史与发票引用保持可追溯」。后两样靠重新指向外键就够了（采购记录与发票行
    一条都不删，`original_description` 原封不动，所以供应商的那句原话仍然在）；
    **别名不行**——被合并掉的那条食材的名字会随它一起消失，而那个名字正是老板
    当初建它时用的叫法。下次他再按那个叫法搜，就什么都搜不到，然后再建一条重复的。

    所以这张表存的是「**这个名字现在归谁了**」：

    - `services/suggestions.py` 搜索时会查它，命中就给出合并后的那条食材，
      理由是 `merged_from`。10.6 的「保存过的纠正能够辅助下一次匹配」由此成立；
    - 合并了几条采购记录、几行草稿、几条箱规也记下来，老板事后能核对这次合并
      到底动了多少东西。

    ⚠️ **`merged_food_item_id` 刻意没有外键**：那一行在同一个事务里被删掉了，
    建外键等于要求它还在。它只作追溯用的编号，不参与任何关联查询。

    ⚠️ **`name_normalized` 不唯一**。合并之后那个名字就被释放了，老板完全可以
    再建一条同名食材、再合并一次——那是两次独立的事实，各记一行。

    没有 `updated_at`：日志只追加、不修改（同 `PurchaseRevision`）。
    """

    __tablename__ = "food_item_merges"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_food_item_id: Mapped[int] = mapped_column(
        ForeignKey("food_items.id"), nullable=False, index=True
    )
    merged_food_item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    merged_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )

    # 被合并掉那条食材的身份快照。展示用原值，检索用规范化值。
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        String(NAME_MAX_LENGTH), nullable=False, index=True
    )
    name_zh: Mapped[Optional[str]] = mapped_column(String(NAME_MAX_LENGTH))
    name_zh_normalized: Mapped[Optional[str]] = mapped_column(
        String(NAME_MAX_LENGTH), index=True
    )
    specification: Mapped[Optional[str]] = mapped_column(
        String(SPECIFICATION_MAX_LENGTH)
    )
    base_unit: Mapped[str] = mapped_column(String(BASE_UNIT_MAX_LENGTH), nullable=False)

    # 这次合并搬动了多少引用。三个数都在同一个事务里数出来，不是事后统计。
    moved_purchase_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    moved_invoice_line_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    moved_pack_spec_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    target_food_item: Mapped[FoodItem] = relationship()

    def __repr__(self) -> str:
        return (
            f"FoodItemMerge(id={self.id!r}, name={self.name!r} -> "
            f"food_item_id={self.target_food_item_id!r})"
        )


# --- 步骤 04：采购记录 -------------------------------------------------------

# 数值列的小数位数（scale）。三个数都按「整数最小单位」存：数量与单价留 4 位，
# 行金额留 2 位。位数是 schema 的一部分，以后要放宽得靠迁移乘系数，所以一次定得宽些：
# 真实发票上数量最多 3 位小数（12.345 lb），批发单价偶尔 4 位（0.0125/each），
# USD 行金额一律 2 位。
QUANTITY_SCALE = 4
UNIT_PRICE_SCALE = 4
AMOUNT_SCALE = 2

# 整数部分的位数上限。校验在 services/purchases.py，这里只是同一处来源，
# 避免「校验放过、入库溢出」。SQLite 的 INTEGER 是 64 位，12 + 4 位远在范围内。
MAX_INTEGER_DIGITS = 12

LINE_KIND_MAX_LENGTH = 16

# 行类别。`purchase` 是正常采购行；`adjustment` 是退货、折让或其它调整行，金额可以
# 为负。默认值放在模型层而不是服务层，是因为迁移给既有行回填的也是这个值。
LINE_KIND_PURCHASE = "purchase"
LINE_KIND_ADJUSTMENT = "adjustment"

SOURCE_NAME_MAX_LENGTH = 120
DESCRIPTION_MAX_LENGTH = 300
PURCHASE_UNIT_MAX_LENGTH = 32
CURRENCY_LENGTH = 3
ENTRY_SOURCE_MAX_LENGTH = 16
REVISION_ACTION_MAX_LENGTH = 16
REVISION_REASON_MAX_LENGTH = 300


class DecimalAsInteger(TypeDecorator[Decimal]):
    """按固定小数位数把 Decimal 存成整数最小单位（例如金额存「分」）。

    **为什么不直接用 Numeric。** SQLite 没有十进制类型，SQLAlchemy 的 Numeric
    在它下面落成 REAL（浮点），实测三处问题：

    1. 超出 scale 的位数被**静默**改掉——Numeric(14, 4) 收到 12.34567 存成
       12.3457，没有报错也没有警告；
    2. 有效位数超过 double 能承载的范围时回读就变了——12345678901.234567
       变成 ...568，99999999999999.123456 变成 ...125000；
    3. SQL 侧 sum() 是浮点运算（10 × 0.07 得到 0.7000000000000001），
       而读取时按 scale 量化又会**掩盖**这个误差，于是错误不容易被发现。

    存成 TEXT 能保住原值，但 order by 退化成字典序（"10.1" < "100.0" < "9.9"）、
    sum() 又被 SQLite 强转回 real。整数最小单位同时满足精确、可排序、可聚合，
    代价是 scale 固定在 schema 里。

    进出都是精确整数运算，**全程不经 float**：

    - 写入：拒绝非 Decimal、拒绝 NaN / Infinity、拒绝小数位数超过 scale 的值
      （由 services/purchases.py 先给出可读的 400，这里是最后一道防线）；
    - 读出：用 Decimal((sign, digits, -scale)) 直接构造，不做除法、不受
      decimal 上下文精度影响，因此 1234567.89 原值进原值出。
    """

    impl = Integer
    cache_ok = True

    def __init__(self, scale: int) -> None:
        if scale < 0:
            raise ValueError("scale 不能为负")
        super().__init__()
        self.scale = scale

    def process_bind_param(
        self, value: Decimal | None, dialect: Dialect
    ) -> int | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(
                f"数值列只接受 Decimal，收到 {type(value).__name__}："
                "float 会在这里引入精度损失，必须在服务层先解析成 Decimal。"
            )
        sign, digits, exponent = value.as_tuple()
        # NaN / Infinity 的 exponent 是 "n" / "N" / "F"，不是整数。
        if not isinstance(exponent, int):
            raise ValueError(f"数值列不接受 {value!r}。")
        if -exponent > self.scale:
            raise ValueError(
                f"{value} 的小数位数超过本列允许的 {self.scale} 位；"
                "请在服务层拒绝它，不要在这里四舍五入。"
            )
        magnitude = int("".join(str(digit) for digit in digits) or "0")
        # exponent + scale >= 0 由上面的检查保证，于是这里是纯整数乘法。
        scaled = magnitude * 10 ** (exponent + self.scale)
        return -scaled if sign else scaled

    def process_result_value(
        self, value: int | None, dialect: Dialect
    ) -> Decimal | None:
        if value is None:
            return None
        sign = 1 if value < 0 else 0
        digits = tuple(int(digit) for digit in str(abs(value)))
        return Decimal((sign, digits, -self.scale))


class Purchase(Base, TimestampMixin):
    """一条采购记录（docs/imp_notes.md Feature 2 的历史价格记录）。

    每次采购追加一条，不覆盖旧价格。它同时是步骤 05 逐行确认的**写入目标**：
    发票上确认的行调用同一套保存逻辑，于是手工、表格与 OCR 三条入口的数据长得一样。

    几个列的存在理由（决定见 docs/stru_dev.md 步骤 04）：

    - purchase_date **可空**。手写单上常常没写日期，识别不出时允许留空直接保存，
      不编造单据上没有的日期。代价是这些记录进不了步骤 09 的月度均价，所以查询
      接口必须能按「未填日期」筛出一张待补清单，否则它们会悄悄从价格分析里消失。
    - source_name 是自由文本，**没有供应商表**（与步骤 03 不建别名表一致）。
      source_name_normalized 让 Costco / costco / "Costco " 算同一个来源，
      步骤 09 的来源比价才不会把它们拆成三组。
    - original_description 保存供应商发票上的原始商品描述并指向标准食材，于是
      「同一食材的不同叫法」由采购历史自己记下来。original_description_normalized
      带索引，供步骤 05 的「这条描述以前确认到哪个食材」做精确命中。
    - purchase_unit 保留**原文**（CS、箱、lbs），purchase_unit_normalized
      可空：认不出的单位存 NULL 且不阻断录入。换算在步骤 09。
    - amount_mismatch 记录「数量 × 单价 与行金额对不上、但老板明确确认过」的行。
      行金额是权威（那是实付的钱），不拿单价反算金额。
    - entry_source 区分手工 / 发票 / 表格导入。步骤 05 补上发票外键后，
      invoice_line_id IS NULL 只能区分「有没有发票」，分不出手工录的和 CSV 导的。
    - voided_at 是软删：步骤 05 的发票行要能追溯到它，所以不硬删。
    - line_kind 区分正常采购行与**调整行**（退货、折让、其它调整）。调整行的
      数量与金额**可以为负**，它们留在库里是为了对账（发票各行相加要等于单据总额），
      但默认**不参与食材的采购价格统计**——把一笔退货混进加权均价会把价格算低。
      这个判断存成列而不是靠「金额是不是负的」去推断：步骤 09 的每个统计查询都要
      用到它，靠符号推断意味着漏写一处就静默污染一个均价。
    - food_item_id **可空**，但只对调整行可空。整单折让（「本单让 $20」）在单据上
      不指向任何一种食材，强行挂到某个食材上会污染那个食材的采购历史。正常采购行
      必须有食材，这条由 services/purchases.py 校验——数据库层面放宽是为了能表达
      整单级调整，不是放弃这条规则。

    **本表刻意没有的列**：normalized_unit_price（换算后的标准化价格是派生值，
    归步骤 09）。`invoice_line_id` 当初刻意没建（不预留悬空外键），已由步骤 05
    的迁移补上，见下面的列定义与那条局部唯一索引。
    """

    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 可空：只有调整行允许不指向食材（整单折让）。正常采购行必须有食材，
    # 这条规则在服务层强制，见本类文档字符串。
    food_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("food_items.id"), index=True
    )
    # 日历日而不是时刻：发票上写的是 11/08，用带时区的时间会让同一张单据在纽约和
    # 上海落进不同的「月」，直接污染步骤 09 的月度均价。
    purchase_date: Mapped[Optional[date]] = mapped_column(Date, index=True)

    source_name: Mapped[Optional[str]] = mapped_column(String(SOURCE_NAME_MAX_LENGTH))
    source_name_normalized: Mapped[Optional[str]] = mapped_column(
        String(SOURCE_NAME_MAX_LENGTH), index=True
    )
    original_description: Mapped[Optional[str]] = mapped_column(
        String(DESCRIPTION_MAX_LENGTH)
    )
    original_description_normalized: Mapped[Optional[str]] = mapped_column(
        String(DESCRIPTION_MAX_LENGTH), index=True
    )

    quantity: Mapped[Decimal] = mapped_column(
        DecimalAsInteger(QUANTITY_SCALE), nullable=False
    )
    purchase_unit: Mapped[str] = mapped_column(
        String(PURCHASE_UNIT_MAX_LENGTH), nullable=False
    )
    purchase_unit_normalized: Mapped[Optional[str]] = mapped_column(
        String(PURCHASE_UNIT_MAX_LENGTH)
    )
    unit_price: Mapped[Decimal] = mapped_column(
        DecimalAsInteger(UNIT_PRICE_SCALE), nullable=False
    )
    line_amount: Mapped[Decimal] = mapped_column(
        DecimalAsInteger(AMOUNT_SCALE), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(CURRENCY_LENGTH), nullable=False)

    line_kind: Mapped[str] = mapped_column(
        String(LINE_KIND_MAX_LENGTH),
        nullable=False,
        default=LINE_KIND_PURCHASE,
        index=True,
    )
    amount_mismatch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    entry_source: Mapped[str] = mapped_column(
        String(ENTRY_SOURCE_MAX_LENGTH), nullable=False
    )
    voided_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    # 步骤 05 补上的关联列：这条采购记录是从哪一张单据的哪一行确认出来的。
    # 手工录入与表格导入的记录为 NULL（「有没有发票」与「从哪条入口来」是两件事，
    # 后者看 entry_source）。
    invoice_line_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("invoice_lines.id"), index=True
    )

    food_item: Mapped[Optional[FoodItem]] = relationship()
    invoice_line: Mapped[Optional["InvoiceLine"]] = relationship(
        back_populates="purchases"
    )
    revisions: Mapped[list["PurchaseRevision"]] = relationship(
        back_populates="purchase",
        cascade="all, delete-orphan",
        order_by="PurchaseRevision.id",
    )

    __table_args__ = (
        # **重复确认保护**（步骤 05）：一个发票行最多只能有一条**未作废**的采购记录。
        # 老板连点两次确认、或两个标签页同时确认同一行，第二次会撞在这个索引上
        # 而不是悄悄记两次价。
        #
        # 为什么是局部索引而不是普通唯一约束：撤销确认是把采购记录**软删**（置
        # voided_at）并把行退回待确认，普通唯一约束会把那一行永远锁死，撤销之后
        # 再也确认不了。条件里同时排除 NULL 是为了手工录入——它们的 invoice_line_id
        # 全是 NULL，SQLite 的唯一索引虽然容许多个 NULL，写明更不容易被误读。
        Index(
            "uq_purchases_active_invoice_line",
            "invoice_line_id",
            unique=True,
            sqlite_where=text("invoice_line_id IS NOT NULL AND voided_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"Purchase(id={self.id!r}, kind={self.line_kind!r}, "
            f"food_item_id={self.food_item_id!r}, "
            f"quantity={self.quantity!r}, line_amount={self.line_amount!r})"
        )


class PurchaseRevision(Base):
    """一条采购记录的修改日志。

    按 2026-11-10 的用户确认，纠错是**直接修改**（含数值），不强制作废重录；
    但每次修改都在这里追加一行，记下改之前和改之后的值。

    这不是可选功能：当前记录始终是修正后的值，步骤 09 的统计按当前值计算，所以
    改过数值之后上个月的均价会跟着变（修正后的值才是事实）。老板下次看到不一样的
    数字时，能不能解释清楚「这个数变过、原来是多少、什么时候改的」，完全取决于
    这张表。

    changed_fields 存 JSON 文本，形如
    {"unit_price": {"before": "5.8000", "after": "8.5000"}}——数值一律**以字符串
    记录**，不经 JSON 的浮点数中转，否则日志本身就会丢精度。

    没有 updated_at：日志只追加、不修改，因此只保留创建时间语义的 changed_at。
    """

    __tablename__ = "purchase_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(
        ForeignKey("purchases.id"), nullable=False, index=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    action: Mapped[str] = mapped_column(
        String(REVISION_ACTION_MAX_LENGTH), nullable=False
    )
    changed_fields: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(REVISION_REASON_MAX_LENGTH))

    purchase: Mapped[Purchase] = relationship(back_populates="revisions")

    def __repr__(self) -> str:
        return (
            f"PurchaseRevision(id={self.id!r}, purchase_id={self.purchase_id!r}, "
            f"action={self.action!r})"
        )


# --- 步骤 05：原始发票、审核草稿与逐行确认 ---------------------------------

INVOICE_NUMBER_MAX_LENGTH = 60
STORAGE_KEY_MAX_LENGTH = 200
UPLOAD_NAME_MAX_LENGTH = 255
CONTENT_TYPE_MAX_LENGTH = 100
SHA256_LENGTH = 64
RECOGNITION_PROVIDER_MAX_LENGTH = 32
LINE_STATUS_MAX_LENGTH = 16
SKIP_REASON_MAX_LENGTH = 200

# 行状态。三态，不多不少（决定见 docs/stru_dev.md 步骤 05）：
# - pending：还没处理。只有它能被确认。
# - confirmed：已经确认并写进了采购历史。
# - skipped：这一行不入账（签名、手写备注、认不出的内容），带一句原因。
# 没有第四种状态的理由：发票整体状态由这三者**派生**出来，不另存一列，
# 免得行状态与发票状态变成互相打架的两份真相。
LINE_STATUS_PENDING = "pending"
LINE_STATUS_CONFIRMED = "confirmed"
LINE_STATUS_SKIPPED = "skipped"
LINE_STATUSES = (LINE_STATUS_PENDING, LINE_STATUS_CONFIRMED, LINE_STATUS_SKIPPED)


class Invoice(Base, TimestampMixin):
    """一张原始单据（docs/imp_notes.md Feature 4）。

    它是个**容器**：挂着若干原始文件（手机拍的多页单据就是多个文件）与若干待确认
    的行。单据头上的几个字段全部**可空**——手写采购单经常没有供应商名、没有单号、
    没有日期、也没有总额，强制必填只会逼着录入的人编一个（docs/stru_dev.md 步骤 05
    的「不搬强制供应商 / 发票号 / USD」）。

    几个列的存在理由：

    - `created_at`（继承自 TimestampMixin）是**上传时间**，`invoice_date` 是**单据
      上写的采购日期**，两者分开存。混在一起的话，补录三个月前的单子会把那笔采购
      算进这个月的均价。
    - `source_name_normalized` / `invoice_number_normalized` 服务重复上传判定：
      Costco 与 costco 要算同一家，否则「可能重复」这条提示形同虚设。
    - `invoice_total` 是单据上印的总额，用来和各行金额之和对账。它**只是单据事实**，
      不参与任何价格统计。
    - `recognition_provider` / `recognition_is_fake` / `recognized_at`：这张草稿是
      谁识别出来的。本轮只有 Fake 识别器，`recognition_is_fake=True` 必须一路传到
      接口响应上——演示时不能让假数据看起来像真识别。步骤 02 的契约已经强制了
      `is_fake`，这里把它**落库**，于是事后翻历史也分得清。手工建的空草稿这三列
      都是 NULL。
    - `completed_at` 是**收口确认**：老板审完最后一行点一下「完成」，系统把「已确认
      N 行、跳过 M 行、共几个文件」摆给他看，他对一眼原件再确认。这是**漏行唯一会
      被拦住的时刻**——各行金额之和与单据总额对不上能抓到一部分，但手写单常常连总额
      都没有，那时就只剩人眼这一关。
    - `voided_at` 是软删（传错了整张单）。已确认行须显式核对 ID，删除时在同一
      事务中作废对应采购记录并保留修改日志。
    """

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    # invoices 同时承载表格审核批次；不把整月流水伪装成一张真实发票。
    entry_source: Mapped[str] = mapped_column(String(16), nullable=False, default="invoice")
    import_metadata: Mapped[Optional[str]] = mapped_column(Text)
    source_name: Mapped[Optional[str]] = mapped_column(String(SOURCE_NAME_MAX_LENGTH))
    source_name_normalized: Mapped[Optional[str]] = mapped_column(
        String(SOURCE_NAME_MAX_LENGTH), index=True
    )
    invoice_number: Mapped[Optional[str]] = mapped_column(
        String(INVOICE_NUMBER_MAX_LENGTH)
    )
    invoice_number_normalized: Mapped[Optional[str]] = mapped_column(
        String(INVOICE_NUMBER_MAX_LENGTH), index=True
    )
    # 日历日，与 purchases.purchase_date 同一个理由：用带时区的时刻会让同一张单据
    # 在纽约和上海落进不同的「月」。
    invoice_date: Mapped[Optional[date]] = mapped_column(Date)
    currency: Mapped[Optional[str]] = mapped_column(String(CURRENCY_LENGTH))
    invoice_total: Mapped[Optional[Decimal]] = mapped_column(
        DecimalAsInteger(AMOUNT_SCALE)
    )

    recognition_provider: Mapped[Optional[str]] = mapped_column(
        String(RECOGNITION_PROVIDER_MAX_LENGTH)
    )
    recognition_is_fake: Mapped[Optional[bool]] = mapped_column(Boolean)
    recognized_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    # 单据头的原始识别证据，一列 JSON（与行上的 raw_fields 同一个道理）。
    # 上面那几个列是**解析后**的值：识别出来的日期是 `11/08/26` 这种写法时解析不出
    # 日历日，invoice_date 就只能留空。原文如果不留着，老板只会看到一个空日期，
    # 既不知道单据上到底写了什么，也不知道是没识别还是识别错了。
    raw_document_fields: Mapped[Optional[str]] = mapped_column(Text)

    completed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    voided_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)

    files: Mapped[list["InvoiceFile"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceFile.page_order, InvoiceFile.id",
    )
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLine.line_no, InvoiceLine.id",
    )

    def __repr__(self) -> str:
        return (
            f"Invoice(id={self.id!r}, source_name={self.source_name!r}, "
            f"invoice_date={self.invoice_date!r})"
        )


class InvoiceFile(Base):
    """一张单据的一个原始文件（一张照片，或一份 PDF）。

    `storage_key` 指向 `integrations/file_storage.py` 存下来的那份字节，数据库只存
    引用、不存内容。原始文件**只写不改**，它是日后核对的证据。

    `sha256` 上有**唯一索引**，这就是「完全相同的文件不许重复上传」的落点：老板把
    同一张照片传两次，第二次撞索引，接口回 409 并指回已有的那张发票，让他去接着审
    那一张，而不是又建一张一模一样的草稿。补拍的照片字节不同、摘要也不同，照样能
    传——那本来就是正常操作。

    `page_order` 是**上传顺序**，用作默认页序。多页 PDF 的内部页码不在这里展开
    （行上的 `page` 才是「这一行在第几页」）；把多个文件与多页归并成一份连续页序
    属于步骤 08。

    没有 updated_at：文件记录只新增与删除，不修改。
    """

    __tablename__ = "invoice_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id"), nullable=False, index=True
    )
    storage_key: Mapped[str] = mapped_column(
        String(STORAGE_KEY_MAX_LENGTH), nullable=False, unique=True
    )
    original_name: Mapped[str] = mapped_column(
        String(UPLOAD_NAME_MAX_LENGTH), nullable=False
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(
        String(SHA256_LENGTH), nullable=False, unique=True
    )
    content_type: Mapped[Optional[str]] = mapped_column(String(CONTENT_TYPE_MAX_LENGTH))
    page_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )

    invoice: Mapped[Invoice] = relationship(back_populates="files")

    def __repr__(self) -> str:
        return (
            f"InvoiceFile(id={self.id!r}, invoice_id={self.invoice_id!r}, "
            f"original_name={self.original_name!r})"
        )


class InvoiceLine(Base, TimestampMixin):
    """单据上的一行，以及老板审到哪一步了。

    **原始识别值与当前待确认值分开存**，这是本表最要紧的一条：

    - 下面那几个业务列（`description`、`quantity`、`purchase_unit`、`unit_price`、
      `line_amount`）是**当前值**——识别出来的、或者老板改过的，确认时就按它们入账。
      全部**可空**：认不出来的格子就是空的，`unit_price` 不会因为识别失败变成 0
      （0 是「赠品」这个具体业务含义，「没认出来」不是）。
    - `raw_fields` 是**原始识别证据**，一列 JSON：每个字段的原文、
      `recognized` / `unreadable` / `missing` 状态、置信度、页码与可得的区域坐标，
      直接来自 `src/contracts.py` 的 `RecognizedLine`。确认入账之后它**永不改动**
      ——那是单据事实；老板后来把数字改对了，差异由 `purchase_revisions` 解释。
      用一列 JSON 而不是摊成十几个列，是因为它只被原样读出来给人看，不参与查询、
      排序与统计。

    `purchase_date` / `source_name` / `currency` 是**行级覆盖值**，为空就继承单据头
    （一张单上偶尔有一行是别的日期或别的来源）。继承发生在确认那一刻，之后改单据头
    **不会**回溯已经入账的采购记录——那些走 `PATCH /api/purchases/<id>`，差异由修改
    日志解释。

    `line_kind` 沿用步骤 04 的两种：正常食材行是 `purchase`；税费、运费、押金、
    退货与折让是 `adjustment`（金额可负、食材可空、默认不进价格统计，只用于对账）。

    `version` 是乐观锁：修改与确认都可以带上它，不一致就 409。防的是两个标签页（或
    手机和电脑）同时审同一行——后一个会被拦住，而不是悄悄覆盖前一个的改动。

    `line_no` 在同一张单据内唯一：它是「单据上的第几行」，也是排序的稳定键。
    """

    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(LINE_STATUS_MAX_LENGTH),
        nullable=False,
        default=LINE_STATUS_PENDING,
        index=True,
    )
    skip_reason: Mapped[Optional[str]] = mapped_column(String(SKIP_REASON_MAX_LENGTH))
    line_kind: Mapped[str] = mapped_column(
        String(LINE_KIND_MAX_LENGTH), nullable=False, default=LINE_KIND_PURCHASE
    )

    food_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("food_items.id"), index=True
    )
    description: Mapped[Optional[str]] = mapped_column(String(DESCRIPTION_MAX_LENGTH))
    quantity: Mapped[Optional[Decimal]] = mapped_column(
        DecimalAsInteger(QUANTITY_SCALE)
    )
    purchase_unit: Mapped[Optional[str]] = mapped_column(
        String(PURCHASE_UNIT_MAX_LENGTH)
    )
    unit_price: Mapped[Optional[Decimal]] = mapped_column(
        DecimalAsInteger(UNIT_PRICE_SCALE)
    )
    line_amount: Mapped[Optional[Decimal]] = mapped_column(
        DecimalAsInteger(AMOUNT_SCALE)
    )

    purchase_date: Mapped[Optional[date]] = mapped_column(Date)
    source_name: Mapped[Optional[str]] = mapped_column(String(SOURCE_NAME_MAX_LENGTH))
    currency: Mapped[Optional[str]] = mapped_column(String(CURRENCY_LENGTH))

    raw_fields: Mapped[Optional[str]] = mapped_column(Text)
    # 当前待处理问题和显式行级覆盖，与永不修改的原始证据分开。
    review_metadata: Mapped[Optional[str]] = mapped_column(Text)
    page: Mapped[Optional[int]] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
    food_item: Mapped[Optional[FoodItem]] = relationship()
    # 一行**最多一条未作废**的采购记录（局部唯一索引在 Purchase 上）；撤销后重新
    # 确认会留下已作废的旧记录，所以这里是一对多。
    purchases: Mapped[list[Purchase]] = relationship(
        back_populates="invoice_line", order_by="Purchase.id"
    )

    __table_args__ = (UniqueConstraint("invoice_id", "line_no"),)

    def __repr__(self) -> str:
        return (
            f"InvoiceLine(id={self.id!r}, invoice_id={self.invoice_id!r}, "
            f"line_no={self.line_no!r}, status={self.status!r})"
        )


# --- 步骤 08：真实 OCR 提取任务与供应商路由 ---------------------------------

EXTRACTION_STATUS_MAX_LENGTH = 16
DOC_CLASS_MAX_LENGTH = 24
ROUTE_DECIDED_FROM_MAX_LENGTH = 32
ERROR_CODE_MAX_LENGTH = 40
ERROR_MESSAGE_MAX_LENGTH = 600
PROMPT_SHA_MAX_LENGTH = 16
DATE_ORDER_MAX_LENGTH = 8

# 提取任务的状态。四态，不多不少：
# - queued：已入队，等 worker 取。
# - running：worker 正在跑。
# - succeeded / failed：跑完了。
#
# **没有「部分成功」这一态**：多页单据里某一页失败时，任务整体仍算 succeeded，
# 失败的那一页在 warnings 里写明并且不产出行。理由是老板关心的是"这张单能不能
# 接着审"，而不是任务的内部进度；哪一页没出来，审核界面按行列出来就看得见。
EXTRACTION_STATUS_QUEUED = "queued"
EXTRACTION_STATUS_RUNNING = "running"
EXTRACTION_STATUS_SUCCEEDED = "succeeded"
EXTRACTION_STATUS_FAILED = "failed"
EXTRACTION_STATUSES = (
    EXTRACTION_STATUS_QUEUED,
    EXTRACTION_STATUS_RUNNING,
    EXTRACTION_STATUS_SUCCEEDED,
    EXTRACTION_STATUS_FAILED,
)

# 单据类别，决定走哪条提取路径（步骤 08 的分流表）。
# 三条路径的耗时差一个数量级，所以分流是必须的，不是优化。
DOC_CLASS_DIGITAL_PDF = "digital_pdf"
"""带文字层的数字 PDF：直接抽文字层，**根本不调 OCR**。近 0 耗时、零成本。"""

DOC_CLASS_PRINTED_SCAN = "printed_scan"
"""印刷体扫描件：墨迹检测 + 1 轮单模型，约 5 秒。"""

DOC_CLASS_HANDWRITTEN_SCAN = "handwritten_scan"
"""手写件：2 轮（先结构后内容）+ 多模型并行，约 15 秒。**所有字段默认不预填。**"""

DOC_CLASS_UNKNOWN = "unknown"
"""判不出来。按最保守的那条走（当手写件处理：不预填），而不是按最省钱的那条走。"""

DOC_CLASSES = (
    DOC_CLASS_DIGITAL_PDF,
    DOC_CLASS_PRINTED_SCAN,
    DOC_CLASS_HANDWRITTEN_SCAN,
    DOC_CLASS_UNKNOWN,
)

# 这条路线是怎么定下来的。
# ⚠️ **刻意没有「按识别出的内容判断」这一种**：步骤 08 已明确「分流依据必须与
# 识别出的内容无关」，且「禁止用各数字算得平当分流开关」——步骤 07 的
# `8 × 117 = 936` 证明算术自洽的编造完全存在，拿它当放行条件等于给编造开绿灯。
ROUTE_FROM_SUPPLIER = "supplier"
"""按供应商路由（主）。供应商是固定的，首次确认时记下这家走哪条路。"""

ROUTE_FROM_PDF_TEXT_LAYER = "pdf_text_layer"
"""PDF 里有没有文字层——这是文件结构的客观事实，与识别内容无关。"""

ROUTE_FROM_LOCAL_DISCRIMINATOR = "local_discriminator"
"""本地的印刷 / 手写判别（笔画粗细是否均匀），新供应商首次出现时的兜底。"""

ROUTE_FROM_MANUAL = "manual"
"""人工指定。"""

ROUTE_SOURCES = (
    ROUTE_FROM_SUPPLIER,
    ROUTE_FROM_PDF_TEXT_LAYER,
    ROUTE_FROM_LOCAL_DISCRIMINATOR,
    ROUTE_FROM_MANUAL,
)


class ExtractionJob(Base, TimestampMixin):
    """一次提取任务（步骤 08.5：提取状态必须持久化）。

    **为什么要落库而不是放内存**：手写件一页约 15 秒，多页单据要更久，不能挂在
    HTTP 请求里（08.3g）。老板拍完照可以去忙别的，回来再审——那就要求「提取到哪
    一步了」在进程重启、浏览器关掉之后仍然查得到。放内存的队列一重启就全丢，
    而丢掉的表现是"那张单一直没出行"，没有任何错误信息。

    **失败也要留下来**：超时、限流、凭据失效都记 `error_code` 与
    `error_message`，并且**不动原始文件与已有草稿**（08.5 的验收条件）。
    一次失败的提取不该让单据跟着消失。

    `models` / `usage` / `warnings` 都是 JSON 文本：它们只被原样读出来给人看，
    不参与查询与统计（与 `invoice_lines.raw_fields` 同一个道理）。

    ⚠️ **提取永远不写采购记录。** 采购记录的唯一入口是老板对某一行点确认
    （步骤 05 的口径）。所以「提取重试不导致采购重复入账」（08.6）在结构上就成立：
    重试最多影响草稿行，碰不到 `purchases`。重试与已确认行的关系另有一道闸门，
    见 `services/extraction.py` 的 `_check_rerun_allowed`。
    """

    __tablename__ = "extraction_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id"), nullable=False, index=True
    )
    # 这次提取针对哪个文件。多页 PDF 是一个文件多页；一张单据挂多张照片时，
    # 每个文件一个任务，于是某一张拍坏了只影响它自己那一条。
    invoice_file_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("invoice_files.id"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(EXTRACTION_STATUS_MAX_LENGTH),
        nullable=False,
        default=EXTRACTION_STATUS_QUEUED,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    doc_class: Mapped[Optional[str]] = mapped_column(String(DOC_CLASS_MAX_LENGTH))
    route_source: Mapped[Optional[str]] = mapped_column(
        String(ROUTE_DECIDED_FROM_MAX_LENGTH)
    )
    provider: Mapped[Optional[str]] = mapped_column(
        String(RECOGNITION_PROVIDER_MAX_LENGTH)
    )
    models: Mapped[Optional[str]] = mapped_column(Text)
    # 提示词的身份证。步骤 07 的选型结论绑定在它上面（sha256 前 12 位
    # 12e0246b0377）；换了提示词，那批准确率数字就不再适用，而这一列让事后能查出
    # 「这张单是用哪个被测配置提取的」。
    prompt_sha12: Mapped[Optional[str]] = mapped_column(String(PROMPT_SHA_MAX_LENGTH))
    pages_total: Mapped[Optional[int]] = mapped_column(Integer)
    pages_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(ERROR_CODE_MAX_LENGTH))
    error_message: Mapped[Optional[str]] = mapped_column(
        String(ERROR_MESSAGE_MAX_LENGTH)
    )
    usage: Mapped[Optional[str]] = mapped_column(Text)
    warnings: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)

    invoice: Mapped[Invoice] = relationship()
    invoice_file: Mapped[Optional[InvoiceFile]] = relationship()

    __table_args__ = (
        # 「这张单还有没有在跑的提取」要快；worker 取任务也按状态 + 时间扫。
        Index("ix_extraction_jobs_status_created", "status", "created_at"),
    )

    @property
    def is_finished(self) -> bool:
        return self.status in (
            EXTRACTION_STATUS_SUCCEEDED,
            EXTRACTION_STATUS_FAILED,
        )

    def __repr__(self) -> str:
        return (
            f"ExtractionJob(id={self.id!r}, invoice_id={self.invoice_id!r}, "
            f"status={self.status!r}, doc_class={self.doc_class!r})"
        )


class OcrRoute(Base, TimestampMixin):
    """某家供应商的单据走哪条提取路径（步骤 08.3f：按供应商路由分流）。

    **这不是供应商表。** 步骤 05 刻意没建供应商主表，本表也不是它：这里只有
    「规范化后的来源名 → 走哪条路」这一个映射，没有联系人、地址、账期、商品目录。
    键用 `source_name_normalized`，与 `invoices` / `purchases` 上那两列同一个口径
    （Costco 与 costco 是同一家）。

    **为什么按供应商而不按内容判**：供应商是固定的——这家一直寄印刷体 PDF，那家
    一直是手写复写纸。首次确认时记下来，之后自动分流，既快又不依赖对内容的判断。
    步骤 08 已明确「分流依据必须与识别出的内容无关」。

    `date_order` 解一个实际麻烦：`6/11/2026` 的日/月顺序**文本本身定不死**，
    `normalization` 因此拒绝解析，于是老板要在每张美式日期的单据上手填日期。
    这一列让这件事变成**每家供应商说一次**：填 `mdy` 之后，这家的单据就按月/日/年
    读。留空就继续按 `strict` 拒绝——**宁可留空让人填，也不猜**。
    """

    __tablename__ = "ocr_routes"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_name_normalized: Mapped[str] = mapped_column(
        String(SOURCE_NAME_MAX_LENGTH), nullable=False, unique=True
    )
    source_name: Mapped[Optional[str]] = mapped_column(String(SOURCE_NAME_MAX_LENGTH))
    doc_class: Mapped[str] = mapped_column(String(DOC_CLASS_MAX_LENGTH), nullable=False)
    decided_from: Mapped[str] = mapped_column(
        String(ROUTE_DECIDED_FROM_MAX_LENGTH), nullable=False
    )
    date_order: Mapped[Optional[str]] = mapped_column(String(DATE_ORDER_MAX_LENGTH))
    note: Mapped[Optional[str]] = mapped_column(String(ERROR_MESSAGE_MAX_LENGTH))

    def __repr__(self) -> str:
        return (
            f"OcrRoute(source_name_normalized={self.source_name_normalized!r}, "
            f"doc_class={self.doc_class!r})"
        )


# --- 步骤 09：包装规格与涨价提醒设置 ---------------------------------------
#
# 本步骤**只加这两张表**，而且都不存派生值。
#
# `purchases` 上刻意**没有**加 `normalized_unit_price` 列：标准化价格是派生值，
# 由 `services/prices.py` 在查询时算（决定与理由见 docs/stru_dev.md 步骤 09）。
# 落一列回填的话，箱规一改、采购记录一改，那列就悄悄过期——而它看起来仍然像个
# 可信的数字。这正是本项目反复防的那一类错误。

PACK_SPEC_TEXT_MAX_LENGTH = 60
PACK_SPEC_NOTE_MAX_LENGTH = 300

# 一个包装单位能换出多少基础单位，小数位数沿用数量的 scale（步骤 04 定的 4 位）：
# 真实箱规出现过 `6X44.10`（44.1 lb/箱）这种两位小数，4 位足够且与数量同口径。
PACK_SIZE_SCALE = QUANTITY_SCALE

# 涨价阈值的百分比，2 位小数（10.00 表示上涨 10%）。
THRESHOLD_PERCENT_SCALE = 2

# 提醒设置是单行表，主键固定为 1。见 PriceAlertSettings 的文档字符串。
PRICE_ALERT_SETTINGS_ID = 1
DEFAULT_ALERT_THRESHOLD_PERCENT = Decimal("10.00")


class PackSpec(Base, TimestampMixin):
    """一条包装规格：「这家供应商的这种食材，1 个包装单位 = 多少基础单位」。

    **为什么必须有这张表**：`imp_notes.md` Feature 2 写明「Case、pack
    等包装单位必须知道具体包装规格后才能换算」。采购记录里能存的只有单据上印的
    东西（`20` `箱` `$52.00`），而「一箱里有多少」纸上**不一定**写，写了也**读不准**
    ——步骤 07 的真实 Ground Truth 里同样的 `数字X数字` 写法有两种互不相容的读法：

    | 原文 | 供应商 | 单据上的数量/单位 | 实际含义 |
    | --- | --- | --- | --- |
    | `1000g x 10` | Kun Kee | `20` `箱` | 每箱 10 × 1000 g |
    | `6X30` | New Moy's | `180` `lbs.` | **6 箱 × 每箱 30 磅** |

    差 6 倍，而且两种读法各自都算得通。所以换算依据只能是**人确认过一次**的事实，
    存在这里；原文放 `spec_text` 作为他当时依据的证据，**不参与计算**。

    **键是（食材, 来源, 包装单位）**，不是采购记录。理由是产品上的：箱规是这家
    供应商的长期事实，不是某一张单据的属性。维护一次，这家这种货的**历史与以后**
    的采购全部能算出标准化价格；否则每来一张新单据都要重填一遍箱规，而老板要的
    恰恰是少操作。

    `source_name_normalized` **可空**，表示「不限来源」：步骤 04 允许采购来源留空
    （老板直接去超市也要能录），那些记录只能靠不限来源的箱规。查找顺序是**先精确
    来源、再不限来源**，由 `repositories/prices.py` 实现。

    ⚠️ **代价要说明白**：改一条箱规会让**历史**标准化价格跟着变。这与步骤 04 已定的
    口径一致（「统计按当前值算，所以改过数值之后上个月的均价会跟着变——修正后的值
    才是事实」），而且价格响应里会标明用的是哪一条箱规（`pack_spec_id` 与
    `spec_text`），所以数字变了解释得清。按生效日期分段的箱规（同一家中途换了包装）
    本轮**不做**：没有实测样本支持，先不造这个复杂度。
    """

    __tablename__ = "pack_specs"

    id: Mapped[int] = mapped_column(primary_key=True)
    food_item_id: Mapped[int] = mapped_column(
        ForeignKey("food_items.id"), nullable=False, index=True
    )
    # 可空 = 不限来源。与 purchases.source_name_normalized 同一个规范化口径。
    source_name_normalized: Mapped[Optional[str]] = mapped_column(
        String(SOURCE_NAME_MAX_LENGTH)
    )
    source_name: Mapped[Optional[str]] = mapped_column(String(SOURCE_NAME_MAX_LENGTH))
    # 包装单位 token（case / pack / box / carton / bag），与采购记录的
    # purchase_unit_normalized 同一套 token。
    purchase_unit_normalized: Mapped[str] = mapped_column(
        String(PURCHASE_UNIT_MAX_LENGTH), nullable=False
    )
    pack_size: Mapped[Decimal] = mapped_column(
        DecimalAsInteger(PACK_SIZE_SCALE), nullable=False
    )
    # 基础单位 token（lb / g / ml / each……）。不能是包装单位，否则「1 箱 = 6 箱」
    # 这种循环定义会存进来；由 services/units.py 的 require_measurement_unit 校验。
    pack_unit: Mapped[str] = mapped_column(
        String(BASE_UNIT_MAX_LENGTH), nullable=False
    )
    # 人当时依据的原文（`6X44.10`、`1000g x 10`）。只作证据，不参与计算。
    spec_text: Mapped[Optional[str]] = mapped_column(String(PACK_SPEC_TEXT_MAX_LENGTH))
    note: Mapped[Optional[str]] = mapped_column(String(PACK_SPEC_NOTE_MAX_LENGTH))

    food_item: Mapped[FoodItem] = relationship()

    __table_args__ = (
        # 同一个（食材, 来源, 包装单位）只能有一条箱规。
        #
        # 为什么是**两条局部唯一索引**而不是一条普通唯一约束：SQLite（以及标准
        # SQL）的唯一索引把多个 NULL 视为互不相同，于是「不限来源」那条记录可以
        # 重复插入无数份，而重复的箱规意味着同一行采购记录的标准化价格取决于查询
        # 命中了哪一条——不报错，却会给出两个不同的价格。拆成两条带 WHERE 的索引
        # 之后，两种情况都真的唯一。局部索引这个手法步骤 05 已经用过一次
        # （purchases.uq_purchases_active_invoice_line）。
        Index(
            "uq_pack_specs_item_source_unit",
            "food_item_id",
            "source_name_normalized",
            "purchase_unit_normalized",
            unique=True,
            sqlite_where=text("source_name_normalized IS NOT NULL"),
        ),
        Index(
            "uq_pack_specs_item_any_source_unit",
            "food_item_id",
            "purchase_unit_normalized",
            unique=True,
            sqlite_where=text("source_name_normalized IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"PackSpec(id={self.id!r}, food_item_id={self.food_item_id!r}, "
            f"source={self.source_name_normalized!r}, "
            f"1 {self.purchase_unit_normalized} = {self.pack_size!r} {self.pack_unit!r})"
        )


class PriceAlertSettings(Base, TimestampMixin):
    """涨价提醒的阈值与开关（步骤 09.8：阈值可调整，也可以关闭）。

    **单行表**：主键固定为 1，由一条 CHECK 约束守着。为什么不放配置文件或环境变量
    ——`imp_notes.md` Feature 2 要求阈值「可调整，也可以关闭」，那是老板在界面上
    按一下的事，不是改部署配置重启应用的事。

    **为什么不做按食材的阈值**：本轮没有要求，也没有实测依据说明牛肉和米该用不同的
    阈值。要加的时候是另建一张 `(food_item_id → 阈值)` 的覆盖表，不是改这一行，
    所以现在不预留那个可空外键（步骤 04.8 的「不预留悬空外键」同一条）。

    阈值的含义固定为「与基期相比的上涨百分比」，基期由 `services/prices.py` 的
    可比期间口径决定（本月截至今日 vs 上月同样天数）。阈值只管上涨：**跌价不提醒**
    ——老板要的是"别悄悄涨价"，跌价不需要提醒他去确认。
    """

    __tablename__ = "price_alert_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    threshold_percent: Mapped[Decimal] = mapped_column(
        DecimalAsInteger(THRESHOLD_PERCENT_SCALE),
        nullable=False,
        default=DEFAULT_ALERT_THRESHOLD_PERCENT,
    )

    __table_args__ = (
        CheckConstraint(
            f"id = {PRICE_ALERT_SETTINGS_ID}", name="single_row"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"PriceAlertSettings(enabled={self.enabled!r}, "
            f"threshold_percent={self.threshold_percent!r})"
        )
