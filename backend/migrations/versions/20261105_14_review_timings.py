"""Opt-in review durations for measuring time saved in a pilot.

Revision ID: 20261105_14
Revises: 20261105_13

ADDITIVE ONLY: one new table, written only when REVIEW_TIMING_ENABLED is on.
Stores no person's identity and no ticket contents.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_14"
down_revision = "20261105_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("review_timings"):
        op.create_table(
            "review_timings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("batches.id"), nullable=False),
            sa.Column("intake_mode", sa.String(20), nullable=False),
            sa.Column("seconds", sa.Numeric(8, 1), nullable=False),
            sa.Column("fields_corrected", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    if "ix_review_timings_organization_id" not in {i["name"] for i in sa.inspect(bind).get_indexes("review_timings")}:
        op.create_index("ix_review_timings_organization_id", "review_timings", ["organization_id"])


def downgrade() -> None:
    raise RuntimeError("Pilot measurements must not be dropped automatically.")
