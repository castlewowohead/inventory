"""Persist parsed spreadsheet rows and file mapping for intake M04.

Revision ID: b91e6a74d320
Revises: a3c81d57e240
"""

from alembic import op
import sqlalchemy as sa


revision = "b91e6a74d320"
down_revision = "a3c81d57e240"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intake_batch_files") as batch_op:
        batch_op.add_column(sa.Column("table_metadata", sa.Text(), nullable=True))
    with op.batch_alter_table("intake_items") as batch_op:
        batch_op.add_column(sa.Column("row_data", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("intake_items") as batch_op:
        batch_op.drop_column("row_data")
    with op.batch_alter_table("intake_batch_files") as batch_op:
        batch_op.drop_column("table_metadata")
