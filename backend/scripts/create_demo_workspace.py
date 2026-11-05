"""Create a throwaway owner in a demo database, for the local demo and browser test.

Refuses unless DATABASE_URL points at a file with "demo" in its name, so it can't
touch a real database. Usage: DATABASE_URL=sqlite:////tmp/x/demo.db python scripts/create_demo_workspace.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

USERNAME, PASSWORD = "demo-owner", "demo-password-not-real"


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///") or "demo" not in Path(url.removeprefix("sqlite:///")).name:
        print("Refusing: set DATABASE_URL to a SQLite file with 'demo' in its name.", file=sys.stderr)
        return 1
    from app.auth import password_hash
    from app.db import SessionLocal
    from app.models import Organization, User

    with SessionLocal() as db:
        organization = db.query(Organization).first() or Organization(name="Fictional Contractor")
        db.add(organization)
        db.flush()
        db.add(User(organization_id=organization.id, username=USERNAME, display_name="Demo Owner",
                    password_hash=password_hash(PASSWORD), role="owner"))
        db.commit()
    print(f"Created {USERNAME}. This account is for the demo database only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
