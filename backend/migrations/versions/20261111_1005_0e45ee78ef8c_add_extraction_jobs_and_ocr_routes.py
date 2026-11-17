"""add extraction jobs and ocr routes

Revision ID: 0e45ee78ef8c
Revises: 6f0c81da2306
Create Date: 2026-11-11 10:05:22.931332

步骤 08 加两张表：

1. `extraction_jobs`：一次提取任务的状态（08.5「提取状态必须持久化」）。手写件
   一页约 15 秒、多页单据更久，提取不挂在 HTTP 请求里（08.3g），所以「提取到哪一
   步了」必须在进程重启与浏览器关掉之后仍然查得到。失败也留下来——`error_code` /
   `error_message` 记下超时、限流、凭据失效，而原始文件与已有草稿一概不动。
2. `ocr_routes`：规范化来源名 → 走哪条提取路径（08.3f「按供应商路由分流」）。
   ⚠️ **这不是供应商表**：步骤 05 刻意没建供应商主表，这里也只有一个映射，
   没有联系人、账期、商品目录。

两处沿用既有约定（同 e2a825e1dbe5 / 520863805194）：

- **时间列声明成 `sa.DateTime()`，不引用 `src.models`。** 迁移脚本是结构的历史
  快照，不该 import 应用模型。`UtcDateTime` 的 impl 就是 DateTime，生成的 DDL
  完全相同。（`--autogenerate` 会渲染成 `src.models.UtcDateTime()` 并留下一个
  未导入的名字，必须手工改回 impl 类型。）
- **约束一律显式命名**，与基线修订起定下的命名规则一致——SQLite 要靠批处理模式
  重建表才能改约束，而重建时必须能按名字引用既有约束。

`downgrade()` 是无损的：两张表整体删掉。它们只存**提取过程**与**路由偏好**，
不存任何业务事实——采购记录、发票、草稿行都在别的表里，删掉这两张表不会丢单据。
代价只是「这家供应商走哪条路」要重新记一次。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0e45ee78ef8c"
down_revision: Union[str, Sequence[str], None] = "6f0c81da2306"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ocr_routes",
        sa.Column("id", sa.Integer(), nullable=False),
        # 键与 invoices / purchases 上的 source_name_normalized 同一个口径
        # （Costco 与 costco 是同一家），长度也跟着那边。
        sa.Column("source_name_normalized", sa.String(length=120), nullable=False),
        sa.Column("source_name", sa.String(length=120), nullable=True),
        sa.Column("doc_class", sa.String(length=24), nullable=False),
        sa.Column("decided_from", sa.String(length=32), nullable=False),
        # 日/月顺序：`6/11/2026` 文本本身定不死，默认拒绝解析。这一列让这件事
        # 变成"每家供应商说一次"；留空就继续拒绝——宁可留空让人填，也不猜。
        sa.Column("date_order", sa.String(length=8), nullable=True),
        sa.Column("note", sa.String(length=600), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ocr_routes")),
        sa.UniqueConstraint(
            "source_name_normalized",
            name=op.f("uq_ocr_routes_source_name_normalized"),
        ),
    )
    op.create_table(
        "extraction_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        # 一张单据挂多个文件时每个文件一个任务：某一张拍坏了只影响它自己那一条。
        sa.Column("invoice_file_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("doc_class", sa.String(length=24), nullable=True),
        sa.Column("route_source", sa.String(length=32), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("models", sa.Text(), nullable=True),
        # 提示词的身份证（sha256 前 12 位）。步骤 07 的选型结论绑定在它上面，
        # 这一列让事后能查出"这张单是用哪个被测配置提取的"。
        sa.Column("prompt_sha12", sa.String(length=16), nullable=True),
        sa.Column("pages_total", sa.Integer(), nullable=True),
        sa.Column("pages_done", sa.Integer(), nullable=False),
        sa.Column("lines_created", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.String(length=600), nullable=True),
        sa.Column("usage", sa.Text(), nullable=True),
        sa.Column("warnings", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_file_id"],
            ["invoice_files.id"],
            name=op.f("fk_extraction_jobs_invoice_file_id_invoice_files"),
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name=op.f("fk_extraction_jobs_invoice_id_invoices"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_jobs")),
    )
    with op.batch_alter_table("extraction_jobs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_extraction_jobs_invoice_file_id"),
            ["invoice_file_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_extraction_jobs_invoice_id"), ["invoice_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_extraction_jobs_status"), ["status"], unique=False
        )
        # worker 取任务按「状态 + 创建时间」扫，先进先出；查"这张单还有没有在跑的
        # 提取"也走它。
        batch_op.create_index(
            "ix_extraction_jobs_status_created", ["status", "created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("extraction_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_extraction_jobs_status_created")
        batch_op.drop_index(batch_op.f("ix_extraction_jobs_status"))
        batch_op.drop_index(batch_op.f("ix_extraction_jobs_invoice_id"))
        batch_op.drop_index(batch_op.f("ix_extraction_jobs_invoice_file_id"))

    op.drop_table("extraction_jobs")
    op.drop_table("ocr_routes")
