"""Store what reports and matching need, so they don't recompute it for every row.

Revision ID: 20261106_19
Revises: 20261106_18

ADDITIVE: new columns and indexes, filled from existing values, plus replacing two
indexes these make redundant. No existing value changes.

- supplier_key, material_key, number_key, number_loose: the normalized forms that
  matching compares. They were computed in Python, which meant reading every row.
- approved_cost: tons x locked rate, kept with the approval. Reports add these up
  in the database rather than loading every ticket into memory.
"""
from decimal import ROUND_HALF_UP, Decimal
import re

from alembic import op
import sqlalchemy as sa

revision = "20261106_19"
down_revision = "20261106_18"
branch_labels = None
depends_on = None

LOCKED = "Approved tons and locked prices can only change by withdrawing the approval."
NEW_COLUMNS = (
    ("supplier_key", sa.String(200)), ("material_key", sa.String(200)), ("number_key", sa.String(100)),
    ("number_loose", sa.String(100)), ("approved_cost", sa.Numeric(14, 2)),
)
NEW_INDEXES = (
    ("ix_tickets_org_status", ["organization_id", "status"]),
    ("ix_tickets_org_date", ["organization_id", "delivery_date", "id"]),
    ("ix_tickets_org_scope", ["organization_id", "supplier_key", "material_key", "delivery_date"]),
    ("ix_tickets_org_number", ["organization_id", "number_key"]),
    ("ix_tickets_org_loose", ["organization_id", "number_loose"]),
    ("ix_tickets_org_load", ["organization_id", "delivery_date", "tonnage"]),
)
_CHUNK = 5000


def _norm(name: str | None) -> str:
    """Frozen copy of pricing.norm: a migration must not depend on current code."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("tickets"):
        return
    columns = {column["name"] for column in inspector.get_columns("tickets")}
    if not {"supplier", "material_type", "ticket_number", "tonnage", "batch_id"} <= columns:
        return  # A partial legacy table in an old migration test.
    for name, kind in NEW_COLUMNS:
        if name not in columns:
            op.add_column("tickets", sa.Column(name, kind, nullable=True))

    # One update per distinct spelling, however many tickets use it.
    for column, key in (("supplier", "supplier_key"), ("material_type", "material_key")):
        for (value,) in bind.execute(sa.text(f"SELECT DISTINCT {column} FROM tickets WHERE {key} IS NULL AND {column} IS NOT NULL")).fetchall():
            bind.execute(sa.text(f"UPDATE tickets SET {key} = :key WHERE {column} = :value AND {key} IS NULL"), {"key": _norm(value), "value": value})
    bind.execute(sa.text("UPDATE tickets SET number_key = UPPER(TRIM(ticket_number)), number_loose = LTRIM(UPPER(TRIM(ticket_number)), '0') "
                         "WHERE number_key IS NULL AND ticket_number IS NOT NULL"))

    # Exact decimal arithmetic, in Python, for approvals that have a locked price.
    if {"applied_unit_price", "price_snapshot_state", "tonnage_confirmed"} <= columns:
        last = 0
        while True:
            rows = bind.execute(sa.text(
                "SELECT id, tonnage, applied_unit_price FROM tickets WHERE id > :last AND status = 'verified' AND price_snapshot_state = 'priced' "
                "AND approved_cost IS NULL AND tonnage IS NOT NULL AND applied_unit_price IS NOT NULL ORDER BY id LIMIT :size"),
                {"last": last, "size": _CHUNK}).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            bind.execute(sa.text("UPDATE tickets SET approved_cost = :cost WHERE id = :id").bindparams(sa.bindparam("cost", type_=sa.Numeric(14, 2))), [
                {"id": row[0], "cost": (Decimal(str(row[1])) * Decimal(str(row[2]))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)}
                for row in rows])

    existing = {index["name"] for index in sa.inspect(bind).get_indexes("tickets")}
    for name, index_columns in NEW_INDEXES:
        if name not in existing:
            op.create_index(name, "tickets", index_columns)
    for name in ("ix_tickets_ticket_number", "ix_tickets_delivery_date"):
        if name in existing:
            op.drop_index(name, table_name="tickets")  # Replaced by the company-scoped ones above.
    if bind.dialect.name == "postgresql" and "ix_tickets_org_date_recent" not in existing:
        op.execute("CREATE INDEX ix_tickets_org_date_recent ON tickets (organization_id, delivery_date DESC NULLS LAST, id DESC)")

    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS tickets_approved_figures_locked")
        op.execute(
            "CREATE TRIGGER tickets_approved_figures_locked BEFORE UPDATE ON tickets "
            "WHEN OLD.status = 'verified' AND NEW.status = 'verified' AND OLD.price_snapshot_state = 'priced' "
            "AND (NEW.tonnage IS NOT OLD.tonnage OR NEW.applied_unit_price IS NOT OLD.applied_unit_price "
            "OR NEW.approved_cost IS NOT OLD.approved_cost) "
            f"BEGIN SELECT RAISE(ABORT, '{LOCKED}'); END")
    else:
        op.execute("CREATE OR REPLACE FUNCTION ticketflow_approved_figures_locked() RETURNS trigger AS $$ "
                   "BEGIN IF OLD.status = 'verified' AND NEW.status = 'verified' AND OLD.price_snapshot_state = 'priced' "
                   "AND (NEW.tonnage IS DISTINCT FROM OLD.tonnage OR NEW.applied_unit_price IS DISTINCT FROM OLD.applied_unit_price "
                   "OR NEW.approved_cost IS DISTINCT FROM OLD.approved_cost) THEN "
                   f"RAISE EXCEPTION '{LOCKED}' USING ERRCODE = 'restrict_violation'; END IF; RETURN NEW; END $$ LANGUAGE plpgsql")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
