"""Requests for a pilot workspace from the public sign-up page.

Revision ID: 20261109_23
Revises: 20261106_22

ADDITIVE ONLY: one new table. No existing table or row changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261109_23"
down_revision = "20261106_22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("access_requests"):
        return
    op.create_table(
        "access_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("contact_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("monthly_tickets", sa.String(20), nullable=True),
        sa.Column("message", sa.String(1000), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_access_requests_email", "access_requests", ["email"])
    op.create_index("ix_access_requests_status", "access_requests", ["status"])


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
