"""Let a saved view name its export columns, to match a team's own spreadsheet.

Revision ID: 20261106_21
Revises: 20261106_20

ADDITIVE ONLY: one column, empty for every existing view. Exports without a view are
unchanged, and so is their default column order.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261106_21"
down_revision = "20261106_20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("saved_views"):
        return
    if "labels" not in {column["name"] for column in sa.inspect(bind).get_columns("saved_views")}:
        op.add_column("saved_views", sa.Column("labels", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
