"""Find copies of one scanned image without walking the company's tickets.

Revision ID: 20261106_22
Revises: 20261106_21

ADDITIVE ONLY: one index. Approval and every ticket read check for copies of the same
scan. Without a company-and-image index, SQLite answered that by reading every ticket
the company has: about 2 seconds per check at 1,000,000 tickets.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261106_22"
down_revision = "20261106_21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("tickets"):
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("tickets")}
    if not {"organization_id", "crop_sha256"} <= columns:
        return  # Nothing to index in a schema without these columns.
    if "ix_tickets_org_image" not in {index["name"] for index in inspector.get_indexes("tickets")}:
        op.create_index("ix_tickets_org_image", "tickets", ["organization_id", "crop_sha256"])


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported; restore from your backup instead.")
