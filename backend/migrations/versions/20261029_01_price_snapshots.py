"""Baseline existing TicketFlow databases and add immutable ticket price snapshots.

Revision ID: 20261029_01
Revises:
Existing verified tickets cannot be assigned a historic price from current rates.
The migration therefore leaves their snapshot_state NULL ('legacy'). New ORM
inserts default to 'pending'; the explicit verification workflow fills snapshots.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261029_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # The original app used create_all() without Alembic. This baseline
    # establishes the tables on fresh installs and keeps existing data intact.
    from app.db import Base
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=bind, checkfirst=True)
    cols = {c["name"] for c in sa.inspect(bind).get_columns("tickets")}
    additions = [
        sa.Column("price_snapshot_state", sa.String(20), nullable=True),
        sa.Column("applied_unit_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("applied_price_source", sa.String(20), nullable=True),
    ]
    for col in additions:
        if col.name not in cols:
            op.add_column("tickets", col)
    # Existing verified rows retain NULL state, which is explicitly reported
    # as an unsnapshotted legacy price. We never backfill using today's rates.


def downgrade() -> None:
    raise RuntimeError("Price snapshot history is non-destructive and cannot be downgraded safely.")
