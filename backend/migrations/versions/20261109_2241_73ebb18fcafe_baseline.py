"""baseline: chain root, creates no business tables

Revision ID: 73ebb18fcafe
Revises:
Create Date: 2026-11-09 22:41:58.884732

（首行刻意用 ASCII：Alembic 会把它作为修订说明打到控制台，中文在 GBK 控制台下
显示为乱码。本项目的中文说明放在首行之后，不影响命令输出。）

本修订**有意不建任何业务表**。

步骤 02 只负责建立迁移通路；业务表随对应 Feature 步骤增加，不一次设计全部表
（见 docs/stru_dev.md）：分类/标准食材/别名在步骤 03，采购记录在步骤 04，
发票与行项目草稿在步骤 05。现在凭想象建表，到了那几步必然要改名、改类型、
改约束，在 SQLite 下每次都要批处理重建表，徒增迁移历史。

它仍然是必要的一个修订，不是占位文件：

- 它是修订链的根（`down_revision = None`），后续修订都挂在它之后；
- 执行它会让 Alembic 在目标库里建出 `alembic_version` 表并写入版本号，
  于是「这个数据库处于哪个结构版本」从此有记录，而不是靠猜；
- 因此 `python -m alembic upgrade head` 在全新的空数据库上可以真正跑完，
  这一点由 tests/test_migrations.py 在临时数据库上验证。

downgrade() 同样为空，且与 upgrade() 对称：回退到 base 只需移除版本记录，
该动作由 Alembic 自己完成。
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "73ebb18fcafe"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """不建表：原因见模块文档。"""


def downgrade() -> None:
    """与 upgrade() 对称，无需操作。"""
