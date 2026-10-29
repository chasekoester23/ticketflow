"""Single workspace owner login and revocable sessions.

Revision ID: 20261029_02
Revises: 20261029_01

This is an additive migration. It does NOT assign companies/tenants to
existing construction records. True multi-company isolation is future work.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261029_02"
down_revision = "20261029_01"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String(100), unique=True, nullable=False),
            sa.Column("password_hash", sa.String(255), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    if not sa.inspect(bind).has_table("login_sessions"):
        op.create_table(
            "login_sessions",
            sa.Column("token_digest", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_login_sessions_expires_at", "login_sessions", ["expires_at"])


def downgrade():
    raise RuntimeError("Do not drop account and session history automatically.")
