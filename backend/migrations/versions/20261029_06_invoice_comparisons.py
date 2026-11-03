"""Add non-payable, tenant-scoped manual invoice comparison and audit events.

Revision ID: 20261029_06
Revises: 20261029_05

Additive migration only. All existing tickets, rate snapshots, user accounts,
files and projects remain unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261029_06"
down_revision = "20261029_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if not sa.inspect(connection).has_table("invoice_comparisons"):
        op.create_table(
            "invoice_comparisons",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("invoice_number", sa.String(100), nullable=False),
            sa.Column("supplier_name", sa.String(200), nullable=False),
            sa.Column("supplier_key", sa.String(200), nullable=False),
            sa.Column("material_name", sa.String(200), nullable=False),
            sa.Column("material_key", sa.String(200), nullable=False),
            sa.Column("date_from", sa.Date(), nullable=False),
            sa.Column("date_to", sa.Date(), nullable=False),
            sa.Column("billed_tons", sa.Numeric(12, 3), nullable=False),
            sa.Column("billed_unit_price", sa.Numeric(12, 4), nullable=False),
            sa.Column("billed_subtotal", sa.Numeric(14, 2), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        )
        op.create_index("ix_invoice_comparisons_organization_id", "invoice_comparisons", ["organization_id"])
        op.create_index("ix_invoice_comparisons_project_id", "invoice_comparisons", ["project_id"])
    if not sa.inspect(connection).has_table("invoice_comparison_events"):
        op.create_table(
            "invoice_comparison_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("comparison_id", sa.Integer(), sa.ForeignKey("invoice_comparisons.id"), nullable=False),
            sa.Column("field", sa.String(60), nullable=False),
            sa.Column("old_value", sa.Text(), nullable=True),
            sa.Column("new_value", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        )
        op.create_index("ix_invoice_comparison_events_comparison_id", "invoice_comparison_events", ["comparison_id"])


def downgrade() -> None:
    raise RuntimeError("Invoice comparison records and their audit history must not be destroyed.")
