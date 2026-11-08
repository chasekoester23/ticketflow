"""Make one decision at a time, for decisions a unique index can't express.

"Is another line already covering these dates?" can't be a constraint. So people
making the same kind of decision take turns: the first finishes and commits, then
the next one's check sees the result. Held until the transaction ends, and released
automatically if it fails.
"""
from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session


def take_turn(db: Session, *key) -> None:
    """Wait until nobody else holding this key is mid-decision. Call before the check."""
    if db.get_bind().dialect.name == "postgresql":
        name = "|".join(str(part) for part in key)
        db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(name, 0))))
    else:
        # SQLite has one writer at a time. A write takes that turn immediately, and
        # any later read in this transaction sees everything committed before it.
        db.execute(text("UPDATE organizations SET id = id WHERE id = (SELECT MIN(id) FROM organizations)"))
