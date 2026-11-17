"""Spreadsheet review batches and unresolved field issues (step 06)."""

from alembic import op
import sqlalchemy as sa

revision = "6f0c81da2306"
down_revision = "e2a825e1dbe5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        batch.add_column(sa.Column("entry_source", sa.String(16), nullable=False, server_default="invoice"))
        batch.add_column(sa.Column("import_metadata", sa.Text(), nullable=True))
    with op.batch_alter_table("invoices") as batch:
        batch.alter_column("entry_source", server_default=None)
    with op.batch_alter_table("invoice_lines") as batch:
        batch.add_column(sa.Column("review_metadata", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoice_lines") as batch:
        batch.drop_column("review_metadata")
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("import_metadata")
        batch.drop_column("entry_source")
