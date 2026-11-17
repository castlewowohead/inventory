"""add purchases and purchase revisions

Revision ID: 520863805194
Revises: c6f14af1ab06
Create Date: 2026-11-10 12:09:34.395301

（首行刻意用 ASCII：Alembic 会把它作为修订说明打到控制台，中文在 GBK 控制台下
显示为乱码。中文说明放在首行之后，不影响命令输出。）

步骤 04 的业务表：采购记录与它的修改日志。

四处需要说明的写法：

1. **数值列声明成 `sa.Integer()`，不引用 `src.models.DecimalAsInteger`。**
   三个数值列按整数最小单位存（数量与单价 4 位小数、行金额 2 位），放大与缩小由
   模型层的类型装饰器负责，数据库里就是整数。和步骤 03 的时间列同一个道理：迁移
   脚本是结构的历史快照，不应该 import 应用模型——模型以后改名或搬家时，旧修订
   仍然要能原样执行。`alembic revision --autogenerate` 会在这里写出
   `src.models.DecimalAsInteger()`（而且丢掉 scale 参数），必须手工改回 impl 类型。
2. **时间列同理用 `sa.DateTime()`**，与 `UtcDateTime` 生成的 DDL 完全相同。
3. **`purchase_date` 是 `sa.Date()` 而不是 `DateTime`。** 发票上写的是日历日；
   用带时区的时间会让同一张单据在纽约和上海落进不同的「月」，污染步骤 09 的月度
   均价。它**可空**：手写单上常常没写日期，识别不出时允许留空直接保存。
4. **没有 `invoice_line_id`，也没有 `normalized_unit_price`。** 发票表在步骤 05
   才出现，本修订不预留悬空外键，由那一步的迁移补上关联列；标准化价格是换算后的
   派生值，归步骤 09。

`source_name_normalized` 与 `original_description_normalized` 是检索冗余列，由
`src/services/purchases.py` 在写入时生成：前者让 `Costco` / `costco` 算同一个来源，
后者供步骤 05 做「这条原始描述以前确认到哪个食材」的精确命中，所以**现在就带索引**
（否则那一步得全表扫，而且又要改表）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "520863805194"
down_revision: Union[str, Sequence[str], None] = "c6f14af1ab06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "purchases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("food_item_id", sa.Integer(), nullable=False),
        sa.Column("purchase_date", sa.Date(), nullable=True),
        sa.Column("source_name", sa.String(length=120), nullable=True),
        sa.Column("source_name_normalized", sa.String(length=120), nullable=True),
        sa.Column("original_description", sa.String(length=300), nullable=True),
        sa.Column(
            "original_description_normalized", sa.String(length=300), nullable=True
        ),
        # 整数最小单位：数量与单价 4 位小数、行金额 2 位。见本文件说明第 1 条。
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("purchase_unit", sa.String(length=32), nullable=False),
        sa.Column("purchase_unit_normalized", sa.String(length=32), nullable=True),
        sa.Column("unit_price", sa.Integer(), nullable=False),
        sa.Column("line_amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_mismatch", sa.Boolean(), nullable=False),
        sa.Column("entry_source", sa.String(length=16), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["food_item_id"],
            ["food_items.id"],
            name=op.f("fk_purchases_food_item_id_food_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchases")),
    )
    op.create_index(
        op.f("ix_purchases_food_item_id"), "purchases", ["food_item_id"], unique=False
    )
    op.create_index(
        op.f("ix_purchases_purchase_date"), "purchases", ["purchase_date"], unique=False
    )
    op.create_index(
        op.f("ix_purchases_source_name_normalized"),
        "purchases",
        ["source_name_normalized"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchases_original_description_normalized"),
        "purchases",
        ["original_description_normalized"],
        unique=False,
    )

    op.create_table(
        "purchase_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purchase_id", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("changed_fields", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(
            ["purchase_id"],
            ["purchases.id"],
            name=op.f("fk_purchase_revisions_purchase_id_purchases"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_revisions")),
    )
    op.create_index(
        op.f("ix_purchase_revisions_purchase_id"),
        "purchase_revisions",
        ["purchase_id"],
        unique=False,
    )


def downgrade() -> None:
    # 先删引用方（purchase_revisions）再删被引用方（purchases），否则外键拦住。
    op.drop_index(
        op.f("ix_purchase_revisions_purchase_id"), table_name="purchase_revisions"
    )
    op.drop_table("purchase_revisions")

    op.drop_index(
        op.f("ix_purchases_original_description_normalized"), table_name="purchases"
    )
    op.drop_index(op.f("ix_purchases_source_name_normalized"), table_name="purchases")
    op.drop_index(op.f("ix_purchases_purchase_date"), table_name="purchases")
    op.drop_index(op.f("ix_purchases_food_item_id"), table_name="purchases")
    op.drop_table("purchases")
