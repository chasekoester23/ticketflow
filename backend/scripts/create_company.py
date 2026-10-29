"""Provision a separate organization and its first owner from a trusted terminal.

No browser self-signup or cross-company invitation is exposed.
Run ONLY after alembic upgrade head, from backend/:
  python scripts/create_company.py --name "Sample Construction" --username sampleowner

The company is created only when the chosen login is unique and both
password entries match. This never reassigns existing projects or users.
"""
import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import password_hash  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Organization, User  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision a separate company and owner")
    parser.add_argument("--name", required=True, help="Company/workspace name")
    parser.add_argument("--username", required=True, help="Unique owner login")
    args = parser.parse_args()
    name = args.name.strip()
    username = args.username.strip().casefold()
    if not 1 <= len(name) <= 200:
        print("Company name must contain 1-200 characters.", file=sys.stderr)
        return 1
    if not 3 <= len(username) <= 100 or not username.replace("_", "").replace("-", "").isalnum():
        print("Username must be 3-100 letters/numbers (plus dashes/underscores).", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.username == username)) is not None:
            print("That username is already registered.", file=sys.stderr)
            return 1
        password = getpass.getpass("New company owner's password (12+ characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 1
        try:
            encoded = password_hash(password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        try:
            organization = Organization(name=name)
            db.add(organization)
            db.flush()
            db.add(User(organization_id=organization.id, username=username, password_hash=encoded))
            db.commit()
        except IntegrityError:
            db.rollback()
            print("Could not create company owner; username may already exist.", file=sys.stderr)
            return 1
    print("New organization and owner created without changing existing TicketFlow records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
