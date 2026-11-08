"""Fill a demo database with thousands of fictional records and planted mistakes.

Usage (from backend/):
    DATABASE_URL=sqlite:////tmp/x/demo.db python -m alembic upgrade head
    DATABASE_URL=sqlite:////tmp/x/demo.db python scripts/create_demo_workspace.py
    DATABASE_URL=sqlite:////tmp/x/demo.db STORAGE_DIR=/tmp/x/storage python scripts/make_synthetic_dataset.py --tickets 5000

Refuses unless DATABASE_URL points at a file with "demo" in its name, so it can't
touch a real database. All fictional. No AI and no network.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickets", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///") or "demo" not in Path(url.removeprefix("sqlite:///")).name:
        print("Refusing: set DATABASE_URL to a SQLite file with 'demo' in its name.", file=sys.stderr)
        return 1

    from app.db import SessionLocal
    from app.models import Organization
    from app.services import storage
    from tests.sample_data import make_ticket_image
    from tests.scan_conditions import STYLES
    from tests.synthetic_dataset import build, plant_invoices

    store = storage.current()
    paths = []
    base = dict(ticket_number="100482", delivery_date="2026-04-15", material_type="No. 57 Stone", tonnage="22.40",
                supplier="Example Aggregates", job_location="Fictional Site")
    for name, style in STYLES.items():
        paths.append(store.save_png(f"crops/synthetic/{name}.png", style(make_ticket_image(base).resize((680, 880))), replace=True))
    with SessionLocal() as db:
        organization = db.query(Organization).first()
        if organization is None:
            print("Create the demo workspace first: scripts/create_demo_workspace.py", file=sys.stderr)
            return 1
        key = build(db, organization.id, tickets=args.tickets, seed=args.seed, crop_paths=paths)
        plant_invoices(db, organization.id, key)
    print(f"Added {key.tickets} fictional tickets ({key.misspelled_tickets} misspelled supplier names) "
          f"and {len(key.invoices)} invoice lines with planted mistakes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
