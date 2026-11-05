"""A short note saying why a ticket was set aside.

Revision ID: 20261105_16
Revises: 20261105_15

ADDITIVE ONLY: one nullable column. Existing tickets keep NULL.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_16"
down_revision = "20261105_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tickets") and "flag_note" not in {c["name"] for c in inspector.get_columns("tickets")}:
        # Native ADD COLUMN: no table copy, so existing rows are untouched.
        op.add_column("tickets", sa.Column("flag_note", sa.String(300), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
