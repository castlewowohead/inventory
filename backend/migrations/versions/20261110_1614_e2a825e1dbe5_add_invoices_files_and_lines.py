"""add invoices files and lines

Revision ID: e2a825e1dbe5
Revises: 78a4af6ad755
Create Date: 2026-11-10 16:14:05.092853

（首行刻意用 ASCII：Alembic 会把它作为修订说明打到控制台，中文在 GBK 控制台下
显示为乱码。中文说明放在首行之后，不影响命令输出。）

步骤 05 的结构：三张新表 + 一列。

1. `invoices`：一张原始单据。单据头上的来源、单号、日期、币种、总额**全部可空**
   ——手写采购单经常一样都没有，强制必填只会逼着录入的人编一个。识别出来的原文另存
   在 `raw_document_fields`（一列 JSON）：`11/08/26` 这种写法解析不成日历日时，
   `invoice_date` 只能留空，但原文不能跟着丢。
2. `invoice_files`：一张单据的若干原始文件。`sha256` 上有唯一约束，这就是
   「完全相同的文件不许重复上传」的落点；`storage_key` 唯一是防同一份字节被两条
   记录指向。
3. `invoice_lines`：单据上的每一行。当前待确认值（数量、单位、单价、金额）**可空**
   ——认不出来的格子就是空的，不会因为识别失败变成 0；原始识别证据存在
   `raw_fields`（一列 JSON）。`(invoice_id, line_no)` 唯一。
4. `purchases.invoice_line_id`：步骤 04 刻意没建的那一列（当时发票表还不存在，
   不预留悬空外键），现在补上，于是任何一条采购记录都能回到它的原始单据。

两处需要说明的写法（与 520863805194 同一套约定）：

- **数值列声明成 `sa.Integer()`、时间列声明成 `sa.DateTime()`，不引用
  `src.models`。** 迁移脚本是结构的历史快照，不该 import 应用模型。`DecimalAsInteger`
  的 impl 就是 Integer、`UtcDateTime` 的 impl 就是 DateTime，生成的 DDL 完全相同。
  （`--autogenerate` 会把它们渲染成 `src.models.DecimalAsInteger()` 并**丢掉 scale
  参数**，必须手工改回 impl 类型。）
- **`uq_purchases_active_invoice_line` 是局部唯一索引**（`sqlite_where`）：一个发票
  行最多只能有一条**未作废**的采购记录。用局部索引而不是普通唯一约束，是因为撤销
  确认把采购记录软删（置 `voided_at`）后那一行还要能重新确认；普通唯一约束会把它
  永远锁死。

`downgrade()` 是无损的：三张表整体删掉，`purchases` 去掉那一列。已确认的采购记录
本身不删——它们的数据（食材、数量、单价、金额）是独立成立的，只是失去了与原始单据
的关联。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e2a825e1dbe5"
down_revision: Union[str, Sequence[str], None] = "78a4af6ad755"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_INVOICE_LINE_INDEX = "uq_purchases_active_invoice_line"
ACTIVE_INVOICE_LINE_WHERE = "invoice_line_id IS NOT NULL AND voided_at IS NULL"


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(length=120), nullable=True),
        sa.Column("source_name_normalized", sa.String(length=120), nullable=True),
        sa.Column("invoice_number", sa.String(length=60), nullable=True),
        sa.Column("invoice_number_normalized", sa.String(length=60), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        # 金额按整数最小单位（分）存，见 src/models.py 的 DecimalAsInteger。
        sa.Column("invoice_total", sa.Integer(), nullable=True),
        sa.Column("recognition_provider", sa.String(length=32), nullable=True),
        sa.Column("recognition_is_fake", sa.Boolean(), nullable=True),
        sa.Column("recognized_at", sa.DateTime(), nullable=True),
        sa.Column("raw_document_fields", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
    )
    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_invoices_invoice_number_normalized"),
            ["invoice_number_normalized"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_invoices_source_name_normalized"),
            ["source_name_normalized"],
            unique=False,
        )

    op.create_table(
        "invoice_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("page_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name=op.f("fk_invoice_files_invoice_id_invoices"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_files")),
        sa.UniqueConstraint("sha256", name=op.f("uq_invoice_files_sha256")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_invoice_files_storage_key")),
    )
    with op.batch_alter_table("invoice_files", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_invoice_files_invoice_id"), ["invoice_id"], unique=False
        )

    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(length=200), nullable=True),
        sa.Column("line_kind", sa.String(length=16), nullable=False),
        sa.Column("food_item_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=300), nullable=True),
        # 数量 4 位、单价 4 位、金额 2 位小数，一律按整数最小单位存。
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("purchase_unit", sa.String(length=32), nullable=True),
        sa.Column("unit_price", sa.Integer(), nullable=True),
        sa.Column("line_amount", sa.Integer(), nullable=True),
        sa.Column("purchase_date", sa.Date(), nullable=True),
        sa.Column("source_name", sa.String(length=120), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("raw_fields", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["food_item_id"],
            ["food_items.id"],
            name=op.f("fk_invoice_lines_food_item_id_food_items"),
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name=op.f("fk_invoice_lines_invoice_id_invoices"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_lines")),
        sa.UniqueConstraint(
            "invoice_id", "line_no", name=op.f("uq_invoice_lines_invoice_id_line_no")
        ),
    )
    with op.batch_alter_table("invoice_lines", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_invoice_lines_food_item_id"), ["food_item_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_invoice_lines_invoice_id"), ["invoice_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_invoice_lines_status"), ["status"], unique=False
        )

    with op.batch_alter_table("purchases", schema=None) as batch_op:
        batch_op.add_column(sa.Column("invoice_line_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_purchases_invoice_line_id"),
            ["invoice_line_id"],
            unique=False,
        )
        batch_op.create_index(
            ACTIVE_INVOICE_LINE_INDEX,
            ["invoice_line_id"],
            unique=True,
            sqlite_where=sa.text(ACTIVE_INVOICE_LINE_WHERE),
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_purchases_invoice_line_id_invoice_lines"),
            "invoice_lines",
            ["invoice_line_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("purchases", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_purchases_invoice_line_id_invoice_lines"), type_="foreignkey"
        )
        batch_op.drop_index(
            ACTIVE_INVOICE_LINE_INDEX, sqlite_where=sa.text(ACTIVE_INVOICE_LINE_WHERE)
        )
        batch_op.drop_index(batch_op.f("ix_purchases_invoice_line_id"))
        batch_op.drop_column("invoice_line_id")

    with op.batch_alter_table("invoice_lines", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invoice_lines_status"))
        batch_op.drop_index(batch_op.f("ix_invoice_lines_invoice_id"))
        batch_op.drop_index(batch_op.f("ix_invoice_lines_food_item_id"))
    op.drop_table("invoice_lines")

    with op.batch_alter_table("invoice_files", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invoice_files_invoice_id"))
    op.drop_table("invoice_files")

    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invoices_source_name_normalized"))
        batch_op.drop_index(batch_op.f("ix_invoices_invoice_number_normalized"))
    op.drop_table("invoices")
