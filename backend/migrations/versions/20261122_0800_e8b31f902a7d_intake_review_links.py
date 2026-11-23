"""Link intake contents to existing invoice review drafts (M08).

Revision ID: e8b31f902a7d
Revises: d06a4c71e812
"""

from alembic import op
import sqlalchemy as sa


revision = "e8b31f902a7d"
down_revision = "d06a4c71e812"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_review_links",
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("intake_items.id"), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_intake_review_links_invoice_id", "intake_review_links", ["invoice_id"])


def downgrade() -> None:
    op.drop_index("ix_intake_review_links_invoice_id", table_name="intake_review_links")
    op.drop_table("intake_review_links")
