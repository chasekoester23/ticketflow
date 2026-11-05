"""Print the endpoint/role matrix as Markdown. Reads code only; no database."""
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.permissions import PERMISSIONS, ROLES  # noqa: E402
from app.route_inventory import SELF_SERVICE, all_routes  # noqa: E402


def main() -> int:
    print("| Method | Path | Permission | " + " | ".join(r.title() for r in ROLES) + " |")
    print("|---|---|---|" + "---|" * len(ROLES))
    for route in all_routes():
        if route.permissions:
            allowed = [all(role in PERMISSIONS[p] for p in route.permissions) for role in ROLES]
            label = ", ".join(route.permissions)
        elif route.path in SELF_SERVICE or route.path == "/api/auth/me":
            allowed, label = [True] * len(ROLES), "self"
        else:
            allowed, label = [True] * len(ROLES), "signed in (company-scoped read)"
        cells = " | ".join("yes" if a else "—" for a in allowed)
        print(f"| {route.method} | `{route.path}` | {label} | {cells} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
