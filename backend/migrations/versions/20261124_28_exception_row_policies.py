"""Row-level security policies for the invoice exceptions tables, on PostgreSQL.

Revision ID: 20261124_28
Revises: 20261124_27

ADDITIVE ONLY, and PostgreSQL only, like 20261111_25: one policy per table, inert until
an operator enables row-level security (scripts/postgres_rls.py enable). Both tables
carry the company directly.
"""
from alembic import op

revision = "20261124_28"
down_revision = "20261124_27"
branch_labels = None
depends_on = None

POLICY = "ticketflow_tenant"
COMPANY = "organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer"
TABLES = ("exception_reviews", "exception_events")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(f"CREATE POLICY {POLICY} ON {table} USING ({COMPANY}) WITH CHECK ({COMPANY})")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
