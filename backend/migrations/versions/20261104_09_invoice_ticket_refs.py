"""Itemized ticket numbers on supplier invoice lines.

Revision ID: 20261104_09
Revises: 20261103_08

ADDITIVE ONLY: creates the invoice_ticket_refs table. No existing table or
row is changed. Every existing invoice comparison has no ticket list and
remains a totals-only comparison, computed exactly as before.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261104_09"
down_revision = "20261103_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("invoice_ticket_refs"):
        op.create_table(
            "invoice_ticket_refs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("comparison_id", sa.Integer(), sa.ForeignKey("invoice_comparisons.id"), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("ticket_number", sa.String(100), nullable=False),
            sa.Column("ticket_key", sa.String(100), nullable=False),
            sa.Column("billed_tons", sa.Numeric(12, 3), nullable=True),
            sa.UniqueConstraint("comparison_id", "position", name="uq_invoice_ticket_ref_position"),
        )
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("invoice_ticket_refs")}
    for column in ("comparison_id", "ticket_key"):
        name = f"ix_invoice_ticket_refs_{column}"
        if name not in indexes:
            op.create_index(name, "invoice_ticket_refs", [column])


def downgrade() -> None:
    raise RuntimeError("Invoice ticket lists must not be dropped automatically.")
