"""The figures an exception had when a person last acted on it.

Revision ID: 20261124_26
Revises: 20261123_25

ADDITIVE ONLY: one nullable column. Existing reviews have no stored figures, so none of
them is treated as changed since it was closed.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261124_26"
down_revision = "20261123_25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("exception_reviews") and "basis" not in {c["name"] for c in inspector.get_columns("exception_reviews")}:
        op.add_column("exception_reviews", sa.Column("basis", sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
