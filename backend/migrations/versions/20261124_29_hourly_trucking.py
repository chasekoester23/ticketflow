"""Hourly trucking: haulers, trucks, hourly agreements, truck shifts, their allocations
and history, and a link from a delivery ticket to the shift that carried it.

Revision ID: 20261124_29
Revises: 20261124_28

ADDITIVE ONLY: six new tables and one nullable column on tickets. No existing row
changes, and no table is rebuilt. Approved shifts get the same database protection as
approved tickets (db_guards): their hours, rates and cost can't change while approved,
nor can their allocations, and their history is append-only. On PostgreSQL, each new
table gets the company row-level policy, inert until row-level security is enabled.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261124_29"
down_revision = "20261124_28"
branch_labels = None
depends_on = None

APPEND_ONLY = "This history is append-only."
POLICY = "ticketflow_tenant"
COMPANY = "organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer"
POLICIES = {
    "haulers": COMPANY, "trucks": COMPANY, "hauling_rates": COMPANY, "truck_shifts": COMPANY,
    "shift_allocations": "shift_id IN (SELECT id FROM truck_shifts)",
    "truck_shift_events": "shift_id IN (SELECT id FROM truck_shifts)",
}


# Frozen copies of db_guards' shift rules, so this migration never changes with the app.
SHIFT_SQLITE = ["CREATE TRIGGER IF NOT EXISTS truck_shifts_approved_figures_locked BEFORE UPDATE ON truck_shifts WHEN OLD.status = 'approved' AND NEW.status = 'approved' AND (NEW.approved_work_hours IS NOT OLD.approved_work_hours OR NEW.approved_standby_hours IS NOT OLD.approved_standby_hours OR NEW.approved_billable_hours IS NOT OLD.approved_billable_hours OR NEW.applied_hourly_rate IS NOT OLD.applied_hourly_rate OR NEW.applied_standby_rate IS NOT OLD.applied_standby_rate OR NEW.applied_minimum_hours IS NOT OLD.applied_minimum_hours OR NEW.approved_cost IS NOT OLD.approved_cost) BEGIN SELECT RAISE(ABORT, 'An approved truck shift has locked hours, rates and cost. Reopen it to change them.'); END", "CREATE TRIGGER IF NOT EXISTS shift_allocations_locked_update BEFORE UPDATE ON shift_allocations WHEN (SELECT status FROM truck_shifts WHERE id = OLD.shift_id) = 'approved' BEGIN SELECT RAISE(ABORT, 'An approved truck shift has locked hours, rates and cost. Reopen it to change them.'); END", "CREATE TRIGGER IF NOT EXISTS shift_allocations_locked_delete BEFORE DELETE ON shift_allocations WHEN (SELECT status FROM truck_shifts WHERE id = OLD.shift_id) = 'approved' BEGIN SELECT RAISE(ABORT, 'An approved truck shift has locked hours, rates and cost. Reopen it to change them.'); END"]
SHIFT_POSTGRES = ["CREATE OR REPLACE FUNCTION ticketflow_shift_figures_locked() RETURNS trigger AS $$ BEGIN IF OLD.status = 'approved' AND NEW.status = 'approved' AND (NEW.approved_work_hours IS DISTINCT FROM OLD.approved_work_hours OR NEW.approved_standby_hours IS DISTINCT FROM OLD.approved_standby_hours OR NEW.approved_billable_hours IS DISTINCT FROM OLD.approved_billable_hours OR NEW.applied_hourly_rate IS DISTINCT FROM OLD.applied_hourly_rate OR NEW.applied_standby_rate IS DISTINCT FROM OLD.applied_standby_rate OR NEW.applied_minimum_hours IS DISTINCT FROM OLD.applied_minimum_hours OR NEW.approved_cost IS DISTINCT FROM OLD.approved_cost) THEN RAISE EXCEPTION 'An approved truck shift has locked hours, rates and cost. Reopen it to change them.' USING ERRCODE = 'restrict_violation'; END IF; RETURN NEW; END $$ LANGUAGE plpgsql", 'DROP TRIGGER IF EXISTS truck_shifts_approved_figures_locked ON truck_shifts', 'CREATE TRIGGER truck_shifts_approved_figures_locked BEFORE UPDATE ON truck_shifts FOR EACH ROW EXECUTE FUNCTION ticketflow_shift_figures_locked()', "CREATE OR REPLACE FUNCTION ticketflow_allocation_locked() RETURNS trigger AS $$ BEGIN IF (SELECT status FROM truck_shifts WHERE id = OLD.shift_id) = 'approved' THEN RAISE EXCEPTION 'An approved truck shift has locked hours, rates and cost. Reopen it to change them.' USING ERRCODE = 'restrict_violation'; END IF; IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$ LANGUAGE plpgsql", 'DROP TRIGGER IF EXISTS shift_allocations_locked ON shift_allocations', 'CREATE TRIGGER shift_allocations_locked BEFORE UPDATE OR DELETE ON shift_allocations FOR EACH ROW EXECUTE FUNCTION ticketflow_allocation_locked()']


def _org():
    return sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False)


def upgrade() -> None:
    bind = op.get_bind()
    has = sa.inspect(bind).has_table
    if not has("haulers"):
        op.create_table("haulers",
                        sa.Column("id", sa.Integer(), primary_key=True), _org(),
                        sa.Column("name", sa.String(200), nullable=False), sa.Column("name_key", sa.String(200), nullable=False),
                        sa.Column("contact", sa.String(200), nullable=True),
                        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_haulers_organization_id", "haulers", ["organization_id"])
        op.create_index("uq_hauler_name", "haulers", ["organization_id", "name_key"], unique=True)
    if not has("trucks"):
        op.create_table("trucks",
                        sa.Column("id", sa.Integer(), primary_key=True), _org(),
                        sa.Column("hauler_id", sa.Integer(), sa.ForeignKey("haulers.id"), nullable=False),
                        sa.Column("identifier", sa.String(50), nullable=False),
                        sa.Column("equipment_type", sa.String(30), nullable=False),
                        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))
        op.create_index("ix_trucks_organization_id", "trucks", ["organization_id"])
        op.create_index("ix_trucks_hauler_id", "trucks", ["hauler_id"])
        op.create_index("uq_truck_identifier", "trucks", ["organization_id", "hauler_id", "identifier"], unique=True)
    if not has("hauling_rates"):
        op.create_table("hauling_rates",
                        sa.Column("id", sa.Integer(), primary_key=True), _org(),
                        sa.Column("hauler_id", sa.Integer(), sa.ForeignKey("haulers.id"), nullable=False),
                        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
                        sa.Column("equipment_type", sa.String(30), nullable=True),
                        sa.Column("hourly_rate", sa.Numeric(12, 4), nullable=False),
                        sa.Column("minimum_hours", sa.Numeric(6, 2), nullable=True),
                        sa.Column("standby_rate", sa.Numeric(12, 4), nullable=True),
                        sa.Column("effective_from", sa.Date(), nullable=True), sa.Column("effective_to", sa.Date(), nullable=True),
                        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
                        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_hauling_rates_organization_id", "hauling_rates", ["organization_id"])
        op.create_index("ix_hauling_rates_hauler_id", "hauling_rates", ["hauler_id"])
    if not has("truck_shifts"):
        op.create_table("truck_shifts",
                        sa.Column("id", sa.Integer(), primary_key=True), _org(),
                        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
                        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
                        sa.Column("truck_id", sa.Integer(), sa.ForeignKey("trucks.id"), nullable=False),
                        sa.Column("hauler_id", sa.Integer(), sa.ForeignKey("haulers.id"), nullable=False),
                        sa.Column("work_date", sa.Date(), nullable=False),
                        sa.Column("driver_name", sa.String(100), nullable=True),
                        sa.Column("start_at", sa.DateTime(), nullable=False), sa.Column("end_at", sa.DateTime(), nullable=False),
                        sa.Column("unpaid_break_minutes", sa.Integer(), nullable=False, server_default="0"),
                        sa.Column("standby_hours", sa.Numeric(6, 2), nullable=False, server_default="0"),
                        sa.Column("claimed_hours", sa.Numeric(6, 2), nullable=True),
                        sa.Column("loads", sa.Integer(), nullable=True),
                        sa.Column("field_signoff", sa.String(100), nullable=True),
                        sa.Column("cost_code", sa.String(50), nullable=True),
                        sa.Column("notes", sa.Text(), nullable=True),
                        sa.Column("status", sa.String(20), nullable=False, server_default="needs_review"),
                        sa.Column("approved_work_hours", sa.Numeric(6, 2), nullable=True),
                        sa.Column("approved_standby_hours", sa.Numeric(6, 2), nullable=True),
                        sa.Column("approved_billable_hours", sa.Numeric(6, 2), nullable=True),
                        sa.Column("applied_rate_id", sa.Integer(), nullable=True),
                        sa.Column("applied_hourly_rate", sa.Numeric(12, 4), nullable=True),
                        sa.Column("applied_standby_rate", sa.Numeric(12, 4), nullable=True),
                        sa.Column("applied_minimum_hours", sa.Numeric(6, 2), nullable=True),
                        sa.Column("approved_cost", sa.Numeric(14, 2), nullable=True),
                        sa.Column("approved_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
                        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
                        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
                        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        for name, columns in (("ix_truck_shifts_org_date", ["organization_id", "work_date"]),
                              ("ix_truck_shifts_organization_id", ["organization_id"]), ("ix_truck_shifts_project_id", ["project_id"]),
                              ("ix_truck_shifts_truck_id", ["truck_id"]), ("ix_truck_shifts_hauler_id", ["hauler_id"])):
            op.create_index(name, "truck_shifts", columns)
    if not has("shift_allocations"):
        op.create_table("shift_allocations",
                        sa.Column("id", sa.Integer(), primary_key=True),
                        sa.Column("shift_id", sa.Integer(), sa.ForeignKey("truck_shifts.id"), nullable=False),
                        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
                        sa.Column("cost_code", sa.String(50), nullable=True),
                        sa.Column("hours", sa.Numeric(6, 2), nullable=False), sa.Column("cost", sa.Numeric(14, 2), nullable=False))
        op.create_index("ix_shift_allocations_shift_id", "shift_allocations", ["shift_id"])
        op.create_index("ix_shift_allocations_project_id", "shift_allocations", ["project_id"])
    if not has("truck_shift_events"):
        op.create_table("truck_shift_events",
                        sa.Column("id", sa.Integer(), primary_key=True),
                        sa.Column("shift_id", sa.Integer(), sa.ForeignKey("truck_shifts.id"), nullable=False),
                        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
                        sa.Column("field", sa.String(40), nullable=False),
                        sa.Column("old_value", sa.Text(), nullable=True), sa.Column("new_value", sa.Text(), nullable=True),
                        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_truck_shift_events_shift_id", "truck_shift_events", ["shift_id"])

    if "truck_shift_id" not in {c["name"] for c in sa.inspect(bind).get_columns("tickets")}:
        if bind.dialect.name == "sqlite":
            # In place: SQLite accepts a nullable REFERENCES column without rebuilding tickets.
            op.execute("ALTER TABLE tickets ADD COLUMN truck_shift_id INTEGER REFERENCES truck_shifts (id)")
        else:
            op.add_column("tickets", sa.Column("truck_shift_id", sa.Integer(), nullable=True))
            op.create_foreign_key("fk_tickets_truck_shift", "tickets", "truck_shifts", ["truck_shift_id"], ["id"])
        op.create_index("ix_tickets_truck_shift_id", "tickets", ["truck_shift_id"])

    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER IF NOT EXISTS truck_shift_events_no_{action.lower()} BEFORE {action} "
                       f"ON truck_shift_events BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY}'); END")
        for statement in SHIFT_SQLITE:
            op.execute(statement)
    else:
        op.execute("CREATE OR REPLACE FUNCTION ticketflow_append_only() RETURNS trigger AS $$ "
                   f"BEGIN RAISE EXCEPTION '{APPEND_ONLY}' USING ERRCODE = 'restrict_violation'; END $$ LANGUAGE plpgsql")
        op.execute("DROP TRIGGER IF EXISTS truck_shift_events_append_only ON truck_shift_events")
        op.execute("CREATE TRIGGER truck_shift_events_append_only BEFORE UPDATE OR DELETE ON truck_shift_events "
                   "FOR EACH ROW EXECUTE FUNCTION ticketflow_append_only()")
        for statement in SHIFT_POSTGRES:
            op.execute(statement)
        for table, condition in POLICIES.items():
            op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
            op.execute(f"CREATE POLICY {POLICY} ON {table} USING ({condition}) WITH CHECK ({condition})")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
