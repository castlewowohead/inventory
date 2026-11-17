"""add categories and food items

Revision ID: 3f6bd2d5edaa
Revises: 73ebb18fcafe
Create Date: 2026-11-10 10:23:19.905128

（首行刻意用 ASCII：Alembic 会把它作为修订说明打到控制台，中文在 GBK 控制台下
显示为乱码。中文说明放在首行之后，不影响命令输出。）

步骤 03 的业务表：分类与标准食材。初始分类数据不在本修订里，见紧随其后的
`seed initial categories` 修订。

两处需要说明的写法：

1. **时间列用 `sa.DateTime()`，不引用 `src.models.UtcDateTime`。** 两者在数据库里
   生成的 DDL 完全相同（`UtcDateTime` 的 `impl` 就是 `DateTime`，它只负责在进出
   Python 时处理时区）。迁移脚本是结构的历史快照，不应该 import 应用模型：模型以后
   改名或搬家时，旧修订仍然要能原样执行。
2. **`name_zh_normalized` 唯一但可空。** SQLite（以及 PostgreSQL）的唯一约束允许多个
   NULL 并存，所以"中文名可以不填，但填了就不能和别人重复"用一个唯一约束就能表达，
   不需要额外的条件索引。

`search_text` 是英文名 + 中文名 + 规格的规范化拼接，由 `src/services/ingredients.py`
在写入时生成，供基础搜索做一次 LIKE 命中；它是检索冗余列，不是业务字段。

本修订**不含任何数值列**：金额、数量与 `Numeric` 在 SQLite 下的精确存储属于步骤 04。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3f6bd2d5edaa"
down_revision: Union[str, Sequence[str], None] = "73ebb18fcafe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("name_normalized", sa.String(length=80), nullable=False),
        sa.Column("name_zh", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
        sa.UniqueConstraint(
            "name_normalized", name=op.f("uq_categories_name_normalized")
        ),
    )
    op.create_table(
        "food_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("name_normalized", sa.String(length=120), nullable=False),
        sa.Column("name_zh", sa.String(length=120), nullable=True),
        sa.Column("name_zh_normalized", sa.String(length=120), nullable=True),
        sa.Column("specification", sa.String(length=200), nullable=True),
        sa.Column("base_unit", sa.String(length=16), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_food_items_category_id_categories"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_food_items")),
        sa.UniqueConstraint(
            "name_normalized", name=op.f("uq_food_items_name_normalized")
        ),
        sa.UniqueConstraint(
            "name_zh_normalized", name=op.f("uq_food_items_name_zh_normalized")
        ),
    )
    op.create_index(
        op.f("ix_food_items_category_id"), "food_items", ["category_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_food_items_category_id"), table_name="food_items")
    # 先删引用方（food_items）再删被引用方（categories），否则外键拦住。
    op.drop_table("food_items")
    op.drop_table("categories")
