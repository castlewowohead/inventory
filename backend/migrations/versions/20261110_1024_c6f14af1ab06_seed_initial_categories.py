"""seed initial categories

Revision ID: c6f14af1ab06
Revises: 3f6bd2d5edaa
Create Date: 2026-11-10 10:24:04.000000

（首行刻意用 ASCII，原因见基线修订。）

写入 7 个常见初始分类，和建表分成两条修订：结构修订保持只改结构，初始数据单独一条，
按 `migrations/script.py.mako` 的约定在此说明来源与可重复执行方式。

- **来源**：`docs/imp_notes.md` Feature 1 列出的常见分类
  （Meat & Poultry、Seafood、Vegetables、Dairy & Eggs、Dry Goods、Frozen Food、
  Oils & Condiments）。中文名由本项目补充，方便中文录入时辨认。
  这些只是**初始值**：餐厅可以新增、改名，也可以删掉没在用的分类
  （`docs/imp_notes.md`：不强制所有餐厅采用完全相同的分类体系）。
- **可重复执行**：`upgrade()` 先读出已存在的 `name_normalized`，只插缺的那几条，
  因此在"结构已建、部分分类已有"的库上重复执行也不会报唯一约束错误。
- **时间列**：用 Python 算出当前 UTC 时间再写入，不使用 SQLite 的
  `CURRENT_TIMESTAMP`：后者写的是不带时区信息的本地/UTC 混淆值，而本项目的
  `UtcDateTime` 约定列里存的是 naive UTC（见 src/models.py）。
- **downgrade()**：按名称删掉这 7 条。若某条已被食材引用，外键会让删除失败——
  这是刻意的：宁可回退报错，也不悄悄删掉正在使用的分类或连带删掉食材。
"""

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c6f14af1ab06"
down_revision: Union[str, Sequence[str], None] = "3f6bd2d5edaa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (英文名, 中文名)。`name_normalized` 由下面按同一规则算出：NFKC + 折叠空白 + casefold，
# 与 src/services/ingredients.py 的 normalize_name() 一致。这里不 import 应用代码
# （迁移是历史快照），但这 7 个名字都是 ASCII，两种实现的结果一定相同；
# 一致性由 tests/test_ingredients_service.py 中的用例断言。
INITIAL_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Meat & Poultry", "肉类与禽类"),
    ("Seafood", "海鲜"),
    ("Vegetables", "蔬菜"),
    ("Dairy & Eggs", "乳制品与蛋类"),
    ("Dry Goods", "干货"),
    ("Frozen Food", "冷冻食品"),
    ("Oils & Condiments", "油类与调味品"),
)

# 只声明本修订要用到的列，不引用应用模型：迁移必须在模型以后变化时仍能执行。
categories = sa.table(
    "categories",
    sa.column("name", sa.String),
    sa.column("name_normalized", sa.String),
    sa.column("name_zh", sa.String),
    sa.column("created_at", sa.DateTime),
    sa.column("updated_at", sa.DateTime),
)


def _normalized(name: str) -> str:
    return " ".join(name.split()).casefold()


def upgrade() -> None:
    connection = op.get_bind()
    existing = set(
        connection.scalars(sa.select(categories.c.name_normalized)).all()
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        {
            "name": name,
            "name_normalized": _normalized(name),
            "name_zh": name_zh,
            "created_at": now,
            "updated_at": now,
        }
        for name, name_zh in INITIAL_CATEGORIES
        if _normalized(name) not in existing
    ]
    if rows:
        connection.execute(sa.insert(categories), rows)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.delete(categories).where(
            categories.c.name_normalized.in_(
                [_normalized(name) for name, _ in INITIAL_CATEGORIES]
            )
        )
    )
