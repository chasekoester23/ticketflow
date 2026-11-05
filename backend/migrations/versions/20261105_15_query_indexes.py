"""Indexes for the lookups a person's every edit and approval depends on.

Revision ID: 20261105_15
Revises: 20261105_14

ADDITIVE ONLY: four indexes. No data changes, safe to re-run.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_15"
down_revision = "20261105_14"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_tickets_ticket_number", "tickets", ["ticket_number"]),
    ("ix_tickets_delivery_date", "tickets", ["delivery_date"]),
    ("ix_tickets_batch_status", "tickets", ["batch_id", "status"]),
    ("ix_uploaded_files_batch_id", "uploaded_files", ["batch_id"]),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, columns in INDEXES:
        present = {column["name"] for column in inspector.get_columns(table)}
        if name not in {index["name"] for index in inspector.get_indexes(table)} and set(columns) <= present:
            op.create_index(name, table, columns)


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
