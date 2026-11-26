"""Hauling billed per ton or per load, freight included in a material price, and who
hauled a delivery.

Revision ID: 20261126_31
Revises: 20261125_30

ADDITIVE ONLY. No existing value changes:
- tickets: hauler_id, truck_number (who hauled it), and the hauling charge locked at
  approval (haul_billing, haul_rate_id, haul_unit_rate, haul_cost). All NULL for
  existing tickets, which therefore carry no hauling charge, exactly as before.
- supplier_rates.freight: 'included' | 'separate' | NULL (not stated). NULL for every
  existing rate, which therefore prices material exactly as before.
- hauling_rates.billing_method ('hourly' for every existing agreement, its only kind
  so far) and unit_rate (for per-ton and per-load). hourly_rate becomes nullable, since
  a per-ton rate has none; every existing row keeps its value.
- A trigger refuses changes to an approved ticket's locked hauling charge, like the
  one that locks its tons and material price.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261126_31"
down_revision = "20261125_30"
branch_labels = None
depends_on = None

HAUL_LOCKED = "The hauling charge of an approved ticket is locked. Withdraw the approval to change it."


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    has = sa.inspect(bind).has_table
    # A legacy database may not have every table; each part applies only where its table exists.
    have = _columns("tickets") if has("tickets") else None
    if have is not None and has("haulers") and "hauler_id" not in have:
        if sqlite:
            # In place: SQLite accepts a nullable REFERENCES column without rebuilding tickets.
            op.execute("ALTER TABLE tickets ADD COLUMN hauler_id INTEGER REFERENCES haulers (id)")
        else:
            op.add_column("tickets", sa.Column("hauler_id", sa.Integer(), nullable=True))
            op.create_foreign_key("fk_tickets_hauler", "tickets", "haulers", ["hauler_id"], ["id"])
        op.create_index("ix_tickets_hauler_id", "tickets", ["hauler_id"])
    for column in (sa.Column("truck_number", sa.String(50), nullable=True),
                   sa.Column("haul_billing", sa.String(20), nullable=True),
                   sa.Column("haul_rate_id", sa.Integer(), nullable=True),
                   sa.Column("haul_unit_rate", sa.Numeric(12, 4), nullable=True),
                   sa.Column("haul_cost", sa.Numeric(14, 2), nullable=True)):
        if have is not None and column.name not in have:
            op.add_column("tickets", column)

    if has("supplier_rates") and "freight" not in _columns("supplier_rates"):
        op.add_column("supplier_rates", sa.Column("freight", sa.String(10), nullable=True))

    if has("hauling_rates"):
        rates = _columns("hauling_rates")
        if "billing_method" not in rates:
            op.add_column("hauling_rates", sa.Column("billing_method", sa.String(10), nullable=False, server_default="hourly"))
        if "unit_rate" not in rates:
            op.add_column("hauling_rates", sa.Column("unit_rate", sa.Numeric(12, 4), nullable=True))
        if sqlite:
            # SQLite rebuilds the table to drop NOT NULL. Its final rename would otherwise make
            # SQLite re-check every trigger in the database; the legacy setting skips only that.
            op.execute("PRAGMA legacy_alter_table=ON")
            with op.batch_alter_table("hauling_rates") as batch:
                batch.alter_column("hourly_rate", existing_type=sa.Numeric(12, 4), nullable=True)
            op.execute("PRAGMA legacy_alter_table=OFF")
        else:
            op.alter_column("hauling_rates", "hourly_rate", existing_type=sa.Numeric(12, 4), nullable=True)

    if have is None:
        return
    if sqlite:
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS tickets_haul_charge_locked BEFORE UPDATE ON tickets "
            "WHEN OLD.status = 'verified' AND NEW.status = 'verified' AND OLD.haul_billing IS NOT NULL "
            "AND (NEW.haul_billing IS NOT OLD.haul_billing OR NEW.haul_unit_rate IS NOT OLD.haul_unit_rate "
            "OR NEW.haul_cost IS NOT OLD.haul_cost OR NEW.hauler_id IS NOT OLD.hauler_id) "
            f"BEGIN SELECT RAISE(ABORT, '{HAUL_LOCKED}'); END")
    else:
        op.execute(
            "CREATE OR REPLACE FUNCTION ticketflow_haul_charge_locked() RETURNS trigger AS $$ "
            "BEGIN IF OLD.status = 'verified' AND NEW.status = 'verified' AND OLD.haul_billing IS NOT NULL "
            "AND (NEW.haul_billing IS DISTINCT FROM OLD.haul_billing OR NEW.haul_unit_rate IS DISTINCT FROM OLD.haul_unit_rate "
            "OR NEW.haul_cost IS DISTINCT FROM OLD.haul_cost OR NEW.hauler_id IS DISTINCT FROM OLD.hauler_id) THEN "
            f"RAISE EXCEPTION '{HAUL_LOCKED}' USING ERRCODE = 'restrict_violation'; END IF; RETURN NEW; END "
            "$$ LANGUAGE plpgsql")
        op.execute("DROP TRIGGER IF EXISTS tickets_haul_charge_locked ON tickets")
        op.execute("CREATE TRIGGER tickets_haul_charge_locked BEFORE UPDATE ON tickets "
                   "FOR EACH ROW EXECUTE FUNCTION ticketflow_haul_charge_locked()")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
