"""When a rate stops being valid: a supplier quote's expiry date.

Revision ID: 20261124_27
Revises: 20261124_26

ADDITIVE ONLY: one nullable column. Every existing rate is open-ended, which is what it
meant before, so no price shown or locked changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261124_27"
down_revision = "20261124_26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("supplier_rates") and "effective_to" not in {c["name"] for c in inspector.get_columns("supplier_rates")}:
        op.add_column("supplier_rates", sa.Column("effective_to", sa.Date(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
