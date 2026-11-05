"""Saved record filters and export columns, shared within a company.

Revision ID: 20261105_17
Revises: 20261105_16

ADDITIVE ONLY: one new table.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_17"
down_revision = "20261105_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("saved_views"):
        op.create_table(
            "saved_views",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("filters", sa.JSON(), nullable=False),
            sa.Column("columns", sa.JSON(), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("organization_id", "name", name="uq_saved_view_org_name"),
        )
    if "ix_saved_views_organization_id" not in {i["name"] for i in sa.inspect(bind).get_indexes("saved_views")}:
        op.create_index("ix_saved_views_organization_id", "saved_views", ["organization_id"])


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
