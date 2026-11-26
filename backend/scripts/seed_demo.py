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
  - units: screened topsoil priced and delivered per cubic yard (never added to tons), some
    recycled-base tickets written in pounds (saved as tons, the pounds kept), scale weights on
    sand tickets, and one waiting ticket whose gross minus tare disagrees with its tons
  - per-ton and per-load hauler invoices: one correct, one billing extra tons, one an extra
    load, one billing a load already invoiced, one billing another project's delivery, and
    one billing only part of the month
The expected results are worked out here from the plan itself, independently of Quarvera's
arithmetic, and written to expected-results.json; scripts/verify_demo.py checks Quarvera
shows exactly those figures.

Usage (from backend/, venv active):
    python scripts/seed_demo.py            # create it (refuses if it exists)
    python scripts/seed_demo.py --reset    # delete the demo folder and create it again
Then run the API and worker with the two exports it prints, and sign in as demo-owner.

The hosted demo (PostgreSQL, its own bucket; see docs/deploy/founder-launch-checklist.md):
    APP_ENVIRONMENT=demo STORAGE_BACKEND=s3 S3_BUCKET=quarvera-demo-documents ... \\
        python scripts/seed_demo.py --hosted --database-url postgresql://.../quarvera_demo
It refuses unless APP_ENVIRONMENT=demo, the database's name contains "demo", the scans go to
a bucket named "demo", the startup checks pass for the demo, and the database has no users
yet. It never opens another database, never resets one, and prints a one-time owner
password that must be changed at first sign-in.
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
# Materials priced in a unit other than tons. Their tickets are entered in that unit.
UNIT_OF = {("Central Indiana Fill", "Screened Topsoil"): "cy"}
EXPECTED_PATH = DEMO_DIR / "expected-results.json"
HOURLY = [h for h in HAULERS if h[1] == "hourly"]


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- the expected results, worked out from the plan
# Written from the price lists above, not from anything Quarvera calculates, so the demo
# checks the product rather than repeating it.

def expected_price(row: dict) -> Decimal | None:
    supplier, material, project, day = row["supplier"], row["material_type"], row["project"], row["delivery_date"]
    if supplier == "Summit Asphalt Supply":
        return None  # no rate on file
    if (supplier, material) == ("Granite Ridge Aggregates", "No. 57 Stone"):
        if project == "25-101":
            return Decimal("25.80")  # the project agreement, from 2026-09-01, over every company price
        return Decimal("27.20") if day >= "2026-11-01" else Decimal("26.50")
    if (supplier, material) == ("Hillcrest Aggregates", "No. 53 Crushed Stone") and project == "25-140":
        return Decimal("24.40")
    return Decimal(next(price for m, price, _f in SUPPLIERS[supplier] if m == material))


def expected_haul(row: dict) -> Decimal | None:
    """Hauling for a delivery billed per ton or per load; None when no rate applies (missing, not $0)."""
    hauler, tons = row["hauler"], Decimal(row["tonnage"])
    if hauler == "Ironwood Haul":
        return cents(tons * Decimal("4.25"))
    if hauler == "River City Transport":
        return cents(tons * (Decimal("3.60") if row["project"] == "25-114" else Decimal("3.90")))
    if hauler == "Lincoln Dump Service":
        return Decimal("88.00")
    if hauler == "Summit Logistics":
        return Decimal("92.00") if row["project"] == "25-152" else None
    raise AssertionError(hauler)


def decorate(rows: list[dict], rng: random.Random, days: list[date]) -> None:
    """Units and scale weights, decided after the plan so the deliveries themselves don't move."""
    recent = set(d.isoformat() for d in days[-6:])
    mismatch_planted = False
    for row in rows:
        row["unit"] = UNIT_OF.get((row["supplier"], row["material_type"]), "ton")
        if row["unit"] == "cy":
            row["tonnage"] = f"{rng.randint(10, 16)}.0"  # cubic yards, per the truck's box
            row["hauler"] = None  # hauling per ton can't price yards, so the demo doesn't pretend
        elif row["supplier"] == "Redbud Recycled Base" and rng.random() < 0.25:
            pounds = int(Decimal(row["tonnage"]) * 2000)  # 1 decimal of tons is a whole 200 lb
            row["pounds"] = str(pounds)
            row["tonnage"] = str((Decimal(pounds) / 2000).quantize(Decimal("0.001")))  # what the conversion must give
        elif row["supplier"] == "Lakeside Sand & Gravel":
            tare = rng.randrange(28000, 34000, 20)
            row["tare"], row["gross"] = str(tare), str(tare + int(Decimal(row["tonnage"]) * 2000))
            if not mismatch_planted and row["outcome"] == "needs_review" and row["delivery_date"] in recent:
                row["gross"] = str(int(row["gross"]) + 600)  # 0.3 t more than the ticket's tons: flagged for a person
                mismatch_planted = True


def guard_paths() -> None:
    # Only ever this one folder, inside the repository's demo area, with "demo" in its name.
    assert DEMO_DIR.resolve().is_relative_to((BACKEND / "demo_workspace").resolve()), DEMO_DIR
    assert "demo" in DB_PATH.name and "demo" in DEMO_DIR.name


HOSTED_EXPECTED_PATH = BACKEND / "demo_workspace" / "hosted-demo-expected-results.json"


def hosted_problems(database_url: str, environ) -> list[str]:
    """Why --hosted must not run, checked before anything is imported or opened."""
    from sqlalchemy.engine import make_url

    problems = []
    if environ.get("APP_ENVIRONMENT") != "demo":
        problems.append("Set APP_ENVIRONMENT=demo in this shell: the hosted seeder only ever fills the fictional demo.")
    try:
        url = make_url(database_url)
    except Exception:  # Any unreadable URL is refused the same way.
        url = None
    if url is None or not url.get_backend_name().startswith("postgres"):
        problems.append("--database-url must be the demo's PostgreSQL URL (postgresql://...).")
    elif "demo" not in (url.database or "").lower():
        problems.append('The database\'s name must contain "demo" (for example quarvera_demo). Nothing else is ever seeded.')
    if environ.get("DATABASE_URL") and environ["DATABASE_URL"] != database_url:
        problems.append("DATABASE_URL in this shell names a different database. Unset it: --database-url is the only target.")
    if environ.get("STORAGE_BACKEND") != "s3" or "demo" not in environ.get("S3_BUCKET", "").lower():
        problems.append('The demo\'s scans go to its own bucket: set STORAGE_BACKEND=s3, S3_BUCKET (with "demo" in its name) '
                        "and that bucket's S3_* variables in this shell.")
    return problems


def database_has_users(database_url: str) -> bool:
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("users"):
                return False
            return bool(connection.execute(text("SELECT COUNT(*) FROM users")).scalar())
    finally:
        engine.dispose()


def hosted(database_url: str) -> int:
    """Seed the hosted demo's PostgreSQL database, and nothing else."""
    global EXPECTED_PATH
    problems = hosted_problems(database_url, os.environ)
    if problems:
        print("Hosted demo seeding refused. Nothing was opened or changed.\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    # Set before the app is imported, so it can reach this database only, and never an AI reader.
    os.environ["DATABASE_URL"] = database_url
    os.environ["AI_READING_ENABLED"] = "false"
    for limit in ("UPLOADS", "EXPORTS", "REPORTS"):
        os.environ[f"RATE_LIMIT_{limit}_PER_MINUTE"] = "0"
    sys.path.insert(0, str(BACKEND))
    from app.config import Settings, configuration_problems

    problems = configuration_problems(Settings())
    if problems:
        print("Hosted demo seeding refused: these variables don't pass the demo's startup checks. Nothing was changed.\n  "
              + "\n  ".join(problems), file=sys.stderr)
        return 2
    from app.db import normalized_url

    if database_has_users(normalized_url(database_url)):
        print("Hosted demo seeding refused: this database already has people in it. It only fills an empty demo database; "
              "to rebuild, drop and recreate the demo database first. Nothing was changed.", file=sys.stderr)
        return 1
    EXPECTED_PATH = HOSTED_EXPECTED_PATH
    EXPECTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    password = seed(hosted=True)
    print(f"\nHosted demo ready: {COMPANY}")
    print(f"  Sign in as {OWNER} with this one-time password, shown only now: {password}")
    print("  You'll be asked to choose a new password at first sign-in. Keep it in your password manager.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the demo folder first (only that folder)")
    parser.add_argument("--hosted", action="store_true", help="seed the hosted demo's PostgreSQL database (see above)")
    parser.add_argument("--database-url", help="with --hosted: the demo database, whose name must contain 'demo'")
    args = parser.parse_args(argv)
    if args.hosted:
        if args.reset:
            print("--reset is for the local demo folder only. A hosted demo database is rebuilt by recreating it.", file=sys.stderr)
            return 2
        return hosted(args.database_url or "")
    if args.database_url:
        print("--database-url is used only with --hosted.", file=sys.stderr)
        return 2
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
    print("    export AI_READING_ENABLED=false   # the demo never calls an AI reader, whatever keys .env holds")
    print("    export APP_ENVIRONMENT=demo       # startup then refuses any database or folder not named demo")
    return 0


def seed(hosted: bool = False) -> str:
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
                     role="owner", active=True, must_change_password=hosted)
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
    # In process, never over the network. The hosted demo's settings accept only its own
    # hostnames over https, so the in-process client uses the first of them.
    if hosted:
        from app.config import settings
        options = {"base_url": f"https://{settings.trusted_hosts[0]}"}
    else:
        options = {}
    try:
        with TestClient(app, **options) as client:
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
                body = {"supplier_id": suppliers[supplier], "material_id": materials[material], "unit_price": price, "effective_from": "2026-08-01",
                        "unit": UNIT_OF.get((supplier, material), "ton")}
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
    decorate(rows, random.Random(SEED + 1), days)
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
            if row.get("pounds"):
                body.update(tonnage=row["pounds"], unit="lb")  # typed as written; Quarvera converts and keeps the pounds
            elif row["unit"] != "ton":
                body["unit"] = row["unit"]
            if row.get("gross"):
                body.update(gross_weight=row["gross"], tare_weight=row["tare"], weight_unit="lb")
            if row["hauler"]:
                body.update(hauler_id=haulers[row["hauler"]]["id"], truck_number=row["truck"])
            edited = ok(client.patch(f"/api/tickets/{ticket['id']}", json=body))
            if row["outcome"] == "verified":
                done = ok(client.post(f"/api/tickets/{edited['id']}/verify", json={"confirm_duplicate": True}, headers={"If-Match": edited["revision"]}))
                if done["tonnage"] != str(Decimal(row["tonnage"]).quantize(Decimal("0.001"))) or done.get("unit", "ton") != row["unit"]:
                    raise SystemExit(f"Ticket {row['ticket_number']}: entered {row}, Quarvera kept {done['tonnage']} {done.get('unit')}")
                approved.append({**row, "id": done["id"], "cost": done.get("cost"), "unit_price": done.get("unit_price"),
                                 "haul_billing": done.get("haul_billing"), "haul_cost": done.get("haul_cost"),
                                 "haul_unit_rate": done.get("haul_unit_rate")})
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
            by_material[(row["material_type"], row["unit_price"] or "", row["unit"])].append(row)
        for (material, locked, unit), items in by_material.items():  # noqa: B007
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
                    "date_from": f"{month}-01", "date_to": f"{month}-{last_day}", "billed_tons": str(tons.quantize(Decimal("0.001"))), "unit": unit,
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

    hauler_expected = unit_hauler_invoices(client, approved, haulers, projects)
    invoices += len(hauler_expected)

    ok(client.patch(f"/api/projects/{projects[ARCHIVED[0]]}", json={"status": "archived"}))
    queue = ok(client.get("/api/review-queue"))
    report = ok(client.get("/api/reports/summary", params={"period": "month"}))
    print(f"Seeded {len(rows)} delivery tickets ({len(approved)} approved), {count} truck shifts, {invoices} invoices; "
          f"{queue['summary']['total']} items waiting for review.")
    print(f"Approved cost, from the records: material ${report['costs']['material']}, hauling ${report['costs']['trucking']}, "
          f"total ${report['costs']['total']} ({report['costs']['unpriced']} approved without a rate).")
    write_expected(approved, projects, hauler_expected)


def _line(day: str, items: list[dict], kind: str) -> dict:
    rate = Decimal(items[0]["haul_unit_rate"])
    quantity = sum((Decimal(i["tonnage"]) for i in items), Decimal(0)) if kind == "tons" else Decimal(len(items))
    # The hauler bills each delivery at its rate, rounded per ticket, as the locked charges are.
    amount = sum((expected_haul(i) for i in items), Decimal(0))
    return {"kind": kind, "work_date": day, "tickets": " ".join(i["ticket_number"] for i in items),
            "quantity": str(quantity), "rate": str(rate), "amount": str(amount)}


def unit_hauler_invoices(client, approved: list[dict], haulers: dict, projects: dict) -> dict:
    """September invoices from haulers paid per ton and per load, each with a known outcome."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in approved:
        if row["hauler"] in ("Ironwood Haul", "Lincoln Dump Service") and row["delivery_date"].startswith("2026-09") \
                and row["haul_billing"] in ("per_ton", "per_load"):
            groups[(row["hauler"], row["project"])].append(row)

    def days_of(items):
        by_day: dict[str, list[dict]] = defaultdict(list)
        for i in sorted(items, key=lambda r: (r["delivery_date"], r["ticket_number"])):
            by_day[i["delivery_date"]].append(i)
        return sorted(by_day.items())

    ranked = {h: sorted(((p, g) for (hh, p), g in groups.items() if hh == h and len(days_of(g)) >= 3), key=lambda pg: (-len(pg[1]), pg[0]))
              for h in ("Ironwood Haul", "Lincoln Dump Service")}
    ironwood, lincoln = ranked["Ironwood Haul"], ranked["Lincoln Dump Service"]
    if len(ironwood) < 3 or len(lincoln) < 3:
        raise SystemExit("Demo seeding stopped: not enough per-ton and per-load hauling in September for the invoice scenarios.")
    expected: dict[str, dict] = {}

    def post(hauler, project, number, lines, outcome):
        ok(client.post("/api/hauling-invoices", json={"hauler_id": haulers[hauler]["id"], "project_id": projects[project],
                                                      "invoice_number": number, "period_from": "2026-09-01", "period_to": "2026-09-30",
                                                      "lines": lines}))
        expected[number] = outcome

    (p1, g1), (p2, g2), (p3, g3) = ironwood[:3]
    post("Ironwood Haul", p1, "IRO-T-0901", [_line(d, items, "tons") for d, items in days_of(g1)],
         {"scenario": "Per ton, correct", "difference": "0.00", "exceptions": 0, "unbilled_in_period": 0})
    lines = [_line(d, items, "tons") for d, items in days_of(g2)]
    lines[0] = {**lines[0], "quantity": str(Decimal(lines[0]["quantity"]) + 3), "amount": str(Decimal(lines[0]["amount"]) + Decimal("12.75"))}
    post("Ironwood Haul", p2, "IRO-T-0902", lines,
         {"scenario": "Per ton, 3 t more than delivered", "difference": "12.75", "exceptions": 1, "unbilled_in_period": 0})
    billed_days = days_of(g3)[: len(days_of(g3)) // 2]
    left = sum(len(items) for _d, items in days_of(g3)[len(billed_days):])
    post("Ironwood Haul", p3, "IRO-T-0903", [_line(d, items, "tons") for d, items in billed_days],
         {"scenario": "Per ton, only part of the month billed", "difference": "0.00", "exceptions": 0, "unbilled_in_period": left})

    (q1, h1), (q2, h2), (q3, h3) = lincoln[:3]
    lines = [_line(d, items, "loads") for d, items in days_of(h1)]
    again = dict(lines[1])
    lines[0] = {**lines[0], "quantity": str(Decimal(lines[0]["quantity"]) + 1), "amount": str(Decimal(lines[0]["amount"]) + Decimal("88.00"))}
    post("Lincoln Dump Service", q1, "LIN-L-0901", lines,
         {"scenario": "Per load, one load more than delivered", "difference": "88.00", "exceptions": 1, "unbilled_in_period": 0})
    post("Lincoln Dump Service", q1, "LIN-L-0901B", [again],
         {"scenario": "Per load, a day already invoiced billed again", "difference": again["amount"], "exceptions": 1, "unbilled_in_period": 0})
    stray = days_of(h3)[0][1][0]
    lines = [_line(d, items, "loads") for d, items in days_of(h2)] + [_line(stray["delivery_date"], [stray], "loads")]
    post("Lincoln Dump Service", q2, "LIN-L-0902", lines,
         {"scenario": f"Per load, one delivery from project {q3} billed here", "difference": "88.00", "exceptions": 1, "unbilled_in_period": 0})
    print(f"  hauler invoices per ton and per load: {len(expected)}")
    return expected


def write_expected(approved: list[dict], projects: dict, hauler_expected: dict) -> None:
    import json

    tons = yards = material = Decimal(0)
    unpriced = haul_missing = 0
    haul = {"per_ton": Decimal(0), "per_load": Decimal(0)}
    by_project: dict[str, Decimal] = defaultdict(Decimal)
    for row in approved:
        quantity = Decimal(row["tonnage"])
        if row["unit"] == "ton":
            tons += quantity
        else:
            yards += quantity
        price = expected_price(row)
        if price is None:
            unpriced += 1
        else:
            cost = cents(quantity * price)
            material += cost
            by_project[row["project"]] += cost
        if row["hauler"]:
            charge = expected_haul(row)
            if charge is None:
                haul_missing += 1
            else:
                haul["per_load" if row["hauler"] in ("Lincoln Dump Service", "Summit Logistics") else "per_ton"] += charge
    expected = {
        "approved_tickets": len(approved),
        "tons": str(tons.quantize(Decimal("0.001"))),
        "other_units": [{"unit": "cy", "quantity": str(yards.quantize(Decimal("0.001")))}] if yards else [],
        "pound_tickets": sum(1 for r in approved if r.get("pounds")),
        "material_cost": str(material), "unpriced": unpriced,
        "per_ton_hauling": str(haul["per_ton"]), "per_load_hauling": str(haul["per_load"]), "hauling_rate_missing": haul_missing,
        "material_by_project": {code: str(v) for code, v in sorted(by_project.items())},
        "hauler_invoices": hauler_expected,
    }
    EXPECTED_PATH.write_text(json.dumps(expected, indent=2) + "\n")
    print(f"  expected results written to {EXPECTED_PATH.name}: check them with scripts/verify_demo.py")


if __name__ == "__main__":
    raise SystemExit(main())
