"""allow negative adjustment lines

Revision ID: 78a4af6ad755
Revises: 520863805194
Create Date: 2026-11-10 15:18:02.113427

（首行刻意用 ASCII：Alembic 会把它作为修订说明打到控制台，中文在 GBK 控制台下
显示为乱码。中文说明放在首行之后，不影响命令输出。）

让采购记录能表达**退货 / 折让 / 其它调整行**（金额可以为负）。两处结构变化：

1. 新增 `line_kind`（`purchase` / `adjustment`）。调整行留在库里是为了对账，但默认
   不参与食材的采购价格统计。存成列而不是靠「金额是不是负的」推断：步骤 09 的每个
   统计查询都要用到它，靠符号推断意味着漏写一处就静默污染一个均价。
2. `food_item_id` 由 NOT NULL 改为可空。整单折让（「本单让 $20」）在单据上不指向
   任何一种食材，强行挂到某个食材上会污染那个食材的采购历史。**正常采购行仍然必须
   有食材**，这条规则移到 `services/purchases.py` 校验；数据库层面放宽只是为了能
   表达整单级调整。

三处需要说明的写法：

- **`line_kind` 用 `server_default` 回填既有行，随后在第二个批处理块里把这个默认值
  去掉。** 既有行（本机开发库当时有 15 条）都是正常采购，回填成 `purchase` 是正确的；
  但留着 server_default 会与模型（只有 Python 侧 `default`）产生漂移，而本项目的
  `migrations/env.py` 开了 `compare_server_default=True`，`test_models_match_migrations`
  会直接失败。
- **改既有列的可空性必须走 `batch_alter_table`**：SQLite 不支持这类 `ALTER`，批处理
  模式会「建新表—搬数据—换名」，而重建依赖约束有稳定名字（命名约定见 `src/models.py`）。
- `downgrade()` 里先把调整行删掉再把 `food_item_id` 收回 NOT NULL。这是**有损的**：
  调整行在旧结构下无法表达（它们可能没有食材，金额也可能为负），留着会让收紧约束
  直接失败。回退会丢数据这件事写在这里，而不是让它在运行时才暴露。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "78a4af6ad755"
down_revision: Union[str, Sequence[str], None] = "520863805194"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LINE_KIND_PURCHASE = "purchase"
LINE_KIND_ADJUSTMENT = "adjustment"


def upgrade() -> None:
    # 第一个批处理块：加列（带 server_default 回填既有行）+ 放宽 food_item_id。
    with op.batch_alter_table("purchases", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "line_kind",
                sa.String(length=16),
                nullable=False,
                server_default=LINE_KIND_PURCHASE,
            )
        )
        batch_op.alter_column(
            "food_item_id", existing_type=sa.INTEGER(), nullable=True
        )

    # 第二个批处理块：回填已完成，去掉 server_default，与模型保持一致。
    with op.batch_alter_table("purchases", schema=None) as batch_op:
        batch_op.alter_column(
            "line_kind", existing_type=sa.String(length=16), server_default=None
        )

    op.create_index(
        op.f("ix_purchases_line_kind"), "purchases", ["line_kind"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_purchases_line_kind"), table_name="purchases")

    # 有损回退：调整行在旧结构下表达不出来（可能没有食材、金额可能为负），
    # 不先删掉的话下面收紧 NOT NULL 会失败。见本文件开头的说明。
    purchases = sa.table(
        "purchases",
        sa.column("line_kind", sa.String),
    )
    op.execute(
        purchases.delete().where(purchases.c.line_kind == LINE_KIND_ADJUSTMENT)
    )

    with op.batch_alter_table("purchases", schema=None) as batch_op:
        batch_op.alter_column(
            "food_item_id", existing_type=sa.INTEGER(), nullable=False
        )
        batch_op.drop_column("line_kind")
