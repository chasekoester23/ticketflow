"""Invoice PDFs and human-reviewed draft lines, separate from comparisons.

Revision ID: 20261105_12
Revises: 20261105_11

ADDITIVE ONLY: creates three tables. Existing comparisons are not changed and
manual entry works as before.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_12"
down_revision = "20261105_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("invoice_documents"):
        op.create_table(
            "invoice_documents",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("original_name", sa.String(255), nullable=False),
            sa.Column("storage_path", sa.String(500), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("page_count", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("extractor", sa.String(30), nullable=False),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("unread_rows", sa.JSON(), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    if not sa.inspect(bind).has_table("invoice_draft_lines"):
        op.create_table(
            "invoice_draft_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("document_id", sa.Integer(), sa.ForeignKey("invoice_documents.id"), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False),
            sa.Column("source_text", sa.Text(), nullable=True),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("invoice_number", sa.String(100), nullable=True),
            sa.Column("supplier_name", sa.String(200), nullable=True),
            sa.Column("material_name", sa.String(200), nullable=True),
            sa.Column("billed_quantity", sa.Numeric(12, 3), nullable=True),
            sa.Column("unit", sa.String(20), nullable=True),
            sa.Column("billed_unit_price", sa.Numeric(12, 4), nullable=True),
            sa.Column("billed_subtotal", sa.Numeric(14, 2), nullable=True),
            sa.Column("date_from", sa.Date(), nullable=True),
            sa.Column("date_to", sa.Date(), nullable=True),
            sa.Column("ticket_list", sa.Text(), nullable=False, server_default=""),
            sa.Column("comparison_id", sa.Integer(), sa.ForeignKey("invoice_comparisons.id"), nullable=True),
            sa.Column("reviewed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not sa.inspect(bind).has_table("invoice_document_events"):
        op.create_table(
            "invoice_document_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("document_id", sa.Integer(), sa.ForeignKey("invoice_documents.id"), nullable=False),
            sa.Column("line_id", sa.Integer(), sa.ForeignKey("invoice_draft_lines.id"), nullable=True),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("old_value", sa.Text(), nullable=True),
            sa.Column("new_value", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    for table, column in (("invoice_documents", "organization_id"), ("invoice_documents", "sha256"),
                          ("invoice_draft_lines", "document_id"), ("invoice_draft_lines", "comparison_id"),
                          ("invoice_document_events", "document_id")):
        name = f"ix_{table}_{column}"
        if name not in {i["name"] for i in sa.inspect(bind).get_indexes(table)}:
            op.create_index(name, table, [column])


def downgrade() -> None:
    raise RuntimeError("Invoice documents and their history must not be dropped automatically.")
