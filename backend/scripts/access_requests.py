"""See and act on requests from the public sign-up page, from a trusted terminal.

Run after alembic upgrade head, from backend/:
  python scripts/access_requests.py list [--status new]
  python scripts/access_requests.py contacted 12
  python scripts/access_requests.py declined 12
  python scripts/access_requests.py provision 12 --username sampleowner

provision creates a separate company and its owner, and prints a temporary password
once. The owner has to replace it at first sign-in.
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import AccessRequest  # noqa: E402
from app.services.workspaces import ProvisioningError, provision_request  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Requests for a TicketFlow workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=["new", "contacted", "provisioned", "declined"])
    for name in ("contacted", "declined"):
        commands.add_parser(name).add_argument("id", type=int)
    provision = commands.add_parser("provision")
    provision.add_argument("id", type=int)
    provision.add_argument("--username", required=True, help="Unique login for the new company's owner")
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.command == "list":
            query = select(AccessRequest).order_by(AccessRequest.id)
            if args.status:
                query = query.where(AccessRequest.status == args.status)
            for item in db.scalars(query):
                print(f"#{item.id} [{item.status}] {item.created_at:%Y-%m-%d} {item.company_name} | {item.contact_name} | "
                      f"{item.email} | {item.phone or '-'} | {item.monthly_tickets or '-'} | {item.message or '-'}")
            return 0
        if args.command == "provision":
            try:
                organization, owner, temporary = provision_request(db, args.id, args.username)
                db.commit()
            except ProvisioningError as exc:
                db.rollback()
                print(str(exc), file=sys.stderr)
                return 1
            except IntegrityError:
                db.rollback()
                print("That username is already registered.", file=sys.stderr)
                return 1
            print(f"Created {organization.name} with owner {owner.username}.")
            print(f"Temporary password, shown once: {temporary}")
            return 0
        item = db.get(AccessRequest, args.id)
        if item is None or item.status not in {"new", "contacted"}:
            print("No waiting request has that number.", file=sys.stderr)
            return 1
        item.status = args.command
        db.commit()
    print(f"Request #{args.id} marked {args.command}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
