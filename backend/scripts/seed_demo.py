"""Seed the Quarvera sales demonstration workspace: fictional, isolated, repeatable.

Everything lives in backend/demo_workspace/quarvera-demo/ (its own SQLite database and
its own document folder). The script never opens backend/ticketflow.db or backend/storage,
and --reset deletes only that folder. Every record is entered through the real API, so the
same validation, approval locks, price snapshots and audit trail apply as for a customer.
Tickets are entered by hand (manual intake): no AI provider is called, nothing leaves
this machine. All companies, projects, people and numbers are fictional.

What the workspace shows (the demo script in docs/engineering/commercial-transformation.md):
  - three active projects and one archived, several suppliers and two haulers
  - approved, waiting and flagged delivery tickets; one material approved with no rate
    (cost marked incomplete, never $0); a rate change that leaves approved costs alone
  - hourly trucking with minimums, standby and quarter-hour billing; shifts waiting and disputed
  - a supplier invoice that matches, one that bills a ticket never delivered and extra tons,
    and a trucking invoice that bills more hours than were approved

Usage (from backend/, venv active):
    python scripts/seed_demo.py            # create it (refuses if it exists)
    python scripts/seed_demo.py --reset    # delete the demo folder and create it again
Then run the API and worker with the two exports it prints, and sign in as demo-owner.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEMO_DIR = BACKEND / "demo_workspace" / "quarvera-demo"
DB_PATH = DEMO_DIR / "quarvera-demo.db"
FILES_DIR = DEMO_DIR / "files"
COMPANY = "Ridgeline Civil Constructors (DEMO)"
OWNER = "demo-owner"

PROJECTS = [
    ("25-101", "Route 9 Widening", "Fictional County, IN", "active"),
    ("25-114", "Northpark Distribution Center Sitework", "Fictional Township, IN", "active"),
    ("25-120", "Mill Creek Culvert Replacement", "Fictional City, IN", "active"),
    ("24-088", "Westfield Parking Lot", "Fictional City, IN", "archived"),
]
# (supplier, material, $/ton, project code or None, effective from)
RATES = [
    ("Granite Ridge Aggregates", "No. 57 Stone", "18.50", None, "2026-10-01"),
    ("Granite Ridge Aggregates", "No. 57 Stone", "19.25", None, "2026-11-20"),  # a later increase: approved costs stay put
    ("Granite Ridge Aggregates", "No. 57 Stone", "17.90", "25-101", "2026-10-01"),  # a project agreement
    ("Granite Ridge Aggregates", "No. 53 Crushed Stone", "17.25", None, "2026-10-01"),
    ("Lakeside Sand & Gravel", "Fill Sand", "9.80", None, "2026-10-01"),
]
# Summit Asphalt Supply's #2 Stone has no rate on purpose: approved, but its cost is incomplete.
UNPRICED = ("Summit Asphalt Supply", "#2 Stone")


def tickets() -> list[dict]:
    """Deterministic deliveries: (project, number, date, supplier, material, tons, outcome)."""
    rows = []
    plan = [
        ("25-101", "Granite Ridge Aggregates", "No. 57 Stone", 9, "2026-11-0{d}", 48000),
        ("25-114", "Granite Ridge Aggregates", "No. 53 Crushed Stone", 7, "2026-11-0{d}", 52100),
        ("25-114", "Lakeside Sand & Gravel", "Fill Sand", 5, "2026-11-1{d}", 61300),
        ("25-120", *UNPRICED, 3, "2026-11-1{d}", 70400),
    ]
    tons = [Decimal(v) for v in ("22.45", "23.10", "21.85", "24.20", "22.90", "23.55", "21.40", "24.05", "22.75")]
    for project, supplier, material, count, pattern, first in plan:
        for n in range(count):
            outcome = "verified"
            if n == count - 1:
                outcome = "needs_review"
            if project == "25-114" and material == "Fill Sand" and n == 1:
                outcome = "flagged"
            rows.append(dict(project=project, ticket_number=str(first + n), delivery_date=pattern.format(d=2 + n % 7),
                             supplier=supplier, material_type=material, tonnage=str(tons[n % len(tons)]),
                             job_location=project, outcome=outcome))
    return rows


def guard_paths() -> None:
    # Only ever this one folder, inside the repository's demo area, with "demo" in its name.
    assert DEMO_DIR.resolve().is_relative_to((BACKEND / "demo_workspace").resolve()), DEMO_DIR
    assert "demo" in DB_PATH.name and "demo" in DEMO_DIR.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the demo folder first (only that folder)")
    args = parser.parse_args(argv)
    guard_paths()
    if DEMO_DIR.exists():
        if not args.reset:
            print(f"Demo workspace already exists at {DEMO_DIR}. Use --reset to rebuild it.", file=sys.stderr)
            return 1
        shutil.rmtree(DEMO_DIR)
    FILES_DIR.mkdir(parents=True)
    # Set before the app is imported, so nothing can open the real database or files.
    os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
    os.environ["STORAGE_DIR"] = str(FILES_DIR)
    sys.path.insert(0, str(BACKEND))
    password = seed()
    print(f"\nDemo workspace ready: {COMPANY}")
    print(f"  Sign in as {OWNER} / {password}   (demo database only)")
    print("  Run the API and worker with:")
    print(f"    export DATABASE_URL=sqlite:///{DB_PATH}")
    print(f"    export STORAGE_DIR={FILES_DIR}")
    return 0


def seed() -> str:
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND / "alembic.ini"))
    command.upgrade(config, "head")

    from fastapi.testclient import TestClient

    from app.auth import current_user, password_hash
    from app.db import SessionLocal
    from app.main import app
    from app.models import Organization, User
    from app.routers import api as api_module
    from app.services import jobqueue
    from tests.sample_data import make_page, make_pdf

    password = secrets.token_urlsafe(9)
    with SessionLocal() as db:
        company = Organization(name=COMPANY)
        db.add(company)
        db.flush()
        owner = User(organization_id=company.id, username=OWNER, display_name="Demo Owner", password_hash=password_hash(password),
                     role="owner", active=True)
        db.add(owner)
        db.commit()
        owner_id, company_id = owner.id, company.id

    def no_reader():
        raise RuntimeError("The demo is entered by hand; no reader is ever called.")

    def drain(background, _job_id):
        def run():
            with SessionLocal() as session:
                claimed = jobqueue.claim_next(session)
                if claimed:
                    jobqueue.execute_claimed(session, *claimed, extractor_factory=no_reader)
        background.add_task(run)

    api_module._notify_worker = drain
    app.dependency_overrides[current_user] = lambda: User(id=owner_id, organization_id=company_id, username=OWNER,
                                                          display_name="Demo Owner", password_hash="x", active=True, role="owner",
                                                          must_change_password=False)
    try:
        with TestClient(app) as client:
            client.headers.update({"X-TicketFlow-Request": "1"})
            build(client, make_page, make_pdf)
    finally:
        app.dependency_overrides.clear()
    return password


def ok(response, expected=(200, 201)):
    if response.status_code not in expected:
        raise SystemExit(f"Demo seeding stopped: {response.request.method} {response.request.url.path} -> {response.status_code} {response.text[:300]}")
    return response.json() if response.content else None


def build(client, make_page, make_pdf) -> None:
    projects = {}
    for code, name, location, status in PROJECTS:
        p = ok(client.post("/api/projects", json={"code": code, "name": name, "job_location": location,
                                                 "description": "Fictional demonstration project."}))
        projects[code] = p["id"]

    suppliers, materials = {}, {}
    for supplier, material, *_ in [*RATES, (*UNPRICED, None, None, None)]:
        if supplier not in suppliers:
            suppliers[supplier] = ok(client.post("/api/suppliers", json={"name": supplier}))["id"]
        if material not in materials:
            materials[material] = ok(client.post("/api/materials", json={"name": material}))["id"]
    for supplier, material, price, project, start in RATES:
        ok(client.put("/api/rates", json={"supplier_id": suppliers[supplier], "material_id": materials[material], "unit_price": price,
                                          "project_id": projects[project] if project else None, "effective_from": start}))
    ok(client.post("/api/name-aliases", json={"kind": "material", "alias": "#57 Stone", "target_id": materials["No. 57 Stone"]}), (200, 201, 404, 422))

    # Delivery tickets: scanned four to a page, typed in beside each scan, then checked.
    rows = tickets()
    by_project: dict[str, list[dict]] = {}
    for row in rows:
        by_project.setdefault(row["project"], []).append(row)
    ticket_ids: dict[str, int] = {}
    for code, group in by_project.items():
        for start in range(0, len(group), 4):
            page_rows = group[start:start + 4]
            pdf = make_pdf([make_page([{k: r[k] for k in ("ticket_number", "delivery_date", "material_type", "tonnage", "supplier",
                                                          "job_location")} for r in page_rows])])
            batch = ok(client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(projects[code]), "intake_mode": "manual"},
                                   files=[("files", (f"demo-scan-{code}-{start // 4 + 1}.pdf", pdf, "application/pdf"))]))
            found = sorted(ok(client.get(f"/api/batches/{batch['id']}/tickets")), key=lambda t: (t["source_page"], t["position"]))
            for ticket, row in zip(found, page_rows):
                edited = ok(client.patch(f"/api/tickets/{ticket['id']}", json={
                    k: row[k] for k in ("ticket_number", "delivery_date", "material_type", "tonnage", "supplier", "job_location")}))
                ticket_ids[row["ticket_number"]] = edited["id"]
                if row["outcome"] == "verified":
                    ok(client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]}))
                elif row["outcome"] == "flagged":
                    ok(client.post(f"/api/tickets/{edited['id']}/flag", json={"note": "Tons look high for a tandem; confirm with the scale house."}))

    # Hourly trucking.
    haulers = {}
    for name, trucks, rate in [("Bluegrass Trucking LLC", [("BT-21", "quad_axle"), ("BT-22", "quad_axle"), ("BT-25", "tri_axle")],
                                dict(hourly_rate="105.00", minimum_hours="4", standby_rate="80.00", billing_increment="0.25", increment_rounding="up")),
                               ("Crossroads Hauling", [("CH-7", "tri_axle"), ("CH-9", "tri_axle")], dict(hourly_rate="95.00"))]:
        hauler = ok(client.post("/api/haulers", json={"name": name}))
        haulers[name] = {"id": hauler["id"], "trucks": {}}
        for identifier, kind in trucks:
            truck = ok(client.post("/api/trucks", json={"hauler_id": hauler["id"], "identifier": identifier, "equipment_type": kind}))
            haulers[name]["trucks"][identifier] = truck["id"]
        ok(client.post("/api/hauling-rates", json={"hauler_id": hauler["id"], "effective_from": "2026-10-01", **rate}))

    def shift(hauler, truck, project, day, start, end, approve=None, **extra):
        body = {"project_id": projects[project], "truck_id": haulers[hauler]["trucks"][truck], "start_at": f"{day}T{start}",
                "end_at": f"{day}T{end}", "unpaid_break_minutes": 30, "field_signoff": "R. Alvarez (foreman)", **extra}
        created = ok(client.post("/api/truck-shifts", json=body))
        if approve:
            ok(client.post(f"/api/truck-shifts/{created['id']}/approve", json=approve, headers={"If-Match": created["revision"]}))
        return created

    nine = {"work_hours": "9", "standby_hours": "0"}
    shift("Bluegrass Trucking LLC", "BT-21", "25-101", "2026-11-03", "06:30", "16:00", nine, loads=6)
    shift("Bluegrass Trucking LLC", "BT-22", "25-101", "2026-11-03", "06:30", "16:00", nine, loads=6)
    shift("Bluegrass Trucking LLC", "BT-21", "25-101", "2026-11-04", "07:00", "10:15", {"work_hours": "2.25", "standby_hours": "0.50"},
          standby_hours="0.50", loads=2)  # short day: the 4-hour minimum applies
    shift("Crossroads Hauling", "CH-7", "25-114", "2026-11-05", "06:00", "15:30", nine, loads=7)
    shift("Crossroads Hauling", "CH-9", "25-114", "2026-11-06", "06:00", "15:30", nine, loads=7)
    shift("Bluegrass Trucking LLC", "BT-25", "25-120", "2026-11-12", "06:30", "16:00", loads=5)  # waiting for approval
    disputed = shift("Crossroads Hauling", "CH-7", "25-114", "2026-11-13", "06:00", "17:30", loads=8)
    ok(client.post(f"/api/truck-shifts/{disputed['id']}/dispute", json={"status": "disputed",
                                                                        "reason": "Sheet says 11 hours; the gate log shows the truck left at 15:10."},
                      headers={"If-Match": disputed["revision"]}))

    # Supplier invoices: one matches; one bills a ticket never delivered and more tons.
    stone = [r for r in rows if r["project"] == "25-101" and r["outcome"] == "verified"]
    tons = sum(Decimal(r["tonnage"]) for r in stone)
    ok(client.post("/api/invoice-comparisons", json={
        "project_id": projects["25-101"], "invoice_number": "GRA-110482", "supplier_name": "Granite Ridge Aggregates",
        "material_name": "No. 57 Stone", "date_from": "2026-11-01", "date_to": "2026-11-15", "billed_tons": str(tons),
        "billed_unit_price": "17.9000", "billed_subtotal": str((tons * Decimal("17.90")).quantize(Decimal("0.01"))),
        "ticket_list": "\n".join(f"{r['ticket_number']} {r['tonnage']}" for r in stone)}))
    crushed = [r for r in rows if r["material_type"] == "No. 53 Crushed Stone" and r["outcome"] == "verified"]
    billed = [*crushed, {"ticket_number": "52999", "tonnage": "23.40"}]  # never delivered
    billed_tons = sum(Decimal(r["tonnage"]) for r in billed)
    ok(client.post("/api/invoice-comparisons", json={
        "project_id": projects["25-114"], "invoice_number": "GRA-110517", "supplier_name": "Granite Ridge Aggregates",
        "material_name": "No. 53 Crushed Stone", "date_from": "2026-11-01", "date_to": "2026-11-15", "billed_tons": str(billed_tons),
        "billed_unit_price": "17.2500", "billed_subtotal": str((billed_tons * Decimal("17.25")).quantize(Decimal("0.01"))),
        "ticket_list": "\n".join(f"{r['ticket_number']} {r['tonnage']}" for r in billed)}))

    # Trucking invoices: Bluegrass bills ten hours for a nine-hour day; Crossroads matches.
    ok(client.post("/api/hauling-invoices", json={
        "hauler_id": haulers["Bluegrass Trucking LLC"]["id"], "project_id": projects["25-101"], "invoice_number": "BT-2026-1107",
        "period_from": "2026-11-01", "period_to": "2026-11-07", "lines": [
            {"kind": "hours", "truck_identifier": "BT-21", "work_date": "2026-11-03", "hours": "10", "rate": "105", "amount": "1050.00"},
            {"kind": "hours", "truck_identifier": "BT-22", "work_date": "2026-11-03", "hours": "9", "rate": "105", "amount": "945.00"}]}))
    ok(client.post("/api/hauling-invoices", json={
        "hauler_id": haulers["Crossroads Hauling"]["id"], "project_id": projects["25-114"], "invoice_number": "CH-5531",
        "period_from": "2026-11-01", "period_to": "2026-11-07", "lines": [
            {"kind": "hours", "truck_identifier": "CH-7", "work_date": "2026-11-05", "hours": "9", "rate": "95", "amount": "855.00"},
            {"kind": "hours", "truck_identifier": "CH-9", "work_date": "2026-11-06", "hours": "9", "rate": "95", "amount": "855.00"}]}))

    ok(client.patch(f"/api/projects/{projects['24-088']}", json={"status": "archived"}))
    queue = ok(client.get("/api/review-queue"))
    register = ok(client.get("/api/invoice-comparisons/register"))
    print(f"Seeded {len(rows)} delivery tickets, 7 truck shifts, {register['totals']['invoices']} invoices; "
          f"{queue['summary']['total']} items waiting for review.")


if __name__ == "__main__":
    raise SystemExit(main())
