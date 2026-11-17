"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

约定：
- upgrade() 与 downgrade() 成对实现；downgrade 写不出来时在此说明原因，不要留空壳。
- 修改既有表的约束走 `with op.batch_alter_table(...)`：SQLite 不支持大多数 ALTER。
- 迁移只改结构，不塞业务数据；需要初始数据时单独说明来源与可重复执行方式。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
