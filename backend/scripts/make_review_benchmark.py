"""One batch of fictional tickets waiting for review, for timing the review workflow.

Usage (from backend/, after scripts/create_demo_workspace.py):
    DATABASE_URL=sqlite:////tmp/x/demo.db STORAGE_DIR=/tmp/x/storage \\
        python scripts/make_review_benchmark.py --tickets 300 --answers /tmp/x/answers.json

Each ticket is an AI-read ticket (needs_review) with its own scan image. A known share
are planted misreads (a wrong tonnage or ticket number) or blanks, and the answer file
says what the paper says, so a scripted reviewer can correct exactly those. Nothing is
pre-approved: every ticket still has to be accepted through the normal approval.

Refuses unless DATABASE_URL points at a SQLite file with "demo" in its name. No AI.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SUPPLIER, MATERIALS = "Bench Aggregates", ("No. 57 Stone", "No. 8 Stone", "Sand")
RATES = {"No. 57 Stone": Decimal("21.5000"), "No. 8 Stone": Decimal("23.2500"), "Sand": Decimal("14.0000")}
MISREAD, BLANK = 0.10, 0.05


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickets", type=int, default=300)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--answers", type=Path, required=True)
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///") or "demo" not in Path(url.removeprefix("sqlite:///")).name:
        print("Refusing: set DATABASE_URL to a SQLite file with 'demo' in its name.", file=sys.stderr)
        return 1

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Batch, Material, Organization, Project, ScanSlot, Supplier, SupplierRate, Ticket, UploadedFile
    from app.services import storage
    from app.services.pipeline import sha256_bytes
    from tests.sample_data import make_ticket_image

    rng = random.Random(args.seed)
    store = storage.current()
    answers: dict[str, dict] = {}
    with SessionLocal() as db:
        organization = db.scalars(select(Organization).order_by(Organization.id)).first()
        project = Project(organization_id=organization.id, code="BENCH", name="Fictional review benchmark")
        supplier = Supplier(organization_id=organization.id, name=SUPPLIER)
        materials = {name: Material(organization_id=organization.id, name=name) for name in MATERIALS}
        db.add_all([project, supplier, *materials.values()])
        db.flush()
        db.add_all([SupplierRate(organization_id=organization.id, supplier_id=supplier.id, material_id=m.id, unit_price=RATES[name])
                    for name, m in materials.items()])
        batch = Batch(organization_id=organization.id, project_id=project.id, layout="single", intake_mode="extract",
                      status="ready_for_review", expected_ticket_count=args.tickets)
        db.add(batch)
        db.flush()
        file = UploadedFile(batch_id=batch.id, original_name="bench-scans.pdf", storage_path="missing", sha256=f"{batch.id:064d}",
                            status="ready_for_review")
        db.add(file)
        db.flush()
        for n in range(args.tickets):
            truth = {"ticket_number": f"B{300000 + n}", "delivery_date": (date(2026, 10, 1) + timedelta(days=n % 28)).isoformat(),
                     "material_type": rng.choice(MATERIALS), "tonnage": f"{Decimal(rng.randint(1200, 2600)) / 100:.2f}",
                     "supplier": SUPPLIER, "job_location": "Fictional Bench Site"}
            read = dict(truth)
            roll = rng.random()
            if roll < MISREAD / 2:
                read["tonnage"] = f"{Decimal(read['tonnage']) + Decimal('10'):.2f}"  # a plausible-looking misread
            elif roll < MISREAD:
                read["ticket_number"] = read["ticket_number"][:-1] + str((int(read["ticket_number"][-1]) + 3) % 10)
            elif roll < MISREAD + BLANK:
                read["ticket_number"] = None
            image = make_ticket_image(truth)
            path = f"crops/{batch.id}/bench_{n}.png"
            store.save_png(storage.reference_for(path), image)
            digest = sha256_bytes(store.read(path))
            ticket = Ticket(
                batch_id=batch.id, organization_id=organization.id, file_id=file.id, source_page=n + 1, position=0,
                crop_box=[0, 0, image.width, image.height], crop_path=path, crop_sha256=digest,
                ticket_number=read["ticket_number"], delivery_date=date.fromisoformat(read["delivery_date"]),
                material_type=read["material_type"], tonnage=Decimal(read["tonnage"]), supplier=read["supplier"],
                job_location=read["job_location"], raw_extraction=dict(read), flags=[], status="needs_review",
                tonnage_confirmed=False, price_snapshot_state="pending")
            db.add(ticket)
            db.add(ScanSlot(batch_id=batch.id, file_id=file.id, page=n + 1, position=0, crop_box=[0, 0, image.width, image.height],
                            crop_path=path, crop_sha256=digest, ink_fraction=0.1, detection="content"))
            db.flush()
            fixes = {k: v for k, v in truth.items() if read[k] != v}
            answers[str(ticket.id)] = fixes
        db.commit()
        result = {"batch_id": batch.id, "tickets": args.tickets, "corrections": answers,
                  "tickets_with_corrections": sum(1 for fixes in answers.values() if fixes)}
    args.answers.write_text(json.dumps(result))
    print(json.dumps({k: v for k, v in result.items() if k != "corrections"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
