"""Project pricing agreements and rates that start on a date.

Revision ID: 20261123_24
Revises: 20261111_25

ADDITIVE: two nullable columns on supplier_rates (project_id, effective_from) and
one on pricing_events (scope). Every existing rate becomes a company-wide rate with
no start date, which is exactly what it meant before, so every price TicketFlow
would show or lock is unchanged. Approved tickets' locked prices aren't touched.

The one-rate-per-pair unique index is replaced by four partial unique indexes, one
per combination of blank and filled project and date. A plain unique index would
treat each blank as different and let two company-wide rates for one pair coexist.
Existing rows satisfy all four, since the old index already held one row per pair.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261123_24"
down_revision = "20261111_25"
branch_labels = None
depends_on = None

COMPANY, PROJECT = "project_id IS NULL", "project_id IS NOT NULL"
UNDATED, DATED = "effective_from IS NULL", "effective_from IS NOT NULL"
INDEXES = {
    "uq_supplier_rate": ((), f"{COMPANY} AND {UNDATED}"),
    "uq_supplier_rate_dated": (("effective_from",), f"{COMPANY} AND {DATED}"),
    "uq_supplier_rate_project": (("project_id",), f"{PROJECT} AND {UNDATED}"),
    "uq_supplier_rate_project_dated": (("project_id", "effective_from"), f"{PROJECT} AND {DATED}"),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("supplier_rates"):
        return
    columns = {column["name"] for column in inspector.get_columns("supplier_rates")}
    if "effective_from" not in columns:
        op.add_column("supplier_rates", sa.Column("effective_from", sa.Date(), nullable=True))
    if "project_id" not in columns:
        # A nullable column with REFERENCES can be added in place, even on SQLite, so the
        # table is never rebuilt (a rebuild makes SQLite recheck every trigger it holds).
        if bind.dialect.name == "sqlite":
            # SQLite accepts this; Alembic's SQLite support doesn't offer it without a rebuild.
            op.execute("ALTER TABLE supplier_rates ADD COLUMN project_id INTEGER REFERENCES projects (id)")
        else:
            op.add_column("supplier_rates", sa.Column("project_id", sa.Integer(), nullable=True))
            op.create_foreign_key("fk_supplier_rates_project", "supplier_rates", "projects", ["project_id"], ["id"])

    existing = {index["name"]: index for index in sa.inspect(bind).get_indexes("supplier_rates")}
    for name, (extra, where) in INDEXES.items():
        current = existing.get(name)
        options = (current or {}).get("dialect_options") or {}
        partial = options.get("sqlite_where") is not None or options.get("postgresql_where") is not None
        if current is not None and name == "uq_supplier_rate" and not partial:
            # The old unconditional one-rate-per-pair index: replace it with the scoped one.
            op.drop_index(name, table_name="supplier_rates")
            current = None
        if current is None:
            op.create_index(name, "supplier_rates", ["organization_id", "supplier_id", "material_id", *extra],
                            unique=True, sqlite_where=sa.text(where), postgresql_where=sa.text(where))

    if inspector.has_table("pricing_events"):
        if "scope" not in {column["name"] for column in sa.inspect(bind).get_columns("pricing_events")}:
            op.add_column("pricing_events", sa.Column("scope", sa.String(240), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
