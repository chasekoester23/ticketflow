"""Company-defined alternate spellings for suppliers and materials.

Revision ID: 20261105_11
Revises: 20261105_10

ADDITIVE ONLY: creates the name_aliases table. An alias only produces a
suggestion on review. No ticket, rate or price snapshot is touched.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_11"
down_revision = "20261105_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("name_aliases"):
        op.create_table(
            "name_aliases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("alias_text", sa.String(200), nullable=False),
            sa.Column("alias_key", sa.String(200), nullable=False),
            sa.Column("target_name", sa.String(200), nullable=False),
            sa.Column("target_key", sa.String(200), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("removed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("name_aliases")}
    if "ix_name_aliases_organization_id" not in indexes:
        op.create_index("ix_name_aliases_organization_id", "name_aliases", ["organization_id"])
    if "uq_name_alias_active" not in indexes:
        op.create_index(
            "uq_name_alias_active", "name_aliases", ["organization_id", "kind", "alias_key"], unique=True,
            sqlite_where=sa.text("removed_at IS NULL"), postgresql_where=sa.text("removed_at IS NULL"),
        )


def downgrade() -> None:
    raise RuntimeError("Alias history must not be dropped automatically.")
