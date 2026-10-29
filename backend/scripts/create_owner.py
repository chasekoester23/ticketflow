"""Initialize exactly ONE local workspace owner. No public registration.

Run after 'alembic upgrade head':
    cd backend && python scripts/create_owner.py --username owner

Password is entered securely via getpass (not CLI args or source code).
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auth import password_hash  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize a single-owner TicketFlow workspace")
    parser.add_argument("--username", required=True, help="Owner sign-in name")
    args = parser.parse_args()
    username = args.username.strip().casefold()
    if not 3 <= len(username) <= 100 or not username.replace("_", "").replace("-", "").isalnum():
        print("Username must be 3-100 alphanumeric characters (dashes/underscores allowed).", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        if db.scalar(select(User.id).limit(1)) is not None:
            print("An owner already exists. Refusing to add another user to a shared workspace.", file=sys.stderr)
            return 1
        first = getpass.getpass("Create owner password (12+ characters): ")
        second = getpass.getpass("Confirm owner password: ")
        if first != second:
            print("Passwords did not match.", file=sys.stderr)
            return 1
        try:
            encoded = password_hash(first)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        db.add(User(username=username, password_hash=encoded))
        db.commit()
    print("Single-workspace owner created. Start TicketFlow and visit /login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
