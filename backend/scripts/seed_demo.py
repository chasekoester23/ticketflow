"""Seed the Quarvera sales demonstration workspace: fictional, isolated, repeatable.

Everything lives in backend/demo_workspace/quarvera-demo/ (its own SQLite database and
its own document folder). The script never opens backend/ticketflow.db or backend/storage,
and --reset deletes only that folder. Every record is entered through the real API, so the
same validation, approval locks, price snapshots and audit trail apply as for a customer.
Tickets are entered by hand (manual intake): no AI provider is called, nothing leaves
this machine. All companies, projects, people and numbers are fictional. Every figure
Quarvera shows is added up from these records; nothing is a stored total.

Ridgeline Civil Constructors, a midsized sitework contractor, September to November 2026:
  - 6 active projects and 1 archived; 12 material suppliers; 8 haulers
  - about 2,500 delivery tickets: most approved, recent ones waiting, a few flagged;
    one supplier's material has no rate (cost marked incomplete, never $0); a mid-period
    price increase and project agreements; one supplier's delivered price includes freight
  - trucking by the hour (about 350 shifts: approved, waiting, disputed), per ton and per load
  - about 45 invoices: most match; one bills a ticket never delivered, one extra tons,
    one a higher rate, two give totals only, one can't be compared (no rate); trucking
    invoices with extra hours, a day with no shift and a repeated line; November unbilled

Usage (from backend/, venv active):
    python scripts/seed_demo.py            # create it (refuses if it exists)
    python scripts/seed_demo.py --reset    # delete the demo folder and create it again
Then run the API and worker with the two exports it prints, and sign in as demo-owner.
"""
from __future__ import annotations

import argparse
import os
import random
import secrets
import shutil
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEMO_DIR = BACKEND / "demo_workspace" / "quarvera-demo"
DB_PATH = DEMO_DIR / "quarvera-demo.db"
FILES_DIR = DEMO_DIR / "files"
COMPANY = "Ridgeline Civil Constructors (DEMO)"
OWNER = "demo-owner"
SEED = 2026
START, END = date(2026, 9, 1), date(2026, 11, 20)
TICKETS = 2500

# code, name, location, share of deliveries, suppliers used
PROJECTS = [
    ("25-101", "Route 9 Widening", "Fictional County, IN", 0.24, ["Granite Ridge Aggregates", "Blue Valley Quarry", "Lakeside Sand & Gravel", "Redbud Recycled Base", "Oakmont Quarries"]),
    ("25-114", "Northpark Distribution Center Sitework", "Fictional Township, IN", 0.22, ["Granite Ridge Aggregates", "Twin Rivers Materials", "Central Indiana Fill", "Prairie Stone Co.", "Redbud Recycled Base"]),
    ("25-120", "Mill Creek Culvert Replacement", "Fictional City, IN", 0.10, ["Oakmont Quarries", "Prairie Stone Co.", "Summit Asphalt Supply", "Northline Aggregates"]),
    ("25-133", "Riverside Elementary Site Package", "Fictional City, IN", 0.14, ["Lakeside Sand & Gravel", "Westpoint Rock Products", "Twin Rivers Materials", "Northline Aggregates"]),
    ("25-140", "Harbor Road Utility Relocation", "Fictional County, IN", 0.16, ["Hillcrest Aggregates", "Lakeside Sand & Gravel", "Blue Valley Quarry", "Central Indiana Fill"]),
    ("25-152", "Eastgate Retail Pad Grading", "Fictional Township, IN", 0.14, ["Central Indiana Fill", "Granite Ridge Aggregates", "Hillcrest Aggregates", "Redbud Recycled Base"]),
]
ARCHIVED = ("24-088", "Westfield Parking Lot", "Fictional City, IN")
# supplier -> [(material, company $/ton or None for no rate, freight)]
SUPPLIERS = {
    "Granite Ridge Aggregates": [("No. 57 Stone", "26.50", None), ("No. 53 Crushed Stone", "24.70", None), ("No. 2 Stone", "23.90", None)],
    "Lakeside Sand & Gravel": [("Fill Sand", "14.50", "separate"), ("Concrete Sand", "21.00", "separate")],
    "Summit Asphalt Supply": [("No. 2 Stone", None, None)],  # no rate on file: approved, cost incomplete
    "Blue Valley Quarry": [("No. 57 Stone", "27.10", None), ("Surge Stone", "29.40", None)],
    "Prairie Stone Co.": [("No. 53 Crushed Stone", "24.40", None), ("Rip Rap Class 1", "36.50", None)],
    "Twin Rivers Materials": [("No. 57 Stone", "33.70", "included"), ("Structural Fill", "22.60", "included")],  # delivered prices
    "Northline Aggregates": [("No. 8 Stone", "30.20", None)],
    "Redbud Recycled Base": [("Recycled Concrete Base", "17.40", "separate")],
    "Central Indiana Fill": [("Structural Fill", "13.90", "separate"), ("Screened Topsoil", "18.00", "separate")],
    "Oakmont Quarries": [("Rip Rap Class 2", "39.80", None), ("No. 2 Stone", "24.10", None)],
    "Westpoint Rock Products": [("Pea Gravel", "31.50", None)],
    "Hillcrest Aggregates": [("No. 53 Crushed Stone", "25.20", None)],
}
# Extra rates: (supplier, material, $/ton, project code or None, from, freight)
EXTRA_RATES = [
    ("Granite Ridge Aggregates", "No. 57 Stone", "27.20", None, "2026-11-01", None),  # an increase: approved costs stay put
    ("Granite Ridge Aggregates", "No. 57 Stone", "25.80", "25-101", "2026-09-01", None),  # a project agreement
    ("Hillcrest Aggregates", "No. 53 Crushed Stone", "24.40", "25-140", "2026-09-01", None),
]
# name, method, rate, trucks, extras
HAULERS = [
    ("Bluegrass Trucking LLC", "hourly", "105.00", [("BT-21", "quad_axle"), ("BT-22", "quad_axle"), ("BT-25", "tri_axle")],
     dict(minimum_hours="4", standby_rate="80.00", billing_increment="0.25", increment_rounding="up")),
    ("Crossroads Hauling", "hourly", "95.00", [("CH-7", "tri_axle"), ("CH-9", "tri_axle"), ("CH-12", "tandem")], {}),
    ("Mapleton Trucking", "hourly", "110.00", [("MT-3", "quad_axle"), ("MT-5", "quad_axle")], dict(minimum_hours="4")),
    ("Tri-County Hauling", "hourly", "98.00", [("TC-14", "tri_axle"), ("TC-16", "tri_axle")], dict(standby_rate="75.00")),
    ("Ironwood Haul", "per_ton", "4.25", [("IH-2", "semi_end_dump"), ("IH-4", "semi_end_dump")], {}),
    ("River City Transport", "per_ton", "3.90", [("RC-8", "semi_end_dump")], {}),
    ("Lincoln Dump Service", "per_load", "88.00", [("LD-1", "tri_axle"), ("LD-3", "tri_axle")], {}),
    ("Summit Logistics", "per_load", "92.00", [("SL-6", "quad_axle")], {}),  # rate for 25-152 only (see below)
]
UNIT_HAULERS = ["Ironwood Haul", "River City Transport", "Lincoln Dump Service", "Summit Logistics"]
HOURLY = [h for h in HAULERS if h[1] == "hourly"]


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
    # This process alone enters a few months of records in minutes; the per-person limits that
    # protect a live server would stop it. Only this offline seeding process is affected.
    for limit in ("UPLOADS", "EXPORTS", "REPORTS"):
        os.environ[f"RATE_LIMIT_{limit}_PER_MINUTE"] = "0"
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


def workdays() -> list[date]:
    days, d = [], START
    while d <= END:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def plan_tickets(rng: random.Random, days: list[date]) -> list[dict]:
    """Deterministic deliveries. Recent days are still being reviewed, as on a live job."""
    recent = set(days[-6:])
    rows, number = [], {s: 40000 + i * 7000 for i, s in enumerate(SUPPLIERS)}
    weights = [p[3] for p in PROJECTS]
    for _ in range(TICKETS):
        code, _name, _loc, _w, suppliers = rng.choices(PROJECTS, weights=weights)[0]
        supplier = rng.choice(suppliers)
        if supplier == "Summit Asphalt Supply" and rng.random() < 0.6:
            supplier = rng.choice([s for s in suppliers if s != "Summit Asphalt Supply"])  # a handful only
        material, _price, freight = rng.choice(SUPPLIERS[supplier])
        day = rng.choice(days)
        number[supplier] += rng.randint(1, 3)
        hauler = None
        if freight != "included" and rng.random() < 0.28:
            hauler = rng.choice(UNIT_HAULERS[:3]) if code != "25-152" or rng.random() < 0.4 else "Summit Logistics"
            if rng.random() < 0.01:
                hauler = "Summit Logistics"  # off its project: no rate, hauling missing (shown, never $0)
        outcome = "verified"
        if day in recent:
            outcome = "needs_review" if rng.random() < 0.55 else "verified"
        elif day >= days[-15] and rng.random() < 0.04:
            outcome = "flagged"
        rows.append(dict(project=code, ticket_number=str(number[supplier]), delivery_date=day.isoformat(), supplier=supplier,
                         material_type=material, tonnage=f"{rng.uniform(17.5, 25.5):.1f}", job_location=code, hauler=hauler,
                         truck=None, outcome=outcome))
    return rows


def build(client, make_page, make_pdf) -> None:
    rng = random.Random(SEED)
    days = workdays()

    projects = {}
    for code, name, location, *_ in [*PROJECTS, ARCHIVED]:
        projects[code] = ok(client.post("/api/projects", json={"code": code, "name": name, "job_location": location,
                                                               "description": "Fictional demonstration project."}))["id"]

    suppliers, materials = {}, {}
    for supplier, items in SUPPLIERS.items():
        suppliers[supplier] = ok(client.post("/api/suppliers", json={"name": supplier, "contact": f"Dispatch · {supplier.split()[0].lower()}@example.com"}))["id"]
        for material, price, freight in items:
            if material not in materials:
                materials[material] = ok(client.post("/api/materials", json={"name": material}))["id"]
            if price:
                body = {"supplier_id": suppliers[supplier], "material_id": materials[material], "unit_price": price, "effective_from": "2026-08-01"}
                if freight:
                    body["freight"] = freight
                ok(client.put("/api/rates", json=body))
    for supplier, material, price, project, start, freight in EXTRA_RATES:
        body = {"supplier_id": suppliers[supplier], "material_id": materials[material], "unit_price": price,
                "project_id": projects[project] if project else None, "effective_from": start}
        if freight:
            body["freight"] = freight
        ok(client.put("/api/rates", json=body))
    ok(client.post("/api/name-aliases", json={"kind": "material", "alias": "#57 Stone", "target_id": materials["No. 57 Stone"]}), (200, 201, 404, 409, 422))

    haulers = {}
    for name, method, rate, trucks, extra in HAULERS:
        hauler = ok(client.post("/api/haulers", json={"name": name, "contact": f"Dispatch · {name.split()[0].lower()}@example.com"}))
        haulers[name] = {"id": hauler["id"], "trucks": {}, "method": method, "rate": rate}
        for identifier, kind in trucks:
            haulers[name]["trucks"][identifier] = ok(client.post("/api/trucks", json={"hauler_id": hauler["id"], "identifier": identifier,
                                                                                     "equipment_type": kind}))["id"]
        body = {"hauler_id": hauler["id"], "billing_method": method, "effective_from": "2026-08-01"}
        if method == "hourly":
            body.update(hourly_rate=rate, **extra)
        else:
            body.update(unit_rate=rate)
            if name == "Summit Logistics":
                body["project_id"] = projects["25-152"]
        ok(client.post("/api/hauling-rates", json=body))
    ok(client.post("/api/hauling-rates", json={"hauler_id": haulers["River City Transport"]["id"], "billing_method": "per_ton",
                                               "unit_rate": "3.60", "project_id": projects["25-114"], "effective_from": "2026-08-01"}))

    # Delivery tickets: scanned four to a page, a batch per project per week, typed in beside each scan, then checked.
    rows = plan_tickets(rng, days)
    for row in rows:
        if row["hauler"]:
            row["truck"] = rng.choice(list(haulers[row["hauler"]]["trucks"]))
    batches: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r["project"], r["delivery_date"])):
        week = date.fromisoformat(row["delivery_date"]).isocalendar()[1]
        batches[(row["project"], str(week))].append(row)
    approved: list[dict] = []
    for n, ((code, week), group) in enumerate(sorted(batches.items())):
        pages = [make_page([{k: r[k] for k in ("ticket_number", "delivery_date", "material_type", "tonnage", "supplier", "job_location")}
                            for r in group[i:i + 4]]) for i in range(0, len(group), 4)]
        batch = ok(client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(projects[code]), "intake_mode": "manual"},
                               files=[("files", (f"demo-scans-{code}-week{week}.pdf", make_pdf(pages), "application/pdf"))]))
        found = sorted(ok(client.get(f"/api/batches/{batch['id']}/tickets")), key=lambda t: (t["source_page"], t["position"]))
        for ticket, row in zip(found, group):
            body = {k: row[k] for k in ("ticket_number", "delivery_date", "material_type", "tonnage", "supplier", "job_location")}
            if row["hauler"]:
                body.update(hauler_id=haulers[row["hauler"]]["id"], truck_number=row["truck"])
            edited = ok(client.patch(f"/api/tickets/{ticket['id']}", json=body))
            if row["outcome"] == "verified":
                done = ok(client.post(f"/api/tickets/{edited['id']}/verify", json={"confirm_duplicate": True}, headers={"If-Match": edited["revision"]}))
                approved.append({**row, "id": done["id"], "cost": done.get("cost"), "unit_price": done.get("unit_price")})
            elif row["outcome"] == "flagged":
                ok(client.post(f"/api/tickets/{edited['id']}/flag", json={"note": "Tons look high for this truck; confirm with the scale house."}))
        if n % 10 == 0:
            print(f"  deliveries: batch {n + 1} of {len(batches)}")

    # Hourly trucking: about six hired trucks a working day, across the jobs.
    shifts_by: dict[tuple[str, str, str], list[dict]] = defaultdict(list)  # (hauler, project, month) -> approved shifts
    count = 0
    for day in days:
        used: set[str] = set()
        for _ in range(rng.choice([5, 6, 6, 7])):
            name = rng.choice(HOURLY)[0]
            code = rng.choices(PROJECTS, weights=[p[3] for p in PROJECTS])[0][0]
            free = [t for t in haulers[name]["trucks"] if t not in used]
            if not free:
                continue
            truck = rng.choice(free)
            used.add(truck)
            start_h = rng.choice(["06:00", "06:30", "07:00", "07:30"])
            end_h = rng.choice(["14:30", "15:00", "15:30", "16:00", "16:30", "17:00"]) if rng.random() > 0.05 else "10:15"
            # Standby is paid only where the agreement has a standby rate (Quarvera refuses it otherwise).
            standby = rng.choice(["0", "0", "0", "0.50"]) if "standby_rate" in next(h for h in HAULERS if h[0] == name)[4] else "0"
            body = {"project_id": projects[code], "truck_id": haulers[name]["trucks"][truck], "start_at": f"{day}T{start_h}",
                    "end_at": f"{day}T{end_h}", "unpaid_break_minutes": 30 if end_h != "10:15" else 0, "standby_hours": standby,
                    "loads": rng.randint(4, 9), "field_signoff": rng.choice(["R. Alvarez (foreman)", "J. Okafor (super)", "M. Chen (foreman)"]),
                    "driver_name": rng.choice(["D. Price", "L. Ortiz", "K. Brooks", "T. Nguyen", "S. Patel", "B. Wells"])}
            shift = client.post("/api/truck-shifts", json=body)
            if shift.status_code == 409:
                continue  # the same truck already has a shift that day
            shift = ok(shift)
            count += 1
            if day >= days[-4]:
                continue  # recent: waiting for approval
            if count % 117 == 0:
                ok(client.post(f"/api/truck-shifts/{shift['id']}/dispute", json={"status": "disputed",
                   "reason": "Sheet says 11 hours; the gate log shows the truck left at 15:10."}, headers={"If-Match": shift["revision"]}))
                continue
            done = ok(client.post(f"/api/truck-shifts/{shift['id']}/approve", json={"work_hours": shift["work_hours"],
                                  "standby_hours": shift.get("standby_hours", "0")}, headers={"If-Match": shift["revision"]}))
            shifts_by[(name, code, day.isoformat()[:7])].append({**done, "lines": shift["cost_lines"]})
    print(f"  trucking: {count} shifts")

    # Supplier invoices for September and October: one per supplier and project with the most volume.
    by_invoice: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in approved:
        month = row["delivery_date"][:7]
        if month in ("2026-09", "2026-10"):
            by_invoice[(row["project"], row["supplier"], month)].append(row)
    chosen = sorted(by_invoice.items(), key=lambda kv: -len(kv[1]))[:32]
    scenarios = {0: "extra_ticket", 3: "extra_tons", 5: "higher_rate", 7: "totals_only", 11: "totals_only"}
    summit = next((k for k in by_invoice if k[1] == "Summit Asphalt Supply"), None)
    if summit and summit not in dict(chosen):
        chosen.append((summit, by_invoice[summit]))
    invoices = 0
    for n, ((code, supplier, month), group) in enumerate(chosen):
        number = f"{''.join(w[0] for w in supplier.split()[:2]).upper()}-{month.replace('-', '')}-{projects[code]:02d}{n:02d}"
        scenario = scenarios.get(n, "match")
        by_material: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in group:
            by_material[(row["material_type"], row["unit_price"] or "")].append(row)
        for (material, locked), items in by_material.items():  # noqa: B007
            listed = [(r["ticket_number"], Decimal(r["tonnage"])) for r in items]
            tons = sum((t for _, t in listed), Decimal(0))
            # Billed at the rate locked on the tickets; with no rate on file, at the supplier's own
            # price, which Quarvera can't compare yet.
            price = Decimal(locked) if locked else Decimal("24.20")
            subtotal = cents(tons * price)
            if scenario == "extra_ticket":
                listed.append(("99" + listed[0][0][-4:], Decimal("22.80")))  # a ticket never delivered
                tons += Decimal("22.80")
                subtotal = cents(subtotal + Decimal("22.80") * price)
            elif scenario == "extra_tons":
                first = listed[0]
                listed[0] = (first[0], first[1] + Decimal("2.50"))
                tons += Decimal("2.50")
                subtotal = cents(subtotal + Decimal("2.50") * price)
            elif scenario == "higher_rate":
                price = price + Decimal("0.75")
                subtotal = cents(tons * price)
            last_day = "30" if month.endswith("09") else "31"
            body = {"project_id": projects[code], "invoice_number": number, "supplier_name": supplier, "material_name": material,
                    "date_from": f"{month}-01", "date_to": f"{month}-{last_day}", "billed_tons": str(tons.quantize(Decimal("0.001"))),
                    "billed_unit_price": str(price.quantize(Decimal("0.0001"))), "billed_subtotal": str(cents(subtotal)),
                    "ticket_list": "" if scenario == "totals_only" else "\n".join(f"{num} {t}" for num, t in listed)}
            ok(client.post("/api/invoice-comparisons", json=body))
        invoices += 1

    # Trucking invoices for hourly hauling, September and October.
    hauling = sorted(shifts_by.items(), key=lambda kv: -len(kv[1]))
    hauling = [kv for kv in hauling if kv[0][2] in ("2026-09", "2026-10")][:13]
    for n, ((name, code, month), shifts) in enumerate(hauling):
        kind_of = lambda label: "standby" if label.startswith("Standby") else "minimum" if label.startswith("Minimum") else "hours"  # noqa: E731
        lines = [{"kind": kind_of(cl["label"]), "truck_identifier": s["truck"], "work_date": s["work_date"], "hours": cl["hours"],
                  "rate": cl["rate"], "amount": cl["amount"]}
                 for s in sorted(shifts, key=lambda s: s["work_date"]) for cl in s["lines"]]
        if n == 0:  # an hour more than approved on one day
            first = lines[0]
            hours = Decimal(first["hours"]) + 1
            lines[0] = {**first, "hours": str(hours), "amount": str(cents(hours * Decimal(first["rate"])))}
        elif n == 2:  # a day with no shift on record
            lines.append({**lines[-1], "work_date": f"{month}-28" if month.endswith("09") else f"{month}-29"})
        elif n == 4:  # the same line twice
            lines.append(dict(lines[0]))
        last_day = "30" if month.endswith("09") else "31"
        ok(client.post("/api/hauling-invoices", json={"hauler_id": haulers[name]["id"], "project_id": projects[code],
                                                      "invoice_number": f"{name.split()[0][:3].upper()}-{month.replace('-', '')}-{n:02d}",
                                                      "period_from": f"{month}-01", "period_to": f"{month}-{last_day}", "lines": lines}))
        invoices += 1

    ok(client.patch(f"/api/projects/{projects[ARCHIVED[0]]}", json={"status": "archived"}))
    queue = ok(client.get("/api/review-queue"))
    report = ok(client.get("/api/reports/summary", params={"period": "month"}))
    print(f"Seeded {len(rows)} delivery tickets ({len(approved)} approved), {count} truck shifts, {invoices} invoices; "
          f"{queue['summary']['total']} items waiting for review.")
    print(f"Approved cost, from the records: material ${report['costs']['material']}, hauling ${report['costs']['trucking']}, "
          f"total ${report['costs']['total']} ({report['costs']['unpriced']} approved without a rate).")


if __name__ == "__main__":
    raise SystemExit(main())
