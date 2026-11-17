"""add food item needs review

Revision ID: c8a5f2d61e37
Revises: b7e13a4d9c22
Create Date: 2026-11-14 11:20:00.000000

步骤 10.13：给 `food_items` 加一列 `needs_review`（布尔，默认 `false`）。

**它标的是「这条食材是系统按关键词自动建出来的，还没人确认过」**（10.C）。
自动建出来的那一条名字是从供应商描述里剥的、分类是词表猜的——两样都可能不对，
所以要在界面上标出来，而不是混在老板自己建的食材里看不出区别。

⚠️ **为什么不能留在前端内存里**：10.D 把新建食材从「确认那一刻才落库」改成
「填完即落库」之后，这个标记必须跟着那条食材一起活下来——否则刷新一下浏览器，
「待确认」就没了，而那条猜出来的分类还在。一条迁移一列，比在前端维护一份影子状态
便宜得多。

清除时机由 `services/` 决定（老板改过它、或用它确认过任意一行），不写进约束：
那是业务判断，会随流程调整，而列只负责存下来。

**为什么要两次 `batch_alter_table`**：SQLite 给已有表加 `NOT NULL` 列时必须带
`DEFAULT`，否则已有行没有值可填。但本项目的模型层不声明 `server_default`
（默认值由 `src/models.py` 的 Python 端 `default=False` 给），而
`migrations/env.py` 开着 `compare_server_default=True`——库里留着一个模型没声明的
默认值，`tests/test_migrations.py::test_models_match_migrations` 会判成漂移。
所以第一次加列带默认值把已有行填上，第二次把默认值去掉。批处理模式本来就是
「建新表—搬数据—换名字」，第二次重建不额外增加风险。

外键在迁移期间由 `migrations/env.py` 统一关闭并在结束后用 `PRAGMA
foreign_key_check` 核对（步骤 04.11 定下的约定），本修订不自己动 PRAGMA。

`downgrade()` 删掉这一列。丢的是「哪些食材是自动建的、还没人确认」这份标记，
食材本身与它们的采购记录不受影响。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c8a5f2d61e37"
down_revision: Union[str, Sequence[str], None] = "b7e13a4d9c22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("food_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "needs_review",
                sa.Boolean(),
                nullable=False,
                # 已有食材都是老板自己建的，所以是 false——不是"还没来得及确认"，
                # 是"本来就不需要确认"。
                server_default=sa.text("0"),
            )
        )
    with op.batch_alter_table("food_items", schema=None) as batch_op:
        batch_op.alter_column("needs_review", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("food_items", schema=None) as batch_op:
        batch_op.drop_column("needs_review")
