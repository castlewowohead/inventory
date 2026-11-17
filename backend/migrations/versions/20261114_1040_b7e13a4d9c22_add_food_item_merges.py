"""add food item merges

Revision ID: b7e13a4d9c22
Revises: a1c7d4e90b52
Create Date: 2026-11-14 10:40:00.000000

步骤 10.3 加一张表 `food_item_merges`：重复食材合并的日志。

**为什么合并需要一张表，而不是改完外键就完事。** 合并把被并掉那条食材的采购记录、
发票草稿行与箱规全部重新指向目标食材，然后删掉它。采购记录与发票行一条都不删、
`original_description` 一个字都不改，所以「供应商当初那句原话」仍然在——10.3 要求的
「采购历史与发票引用保持可追溯」靠这个成立。

**但「别名」不靠这个成立**：被并掉那条食材自己的**名字**会随它一起消失，而那个名字
正是老板当初建它时用的叫法。下次他再按那个叫法搜就什么都搜不到，然后再建一条重复的
——正好回到这个功能要解决的问题。所以这张表存的是「**这个名字现在归谁了**」，
`services/suggestions.py` 搜索时会查它（10.6 的「保存过的纠正能够辅助下一次匹配」）。

⚠️ **本轮仍然没有别名表**（步骤 03 的决定不变）。这张表记的是**发生过的合并**，
不是「这条食材有哪些叫法」；它只在合并真的发生时才长一行，没人能往里手工添叫法。

两处刻意的取舍：

- **`merged_food_item_id` 没有外键**。那一行在同一个事务里被删掉了，建外键等于
  要求它还在。它只是追溯用的编号，不参与关联查询。
- **`name_normalized` 不唯一**。合并之后名字就释放了，老板可以再建一条同名食材、
  再合并一次；那是两次独立的事实，各记一行。做成唯一索引会让第二次合并直接失败。

沿用既有约定（同 a1c7d4e90b52）：时间列声明成 `sa.DateTime()` 而不 import
`src.models`（迁移是结构的历史快照，`UtcDateTime` 的 impl 就是 DateTime，DDL 相同）；
约束与索引一律显式命名。

`downgrade()` 删掉这张表。⚠️ **它会丢业务事实**：已经发生过的合并本身不会被撤销
（采购记录与发票行仍然指向合并后的食材），丢掉的是「那几个旧叫法归谁」这份记忆，
于是按旧叫法搜索又会一无所获。合并**不可逆**这件事与本迁移无关——被并掉的食材行
在合并那一刻就删掉了，回退这条迁移不会把它找回来。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7e13a4d9c22"
down_revision: Union[str, Sequence[str], None] = "a1c7d4e90b52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "food_item_merges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_food_item_id", sa.Integer(), nullable=False),
        # 刻意没有外键：这一行在合并的同一个事务里已经被删掉了。
        sa.Column("merged_food_item_id", sa.Integer(), nullable=False),
        sa.Column("merged_at", sa.DateTime(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("name_normalized", sa.String(length=120), nullable=False),
        sa.Column("name_zh", sa.String(length=120), nullable=True),
        sa.Column("name_zh_normalized", sa.String(length=120), nullable=True),
        sa.Column("specification", sa.String(length=200), nullable=True),
        sa.Column("base_unit", sa.String(length=16), nullable=False),
        sa.Column("moved_purchase_count", sa.Integer(), nullable=False),
        sa.Column("moved_invoice_line_count", sa.Integer(), nullable=False),
        sa.Column("moved_pack_spec_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_food_item_id"],
            ["food_items.id"],
            name=op.f("fk_food_item_merges_target_food_item_id_food_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_food_item_merges")),
    )
    op.create_index(
        op.f("ix_food_item_merges_target_food_item_id"),
        "food_item_merges",
        ["target_food_item_id"],
    )
    # 两个检索列：按旧叫法搜索时要走索引，不是全表扫。**都不是唯一索引**，理由见
    # 本修订的说明。
    op.create_index(
        op.f("ix_food_item_merges_name_normalized"),
        "food_item_merges",
        ["name_normalized"],
    )
    op.create_index(
        op.f("ix_food_item_merges_name_zh_normalized"),
        "food_item_merges",
        ["name_zh_normalized"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_food_item_merges_name_zh_normalized"), table_name="food_item_merges"
    )
    op.drop_index(
        op.f("ix_food_item_merges_name_normalized"), table_name="food_item_merges"
    )
    op.drop_index(
        op.f("ix_food_item_merges_target_food_item_id"), table_name="food_item_merges"
    )
    op.drop_table("food_item_merges")
