"""Leases for running jobs, a table of live workers, and a ledger of reader usage.

Revision ID: 20261111_24
Revises: 20261109_23

ADDITIVE ONLY: four nullable columns on processing_jobs and two new tables. No existing
row changes. A job already running keeps no lease, which a database-coordinated worker
treats as lapsed, so it is picked up again exactly as a local worker restart would.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261111_24"
down_revision = "20261109_23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("processing_jobs"):
        existing = {column["name"] for column in inspector.get_columns("processing_jobs")}
        for column in (sa.Column("lease_owner", sa.String(64), nullable=True),
                       sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
                       sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
                       sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)):
            if column.name not in existing:
                op.add_column("processing_jobs", column)
    if not inspector.has_table("worker_heartbeats"):
        op.create_table(
            "worker_heartbeats",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("host", sa.String(255), nullable=False),
            sa.Column("pid", sa.Integer(), nullable=False),
            sa.Column("mode", sa.String(20), nullable=False),
            sa.Column("version", sa.String(64), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("current_job_id", sa.Integer(), nullable=True),
        )
        op.create_index("ix_worker_heartbeats_last_seen_at", "worker_heartbeats", ["last_seen_at"])
    if not inspector.has_table("extraction_usage"):
        op.create_table(
            "extraction_usage",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("batches.id"), nullable=True),
            sa.Column("ticket_id", sa.Integer(), nullable=True),
            sa.Column("model", sa.String(100), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("prompt_tokens", sa.Integer(), nullable=True),
            sa.Column("output_tokens", sa.Integer(), nullable=True),
            sa.Column("thinking_tokens", sa.Integer(), nullable=True),
            sa.Column("seconds", sa.Numeric(10, 3), nullable=True),
            sa.Column("outcome", sa.String(30), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_extraction_usage_batch_id", "extraction_usage", ["batch_id"])
        op.create_index("ix_extraction_usage_org_created", "extraction_usage", ["organization_id", "created_at"])


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
