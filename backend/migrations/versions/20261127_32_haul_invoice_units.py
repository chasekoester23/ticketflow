"""Hauler invoice lines billed per ton or per load, and a person's choice of shift.

Revision ID: 20261127_32
Revises: 20261126_31

ADDITIVE ONLY: three nullable columns on hauling_invoice_lines. Existing lines keep
their kind (hours, standby, minimum, fee, credit) and are matched exactly as before.
- quantity: tons or loads billed, for the new kinds "tons" and "loads".
- tickets: the delivery ticket numbers the line bills, when the invoice lists them.
- shift_id: the shift a person chose for a line that matched more than one.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261127_32"
down_revision = "20261126_31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("hauling_invoice_lines"):
        return
    have = {c["name"] for c in sa.inspect(bind).get_columns("hauling_invoice_lines")}
    for column in (sa.Column("quantity", sa.Numeric(12, 3), nullable=True),
                   sa.Column("tickets", sa.Text(), nullable=True),
                   sa.Column("shift_id", sa.Integer(), nullable=True)):
        if column.name not in have:
            op.add_column("hauling_invoice_lines", column)


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
