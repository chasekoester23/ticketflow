"""List demo and access requests sent from the website, newest first. Read-only.

Usage (from backend/, venv active, DATABASE_URL pointing at the database to read):
    python scripts/list_access_requests.py            # requests still marked new
    python scripts/list_access_requests.py --all      # every request
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="include requests already handled")
    args = parser.parse_args()
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import AccessRequest

    with SessionLocal() as db:
        query = select(AccessRequest).order_by(AccessRequest.id.desc())
        if not args.all:
            query = query.where(AccessRequest.status == "new")
        rows = list(db.scalars(query))
    if not rows:
        print("No requests.")
        return 0
    for r in rows:
        print(f"#{r.id}  {r.created_at:%Y-%m-%d %H:%M}  {r.status}")
        print(f"  {r.contact_name}, {r.company_name} <{r.email}>" + (f"  {r.phone}" if r.phone else ""))
        if r.monthly_tickets:
            print(f"  Tickets a month: {r.monthly_tickets}")
        if r.message:
            print(f"  {r.message}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
