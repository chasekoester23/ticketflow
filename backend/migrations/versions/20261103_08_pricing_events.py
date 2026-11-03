"""Attributable history of supplier, material and unit-rate changes.

Revision ID: 20261103_08
Revises: 20261103_07

ADDITIVE ONLY: creates the pricing_events table. No existing table or row is
changed. Rate changes made before this migration have no recorded history;
TicketFlow does not reconstruct or guess them.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261103_08"
down_revision = "20261103_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("pricing_events"):
        op.create_table(
            "pricing_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("supplier_name", sa.String(200), nullable=True),
            sa.Column("material_name", sa.String(200), nullable=True),
            sa.Column("old_value", sa.String(40), nullable=True),
            sa.Column("new_value", sa.String(40), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("pricing_events")}
    if "ix_pricing_events_organization_id" not in indexes:
        op.create_index("ix_pricing_events_organization_id", "pricing_events", ["organization_id"])


def downgrade() -> None:
    raise RuntimeError("Pricing history must not be dropped automatically.")
