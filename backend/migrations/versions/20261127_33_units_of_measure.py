"""Units of measure, source quantities and scale weights.

Revision ID: 20261127_33
Revises: 20261127_32

ADDITIVE ONLY. No existing value changes meaning:
- tickets.unit: the billing unit of the approved quantity (the existing `tonnage`
  column holds the quantity in this unit). Every existing ticket is 'ton', which is
  what its quantity has always been. A trigger refuses a change of unit on an approved
  ticket, like its quantity and price.
- tickets.source_quantity / source_unit: the figure as written when it was converted
  (pounds to tons). Empty for existing tickets: nothing was converted.
- tickets.gross_weight / tare_weight / weight_unit: scale weights, optional.
- supplier_rates.unit: the unit a rate prices. Every existing rate is 'ton'.
- invoice_comparisons.unit: the unit of a supplier invoice line's billed quantity.
  Every existing line is 'ton'.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261127_33"
down_revision = "20261127_32"
branch_labels = None
depends_on = None

UNIT_LOCKED = "The unit of an approved ticket is locked. Withdraw the approval to change it."


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    has = sa.inspect(bind).has_table
    if has("tickets"):
        have = _columns("tickets")
        for column in (sa.Column("unit", sa.String(10), nullable=False, server_default="ton"),
                       sa.Column("source_quantity", sa.Numeric(14, 3), nullable=True),
                       sa.Column("source_unit", sa.String(10), nullable=True),
                       sa.Column("gross_weight", sa.Numeric(12, 3), nullable=True),
                       sa.Column("tare_weight", sa.Numeric(12, 3), nullable=True),
                       sa.Column("weight_unit", sa.String(5), nullable=True)):
            if column.name not in have:
                op.add_column("tickets", column)
        if bind.dialect.name == "sqlite":
            op.execute("CREATE TRIGGER IF NOT EXISTS tickets_unit_locked BEFORE UPDATE ON tickets "
                       "WHEN OLD.status = 'verified' AND NEW.status = 'verified' AND NEW.unit IS NOT OLD.unit "
                       f"BEGIN SELECT RAISE(ABORT, '{UNIT_LOCKED}'); END")
        else:
            op.execute("CREATE OR REPLACE FUNCTION ticketflow_unit_locked() RETURNS trigger AS $$ "
                       "BEGIN IF OLD.status = 'verified' AND NEW.status = 'verified' AND NEW.unit IS DISTINCT FROM OLD.unit THEN "
                       f"RAISE EXCEPTION '{UNIT_LOCKED}' USING ERRCODE = 'restrict_violation'; END IF; RETURN NEW; END "
                       "$$ LANGUAGE plpgsql")
            op.execute("DROP TRIGGER IF EXISTS tickets_unit_locked ON tickets")
            op.execute("CREATE TRIGGER tickets_unit_locked BEFORE UPDATE ON tickets FOR EACH ROW EXECUTE FUNCTION ticketflow_unit_locked()")
    if has("supplier_rates") and "unit" not in _columns("supplier_rates"):
        op.add_column("supplier_rates", sa.Column("unit", sa.String(10), nullable=False, server_default="ton"))
    if has("invoice_comparisons") and "unit" not in _columns("invoice_comparisons"):
        op.add_column("invoice_comparisons", sa.Column("unit", sa.String(10), nullable=False, server_default="ton"))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
