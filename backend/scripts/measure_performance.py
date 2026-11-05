"""Time list endpoints and scan processing on fictional data in a temporary database.

Usage (from backend/): python scripts/measure_performance.py --tickets 5000

Always uses a new temporary database. No AI, no network. Results depend on the
machine, so use them for relative comparison and not as a promise.
"""
from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

_TEMP = Path(tempfile.mkdtemp(prefix="ticketflow-perf-"))
# Must be set before any app import: this can never open a real database.
os.environ["DATABASE_URL"] = f"sqlite:///{_TEMP / 'perf-demo.db'}"
os.environ["STORAGE_DIR"] = str(_TEMP / "storage")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickets", type=int, default=5000)
    parser.add_argument("--invoice-lines", type=int, default=20)
    parser.add_argument("--identical-loads", action="store_true",
                        help="Worst case for the duplicate check: every ticket has the same supplier, date, material and tons")
    args = parser.parse_args(argv)

    from datetime import date, timedelta
    from decimal import Decimal

    from fastapi.testclient import TestClient
    from sqlalchemy import event

    from app.auth import current_user
    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.models import (
        Batch, InvoiceComparison, InvoiceTicketRef, Organization, Project, ScanSlot, Ticket, UploadedFile, User,
    )
    from app.services import pipeline
    from tests.sample_data import TICKETS, make_page, make_pdf

    Base.metadata.create_all(engine)
    per_batch = 400
    with SessionLocal() as db:
        db.add(Organization(id=1, name="Fictional"))
        db.add(User(id=1, organization_id=1, username="perf", password_hash="x", role="owner"))
        db.add(Project(id=1, organization_id=1, code="P", name="Perf"))
        db.flush()
        batches = max(1, args.tickets // per_batch)
        for b in range(1, batches + 1):
            db.add(Batch(id=b, organization_id=1, project_id=1, layout="grid_2x2", status="ready_for_review"))
            db.add(UploadedFile(id=b, batch_id=b, original_name="p.pdf", storage_path="missing", sha256="0" * 64,
                                status="ready_for_review"))
        db.flush()
        tickets, slots = [], []
        for n in range(args.tickets):
            b = n % batches + 1
            page, position = n // 4, n % 4
            tickets.append(Ticket(
                batch_id=b, file_id=b, source_page=page, position=position, crop_box=[0, 0, 1, 1], crop_path="missing",
                crop_sha256=f"{n:064d}", ticket_number=str(10000 + n),
                delivery_date=date(2026, 10, 15) if args.identical_loads else date(2026, 9, 1) + timedelta(days=n % 60),
                material_type="Sand" if args.identical_loads else ("Sand", "No. 57 Stone", "Base Rock")[n % 3],
                supplier="Example Aggregates" if args.identical_loads else f"Supplier {n % 5}", job_location="Lot",
                tonnage=Decimal("20.500") if args.identical_loads else Decimal(1500 + n % 1000) / 100, flags=[], status="verified" if n % 2 else "needs_review",
                tonnage_confirmed=bool(n % 2), price_snapshot_state="priced" if n % 2 else "pending",
                applied_unit_price=Decimal("30") if n % 2 else None))
            slots.append(ScanSlot(batch_id=b, file_id=b, page=page, position=position, crop_box=[0, 0, 1, 1],
                                  crop_path="missing", crop_sha256=f"{n:064d}", ink_fraction=0.1, detection="content", rotation=0))
        db.add_all(tickets + slots)
        db.commit()

    # Invoice lines with itemized ticket lists, one day each so scopes don't overlap.
    with SessionLocal() as db:
        for line in range(args.invoice_lines):
            day = date(2026, 9, 1) + timedelta(days=line % 60)
            supplier_number = line % 5
            item = InvoiceComparison(
                organization_id=1, project_id=1, invoice_number=f"I{line}", supplier_name=f"Supplier {supplier_number}",
                supplier_key=f"supplier {supplier_number}", material_name="Sand", material_key="sand", date_from=day,
                date_to=day, billed_tons=Decimal("100"), billed_unit_price=Decimal("30"), billed_subtotal=Decimal("3000"))
            db.add(item)
            db.flush()
            db.add_all(InvoiceTicketRef(comparison_id=item.id, position=i + 1, ticket_number=str(10000 + line * 50 + i),
                                        ticket_key=str(10000 + line * 50 + i)) for i in range(50))
        db.commit()

    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="perf", password_hash="x", active=True, role="owner", must_change_password=False)
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda _c, _cur, statement, *_a: statements.append(statement))

    print(f"| Request ({args.tickets} tickets, {batches} batches, {args.invoice_lines} invoice lines) | Seconds | SQL queries |\n|---|---|---|")
    with TestClient(app, headers={"X-TicketFlow-Request": "1"}) as client:
        for path in ("/api/batches/1/tickets", "/api/records", "/api/dashboard", "/api/batches/1/manifest",
                     "/api/reports/pilot", "/api/invoice-comparisons"):
            statements.clear()
            started = time.perf_counter()
            response = client.get(path)
            assert response.status_code == 200, response.text
            print(f"| GET `{path}` | {time.perf_counter() - started:.3f} | {len(statements)} |")

        # The requests a person repeats for every ticket. Median of several, on
        # tickets still awaiting review (even numbers are unverified above).
        def timed(label: str, call) -> None:
            seconds, queries = [], 0
            for n in range(10):
                statements.clear()
                started = time.perf_counter()
                response = call(n * 2 + 2)
                assert response.status_code == 200, response.text
                seconds.append(time.perf_counter() - started)
                queries = len(statements)
            print(f"| {label} (median of 10) | {statistics.median(seconds):.3f} | {queries} |")

        timed("PATCH one ticket", lambda n: client.patch(f"/api/tickets/{n * 2 + 1}", json={"job_location": "Lot B"}))
        timed("Approve one ticket", lambda n: client.post(f"/api/tickets/{n * 2 + 1}/verify"))

    # Rendering and splitting one 20-page 4-up PDF, with no extraction.
    page = make_page(TICKETS)
    pdf = _TEMP / "twenty.pdf"
    pdf.write_bytes(make_pdf([page] * 20))
    with SessionLocal() as db:
        db.add(Batch(id=100000, organization_id=1, layout="grid_2x2", intake_mode="manual"))
        file = UploadedFile(id=100000, batch_id=100000, original_name="twenty.pdf", storage_path=str(pdf), sha256="1" * 64)
        db.add(file)
        db.commit()
        started = time.perf_counter()
        pipeline.process_file(db, file, "grid_2x2", None, manual=True)
        print(f"| Process a 20-page 4-up PDF (80 slots, no AI) | {time.perf_counter() - started:.3f} | n/a |")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TEMP, ignore_errors=True)
