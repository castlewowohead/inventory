"""Persist per-item recognition status, raw readings and provenance (intake M05).

本次迁移**只给 `intake_items` 加五列，不改任何既有列、不加表**。

为什么是列而不是一张「识别任务表」：`intake_batch_files.status` 在 M03 已经定下
「状态本身就是队列、没有第二张任务表」那条口径，理由是另立一张任务表会出现
「任务表说跑完了、内容还没候选」两份能互相矛盾的真相。逐内容的识别同样如此，
而且它与 `extraction_jobs` 的差别也一样是**代价**：那边有 `running` 中间态、
重跑要再花一次钱，所以需要独立任务表与「卡住任务退回排队」；这里重跑的闸门就是
`recognition_status`——`done` 的内容不再送模型，这恰恰是「不对同一页重复收费」。

⚠️ `recognition_status` **不加 CHECK 约束**：SQLite 改 CHECK 必须批处理重建整张
表，而 `intake_items` 上有两条带 WHERE 的局部唯一索引（`uq_intake_items_page` /
`uq_intake_items_row`），重建时把 WHERE 丢掉会让「一页只能有一个内容」失效——
代价远大于一条 CHECK。取值由 `services/intake.py` 校验。

Revision ID: c47be2a91f85
Revises: b91e6a74d320
"""

from alembic import op
import sqlalchemy as sa


revision = "c47be2a91f85"
down_revision = "b91e6a74d320"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intake_items") as batch_op:
        # NULL = 还没排到它；pending / done / failed 见 models.py 的常量。
        batch_op.add_column(
            sa.Column("recognition_status", sa.String(length=16), nullable=True)
        )
        batch_op.add_column(sa.Column("recognized_at", sa.DateTime(), nullable=True))
        # 读出来的原文、出处、provider / 模型 / 提示词版本、歧义日期的两种读法，
        # 以及供 M08 复用的行项目——存下来才不用为同一页再付一次钱。
        batch_op.add_column(sa.Column("recognition_payload", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("recognition_error_code", sa.String(length=40), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "recognition_error_message", sa.String(length=600), nullable=True
            )
        )
        # 队列查询就是「这个批次里 status 还没到 done 的内容」，所以这一列要有索引。
        batch_op.create_index(
            batch_op.f("ix_intake_items_recognition_status"),
            ["recognition_status"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("intake_items") as batch_op:
        batch_op.drop_index(batch_op.f("ix_intake_items_recognition_status"))
        batch_op.drop_column("recognition_error_message")
        batch_op.drop_column("recognition_error_code")
        batch_op.drop_column("recognition_payload")
        batch_op.drop_column("recognized_at")
        batch_op.drop_column("recognition_status")
