"""Check the demo shows exactly the results worked out independently when it was seeded.

scripts/seed_demo.py works out, from its own price lists and planned deliveries, what
every approved total should be: tons and cubic yards (never added together), material
cost, per-ton and per-load hauling, what has no rate, and how each per-ton and per-load
hauler invoice should come out. This script asks Quarvera for the same figures through
its API, read-only, and compares them.

Reads only the demo workspace (backend/demo_workspace/quarvera-demo/). It sends GET
requests and nothing else, so it never changes a record.

Usage (from backend/, venv active):
    python scripts/verify_demo.py
Exit code 0 when every figure matches, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEMO_DIR = BACKEND / "demo_workspace" / "quarvera-demo"
DB_PATH = DEMO_DIR / "quarvera-demo.db"
EXPECTED_PATH = DEMO_DIR / "expected-results.json"


def main() -> int:
    if not EXPECTED_PATH.exists():
        print(f"No expected results at {EXPECTED_PATH}. Seed the demo first: python scripts/seed_demo.py --reset", file=sys.stderr)
        return 1
    os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
    os.environ["STORAGE_DIR"] = str(DEMO_DIR / "files")
    for limit in ("UPLOADS", "EXPORTS", "REPORTS"):
        os.environ[f"RATE_LIMIT_{limit}_PER_MINUTE"] = "0"
    sys.path.insert(0, str(BACKEND))
    expected = json.loads(EXPECTED_PATH.read_text())

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.auth import current_user
    from app.db import SessionLocal
    from app.main import app
    from app.models import User

    with SessionLocal() as db:
        owner = db.scalar(select(User).where(User.username == "demo-owner"))
        if owner is None:
            print("The demo owner isn't in this database: is it the demo workspace?", file=sys.stderr)
            return 1
        owner_id, company_id = owner.id, owner.organization_id
    app.dependency_overrides[current_user] = lambda: User(id=owner_id, organization_id=company_id, username="demo-owner",
                                                          display_name="Demo Owner", password_hash="x", active=True, role="owner",
                                                          must_change_password=False)
    results: list[tuple[str, str, str]] = []

    def check(what: str, want, got) -> None:
        results.append((what, str(want), str(got)))

    try:
        with TestClient(app) as client:
            client.headers.update({"X-TicketFlow-Request": "1"})

            def get(path, **params):
                response = client.get(path, params=params)
                response.raise_for_status()
                return response.json()

            report = get("/api/reports/summary", period="month")
            check("Approved tickets", expected["approved_tickets"], report["material"]["tickets"])
            check("Approved tons (tons only)", expected["tons"], report["material"]["tons"])
            check("Other units, never in tons", [(o["unit"], o["quantity"]) for o in expected["other_units"]],
                  [(o["unit"], o["quantity"]) for o in report["material"].get("other_units", [])])
            check("Material cost", expected["material_cost"], report["costs"]["material"])
            check("Approved without a material rate", expected["unpriced"], report["costs"]["unpriced"])
            methods = {m["method"]: m["cost"] for m in report["trucking"]["billing_methods"]}
            check("Hauling billed per ton", expected["per_ton_hauling"], methods.get("per_ton", "0.00"))
            check("Hauling billed per load", expected["per_load_hauling"], methods.get("per_load", "0.00"))
            check("Approved hauling with no rate", expected["hauling_rate_missing"], report["trucking"]["rate_missing"])
            directory = {p["code"]: p for p in get("/api/projects/directory")}
            for code, cost in expected["material_by_project"].items():
                check(f"Material cost, project {code}", cost, directory[code]["material_cost"])
            dashboard = get("/api/dashboard")
            check("Overview tons agree with Analytics", report["material"]["tons"], dashboard["total_tons"])
            check("Overview material cost agrees with Analytics", report["costs"]["material"], dashboard["total_cost"])

            invoices = {i["invoice_number"]: i for i in get("/api/hauling-invoices")}
            for number, want in expected["hauler_invoices"].items():
                got = invoices.get(number)
                if got is None:
                    check(f"{number} ({want['scenario']})", "present", "missing")
                    continue
                check(f"{number} difference ({want['scenario']})", want["difference"], got["difference"])
                check(f"{number} exceptions", want["exceptions"], got["exceptions"])
                check(f"{number} approved deliveries not billed in its period", want["unbilled_in_period"], got.get("unbilled_in_period", 0))
    finally:
        app.dependency_overrides.clear()

    width = max(len(r[0]) for r in results)
    failed = 0
    for what, want, got in results:
        same = want == got
        failed += not same
        print(f"{'PASS' if same else 'FAIL'}  {what:<{width}}  expected {want}" + ("" if same else f", Quarvera shows {got}"))
    print(f"\n{len(results) - failed} of {len(results)} figures match the independently calculated results.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
