"""Record when each signed-in session was last used, for the idle timeout.

Revision ID: 20261106_20
Revises: 20261106_19

ADDITIVE ONLY: one nullable column. Existing sessions get no value, and count as
active until their next request records one. Nothing else changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261106_20"
down_revision = "20261106_19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("login_sessions"):
        return
    if "last_seen_at" not in {column["name"] for column in sa.inspect(bind).get_columns("login_sessions")}:
        op.add_column("login_sessions", sa.Column("last_seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
