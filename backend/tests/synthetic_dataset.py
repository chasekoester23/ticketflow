"""A fictional company with thousands of records and mistakes planted on purpose.

Every supplier, ticket and invoice here is invented. The answer key records each
planted mistake, so a test can check the software finds all of them. This tests
the software's logic. It says nothing about reading accuracy on real documents.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Batch, InvoiceComparison, InvoiceTicketRef, Material, Project, ScanSlot, Supplier, SupplierRate, Ticket, UploadedFile,
)
from app.services.pricing import norm

SUPPLIERS = {
    "Example Aggregates": ["EXAMPLE AGGREGATES", "Example Aggregates Inc.", "Examle Aggregates"],
    "Sample Stone Co": ["Sample Stone", "SAMPLE STONE CO."],
    "Placeholder Paving": ["Placeholder Pavng"],
    "Fictional Sand & Gravel": ["Fictional Sand and Gravel"],
}
MATERIALS = ["No. 57 Stone", "Base Rock", "Asphalt Mix", "Concrete Sand", "Rip Rap Class 2"]
PROJECTS = [("SYN-101", "Fictional Road Job"), ("SYN-207", "Fictional Bridge Deck"), ("SYN-310", "Sample Site Grading")]
START = date(2026, 3, 2)

# Every kind of invoice mistake the software should catch, and the correct result.
INVOICE_CASES = {
    "clean": "matches",
    "ticket_never_delivered": "discrepancy",
    "tons_inflated": "discrepancy",
    "ticket_billed_twice": "discrepancy",
    "delivery_left_off": "discrepancy",
    "rate_too_high": "discrepancy",
    "bad_multiplication": "discrepancy",
    "leading_zeros_stripped": "discrepancy",
    "ticket_from_other_project": "discrepancy",
    "same_ticket_on_earlier_invoice": "discrepancy",
    "delivery_still_in_review": "incomplete",
    "totals_only_clean": "matches",
}


@dataclass
class AnswerKey:
    invoices: dict[int, str] = field(default_factory=dict)  # comparison id -> case name
    duplicate_tickets: list[int] = field(default_factory=list)
    misspelled_tickets: int = 0
    tickets: int = 0


def _ticket(batch_id: int, file_id: int, n: int, *, number: str | None, supplier: str | None, material: str | None,
            delivered: date | None, tons: Decimal | None, status: str, rate: Decimal | None, crop_path: str = "missing") -> Ticket:
    verified = status == "verified"
    return Ticket(
        batch_id=batch_id, file_id=file_id, source_page=n // 4 + 1, position=n % 4, crop_box=[0, 0, 1, 1], crop_path=crop_path,
        crop_sha256=f"{batch_id:010d}{n:054d}", ticket_number=number, delivery_date=delivered, material_type=material,
        tonnage=tons, supplier=supplier, job_location="Fictional Site", flags=[], status=status, tonnage_confirmed=verified,
        price_snapshot_state="priced" if verified and rate is not None else "pending",
        applied_unit_price=rate if verified else None, applied_price_source="rate" if verified and rate is not None else None,
    )


def build(db: Session, organization_id: int, *, tickets: int = 3000, seed: int = 7, per_batch: int = 200, crop_paths: list[str] | None = None) -> AnswerKey:
    """Fill a company with records. Deterministic for a given seed."""
    rng = random.Random(seed)
    key = AnswerKey(tickets=tickets)
    projects = [Project(organization_id=organization_id, code=code, name=name) for code, name in PROJECTS]
    # Reuses catalog entries that already exist, so this can top up a workspace.
    known_suppliers = {s.name: s for s in db.scalars(select(Supplier).where(Supplier.organization_id == organization_id))}
    known_materials = {m.name: m for m in db.scalars(select(Material).where(Material.organization_id == organization_id))}
    suppliers = [known_suppliers.get(name) or Supplier(organization_id=organization_id, name=name) for name in SUPPLIERS]
    materials = [known_materials.get(name) or Material(organization_id=organization_id, name=name) for name in MATERIALS]
    db.add_all(projects + suppliers + materials)
    db.flush()
    existing = {(r.supplier_id, r.material_id) for r in db.scalars(select(SupplierRate).where(SupplierRate.organization_id == organization_id))}
    rates: dict[tuple[str, str], Decimal] = {}
    for supplier in suppliers:
        for material in materials:
            rate = Decimal(rng.randint(1800, 7200)) / 100
            rates[(supplier.name, material.name)] = rate
            if (supplier.id, material.id) not in existing:
                db.add(SupplierRate(organization_id=organization_id, supplier_id=supplier.id, material_id=material.id, unit_price=rate))
    db.flush()

    made = 0
    batch_number = 0
    while made < tickets:
        batch_number += 1
        project = projects[batch_number % len(projects)]
        batch = Batch(organization_id=organization_id, project_id=project.id, layout="grid_2x2", intake_mode="manual", status="ready_for_review")
        db.add(batch)
        db.flush()
        file = UploadedFile(batch_id=batch.id, original_name=f"fictional-scans-{batch_number}.pdf", storage_path="missing",
                            sha256=f"{batch.id:064d}", status="ready_for_review")
        db.add(file)
        db.flush()
        rows, slots = [], []
        for n in range(min(per_batch, tickets - made)):
            supplier = rng.choice(list(SUPPLIERS))
            material = rng.choice(MATERIALS)
            written = supplier
            if rng.random() < 0.05:
                written = rng.choice(SUPPLIERS[supplier])
                key.misspelled_tickets += 1
            delivered = START + timedelta(days=rng.randint(0, 200))
            tons = Decimal(rng.randint(1200, 2600)) / 100
            roll = rng.random()
            status = "verified" if roll < 0.7 and written == supplier else "flagged" if roll < 0.75 else "skipped" if roll < 0.77 else "needs_review"
            number = str(100000 + made + n)
            blank = status == "needs_review" and rng.random() < 0.3
            rows.append(_ticket(batch.id, file.id, n, number=None if blank else number, supplier=None if blank else written,
                                material=None if blank else material, delivered=None if blank else delivered,
                                tons=None if blank else tons, status=status, rate=rates[(supplier, material)],
                                crop_path=rng.choice(crop_paths) if crop_paths else "missing"))
            slots.append(ScanSlot(batch_id=batch.id, file_id=file.id, page=n // 4 + 1, position=n % 4, crop_box=[0, 0, 1, 1],
                                  crop_path="missing", crop_sha256=rows[-1].crop_sha256, ink_fraction=0.1, detection="content"))
        db.add_all(rows + slots)
        db.flush()
        made += len(rows)
    db.commit()
    return key


def plant_invoices(db: Session, organization_id: int, key: AnswerKey) -> AnswerKey:
    """One invoice line for each kind of mistake, on tickets made just for it, so
    each line's correct result is known."""
    project = Project(organization_id=organization_id, code="INV-CHECK", name="Fictional invoice checks")
    other = Project(organization_id=organization_id, code="INV-OTHER", name="Fictional other job")
    db.add_all([project, other])
    db.flush()
    supplier, material, rate = "Planted Supplier", "Planted Stone", Decimal("30.0000")

    def batch_for(target: Project) -> tuple[Batch, UploadedFile]:
        batch = Batch(organization_id=organization_id, project_id=target.id, layout="single", intake_mode="manual", status="ready_for_review")
        db.add(batch)
        db.flush()
        file = UploadedFile(batch_id=batch.id, original_name="planted.pdf", storage_path="missing", sha256=f"{batch.id:064d}", status="ready_for_review")
        db.add(file)
        db.flush()
        return batch, file

    main_batch, main_file = batch_for(project)
    other_batch, other_file = batch_for(other)
    serial = {"n": 0}

    def delivery(day: date, tons: str, *, number: str | None = None, status: str = "verified", in_other: bool = False) -> Ticket:
        serial["n"] += 1
        batch, file = (other_batch, other_file) if in_other else (main_batch, main_file)
        ticket = _ticket(batch.id, file.id, serial["n"], number=number or f"P{serial['n']:05d}", supplier=supplier, material=material,
                         delivered=day, tons=Decimal(tons), status=status, rate=rate)
        db.add(ticket)
        db.flush()
        return ticket

    def line(case: str, day: date, listed: list[tuple[str, Decimal]] | None, *, tons: Decimal | None = None,
             billed_rate: Decimal = rate, subtotal: Decimal | None = None) -> None:
        total = tons if tons is not None else sum((t for _, t in listed or []), Decimal(0))
        item = InvoiceComparison(
            organization_id=organization_id, project_id=project.id, invoice_number=f"PL-{case}",
            supplier_name=supplier, supplier_key=norm(supplier), material_name=material, material_key=norm(material),
            date_from=day, date_to=day, billed_tons=total, billed_unit_price=billed_rate,
            billed_subtotal=subtotal if subtotal is not None else (total * billed_rate).quantize(Decimal("0.01")),
        )
        db.add(item)
        db.flush()
        for position, (number, billed) in enumerate(listed or [], start=1):
            db.add(InvoiceTicketRef(comparison_id=item.id, position=position, ticket_number=number, ticket_key=number.strip().upper(), billed_tons=billed))
        key.invoices[item.id] = case

    # A separate day per case, so no line's scope touches another's.
    days = {case: date(2026, 1, 1) + timedelta(days=i) for i, case in enumerate(INVOICE_CASES)}

    def pair(case: str) -> tuple[Ticket, Ticket]:
        return delivery(days[case], "20.000"), delivery(days[case], "22.500")

    def listing(*tickets: Ticket) -> list[tuple[str, Decimal]]:
        return [(t.ticket_number, t.tonnage) for t in tickets]

    a, b = pair("clean")
    line("clean", days["clean"], listing(a, b))
    a, b = pair("ticket_never_delivered")
    line("ticket_never_delivered", days["ticket_never_delivered"], [(a.ticket_number, a.tonnage), ("NEVER-999", b.tonnage)])
    a, b = pair("tons_inflated")
    line("tons_inflated", days["tons_inflated"], [(a.ticket_number, a.tonnage), (b.ticket_number, b.tonnage + 3)])
    a, b = pair("ticket_billed_twice")
    line("ticket_billed_twice", days["ticket_billed_twice"], listing(a, b, b))
    a, b = pair("delivery_left_off")
    line("delivery_left_off", days["delivery_left_off"], listing(a))
    a, b = pair("rate_too_high")
    line("rate_too_high", days["rate_too_high"], listing(a, b), billed_rate=Decimal("31.5000"))
    a, b = pair("bad_multiplication")
    line("bad_multiplication", days["bad_multiplication"], listing(a, b), subtotal=Decimal("1300.00"))
    day = days["leading_zeros_stripped"]
    a = delivery(day, "20.000", number="010482")
    line("leading_zeros_stripped", day, [("10482", a.tonnage)])
    day = days["ticket_from_other_project"]
    a = delivery(day, "20.000")
    stray = delivery(day, "22.500", in_other=True)
    line("ticket_from_other_project", day, listing(a, stray))
    a, b = pair("same_ticket_on_earlier_invoice")
    line("same_ticket_on_earlier_invoice", days["same_ticket_on_earlier_invoice"], listing(a, b))
    # An earlier, unrelated bill from this supplier that also lists ticket `b`.
    earlier_day = date(2025, 6, 1)
    earlier = delivery(earlier_day, "22.500")
    item = InvoiceComparison(
        organization_id=organization_id, project_id=project.id, invoice_number="PL-earlier", supplier_name=supplier,
        supplier_key=norm(supplier), material_name=material, material_key=norm(material), date_from=earlier_day, date_to=earlier_day,
        billed_tons=Decimal("22.500"), billed_unit_price=rate, billed_subtotal=Decimal("675.00"))
    db.add(item)
    db.flush()
    db.add(InvoiceTicketRef(comparison_id=item.id, position=1, ticket_number=b.ticket_number, ticket_key=b.ticket_number.upper(), billed_tons=earlier.tonnage))
    key.invoices[item.id] = "_earlier"
    day = days["delivery_still_in_review"]
    a = delivery(day, "20.000")
    pending = delivery(day, "22.500", status="needs_review")
    line("delivery_still_in_review", day, listing(a, pending))
    a, b = pair("totals_only_clean")
    line("totals_only_clean", days["totals_only_clean"], None, tons=a.tonnage + b.tonnage)
    db.commit()
    return key
