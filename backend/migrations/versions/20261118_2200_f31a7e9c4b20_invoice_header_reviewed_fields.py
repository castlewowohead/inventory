"""Track reviewed invoice header fields separately from recognition evidence.

Revision ID: f31a7e9c4b20
Revises: d9b4c70e52af
"""

from alembic import op
import sqlalchemy as sa

revision = "f31a7e9c4b20"
down_revision = "d9b4c70e52af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("header_reviewed_fields", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("invoices", "header_reviewed_fields")
