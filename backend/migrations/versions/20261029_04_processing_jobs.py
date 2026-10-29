"""Persist extraction jobs for restart-safe single-machine processing.

Revision ID: 20261029_04
Revises: 20261029_03

Adds a queue table only. Existing ticket rows, pricing snapshots, audit
history and company ownership are unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261029_04"
down_revision = "20261029_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("processing_jobs"):
        op.create_table(
            "processing_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("batches.id"), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("only_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("claim_token", sa.String(64), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        )
        op.create_index("ix_processing_jobs_batch_id", "processing_jobs", ["batch_id"])
        op.create_index("ix_processing_jobs_status", "processing_jobs", ["status"])

    # Jobs created by the old in-process API could have been interrupted
    # without creating a queue row. Preserve all existing tickets and resume
    # only their unfinished files. Do NOT re-extract any approved ticket.
    # Already terminal successful batches are never touched.
    bind.execute(sa.text("""
        INSERT INTO processing_jobs
            (batch_id, status, only_failed, attempts, created_at)
        SELECT b.id, 'pending', 1, 0, CURRENT_TIMESTAMP
        FROM batches AS b
        WHERE b.status IN ('uploaded', 'processing')
          AND NOT EXISTS (
              SELECT 1 FROM processing_jobs AS j WHERE j.batch_id = b.id
          )
    """))


def downgrade() -> None:
    raise RuntimeError("Do not delete persistent extraction work while jobs may be pending.")
