"""seed uncategorized category

Revision ID: d9b4c70e52af
Revises: c8a5f2d61e37
Create Date: 2026-11-14 11:25:00.000000

（首行刻意用 ASCII，原因见基线修订。）

步骤 10.8：再 seed 一条分类 `Uncategorized / 未分类`。

**为什么需要它。** `food_items.category_id` 是 `NOT NULL` 外键（步骤 03 定的，
本修订**不改表结构**）。10.C 要让系统按关键词自动建食材，而分类是词表猜出来的，
猜不出来时必须有一个地方可以落——没有这条分类的话，猜不出就只能报错，于是
「分类认不出」会在发票逐行确认那一屏把入账整个挡住。而那一屏是老板全系统单位
时间成本最高的一屏（认手写字 + 核 15 行数量单价），不该为一个看不出用途的字段
停在那里。

有了它之后，**分类在发票流程里永远不会报错、不会挡住入账**。

⚠️ **刻意新写一条修订，不改 2026-11-10 那条 seed（c6f14af1ab06）。**
迁移是历史快照——改一条已经在别人库里跑过的修订，等于让同一个版本号在不同机器上
代表不同的结构。这是本项目自己的规矩，不是 Alembic 的要求。

⚠️ **判定锚点是 `name_normalized = 'uncategorized'`，不是显示名。**
`imp_notes.md` Feature 1 明确「不强制所有餐厅采用完全相同的分类体系」，老板可以
把它改名成「待归类」「其它」。代码按显示名找这条分类的话，他改个名全系统就乱了；
按规范化名找，他怎么改显示名都不影响。

**可重复执行**：与 c6f14af1ab06 一样，先读出已存在的 `name_normalized`，
只在缺的时候插。

`downgrade()` 按锚点删掉它。⚠️ 若已经有食材挂在这条分类下，外键会让删除失败
——这是刻意的：宁可回退报错，也不悄悄把一批食材的分类指向打断。
"""

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d9b4c70e52af"
down_revision: Union[str, Sequence[str], None] = "c8a5f2d61e37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 与 src/services/matching.py 的 UNCATEGORIZED_NAME_NORMALIZED 必须一致，
# 一致性由 tests/test_migrations.py 断言（迁移不 import 应用代码）。
UNCATEGORIZED_NAME = "Uncategorized"
UNCATEGORIZED_NAME_ZH = "未分类"
UNCATEGORIZED_NORMALIZED = "uncategorized"

categories = sa.table(
    "categories",
    sa.column("name", sa.String),
    sa.column("name_normalized", sa.String),
    sa.column("name_zh", sa.String),
    sa.column("created_at", sa.DateTime),
    sa.column("updated_at", sa.DateTime),
)


def upgrade() -> None:
    connection = op.get_bind()
    exists = connection.scalar(
        sa.select(sa.func.count())
        .select_from(categories)
        .where(categories.c.name_normalized == UNCATEGORIZED_NORMALIZED)
    )
    if exists:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    connection.execute(
        sa.insert(categories),
        [
            {
                "name": UNCATEGORIZED_NAME,
                "name_normalized": UNCATEGORIZED_NORMALIZED,
                "name_zh": UNCATEGORIZED_NAME_ZH,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.delete(categories).where(
            categories.c.name_normalized == UNCATEGORIZED_NORMALIZED
        )
    )
