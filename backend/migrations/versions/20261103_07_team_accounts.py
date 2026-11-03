"""Individual employee accounts, company roles and attributable audit history.

Revision ID: 20261103_07
Revises: 20261029_06

ADDITIVE ONLY. No table is rebuilt and no row is deleted or rewritten except:
- every EXISTING user receives role 'owner' (each was created as the owner of
  their company by create_owner.py / create_company.py, so this is accurate).

Existing audit events, verified tickets, uploads and invoice comparisons keep
NULL actor columns. TicketFlow shows these as "before individual accounts";
it deliberately does not guess who performed historical actions.

Foreign keys on SQLite are declared with native ``ADD COLUMN ... REFERENCES``
(allowed because the default is NULL), which avoids copying tables.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261103_07"
down_revision = "20261029_06"
branch_labels = None
depends_on = None

# (table, column) -> nullable integer FK to users.id
ACTOR_COLUMNS = (
    ("audit_events", "actor_user_id"),
    ("invoice_comparison_events", "actor_user_id"),
    ("batches", "created_by_user_id"),
    ("invoice_comparisons", "created_by_user_id"),
    ("tickets", "verified_by_user_id"),
)


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _add_user_fk(bind, table: str, column: str) -> None:
    if column in _columns(bind, table):
        return
    if bind.dialect.name == "sqlite":
        # Identifiers are module constants above, never user input.
        op.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER REFERENCES users(id)")
    else:
        op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        op.create_foreign_key(f"fk_{table}_{column}", table, "users", [column], ["id"])


def upgrade() -> None:
    bind = op.get_bind()

    users = _columns(bind, "users")
    if "role" not in users:
        # server_default backfills every existing row as owner atomically.
        op.add_column("users", sa.Column(
            "role", sa.String(20), nullable=False, server_default="owner",
        ))
    if "display_name" not in users:
        op.add_column("users", sa.Column("display_name", sa.String(100), nullable=True))
    if "must_change_password" not in users:
        op.add_column("users", sa.Column(
            "must_change_password", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))
    if "created_at" not in users:
        # NULL for existing accounts: their creation time was never recorded.
        op.add_column("users", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))

    for table, column in ACTOR_COLUMNS:
        _add_user_fk(bind, table, column)
    if "verified_at" not in _columns(bind, "tickets"):
        op.add_column("tickets", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))

    session_indexes = {i["name"] for i in sa.inspect(bind).get_indexes("login_sessions")}
    if "ix_login_sessions_user_id" not in session_indexes:
        op.create_index("ix_login_sessions_user_id", "login_sessions", ["user_id"])

    if not sa.inspect(bind).has_table("account_events"):
        op.create_table(
            "account_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("target_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("old_value", sa.String(200), nullable=True),
            sa.Column("new_value", sa.String(200), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    account_indexes = {i["name"] for i in sa.inspect(bind).get_indexes("account_events")}
    for name, cols in (
        ("ix_account_events_organization_id", ["organization_id"]),
        ("ix_account_events_target_user_id", ["target_user_id"]),
    ):
        if name not in account_indexes:
            op.create_index(name, "account_events", cols)


def downgrade() -> None:
    raise RuntimeError("Account roles and audit attribution must not be dropped automatically.")
