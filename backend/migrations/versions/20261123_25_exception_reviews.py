"""People's handling of billing exceptions, and its append-only history.

Revision ID: 20261123_25
Revises: 20261123_24

ADDITIVE ONLY: two new tables. Exceptions themselves aren't stored; they're
recomputed from invoices, tickets and rates. No existing table or row changes.
exception_events gets the same append-only guard as the other history tables.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261123_25"
down_revision = "20261123_24"
branch_labels = None
depends_on = None

APPEND_ONLY = "This history is append-only."


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("exception_reviews"):
        op.create_table(
            "exception_reviews",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("key", sa.String(200), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("assignee_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("recovered_amount", sa.Numeric(14, 2), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_exception_reviews_organization_id", "exception_reviews", ["organization_id"])
        op.create_index("uq_exception_review_key", "exception_reviews", ["organization_id", "key"], unique=True)
    if not inspector.has_table("exception_events"):
        op.create_table(
            "exception_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("key", sa.String(200), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("field", sa.String(40), nullable=False),
            sa.Column("old_value", sa.Text(), nullable=True),
            sa.Column("new_value", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_exception_events_organization_id", "exception_events", ["organization_id"])
        op.create_index("ix_exception_events_key", "exception_events", ["key"])

    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER IF NOT EXISTS exception_events_no_{action.lower()} BEFORE {action} "
                       f"ON exception_events BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY}'); END")
    else:
        op.execute("CREATE OR REPLACE FUNCTION ticketflow_append_only() RETURNS trigger AS $$ "
                   f"BEGIN RAISE EXCEPTION '{APPEND_ONLY}' USING ERRCODE = 'restrict_violation'; END $$ LANGUAGE plpgsql")
        op.execute("DROP TRIGGER IF EXISTS exception_events_append_only ON exception_events")
        op.execute("CREATE TRIGGER exception_events_append_only BEFORE UPDATE OR DELETE ON exception_events "
                   "FOR EACH ROW EXECUTE FUNCTION ticketflow_append_only()")


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
