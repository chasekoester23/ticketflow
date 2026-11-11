"""Row-level security on PostgreSQL: status, enable or disable.

    cd backend
    python scripts/postgres_rls.py status
    python scripts/postgres_rls.py enable     # then set POSTGRES_RLS=on for the API and worker
    python scripts/postgres_rls.py disable    # set POSTGRES_RLS=off first

Run as the role that owns the tables (the one migrations run as), from the deploy step.
Enabling needs permission to create a role. See docs/engineering/postgres.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine  # noqa: E402
from app.services import row_security  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["status", "enable", "disable"])
    args = parser.parse_args()
    if engine.dialect.name != "postgresql":
        print("Row-level security needs PostgreSQL; this database is " + engine.dialect.name + ".", file=sys.stderr)
        return 2
    if args.action == "status":
        with engine.connect() as connection:
            print(json.dumps({**row_security.status(connection), "problems": row_security.problems(connection)}, indent=2))
        return 0
    with engine.begin() as connection:
        tables = getattr(row_security, args.action)(connection)
    print(f"Row-level security {args.action}d on {len(tables)} tables: {', '.join(tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
