"""商家记忆与版式指纹：一张别名表 + `intake_items` 两列（intake M07）。

本次迁移**一张新表 + 两个可空列**，既有列、既有表一个字都没改。

### 为什么只有别名落成一张表

M07 要的三样「记忆」里，只有一样推不出来：

- **别名映射**（`intake_source_aliases`）：「纸面上这种写法以前被确认成哪一家」
  是人的决定，而且 docs/multifiles.md 第 5 节明确要求它是**独立、可更正**的映射。
  能从历史内容里猜出候选，但猜出来的东西删不掉——一条错别名会在之后每一批里
  不停建议错的那一家。
- **「这个规范化键现在叫什么」**：不落表。它是最近一次人工确认的写法聚合出来的
  派生值，与「日期/商家组不落表」同一条。
- **「这个版式属于哪一家」**：不落表。由 `intake_items.layout_fingerprint`
  加那一页上的人工确认派生；要更正就改那一页的确认，不会出现「版式表说是 A、
  页上确认的是 B」两份打架的真相。

### 为什么指纹要落两列

`documents/layout.py` 量一页约 0.3—0.6 秒（2479×3230 的扫描页实测），而「历史上
有哪些版式」要跨批次查几百页。字节按 sha256 寻址、永不修改，所以这份测量不会
过期——与被否决的「标准化价格落列」不同：那一列会随箱规变化悄悄过期，这一列的
输入是不可变的字节。

⚠️ 两列都**不加 CHECK**，`intake_items` 上有两条带 WHERE 的局部唯一索引
（`uq_intake_items_page` / `uq_intake_items_row`），重建整张表的代价远大于一条
CHECK（与 `recognition_status` 同一条）。

Revision ID: f2a9c35db714
Revises: e8b31f902a7d
"""

from alembic import op
import sqlalchemy as sa


revision = "f2a9c35db714"
down_revision = "e8b31f902a7d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_source_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        # 一种写法只能指向一家：否则建议层要在两个答案之间猜。改指向就更新这一行，
        # 撤销就删掉它——这就是「可更正」。
        sa.Column("alias_key", sa.String(length=120), nullable=False),
        sa.Column("alias_text", sa.String(length=120), nullable=False),
        sa.Column("source_key", sa.String(length=120), nullable=False),
        # 记下这条别名那一刻的展示写法，供核对；**不是**「这一家现在叫什么」的来源
        # （那是派生值，见本文件开头）。
        sa.Column("source_name", sa.String(length=120), nullable=False),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        # 指向自己的别名没有信息量，却会让「这个写法是别名」永远为真。
        sa.CheckConstraint("alias_key <> source_key", name="alias_not_self"),
        sa.UniqueConstraint("alias_key", name="uq_intake_source_aliases_alias_key"),
    )
    op.create_index(
        "ix_intake_source_aliases_source_key",
        "intake_source_aliases",
        ["source_key"],
    )

    with op.batch_alter_table("intake_items") as batch_op:
        # 人看得懂的特征拼接，不是哈希：堆的依据要能直接读给人看。
        batch_op.add_column(
            sa.Column("layout_fingerprint", sa.String(length=200), nullable=True)
        )
        # 逐项特征与量不出来时的原因。NULL 的 fingerprint + 这里的 problem 才能把
        # 「还没量过」与「量不出来」分开。
        batch_op.add_column(sa.Column("layout_features", sa.Text(), nullable=True))
        # 相似堆的查询就是「同一个指纹的内容」，所以这一列要有索引。
        batch_op.create_index(
            batch_op.f("ix_intake_items_layout_fingerprint"),
            ["layout_fingerprint"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("intake_items") as batch_op:
        batch_op.drop_index(batch_op.f("ix_intake_items_layout_fingerprint"))
        batch_op.drop_column("layout_features")
        batch_op.drop_column("layout_fingerprint")
    op.drop_index(
        "ix_intake_source_aliases_source_key", table_name="intake_source_aliases"
    )
    op.drop_table("intake_source_aliases")
