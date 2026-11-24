"""Billing increments, time-sheet attachments and extraction provenance on truck
shifts, and hauling invoices with their lines and history.

Revision ID: 20261125_30
Revises: 20261124_29

ADDITIVE ONLY: nullable columns on hauling_rates and truck_shifts (truck_shifts.source
defaults to 'manual', which every existing shift was), and three new tables. No row
changes value. hauling_invoice_events is append-only, like every history table.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261125_30"
down_revision = "20261124_29"
branch_labels = None
depends_on = None

APPEND_ONLY = "This history is append-only."
POLICY = "ticketflow_tenant"
COMPANY = "organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer"
POLICIES = {"hauling_invoices": COMPANY,
            "hauling_invoice_lines": "invoice_id IN (SELECT id FROM hauling_invoices)",
            "hauling_invoice_events": "invoice_id IN (SELECT id FROM hauling_invoices)"}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    has = sa.inspect(bind).has_table
    _add("hauling_rates", sa.Column("billing_increment", sa.Numeric(4, 2), nullable=True))
    _add("hauling_rates", sa.Column("increment_rounding", sa.String(10), nullable=True))
    _add("truck_shifts", sa.Column("applied_billing_increment", sa.Numeric(4, 2), nullable=True))
    _add("truck_shifts", sa.Column("timesheet_path", sa.String(500), nullable=True))
    _add("truck_shifts", sa.Column("timesheet_name", sa.String(255), nullable=True))
    _add("truck_shifts", sa.Column("timesheet_sha256", sa.String(64), nullable=True))
    _add("truck_shifts", sa.Column("source", sa.String(20), nullable=False, server_default="manual"))
    _add("truck_shifts", sa.Column("extraction", sa.JSON(), nullable=True))
    if "ix_truck_shifts_timesheet_sha256" not in {i["name"] for i in sa.inspect(bind).get_indexes("truck_shifts")}:
        op.create_index("ix_truck_shifts_timesheet_sha256", "truck_shifts", ["timesheet_sha256"])
    if not has("hauling_invoices"):
        op.create_table("hauling_invoices",
                        sa.Column("id", sa.Integer(), primary_key=True),
                        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
                        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
                        sa.Column("hauler_id", sa.Integer(), sa.ForeignKey("haulers.id"), nullable=False),
                        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
                        sa.Column("invoice_number", sa.String(100), nullable=False),
                        sa.Column("number_key", sa.String(100), nullable=False),
                        sa.Column("invoice_date", sa.Date(), nullable=True),
                        sa.Column("period_from", sa.Date(), nullable=False), sa.Column("period_to", sa.Date(), nullable=False),
                        sa.Column("document_path", sa.String(500), nullable=True),
                        sa.Column("document_name", sa.String(255), nullable=True),
                        sa.Column("document_sha256", sa.String(64), nullable=True),
                        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
                        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_hauling_invoices_organization_id", "hauling_invoices", ["organization_id"])
        op.create_index("ix_hauling_invoices_hauler_id", "hauling_invoices", ["hauler_id"])
        op.create_index("uq_hauling_invoice_number", "hauling_invoices", ["organization_id", "hauler_id", "number_key"], unique=True)
    if not has("hauling_invoice_lines"):
        op.create_table("hauling_invoice_lines",
                        sa.Column("id", sa.Integer(), primary_key=True),
                        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("hauling_invoices.id"), nullable=False),
                        sa.Column("line_no", sa.Integer(), nullable=False), sa.Column("kind", sa.String(20), nullable=False),
                        sa.Column("truck_identifier", sa.String(50), nullable=True), sa.Column("work_date", sa.Date(), nullable=True),
                        sa.Column("hours", sa.Numeric(6, 2), nullable=True), sa.Column("rate", sa.Numeric(12, 4), nullable=True),
                        sa.Column("amount", sa.Numeric(14, 2), nullable=False), sa.Column("description", sa.String(300), nullable=True))
        op.create_index("ix_hauling_invoice_lines_invoice_id", "hauling_invoice_lines", ["invoice_id"])
    if not has("hauling_invoice_events"):
        op.create_table("hauling_invoice_events",
                        sa.Column("id", sa.Integer(), primary_key=True),
                        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("hauling_invoices.id"), nullable=False),
                        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
                        sa.Column("field", sa.String(40), nullable=False),
                        sa.Column("old_value", sa.Text(), nullable=True), sa.Column("new_value", sa.Text(), nullable=True),
                        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_hauling_invoice_events_invoice_id", "hauling_invoice_events", ["invoice_id"])
    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER IF NOT EXISTS hauling_invoice_events_no_{action.lower()} BEFORE {action} "
                       f"ON hauling_invoice_events BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY}'); END")
    else:
        op.execute("DROP TRIGGER IF EXISTS hauling_invoice_events_append_only ON hauling_invoice_events")
        op.execute("CREATE TRIGGER hauling_invoice_events_append_only BEFORE UPDATE OR DELETE ON hauling_invoice_events "
                   "FOR EACH ROW EXECUTE FUNCTION ticketflow_append_only()")
        for table, condition in POLICIES.items():
            op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
            op.execute(f"CREATE POLICY {POLICY} ON {table} USING ({condition}) WITH CHECK ({condition})")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
