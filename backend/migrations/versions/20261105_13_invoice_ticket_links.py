"""Human-confirmed links from invoice lines to delivery tickets.

Revision ID: 20261105_13
Revises: 20261105_12

ADDITIVE ONLY: one new table. The partial unique index allows at most one
active link per ticket, enforced by the database under concurrent requests.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_13"
down_revision = "20261105_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("invoice_ticket_links"):
        op.create_table(
            "invoice_ticket_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("comparison_id", sa.Integer(), sa.ForeignKey("invoice_comparisons.id"), nullable=False),
            sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=False),
            sa.Column("listed_number", sa.String(100), nullable=False),
            sa.Column("verified_tons", sa.Numeric(12, 3), nullable=False),
            sa.Column("ticket_verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("confirmed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("released_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("release_reason", sa.String(200), nullable=True),
        )
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("invoice_ticket_links")}
    if "ix_invoice_ticket_links_comparison_id" not in indexes:
        op.create_index("ix_invoice_ticket_links_comparison_id", "invoice_ticket_links", ["comparison_id"])
    if "uq_invoice_ticket_link_active" not in indexes:
        op.create_index(
            "uq_invoice_ticket_link_active", "invoice_ticket_links", ["ticket_id"], unique=True,
            sqlite_where=sa.text("released_at IS NULL"), postgresql_where=sa.text("released_at IS NULL"),
        )


def downgrade() -> None:
    raise RuntimeError("Invoice ticket evidence must not be dropped automatically.")
