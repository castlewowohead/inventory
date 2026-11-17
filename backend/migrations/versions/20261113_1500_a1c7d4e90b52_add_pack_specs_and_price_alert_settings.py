"""add pack specs and price alert settings

Revision ID: a1c7d4e90b52
Revises: 0e45ee78ef8c
Create Date: 2026-11-13 15:00:00.000000

步骤 09 加两张表，**都不存派生值**：

1. `pack_specs`：「这家供应商的这种食材，1 个包装单位 = 多少基础单位」。
   没有这个事实就没法把 `20 箱 × $52.00` 变成 `$/kg`——而「一箱里有多少」纸上
   不一定印，印了也读不准（步骤 07 实测：`1000g x 10` 与 `6X30` 这两种同形写法
   的含义差 6 倍）。所以换算依据只能是人确认过一次的值，原文另存 `spec_text`
   作为证据、不参与计算。
2. `price_alert_settings`：涨价阈值与开关（09.8「阈值可调整，也可以关闭」）。
   单行表，主键由 CHECK 约束固定为 1。阈值放库里而不是配置文件：那是老板在界面上
   按一下的事，不是改部署配置重启应用的事。

⚠️ **`purchases` 一列都没有加。** 标准化价格（`normalized_unit_price`）是派生值，
由 `services/prices.py` 查询时算。落一列回填的话，箱规一改、采购记录一改，那列
就悄悄过期，而它看起来仍然像个可信的数字——本项目反复防的正是这一类错误。
原始数量、单位与金额是人工复核过的事实，本步骤不碰（docs/stru_dev.md 09.5）。

沿用既有约定（同 0e45ee78ef8c）：

- **时间列声明成 `sa.DateTime()`，不引用 `src.models`。** 迁移是结构的历史快照，
  不该 import 应用模型；`UtcDateTime` 的 impl 就是 DateTime，DDL 完全相同。
- **数值列声明成 `sa.Integer()`**：`pack_size` 与 `threshold_percent` 走的是步骤 04
  的 `DecimalAsInteger`（按整数最小单位存，pack_size 4 位小数、阈值 2 位），
  底层就是整数列。⚠️ 不要在这里改成 `sa.Numeric`——SQLite 下它落成 REAL，
  步骤 04 已实测会静默丢精度。
- **约束与索引一律显式命名**。
- **两条唯一索引带 WHERE**：`source_name_normalized` 可空（NULL = 不限来源），
  而标准 SQL 把多个 NULL 视为互不相同，一条普通唯一约束拦不住重复的"不限来源"
  箱规；重复箱规会让同一行采购记录的标准化价格取决于查询命中了哪一条——不报错，
  却给出两个不同的价格。局部索引这个手法步骤 05 已经用过
  （`uq_purchases_active_invoice_line`）。

`downgrade()` 删两张表。**它会丢业务事实**：箱规是人确认过一次的换算依据，删掉
之后所有按包装单位采购的记录都重新变成"算不出标准化价格"，要重新维护一遍；
采购记录、发票与草稿都在别的表里，不受影响。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1c7d4e90b52"
down_revision: Union[str, Sequence[str], None] = "0e45ee78ef8c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pack_specs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("food_item_id", sa.Integer(), nullable=False),
        # 可空 = 不限来源。口径与 purchases.source_name_normalized 相同
        # （Costco 与 costco 是同一家）。
        sa.Column("source_name_normalized", sa.String(length=120), nullable=True),
        sa.Column("source_name", sa.String(length=120), nullable=True),
        # 包装单位 token：case / pack / box / carton / bag。
        sa.Column("purchase_unit_normalized", sa.String(length=32), nullable=False),
        # DecimalAsInteger(4)：44.1 lb/箱 存成 441000。
        sa.Column("pack_size", sa.Integer(), nullable=False),
        # 基础单位 token，不能是包装单位（否则「1 箱 = 6 箱」这种循环定义）。
        sa.Column("pack_unit", sa.String(length=16), nullable=False),
        # 人当时依据的原文（`6X44.10` / `1000g x 10`）。只作证据。
        sa.Column("spec_text", sa.String(length=60), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["food_item_id"],
            ["food_items.id"],
            name=op.f("fk_pack_specs_food_item_id_food_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pack_specs")),
    )
    op.create_index(
        op.f("ix_pack_specs_food_item_id"), "pack_specs", ["food_item_id"]
    )
    op.create_index(
        "uq_pack_specs_item_source_unit",
        "pack_specs",
        ["food_item_id", "source_name_normalized", "purchase_unit_normalized"],
        unique=True,
        sqlite_where=sa.text("source_name_normalized IS NOT NULL"),
    )
    op.create_index(
        "uq_pack_specs_item_any_source_unit",
        "pack_specs",
        ["food_item_id", "purchase_unit_normalized"],
        unique=True,
        sqlite_where=sa.text("source_name_normalized IS NULL"),
    )

    op.create_table(
        "price_alert_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        # DecimalAsInteger(2)：10% 存成 1000。
        sa.Column("threshold_percent", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_price_alert_settings_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_alert_settings")),
    )
    # 不在迁移里插入默认设置：默认值（开启、10%）由 services/prices.py 在第一次
    # 读取时就地建出来。放在迁移里的话，老板把提醒关掉之后再执行一次迁移，
    # 这一行不会被重写（INSERT 只在建表时跑一次），两处各有一个"默认"反而容易
    # 让人误以为迁移会重置设置。


def downgrade() -> None:
    op.drop_table("price_alert_settings")
    op.drop_index("uq_pack_specs_item_any_source_unit", table_name="pack_specs")
    op.drop_index("uq_pack_specs_item_source_unit", table_name="pack_specs")
    op.drop_index(op.f("ix_pack_specs_food_item_id"), table_name="pack_specs")
    op.drop_table("pack_specs")
