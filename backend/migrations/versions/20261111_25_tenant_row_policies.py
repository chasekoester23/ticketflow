"""Row-level security policies for company data, on PostgreSQL. Inert until enabled.

Revision ID: 20261111_25
Revises: 20261111_24

ADDITIVE ONLY, and PostgreSQL only: SQLite has no row-level security, so this does
nothing there. It creates one policy per table and does NOT turn row-level security
on, so nothing changes for anyone until an operator runs
`python scripts/postgres_rls.py enable` and sets POSTGRES_RLS=on. See
docs/engineering/postgres.md.

A row is visible when its company matches app.organization_id, which the API sets per
transaction from the signed-in user. Unset, it matches nothing. Child tables follow
their parent, whose own policy then applies inside the subquery.
"""
from alembic import op

revision = "20261111_25"
down_revision = "20261111_24"
branch_labels = None
depends_on = None

POLICY = "ticketflow_tenant"
COMPANY = "organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer"
# Frozen here: a later change makes its own migration.
TABLES = {
    "batches": COMPANY,
    "tickets": COMPANY,
    "projects": COMPANY,
    "invoice_comparisons": COMPANY,
    "invoice_documents": COMPANY,
    "extraction_usage": COMPANY,
    "uploaded_files": "batch_id IN (SELECT id FROM batches)",
    "scan_slots": "batch_id IN (SELECT id FROM batches)",
    "scan_events": "batch_id IN (SELECT id FROM batches)",
    "audit_events": "ticket_id IN (SELECT id FROM tickets)",
    "invoice_ticket_refs": "comparison_id IN (SELECT id FROM invoice_comparisons)",
    "invoice_ticket_links": "comparison_id IN (SELECT id FROM invoice_comparisons)",
    "invoice_comparison_events": "comparison_id IN (SELECT id FROM invoice_comparisons)",
    "invoice_draft_lines": "document_id IN (SELECT id FROM invoice_documents)",
    "invoice_document_events": "document_id IN (SELECT id FROM invoice_documents)",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, condition in TABLES.items():
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(f"CREATE POLICY {POLICY} ON {table} USING ({condition}) WITH CHECK ({condition})")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
