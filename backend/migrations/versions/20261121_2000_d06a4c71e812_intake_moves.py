"""Persist atomic intake moves and undo snapshots (M06).

Revision ID: d06a4c71e812
Revises: c47be2a91f85
"""

from alembic import op
import sqlalchemy as sa


revision = "d06a4c71e812"
down_revision = "c47be2a91f85"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intake_items") as batch_op:
        batch_op.add_column(sa.Column("handed_off_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("pile_excluded", sa.Boolean(), nullable=False, server_default="0"))
    op.create_table(
        "intake_moves",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("intake_batches.id"), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("request_signature", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("batch_id", "request_key", name="uq_intake_moves_batch_id_request_key"),
    )
    op.create_index("ix_intake_moves_batch_id", "intake_moves", ["batch_id"])
    op.create_table(
        "intake_move_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("move_id", sa.Integer(), sa.ForeignKey("intake_moves.id"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("intake_items.id"), nullable=False),
        sa.Column("before_date", sa.Date(), nullable=True),
        sa.Column("before_source", sa.String(120), nullable=True),
        sa.Column("before_decided_at", sa.DateTime(), nullable=True),
        sa.Column("before_pile_excluded", sa.Boolean(), nullable=False),
        sa.Column("after_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("move_id", "item_id", name="uq_intake_move_entries_move_id_item_id"),
    )
    op.create_index("ix_intake_move_entries_move_id", "intake_move_entries", ["move_id"])
    op.create_index("ix_intake_move_entries_item_id", "intake_move_entries", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_intake_move_entries_item_id", table_name="intake_move_entries")
    op.drop_index("ix_intake_move_entries_move_id", table_name="intake_move_entries")
    op.drop_table("intake_move_entries")
    op.drop_index("ix_intake_moves_batch_id", table_name="intake_moves")
    op.drop_table("intake_moves")
    with op.batch_alter_table("intake_items") as batch_op:
        batch_op.drop_column("pile_excluded")
        batch_op.drop_column("handed_off_at")
