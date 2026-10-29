"""Assign all preexisting TicketFlow data to a preserved legacy organization.

Revision ID: 20261029_03
Revises: 20261029_02

No ticket, file, price snapshot or account is deleted or reassigned to a new
customer. Take a database backup before this SQLite table-rebuild migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261029_03"
down_revision = "20261029_02"
branch_labels = None
depends_on = None

OWNED_TABLES = ("users", "projects", "batches", "suppliers", "materials", "supplier_rates")
UNIQUES = {
    "projects": ("code", "uq_project_org_code"),
    "suppliers": ("name", "uq_supplier_org_name"),
    "materials": ("name", "uq_material_org_name"),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("organizations"):
        op.create_table(
            "organizations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
        )
    # Existing records and the existing owner all belong to one initial
    # company. Do NOT automatically distribute legacy records among users.
    first_id = bind.execute(sa.text("SELECT id FROM organizations ORDER BY id LIMIT 1")).scalar()
    if first_id is None:
        result = bind.execute(
            sa.text("INSERT INTO organizations (name) VALUES (:name)"),
            {"name": "Original workspace"},
        )
        first_id = result.lastrowid

    for table in OWNED_TABLES:
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        if "organization_id" not in columns:
            # SQLite cannot ALTER TABLE to add a foreign key in place.
            # Add the nullable integer first, backfill it, then use one
            # Alembic batch rebuild for NOT NULL + FK + tenant uniqueness.
            op.add_column(table, sa.Column("organization_id", sa.Integer(), nullable=True))
        bind.execute(
            sa.text(f"UPDATE {table} SET organization_id = :id WHERE organization_id IS NULL"),
            {"id": first_id},
        )

    # SQLite's old globally unique supplier/material/project fields must
    # become organization-scoped. Alembic batch copy preserves the rows.
    # A naming convention lets batch reflection address unnamed SQLite
    # unique constraints created by the original database schema.
    conv = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
    for table in OWNED_TABLES:
        unique = UNIQUES.get(table)
        existing = sa.inspect(bind).get_unique_constraints(table)
        old_unique = None
        has_new = False
        if unique:
            old_col, new_name = unique
            for constraint in existing:
                cols = set(constraint.get("column_names", []))
                if cols == {"organization_id", old_col}:
                    has_new = True
                elif cols == {old_col}:
                    old_unique = constraint.get("name") or f"uq_{table}_{old_col}"
        nullable = next(
            col["nullable"] for col in sa.inspect(bind).get_columns(table)
            if col["name"] == "organization_id"
        )
        foreign_keys = sa.inspect(bind).get_foreign_keys(table)
        has_fk = any(
            "organization_id" in fk.get("constrained_columns", [])
            and fk.get("referred_table") == "organizations"
            for fk in foreign_keys
        )
        if nullable or old_unique or not has_fk or (unique and not has_new):
            with op.batch_alter_table(table, naming_convention=conv) as batch:
                if old_unique:
                    batch.drop_constraint(old_unique, type_="unique")
                if nullable:
                    batch.alter_column(
                        "organization_id", existing_type=sa.Integer(), nullable=False,
                    )
                if not has_fk:
                    batch.create_foreign_key(
                        f"fk_{table}_organization", "organizations",
                        ["organization_id"], ["id"],
                    )
                if unique and not has_new:
                    batch.create_unique_constraint(new_name, ["organization_id", old_col])
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        index_name = f"ix_{table}_organization_id"
        if index_name not in indexes:
            op.create_index(index_name, table, ["organization_id"])


def downgrade() -> None:
    raise RuntimeError("Organization ownership cannot be rolled back without exposing company data.")
