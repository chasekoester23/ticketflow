"""Rules the database itself enforces, whichever code path touches it.

History tables only ever grow, and an approved ticket's figures can't change
unless its approval is withdrawn in the same statement. The application already
follows both rules. These catch a bug, a script or a mistaken manual query.

migrations/versions/20261106_18_integrity_guards.py installs the same rules on
existing databases. This installs them when tables are created without migrations:
new SQLite local installs, and tests.
"""
from __future__ import annotations

from sqlalchemy import event

from .db import Base

HISTORY_TABLES = ("audit_events", "pricing_events", "account_events", "scan_events",
                  "invoice_comparison_events", "invoice_document_events", "exception_events",
                  "truck_shift_events")
APPEND_ONLY = "This history is append-only."
LOCKED = "Approved tons and locked prices can only change by withdrawing the approval."
SHIFT_LOCKED = "An approved truck shift has locked hours, rates and cost. Reopen it to change them."
SHIFT_FIGURES = ("approved_work_hours", "approved_standby_hours", "approved_billable_hours", "applied_hourly_rate",
                 "applied_standby_rate", "applied_minimum_hours", "approved_cost")


def shift_sqlite_statements() -> list[str]:
    changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in SHIFT_FIGURES)
    return [
        "CREATE TRIGGER IF NOT EXISTS truck_shifts_approved_figures_locked BEFORE UPDATE ON truck_shifts "
        f"WHEN OLD.status = 'approved' AND NEW.status = 'approved' AND ({changed}) "
        f"BEGIN SELECT RAISE(ABORT, '{SHIFT_LOCKED}'); END",
        *(f"CREATE TRIGGER IF NOT EXISTS shift_allocations_locked_{action.lower()} BEFORE {action} ON shift_allocations "
          "WHEN (SELECT status FROM truck_shifts WHERE id = OLD.shift_id) = 'approved' "
          f"BEGIN SELECT RAISE(ABORT, '{SHIFT_LOCKED}'); END" for action in ("UPDATE", "DELETE")),
    ]


def shift_postgres_statements() -> list[str]:
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in SHIFT_FIGURES)
    return [
        "CREATE OR REPLACE FUNCTION ticketflow_shift_figures_locked() RETURNS trigger AS $$ "
        f"BEGIN IF OLD.status = 'approved' AND NEW.status = 'approved' AND ({changed}) THEN "
        f"RAISE EXCEPTION '{SHIFT_LOCKED}' USING ERRCODE = 'restrict_violation'; END IF; RETURN NEW; END $$ LANGUAGE plpgsql",
        "DROP TRIGGER IF EXISTS truck_shifts_approved_figures_locked ON truck_shifts",
        "CREATE TRIGGER truck_shifts_approved_figures_locked BEFORE UPDATE ON truck_shifts "
        "FOR EACH ROW EXECUTE FUNCTION ticketflow_shift_figures_locked()",
        "CREATE OR REPLACE FUNCTION ticketflow_allocation_locked() RETURNS trigger AS $$ "
        "BEGIN IF (SELECT status FROM truck_shifts WHERE id = OLD.shift_id) = 'approved' THEN "
        f"RAISE EXCEPTION '{SHIFT_LOCKED}' USING ERRCODE = 'restrict_violation'; END IF; "
        "IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$ LANGUAGE plpgsql",
        "DROP TRIGGER IF EXISTS shift_allocations_locked ON shift_allocations",
        "CREATE TRIGGER shift_allocations_locked BEFORE UPDATE OR DELETE ON shift_allocations "
        "FOR EACH ROW EXECUTE FUNCTION ticketflow_allocation_locked()",
    ]


def sqlite_statements() -> list[str]:
    statements = []
    for table in HISTORY_TABLES:
        for action in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY}'); END")
    statements.append(
        "CREATE TRIGGER IF NOT EXISTS tickets_approved_figures_locked BEFORE UPDATE ON tickets "
        "WHEN OLD.status = 'verified' AND NEW.status = 'verified' AND OLD.price_snapshot_state = 'priced' "
        "AND (NEW.tonnage IS NOT OLD.tonnage OR NEW.applied_unit_price IS NOT OLD.applied_unit_price "
        "OR NEW.approved_cost IS NOT OLD.approved_cost) "
        f"BEGIN SELECT RAISE(ABORT, '{LOCKED}'); END")
    return statements + shift_sqlite_statements()


def postgres_statements() -> list[str]:
    statements = [
        "CREATE OR REPLACE FUNCTION ticketflow_append_only() RETURNS trigger AS $$ "
        f"BEGIN RAISE EXCEPTION '{APPEND_ONLY}' USING ERRCODE = 'restrict_violation'; END "
        "$$ LANGUAGE plpgsql",
        "CREATE OR REPLACE FUNCTION ticketflow_approved_figures_locked() RETURNS trigger AS $$ "
        "BEGIN IF OLD.status = 'verified' AND NEW.status = 'verified' AND OLD.price_snapshot_state = 'priced' "
        "AND (NEW.tonnage IS DISTINCT FROM OLD.tonnage OR NEW.applied_unit_price IS DISTINCT FROM OLD.applied_unit_price "
        "OR NEW.approved_cost IS DISTINCT FROM OLD.approved_cost) THEN "
        f"RAISE EXCEPTION '{LOCKED}' USING ERRCODE = 'restrict_violation'; END IF; RETURN NEW; END "
        "$$ LANGUAGE plpgsql",
    ]
    statements.append("CREATE INDEX IF NOT EXISTS ix_tickets_org_date_recent ON tickets (organization_id, delivery_date DESC NULLS LAST, id DESC)")
    for table in HISTORY_TABLES:
        statements.append(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        statements.append(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                          "FOR EACH ROW EXECUTE FUNCTION ticketflow_append_only()")
    statements.append("DROP TRIGGER IF EXISTS tickets_approved_figures_locked ON tickets")
    statements.append("CREATE TRIGGER tickets_approved_figures_locked BEFORE UPDATE ON tickets "
                      "FOR EACH ROW EXECUTE FUNCTION ticketflow_approved_figures_locked()")
    return statements + shift_postgres_statements()


@event.listens_for(Base.metadata, "after_create")
def _install(_target, connection, **_kwargs) -> None:
    statements = sqlite_statements() if connection.dialect.name == "sqlite" else postgres_statements()
    for statement in statements:
        connection.exec_driver_sql(statement)
