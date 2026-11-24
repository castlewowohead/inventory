"""Keep manually created and previously occupied intake date headers.

Revision ID: a8c92e74d130
Revises: f2a9c35db714
"""

from alembic import op
import sqlalchemy as sa


revision = "a8c92e74d130"
down_revision = "f2a9c35db714"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_date_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("intake_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purchase_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("batch_id", "purchase_date", name="uq_intake_date_groups_batch_id_purchase_date"),
    )
    op.execute(sa.text(
        "INSERT INTO intake_date_groups (batch_id, purchase_date, created_at) "
        "SELECT DISTINCT f.batch_id, i.decided_purchase_date, CURRENT_TIMESTAMP "
        "FROM intake_items AS i JOIN intake_batch_files AS f ON f.id = i.batch_file_id "
        "WHERE i.decided_purchase_date IS NOT NULL"
    ))


def downgrade() -> None:
    op.drop_table("intake_date_groups")
