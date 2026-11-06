"""Benchmark the API on fictional data, in a new temporary database.

Usage (from backend/):
    python scripts/measure_performance.py --tickets 1000 5000 20000 50000
    python scripts/measure_performance.py --tickets 1000000 --timeout 300 --json out.json

Each request runs in its own process, so peak memory is that request's and a
request that takes too long is reported instead of hanging the run. Times depend
on the machine, so use them to compare, not as a promise. No AI, no network, and
it can never open a real database.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PER_BATCH = 400
MAIN_ORG, OTHER_ORG = 1, 2
SUPPLIERS = ["Example Aggregates", "Sample Stone Co", "Placeholder Paving", "Fictional Sand & Gravel", "Demo Hauling LLC"]
MATERIALS = ["No. 57 Stone", "Base Rock", "Asphalt Mix", "Concrete Sand", "Rip Rap Class 2"]
PROJECTS = 20

READS = [
    ("One batch's tickets", "/api/batches/1/tickets"),
    ("Uploads list", "/api/batches/queue"),
    ("Dashboard", "/api/dashboard"),
    ("Register, first page", "/api/records/search?page_size=50"),
    ("Register, deep page", "/api/records/search?page=100&page_size=50"),
    ("Register, one project", "/api/records/search?page_size=50&project_id=1"),
    ("Register, one project, sort by tons", "/api/records/search?page_size=50&project_id=1&sort=tons"),
    ("Search a ticket number", "/api/records/search?page_size=50&q=100123"),
    ("Search and sort by tons", "/api/records/search?page_size=50&q=Stone&sort=tons"),
    ("Name variants", "/api/records/name-variants?field=supplier"),
    ("Invoice list, 20 lines", "/api/invoice-comparisons"),
    ("Invoice overview", "/api/invoice-comparisons/overview"),
    ("Project report", "/api/reports/project/1"),
    ("Pilot report", "/api/reports/pilot"),
    ("CSV export, one project", "/api/records/export.csv?project_id=1"),
    ("CSV export, everything", "/api/records/export.csv"),
    ("Excel export, one project", "/api/records/export.xlsx?project_id=1"),
    ("Excel export, everything", "/api/records/export.xlsx"),
]
WRITES = [("Edit one ticket", "patch"), ("Approve one ticket", "verify")]


def _environment(work: Path, url: str | None) -> dict:
    return {**os.environ, "DATABASE_URL": url or f"sqlite:///{work / 'perf-demo.db'}", "STORAGE_DIR": str(work / "storage"),
            "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "PYTHONWARNINGS": "ignore"}


def _keys(values: dict) -> dict:
    """Fill any normalized columns this version of the schema has."""
    from app.models import Ticket
    from app.services.pricing import norm
    columns = Ticket.__table__.columns
    if "supplier_key" in columns:
        values["supplier_key"] = norm(values["supplier"])
        values["material_key"] = norm(values["material_type"])
    if "number_key" in columns:
        number = (values["ticket_number"] or "").strip().upper()
        values["number_key"] = number
        values["number_loose"] = number.lstrip("0")
    if "organization_id" in columns:
        values.setdefault("organization_id", MAIN_ORG)
    if "lock_version" in columns:
        values["lock_version"] = 1
    if "approved_cost" in columns:  # Every row of a bulk insert needs the same columns.
        values["approved_cost"] = (values["tonnage"] * values["applied_unit_price"]).quantize(Decimal("0.01")) \
            if values["status"] == "verified" else None
    return values


def generate(tickets: int, seed: int = 7) -> dict:
    """Bulk-insert a company with a realistic mix. Bypasses the ORM for speed;
    another company's rows are mixed in, so scoping has to do real work."""
    from sqlalchemy import insert

    from app.db import Base, engine
    from app.models import (
        Batch, InvoiceComparison, InvoiceTicketRef, Material, Organization, Project, ScanSlot, Supplier, SupplierRate,
        Ticket, UploadedFile, User,
    )
    rng = random.Random(seed)
    Base.metadata.create_all(engine)
    start = date(2025, 1, 1)
    batches = max(1, tickets // PER_BATCH)
    # One batch in ten belongs to the other company. Project 1 stays with the main
    # one, and it's where the invoice lines and project report measure real work.
    is_other = lambda b: b % 10 == 7  # noqa: E731
    with engine.begin() as connection:
        connection.execute(insert(Organization), [{"id": MAIN_ORG, "name": "Fictional"}, {"id": OTHER_ORG, "name": "Fictional Other"}])
        connection.execute(insert(User), [{"id": 1, "organization_id": MAIN_ORG, "username": "perf", "password_hash": "x", "role": "owner", "active": True, "must_change_password": False}])
        connection.execute(insert(Project), [
            {"id": p + 1, "organization_id": MAIN_ORG, "code": f"P{p:03d}", "name": f"Fictional Project {p}", "status": "active"} for p in range(PROJECTS)
        ] + [{"id": PROJECTS + 1, "organization_id": OTHER_ORG, "code": "X", "name": "Other", "status": "active"}])
        connection.execute(insert(Supplier), [{"id": i + 1, "organization_id": MAIN_ORG, "name": name} for i, name in enumerate(SUPPLIERS)])
        connection.execute(insert(Material), [{"id": i + 1, "organization_id": MAIN_ORG, "name": name, "unit": "ton"} for i, name in enumerate(MATERIALS)])
        connection.execute(insert(SupplierRate), [
            {"organization_id": MAIN_ORG, "supplier_id": s + 1, "material_id": m + 1, "unit_price": Decimal(2000 + 317 * s + 113 * m) / 100}
            for s in range(len(SUPPLIERS)) for m in range(len(MATERIALS))])
        batch_rows, file_rows = [], []
        for b in range(1, batches + 1):
            other = is_other(b)
            organization = OTHER_ORG if other else MAIN_ORG
            batch_rows.append({"id": b, "organization_id": organization, "project_id": PROJECTS + 1 if other else (b % PROJECTS) + 1,
                               "layout": "grid_2x2", "intake_mode": "manual", "status": "ready_for_review"})
            file_rows.append({"id": b, "batch_id": b, "original_name": "fictional.pdf", "storage_path": "missing", "sha256": f"{b:064d}", "status": "ready_for_review"})
        connection.execute(insert(Batch), batch_rows)
        connection.execute(insert(UploadedFile), file_rows)
    chunk_t, chunk_s = [], []
    days = 900
    for n in range(tickets):
        batch = n % batches + 1
        organization = OTHER_ORG if is_other(batch) else MAIN_ORG
        roll = rng.random()
        status = "verified" if roll < 0.72 else "flagged" if roll < 0.77 else "skipped" if roll < 0.79 else "needs_review"
        verified = status == "verified"
        s, m = rng.randrange(len(SUPPLIERS)), rng.randrange(len(MATERIALS))
        price = Decimal(2000 + 317 * s + 113 * m) / 100
        tons = Decimal(rng.randint(12000, 26000)) / 1000
        chunk_t.append(_keys({
            "batch_id": batch, "file_id": batch, "source_page": n // 4, "position": n % 4, "crop_box": [0, 0, 1, 1], "crop_path": "missing",
            "crop_sha256": f"{n:064d}", "ticket_number": str(100000 + n), "delivery_date": start + timedelta(days=n * days // max(tickets, 1)),
            "material_type": MATERIALS[m], "supplier": SUPPLIERS[s], "job_location": "Fictional Site", "tonnage": tons, "flags": [],
            "status": status, "tonnage_confirmed": verified, "price_snapshot_state": "priced" if verified else "pending",
            "applied_unit_price": price if verified else None, "applied_price_source": "rate" if verified else None,
            "organization_id": organization,
        }))
        chunk_s.append({"batch_id": batch, "file_id": batch, "page": n // 4, "position": n % 4, "crop_box": [0, 0, 1, 1], "crop_path": "missing",
                        "crop_sha256": f"{n:064d}", "ink_fraction": 0.1, "detection": "content", "rotation": 0})
        if len(chunk_t) >= 20000:
            with engine.begin() as connection:
                connection.execute(insert(Ticket), chunk_t)
                connection.execute(insert(ScanSlot), chunk_s)
            chunk_t, chunk_s = [], []
    if chunk_t:
        with engine.begin() as connection:
            connection.execute(insert(Ticket), chunk_t)
            connection.execute(insert(ScanSlot), chunk_s)
    # Twenty itemized invoice lines on project 1, each a month of one supplier and
    # material, listing 50 tickets that really exist there.
    project_one = [b for b in range(1, batches + 1) if not is_other(b) and b % PROJECTS == 0] or [1]
    with engine.begin() as connection:
        for line in range(20):
            day = start + timedelta(days=line * 30)
            supplier, material = SUPPLIERS[line % len(SUPPLIERS)], MATERIALS[line % len(MATERIALS)]
            result = connection.execute(insert(InvoiceComparison).values(
                organization_id=MAIN_ORG, project_id=1, invoice_number=f"INV-{line}", supplier_name=supplier,
                supplier_key=supplier.lower(), material_name=material, material_key=material.lower(), date_from=day,
                date_to=day + timedelta(days=29), billed_tons=Decimal("100"), billed_unit_price=Decimal("30"), billed_subtotal=Decimal("3000")))
            numbers = [str(100000 + project_one[0] - 1 + (line * 50 + i) * batches) for i in range(50)]
            connection.execute(insert(InvoiceTicketRef), [
                {"comparison_id": result.inserted_primary_key[0], "position": i + 1, "ticket_number": number, "ticket_key": number,
                 "billed_tons": Decimal("2")} for i, number in enumerate(numbers)])
    return {"tickets": tickets, "batches": batches}


def _writable_ticket_ids(limit: int = 10) -> list[int]:
    from sqlalchemy import select

    from app.db import engine
    from app.models import Batch, Ticket
    with engine.connect() as connection:
        return list(connection.scalars(select(Ticket.id).join(Batch, Batch.id == Ticket.batch_id).where(
            Batch.organization_id == MAIN_ORG, Ticket.status == "needs_review").order_by(Ticket.id).limit(limit)))


def one_request(kind: str, target: str) -> dict:
    """Runs in a fresh process: one measured request, then its peak memory."""
    import logging
    from fastapi.testclient import TestClient
    from sqlalchemy import event

    from app.auth import current_user
    from app.db import engine
    from app.main import app
    from app.models import User
    logging.getLogger("ticketflow.request").setLevel(logging.WARNING)
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=MAIN_ORG, username="perf", password_hash="x", active=True, role="owner", must_change_password=False)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda _c, _cur, statement, *_a: statements.append(statement))
    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with TestClient(app, headers={"X-TicketFlow-Request": "1"}) as client:
        statements.clear()
        started = time.perf_counter()
        if kind == "read":
            response = client.get(target)
            ok = response.status_code == 200
            size = len(response.content)
        else:
            timings = []
            ok, size = True, 0
            ids = _writable_ticket_ids()
            for ticket_id in ids:
                begun = time.perf_counter()
                statements.clear()
                response = client.patch(f"/api/tickets/{ticket_id}", json={"job_location": "Lot B"}) if target == "patch" \
                    else client.post(f"/api/tickets/{ticket_id}/verify")
                ok = ok and response.status_code == 200
                timings.append(time.perf_counter() - begun)
            timings.sort()
            elapsed = timings[len(timings) // 2] if timings else 0.0
        if kind == "read":
            elapsed = time.perf_counter() - started
    divisor = 1024 ** 2 if sys.platform == "darwin" else 1024  # bytes on macOS, KB on Linux
    return {"ok": ok, "seconds": round(elapsed, 3), "queries": len(statements), "bytes": size,
            "peak_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor),
            "base_mb": round(baseline / divisor)}


def _measure(env: dict, kind: str, target: str, timeout: int) -> dict:
    command = [sys.executable, str(Path(__file__).resolve()), "--one", kind, target]
    try:
        run = subprocess.run(command, env=env, cwd=BACKEND, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "limit": f"did not finish within {timeout} s"}
    if run.returncode != 0:
        return {"ok": False, "limit": f"failed: exit {run.returncode}: {run.stderr.strip().splitlines()[-1][:160] if run.stderr.strip() else ''}"}
    return json.loads(run.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickets", type=int, nargs="+", default=[5000])
    parser.add_argument("--timeout", type=int, default=300, help="Seconds allowed per request before it's reported as a limit")
    parser.add_argument("--json", type=Path, help="Also write results here")
    parser.add_argument("--database-url", help="Empty PostgreSQL database to fill. It must have 'demo' or 'perf' in its name")
    parser.add_argument("--one", nargs=2, help=argparse.SUPPRESS)
    parser.add_argument("--generate", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(BACKEND))

    if args.one:
        print(json.dumps(one_request(*args.one)))
        return 0
    if args.generate is not None:
        started = time.perf_counter()
        print(json.dumps({**generate(args.generate), "seconds": round(time.perf_counter() - started, 1)}))
        return 0
    if args.database_url and not any(word in args.database_url.rsplit("/", 1)[-1] for word in ("demo", "perf")):
        print("Refusing: the database name must contain 'demo' or 'perf'.", file=sys.stderr)
        return 1

    results = {"measured_at": time.strftime("%Y-%m-%d"), "platform": sys.platform, "runs": []}
    for count in args.tickets:
        work = Path(tempfile.mkdtemp(prefix="ticketflow-perf-"))
        env = _environment(work, args.database_url)
        try:
            generated = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--generate", str(count)], env=env, cwd=BACKEND,
                                       capture_output=True, text=True)
            if generated.returncode != 0:
                print(generated.stderr[-2000:], file=sys.stderr)
                return generated.returncode
            info = json.loads(generated.stdout.strip().splitlines()[-1])
            run = {"tickets": count, "generation_seconds": info["seconds"], "requests": []}
            print(f"\n### {count:,} tickets (generated in {info['seconds']} s)\n\n| Request | Seconds | SQL | Peak MB | Result |\n|---|---:|---:|---:|---|")
            for label, target in READS + WRITES:
                kind = "write" if target in ("patch", "verify") else "read"
                outcome = _measure(env, kind, target, args.timeout)
                run["requests"].append({"request": label, **outcome})
                print(f"| {label} | {outcome.get('seconds', '-')} | {outcome.get('queries', '-')} | {outcome.get('peak_mb', '-')} | "
                      f"{'ok' if outcome.get('ok') else outcome.get('limit', 'not ok')} |", flush=True)
            results["runs"].append(run)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
