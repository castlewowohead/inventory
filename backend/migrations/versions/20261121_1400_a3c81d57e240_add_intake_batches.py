"""add intake batches, files, items and classification candidates

Revision ID: a3c81d57e240
Revises: f31a7e9c4b20
Create Date: 2026-11-21 14:00:00.000000

多文件计划 M02 加五张表，解一个现有结构表达不了的事实：**一批混合文件要按「实际
采购日 + 商家」整理，而一份 PDF 的不同页、一份表格的不同行可以进入不同的组**
（docs/multifiles.md 第 1、5 节与 M02）。

1. `intake_batches`：一次混合上传的整理批次。它是工作队列，不是一张发票——同一个
   批次里可能有好几张真正的发票，总额、重复提示与收口仍然各自处理。状态**派生**
   （`closed_at` / `discarded_at`），不存 status 列。
2. `intake_files`：原件字节，按 `sha256` 内容寻址**一行一份**。「原件字节只存一次」
   就落在这张表的两条唯一约束上。
3. `intake_batch_files`：某个批次对某份原件的引用 + 处理状态。
   `(batch_id, intake_file_id)` 唯一 = 同一批次里同一份字节只能进来一次；
   `status` 覆盖「等处理 / 已切分 / 解析失败 / 已回收」四种明确状态。
4. `intake_items`：可归类内容（PDF 的一页、一张图片、表格的一行），带回看原件的
   坐标与**人工决定的**日期/商家，以及乐观锁 `version`。
5. `intake_item_candidates`：系统建议的日期/商家候选与依据。与 4 分开存，是
   「系统建议／人工确认必须可区分」这条要求在结构上的落点。

⚠️ **本修订一个字都没改 `invoices` 与 `invoice_files`。** `invoice_files.sha256`
全局唯一是步骤 05「完全相同的文件不许重复上传」的落点，`invoice_id` 非空意味着
一份文件只属于一张单据；放宽它们会让旧单张上传路径的重复拦截失效。整理批次因此
另立一套表，旧发票与旧导入批次迁移后照常回读。被否决的几种改法见
docs/impl_record.md 的 M02 记录。

⚠️ **日期/商家组没有表。** 组是 `(decided_purchase_date,
decided_source_name_normalized)` 的分组结果，由内容上的决定值算出来。另存一张组表
就会出现两份能互相矛盾的真相（与「单据状态由行状态派生」、「标准化价格不落列」
同一条）。

沿用既有约定（同 0e45ee78ef8c / e2a825e1dbe5）：

- **不 import `src/models.py`**：迁移是结构的历史快照。时间列写 `sa.DateTime()`
  而不是 `UtcDateTime`，置信度写 `sa.Integer()` 而不是 `DecimalAsInteger(4)`，
  两者生成的 DDL 完全相同。
- **约束一律显式命名**，SQLite 要靠批处理模式重建表才能改约束，重建时必须能按
  名字引用既有约束。
- **`intake_items` 的两条「一页一个内容」唯一索引是局部索引**（`sqlite_where`）：
  页内容的 `row_no` 是 NULL、行内容的 `page_no` 是 NULL，而 SQLite 的唯一索引把
  多个 NULL 视为互不相同，一条普通 UniqueConstraint 根本拦不住重复。手法与
  `uq_pack_specs_*`、`uq_purchases_active_invoice_line` 相同。
- **不给任何列加 `server_default`**：模型只有 Python 侧 default，而本项目开了
  `compare_server_default=True`，留下来 `test_models_match_migrations` 会失败。

`downgrade()` 整体删掉五张表。它们只存**整理过程**：原始字节另有 `FILE_STORAGE_DIR`
里的文件，已确认的采购记录、发票与草稿行都在别的表里，所以回退不会丢单据，代价是
「这一批整理到哪一步了」要重做一次。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3c81d57e240"
down_revision: Union[str, Sequence[str], None] = "f31a7e9c4b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 局部唯一索引的条件。upgrade 与 downgrade 共用同一份文本，避免两边写得不一样
# 导致 drop 不掉。
PAGE_ITEM_WHERE = "kind = 'page'"
ROW_ITEM_WHERE = "kind = 'row'"


def upgrade() -> None:
    op.create_table(
        "intake_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        # 状态派生：整理收口与整批作废各一个时刻，不另存 status 列。
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("discarded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_batches")),
    )
    op.create_table(
        "intake_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        # 「原件字节只存一次」就是这一条唯一约束。同一份 PDF 出现在两个批次里
        # = 一行 intake_files + 两行 intake_batch_files，磁盘上只有一份。
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        # 没有 updated_at：字节只新增与删除，不修改（同 invoice_files）。
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_files")),
        sa.UniqueConstraint("sha256", name=op.f("uq_intake_files_sha256")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_intake_files_storage_key")),
    )
    op.create_table(
        "intake_batch_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("intake_file_id", sa.Integer(), nullable=False),
        # 上传时的文件名：同一份字节在不同批次里可以叫不同的名字，所以它在引用上，
        # 不在 intake_files 上。
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("upload_order", sa.Integer(), nullable=False),
        # pending 等处理 / ready 已切分 / failed 解析失败 / discarded 已回收。
        sa.Column("status", sa.String(length=16), nullable=False),
        # 切出多少个可归类内容。PDF 按页、图片一张算一个、表格按行，同一个计数。
        sa.Column("item_count", sa.Integer(), nullable=True),
        # 只有 XLSX 有值：现有导入一次只读一张工作表。
        sa.Column("sheet_name", sa.String(length=120), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.String(length=600), nullable=True),
        sa.Column("discarded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'ready', 'failed', 'discarded')",
            name=op.f("ck_intake_batch_files_status"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["intake_batches.id"],
            name=op.f("fk_intake_batch_files_batch_id_intake_batches"),
        ),
        sa.ForeignKeyConstraint(
            ["intake_file_id"],
            ["intake_files.id"],
            name=op.f("fk_intake_batch_files_intake_file_id_intake_files"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_batch_files")),
        # 同一批次里同一份字节只能进来一次：一次拖进来的十几个文件里有两份是同一张
        # 照片时，第二份撞在这里，而不是在整理页上出现两张一样的卡片。
        sa.UniqueConstraint(
            "batch_id",
            "intake_file_id",
            name=op.f("uq_intake_batch_files_batch_id_intake_file_id"),
        ),
    )
    with op.batch_alter_table("intake_batch_files", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_intake_batch_files_batch_id"), ["batch_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_intake_batch_files_intake_file_id"),
            ["intake_file_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_intake_batch_files_status"), ["status"], unique=False
        )

    op.create_table(
        "intake_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_file_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        # 回看原件的坐标：页内容记 1 起的页号，表格行记原表格行号。
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("row_no", sa.Integer(), nullable=True),
        # 人工决定的分类值，也是日期/商家组的唯一来源。系统建议不在这几列里。
        sa.Column("decided_purchase_date", sa.Date(), nullable=True),
        sa.Column("decided_source_name", sa.String(length=120), nullable=True),
        sa.Column(
            "decided_source_name_normalized", sa.String(length=120), nullable=True
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        # 乐观锁：批量移动时两个会话同时改同一张卡片，后一个被拦住而不是悄悄覆盖。
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind in ('page', 'row')", name=op.f("ck_intake_items_kind")),
        # 两种内容的坐标不许混填，否则「回看原件」会拿到一个解释不了的位置。
        sa.CheckConstraint(
            "(kind = 'page' and page_no is not null and row_no is null)"
            " or (kind = 'row' and row_no is not null and page_no is null)",
            name=op.f("ck_intake_items_source_ref"),
        ),
        # 展示名与规范化名同时有或同时无：只存一个的话，要么分组键缺失，要么界面上
        # 出现一个没人认得的全小写名字。
        sa.CheckConstraint(
            "(decided_source_name is null"
            " and decided_source_name_normalized is null)"
            " or (decided_source_name is not null"
            " and decided_source_name_normalized is not null)",
            name=op.f("ck_intake_items_source_name_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_file_id"],
            ["intake_batch_files.id"],
            name=op.f("fk_intake_items_batch_file_id_intake_batch_files"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_items")),
    )
    with op.batch_alter_table("intake_items", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_intake_items_batch_file_id"), ["batch_file_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_intake_items_decided_source_name_normalized"),
            ["decided_source_name_normalized"],
            unique=False,
        )
        # 一页一个内容、一行一个内容。局部索引的理由见模块文档。
        batch_op.create_index(
            "uq_intake_items_page",
            ["batch_file_id", "page_no"],
            unique=True,
            sqlite_where=sa.text(PAGE_ITEM_WHERE),
        )
        batch_op.create_index(
            "uq_intake_items_row",
            ["batch_file_id", "row_no"],
            unique=True,
            sqlite_where=sa.text(ROW_ITEM_WHERE),
        )

    op.create_table(
        "intake_item_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=120), nullable=False),
        sa.Column("value_normalized", sa.String(length=120), nullable=True),
        # 依据（纸面读出 / 同文件参考 / 历史写法 / 版式相似）**刻意没有 CHECK**：
        # 这个词表由 M05、M07 的建议逻辑扩展，加一个依据不该要求重建一张表。
        # 取值由 services/intake.py 校验。
        sa.Column("basis", sa.String(length=24), nullable=False),
        # 置信度按整数最小单位存（scale 4），理由同金额与数量：SQLite 下的 Numeric
        # 会落成浮点。模型给的是 0—1 的小数。
        sa.Column("confidence", sa.Integer(), nullable=True),
        # 一列 JSON：原文、页码、可得坐标、依据说明、哪个模型给的。只被原样读出来
        # 给人看，不参与查询与统计（同 invoice_lines.raw_fields）。
        sa.Column("evidence", sa.Text(), nullable=True),
        # 没有 updated_at：候选是重算后整组替换的，不原地修改。
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "field in ('purchase_date', 'source_name')",
            name=op.f("ck_intake_item_candidates_field"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["intake_items.id"],
            name=op.f("fk_intake_item_candidates_item_id_intake_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_item_candidates")),
    )
    with op.batch_alter_table("intake_item_candidates", schema=None) as batch_op:
        # 读取路径一律是「这个内容这个维度的候选」；item_id 在最左，顺带替掉单列
        # 外键索引。
        batch_op.create_index(
            "ix_intake_item_candidates_item_field", ["item_id", "field"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("intake_item_candidates", schema=None) as batch_op:
        batch_op.drop_index("ix_intake_item_candidates_item_field")
    op.drop_table("intake_item_candidates")

    with op.batch_alter_table("intake_items", schema=None) as batch_op:
        batch_op.drop_index("uq_intake_items_row", sqlite_where=sa.text(ROW_ITEM_WHERE))
        batch_op.drop_index(
            "uq_intake_items_page", sqlite_where=sa.text(PAGE_ITEM_WHERE)
        )
        batch_op.drop_index(
            batch_op.f("ix_intake_items_decided_source_name_normalized")
        )
        batch_op.drop_index(batch_op.f("ix_intake_items_batch_file_id"))
    op.drop_table("intake_items")

    with op.batch_alter_table("intake_batch_files", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_intake_batch_files_status"))
        batch_op.drop_index(batch_op.f("ix_intake_batch_files_intake_file_id"))
        batch_op.drop_index(batch_op.f("ix_intake_batch_files_batch_id"))
    op.drop_table("intake_batch_files")

    op.drop_table("intake_files")
    op.drop_table("intake_batches")
