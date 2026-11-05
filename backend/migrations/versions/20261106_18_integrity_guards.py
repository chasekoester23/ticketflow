"""Have the database enforce what double counting and repricing would break.

Revision ID: 20261106_18
Revises: 20261105_17

ADDITIVE ONLY: new columns, indexes and triggers. No existing row changes, apart
from filling the new columns.

A unique index can't be created over rows that already break it, and this never
deletes or changes a record to make one fit. If existing data has duplicates, the
index is left out, the migration says which rows, and System health reports it
until a person resolves them.
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = "20261106_18"
down_revision = "20261105_17"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

HISTORY_TABLES = ("audit_events", "pricing_events", "account_events", "scan_events",
                  "invoice_comparison_events", "invoice_document_events")
APPEND_ONLY = "This history is append-only."
LOCKED = "Approved tons and locked prices can only change by withdrawing the approval."

UNIQUE_INDEXES = (
    ("uq_tickets_slot", "tickets", ["file_id", "source_page", "position"], None,
     "SELECT file_id, source_page, position FROM tickets GROUP BY file_id, source_page, position HAVING COUNT(*) > 1"),
    ("uq_tickets_approved_scan", "tickets", ["organization_id", "crop_sha256"], "status = 'verified'",
     "SELECT organization_id, crop_sha256 FROM tickets WHERE status = 'verified' GROUP BY organization_id, crop_sha256 HAVING COUNT(*) > 1"),
    ("uq_supplier_rate", "supplier_rates", ["organization_id", "supplier_id", "material_id"], None,
     "SELECT organization_id, supplier_id, material_id FROM supplier_rates GROUP BY organization_id, supplier_id, material_id HAVING COUNT(*) > 1"),
    ("uq_invoice_document_file", "invoice_documents", ["organization_id", "sha256"], "status <> 'voided'",
     "SELECT organization_id, sha256 FROM invoice_documents WHERE status <> 'voided' GROUP BY organization_id, sha256 HAVING COUNT(*) > 1"),
)


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("tickets"):
        return
    if "organization_id" not in _columns(bind, "tickets"):
        # Native ADD COLUMN: no table copy, so existing rows are untouched.
        op.add_column("tickets", sa.Column("organization_id", sa.Integer(), nullable=True))
    if "lock_version" not in _columns(bind, "tickets"):
        op.add_column("tickets", sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"))
    if sa.inspect(bind).has_table("invoice_comparisons") and "lock_version" not in _columns(bind, "invoice_comparisons"):
        op.add_column("invoice_comparisons", sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"))
    if sa.inspect(bind).has_table("batches") and "organization_id" in _columns(bind, "batches") and "batch_id" in _columns(bind, "tickets"):
        bind.execute(sa.text("UPDATE tickets SET organization_id = (SELECT b.organization_id FROM batches b WHERE b.id = tickets.batch_id) "
                             "WHERE organization_id IS NULL"))
    inspector = sa.inspect(bind)
    if "ix_tickets_organization_id" not in {i["name"] for i in inspector.get_indexes("tickets")}:
        op.create_index("ix_tickets_organization_id", "tickets", ["organization_id"])

    for name, table, columns, where, check in UNIQUE_INDEXES:
        if not sa.inspect(bind).has_table(table) or not set(columns) <= _columns(bind, table):
            continue
        if name in {i["name"] for i in sa.inspect(bind).get_indexes(table)}:
            continue
        duplicates = bind.execute(sa.text(check)).fetchall()
        if duplicates:
            log.warning("Not creating %s: %d group(s) of existing rows already break it, first %s. Nothing was changed. "
                        "System health will report this until they're resolved.", name, len(duplicates), tuple(duplicates[0]))
            continue
        kwargs = {"sqlite_where": sa.text(where), "postgresql_where": sa.text(where)} if where else {}
        op.create_index(name, table, columns, unique=True, **kwargs)

    if bind.dialect.name == "sqlite":
        for table in HISTORY_TABLES:
            if sa.inspect(bind).has_table(table):
                for action in ("UPDATE", "DELETE"):
                    op.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                               f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY}'); END")
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS tickets_approved_figures_locked BEFORE UPDATE ON tickets "
            "WHEN OLD.status = 'verified' AND NEW.status = 'verified' AND OLD.price_snapshot_state = 'priced' "
            "AND (NEW.tonnage IS NOT OLD.tonnage OR NEW.applied_unit_price IS NOT OLD.applied_unit_price) "
            f"BEGIN SELECT RAISE(ABORT, '{LOCKED}'); END")
    else:
        op.execute("CREATE OR REPLACE FUNCTION ticketflow_append_only() RETURNS trigger AS $$ "
                   f"BEGIN RAISE EXCEPTION '{APPEND_ONLY}' USING ERRCODE = 'restrict_violation'; END $$ LANGUAGE plpgsql")
        op.execute("CREATE OR REPLACE FUNCTION ticketflow_approved_figures_locked() RETURNS trigger AS $$ "
                   "BEGIN IF OLD.status = 'verified' AND NEW.status = 'verified' AND OLD.price_snapshot_state = 'priced' "
                   "AND (NEW.tonnage IS DISTINCT FROM OLD.tonnage OR NEW.applied_unit_price IS DISTINCT FROM OLD.applied_unit_price) THEN "
                   f"RAISE EXCEPTION '{LOCKED}' USING ERRCODE = 'restrict_violation'; END IF; RETURN NEW; END $$ LANGUAGE plpgsql")
        for table in HISTORY_TABLES:
            if sa.inspect(bind).has_table(table):
                op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
                op.execute(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                           "FOR EACH ROW EXECUTE FUNCTION ticketflow_append_only()")
        op.execute("DROP TRIGGER IF EXISTS tickets_approved_figures_locked ON tickets")
        op.execute("CREATE TRIGGER tickets_approved_figures_locked BEFORE UPDATE ON tickets "
                   "FOR EACH ROW EXECUTE FUNCTION ticketflow_approved_figures_locked()")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
