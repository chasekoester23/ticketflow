"""Scan manifest: every physical slot on record, plus expected ticket counts.

Revision ID: 20261105_10
Revises: 20261104_09

ADDITIVE ONLY: creates scan_slots and scan_events and adds one nullable column
to batches using SQLite's native ADD COLUMN (no table rebuild). No existing
ticket, approval or price is changed. Batches processed earlier have no slots
until someone builds the scan check, which also changes no tickets.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_10"
down_revision = "20261104_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "expected_ticket_count" not in {c["name"] for c in inspector.get_columns("batches")}:
        op.execute("ALTER TABLE batches ADD COLUMN expected_ticket_count INTEGER")
    if not inspector.has_table("scan_slots"):
        op.create_table(
            "scan_slots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("batches.id"), nullable=False),
            sa.Column("file_id", sa.Integer(), sa.ForeignKey("uploaded_files.id"), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("crop_box", sa.JSON(), nullable=False),
            sa.Column("crop_path", sa.String(500), nullable=False),
            sa.Column("crop_sha256", sa.String(64), nullable=False),
            sa.Column("ink_fraction", sa.Float(), nullable=False),
            sa.Column("detection", sa.String(20), nullable=False),
            sa.Column("decision", sa.String(20), nullable=True),
            sa.Column("decided_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rotation", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("file_id", "page", "position", name="uq_scan_slot_position"),
        )
    if not sa.inspect(bind).has_table("scan_events"):
        op.create_table(
            "scan_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("batches.id"), nullable=False),
            sa.Column("slot_id", sa.Integer(), sa.ForeignKey("scan_slots.id"), nullable=True),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("old_value", sa.String(100), nullable=True),
            sa.Column("new_value", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    for table, columns in (("scan_slots", ("batch_id", "file_id")), ("scan_events", ("batch_id", "slot_id"))):
        existing = {i["name"] for i in sa.inspect(bind).get_indexes(table)}
        for column in columns:
            name = f"ix_{table}_{column}"
            if name not in existing:
                op.create_index(name, table, [column])


def downgrade() -> None:
    raise RuntimeError("Scan records and decisions must not be dropped automatically.")
