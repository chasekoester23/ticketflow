"""Delivery spreadsheet imports.

Revision ID: 20261128_34
Revises: 20261127_33

ADDITIVE ONLY: one new table, delivery_imports. No existing table or row changes.
A supplier's CSV or Excel export of deliveries is kept here with the columns a person
mapped; its rows become ordinary unapproved tickets in a batch whose intake_mode is
'import' (the column is free text, so no existing value changes meaning). On
PostgreSQL the table gets the same company policy as every company table, inert until
row-level security is switched on (scripts/postgres_rls.py enable).
"""
from alembic import op
import sqlalchemy as sa

revision = "20261128_34"
down_revision = "20261127_33"
branch_labels = None
depends_on = None

POLICY = "ticketflow_tenant"
COMPANY = "organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("delivery_imports"):
        op.create_table(
            "delivery_imports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("original_name", sa.String(255), nullable=False),
            sa.Column("storage_path", sa.String(500), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("file_kind", sa.String(10), nullable=False),
            sa.Column("header_row", sa.Integer(), nullable=False),
            sa.Column("rows", sa.Integer(), nullable=False),
            sa.Column("mapping", sa.JSON(), nullable=True),
            sa.Column("defaults", sa.JSON(), nullable=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("batches.id"), nullable=True),
            sa.Column("imported", sa.Integer(), nullable=False),
            sa.Column("skipped_duplicates", sa.Integer(), nullable=False),
            sa.Column("error_rows", sa.Integer(), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("committed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_delivery_imports_org_created", "delivery_imports", ["organization_id", "id"])
        op.create_index("uq_delivery_imports_batch", "delivery_imports", ["batch_id"], unique=True)
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON delivery_imports")
        op.execute(f"CREATE POLICY {POLICY} ON delivery_imports USING ({COMPANY}) WITH CHECK ({COMPANY})")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
