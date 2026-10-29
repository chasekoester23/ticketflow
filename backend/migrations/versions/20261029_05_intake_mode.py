"""Add explicit, non-AI manual intake mode.

Revision ID: 20261029_05
Revises: 20261029_04

Older batches remain extraction-mode batches. No tickets, audit entries,
prices or image files are altered.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261029_05"
down_revision = "20261029_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if "intake_mode" not in {
        column["name"] for column in sa.inspect(connection).get_columns("batches")
    }:
        op.add_column(
            "batches",
            sa.Column("intake_mode", sa.String(20), nullable=False, server_default="extract"),
        )


def downgrade() -> None:
    raise RuntimeError("Manual intake mode migration is non-destructive only.")
