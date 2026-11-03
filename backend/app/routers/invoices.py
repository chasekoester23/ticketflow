"""Invoice LINE reconciliation against verified delivery tickets (never payment approval).

One manually entered line per project/supplier/material/date window. Values
come from the supplier's invoice, NOT guessed by an AI model. Recompute against
currently approved Ticket rows on every read, using each ticket's locked rate.
"""
import csv
from datetime import date
from io import StringIO
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..models import Batch, InvoiceComparison, InvoiceComparisonEvent, Project, Ticket
from ..services.pricing import CENTS, cost_for, effective_price_for, norm

router = APIRouter(prefix="/api/invoice-comparisons")


class InvoiceLineIn(BaseModel):
    """One manually transcribed MATERIAL-only invoice line, excluding fees/tax."""
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    invoice_number: str = Field(min_length=1, max_length=100)
    supplier_name: str = Field(min_length=1, max_length=200)
    material_name: str = Field(min_length=1, max_length=200)
    date_from: date
    date_to: date
    billed_tons: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    billed_unit_price: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    billed_subtotal: Decimal = Field(ge=0, max_digits=14, decimal_places=2)

    @field_validator("invoice_number", "supplier_name", "material_name")
    @classmethod
    def require_real_text(cls, v: str) -> str:
        result = v.strip()
        if not result or not norm(result):
            raise ValueError("Must contain a nonempty name or invoice number")
        return result


class TicketMatch(BaseModel):
    ticket_id: int
    batch_id: int
    ticket_number: str | None
    tons: Decimal | None
    confirmed_price: Decimal | None
    review_status: str
    scope_match: Literal["exact", "unclassified"]


class InvoiceLineOut(BaseModel):
    id: int
    project_id: int
    invoice_number: str
    supplier_name: str
    material_name: str
    date_from: date
    date_to: date
    billed_tons: Decimal
    billed_unit_price: Decimal
    billed_subtotal: Decimal
    billed_extension: Decimal
    invoice_arithmetic_difference: Decimal
    verified_tons: Decimal
    verified_subtotal: Decimal | None
    quantity_difference: Decimal
    subtotal_difference: Decimal | None
    verified_count: int
    pending_count: int
    unclassified_count: int
    unpriced_count: int
    rate_mismatch_count: int
    status: Literal["matches", "discrepancy", "incomplete", "no_verified_tickets"]
    warnings: list[str]
    tickets: list[TicketMatch]
    created_at: date


def _project_or_404(db: Session, organization_id: int, project_id: int) -> None:
    if db.scalar(select(Project.id).where(
        Project.id == project_id,
        Project.organization_id == organization_id,
    )) is None:
        raise HTTPException(404, "Project not found")


def _scope_overlap(
    db: Session, organization_id: int, body: InvoiceLineIn, *,
    exclude_id: int | None = None,
) -> None:
    """Fail closed: v1 cannot allocate one delivery across overlapping invoices.

    Multiple material lines of the SAME supplier invoice are allowed, but the
    same project, supplier and material may not overlap date windows. Otherwise
    the same verified ticket could be counted in multiple comparisons.
    """
    q = select(InvoiceComparison.id).where(
        InvoiceComparison.organization_id == organization_id,
        InvoiceComparison.project_id == body.project_id,
        InvoiceComparison.supplier_key == norm(body.supplier_name),
        InvoiceComparison.material_key == norm(body.material_name),
        InvoiceComparison.date_from <= body.date_to,
        InvoiceComparison.date_to >= body.date_from,
    )
    if exclude_id is not None:
        q = q.where(InvoiceComparison.id != exclude_id)
    if db.scalar(q.limit(1)) is not None:
        raise HTTPException(
            409, "A comparison already covers this project, supplier, "
            "material and overlapping delivery dates. One ticket cannot be "
            "assigned to two invoice lines in this version.",
        )


def _validate_scope(db: Session, organization_id: int, body: InvoiceLineIn, *,
                    exclude_id: int | None = None) -> None:
    if body.date_from > body.date_to:
        raise HTTPException(422, "Delivery date 'from' must be on or before 'to'")
    _project_or_404(db, organization_id, body.project_id)
    _scope_overlap(db, organization_id, body, exclude_id=exclude_id)


def _assign(item: InvoiceComparison, body: InvoiceLineIn) -> None:
    for field, value in body.model_dump().items():
        setattr(item, field, value)
    item.supplier_key = norm(body.supplier_name)
    item.material_key = norm(body.material_name)


def _line_out(db: Session, item: InvoiceComparison, organization_id: int) -> InvoiceLineOut:
    q = (
        select(Ticket)
        .join(Batch, Batch.id == Ticket.batch_id)
        .where(
            Batch.organization_id == organization_id,
            Batch.project_id == item.project_id,
            Ticket.delivery_date >= item.date_from,
            Ticket.delivery_date <= item.date_to,
            Ticket.status != "skipped",
        )
        .order_by(Ticket.delivery_date, Ticket.id)
    )
    all_tickets = [
        t for t in db.scalars(q)
        if norm(t.supplier) == item.supplier_key
        and norm(t.material_type) == item.material_key
    ]
    verified = [
        t for t in all_tickets
        if t.status == "verified" and t.tonnage_confirmed
    ]
    pending = [t for t in all_tickets if t not in verified]
    # A ticket still under review might lack a date, supplier or material,
    # so it cannot be definitively assigned to an invoice scope. Never show
    # a clean match while a potentially relevant unknown ticket is present.
    # If any KNOWN attribute contradicts the invoice scope, exclude it.
    candidate_q = select(Ticket).join(Batch, Batch.id == Ticket.batch_id).where(
        Batch.organization_id == organization_id,
        Batch.project_id == item.project_id,
        Ticket.status.notin_(["verified", "skipped"]),
    )
    scoped_ids = {t.id for t in all_tickets}
    unclassified = []
    for ticket in db.scalars(candidate_q):
        if ticket.id in scoped_ids:
            continue
        if ticket.delivery_date is not None and not (item.date_from <= ticket.delivery_date <= item.date_to):
            continue
        if norm(ticket.supplier) not in ("", item.supplier_key):
            continue
        if norm(ticket.material_type) not in ("", item.material_key):
            continue
        unclassified.append(ticket)
    verified_tons = sum((t.tonnage or Decimal(0) for t in verified), Decimal(0))
    unpriced = [t for t in verified if t.price_snapshot_state != "priced" or t.applied_unit_price is None]
    rate_mismatch = [
        t for t in verified
        if t.price_snapshot_state == "priced"
        and t.applied_unit_price is not None
        and t.applied_unit_price != item.billed_unit_price
    ]
    prices = [effective_price_for(t, {})[0] for t in verified]
    costs = [cost_for(t, price) for t, price in zip(verified, prices)]
    # Never turn missing prices into a false $0.00 match.
    verified_subtotal = (
        sum((c for c in costs if c is not None), Decimal(0))
        if verified and not unpriced else None
    )
    billed_extension = (item.billed_tons * item.billed_unit_price).quantize(CENTS)
    arithmetic_difference = item.billed_subtotal - billed_extension
    quantity_difference = item.billed_tons - verified_tons
    subtotal_difference = (
        item.billed_subtotal - verified_subtotal
        if verified_subtotal is not None else None
    )

    warnings = []
    if not verified:
        warnings.append("No verified delivery tickets match this invoice line.")
    if pending:
        warnings.append(f"{len(pending)} matching ticket(s) still need review; comparison is incomplete.")
    if unclassified:
        warnings.append(
            f"{len(unclassified)} unreviewed project ticket(s) lack enough date/supplier/material "
            "information to rule out this invoice; classification is required."
        )
    if unpriced:
        warnings.append(f"{len(unpriced)} verified ticket(s) have no locked unit price.")
    if rate_mismatch:
        warnings.append(f"{len(rate_mismatch)} verified ticket rate(s) differ from the invoice rate.")
    if quantity_difference:
        warnings.append("Billed tons differ from verified ticket tons.")
    if arithmetic_difference:
        warnings.append("Invoice line subtotal differs from billed tons × billed unit rate.")
    if subtotal_difference:
        warnings.append("Invoice material subtotal differs from verified ticket material cost.")

    if not verified:
        status = "no_verified_tickets"
    elif pending or unclassified or unpriced:
        status = "incomplete"
    elif quantity_difference or arithmetic_difference or subtotal_difference or rate_mismatch:
        status = "discrepancy"
    else:
        status = "matches"

    return InvoiceLineOut(
        id=item.id, project_id=item.project_id,
        invoice_number=item.invoice_number,
        supplier_name=item.supplier_name, material_name=item.material_name,
        date_from=item.date_from, date_to=item.date_to,
        billed_tons=item.billed_tons, billed_unit_price=item.billed_unit_price,
        billed_subtotal=item.billed_subtotal, billed_extension=billed_extension,
        invoice_arithmetic_difference=arithmetic_difference,
        verified_tons=verified_tons, verified_subtotal=verified_subtotal,
        quantity_difference=quantity_difference,
        subtotal_difference=subtotal_difference,
        verified_count=len(verified), pending_count=len(pending),
        unclassified_count=len(unclassified),
        unpriced_count=len(unpriced), rate_mismatch_count=len(rate_mismatch),
        status=status, warnings=warnings,
        tickets=[
            TicketMatch(
                ticket_id=t.id, batch_id=t.batch_id,
                ticket_number=t.ticket_number, tons=t.tonnage,
                review_status=t.status,
                scope_match="exact" if t.id in scoped_ids else "unclassified",
                confirmed_price=(
                    t.applied_unit_price if t.status == "verified"
                    and t.price_snapshot_state == "priced" else None
                ),
            )
            for t in all_tickets + unclassified
        ],
        created_at=item.created_at.date() if hasattr(item.created_at, "date") else item.created_at,
    )


def _owned_or_404(db: Session, organization_id: int, line_id: int) -> InvoiceComparison:
    item = db.scalar(select(InvoiceComparison).where(
        InvoiceComparison.id == line_id,
        InvoiceComparison.organization_id == organization_id,
    ))
    if not item:
        raise HTTPException(404, "Invoice comparison not found")
    return item


@router.get("", response_model=list[InvoiceLineOut])
def list_comparisons(db: Session = Depends(get_db),
                     organization_id: int = Depends(current_organization_id)):
    items = db.scalars(select(InvoiceComparison).where(
        InvoiceComparison.organization_id == organization_id
    ).order_by(InvoiceComparison.id.desc())).all()
    return [_line_out(db, item, organization_id) for item in items]


@router.post("", response_model=InvoiceLineOut, status_code=201)
def create_comparison(body: InvoiceLineIn, db: Session = Depends(get_db),
                      organization_id: int = Depends(current_organization_id)):
    _validate_scope(db, organization_id, body)
    item = InvoiceComparison(organization_id=organization_id)
    _assign(item, body)
    db.add(item)
    db.commit()
    return _line_out(db, item, organization_id)


@router.get("/{line_id}", response_model=InvoiceLineOut)
def get_comparison(line_id: int, db: Session = Depends(get_db),
                   organization_id: int = Depends(current_organization_id)):
    return _line_out(db, _owned_or_404(db, organization_id, line_id), organization_id)


@router.put("/{line_id}", response_model=InvoiceLineOut)
def correct_comparison(line_id: int, body: InvoiceLineIn, db: Session = Depends(get_db),
                       organization_id: int = Depends(current_organization_id)):
    item = _owned_or_404(db, organization_id, line_id)
    _validate_scope(db, organization_id, body, exclude_id=line_id)
    changes = body.model_dump()
    for field, new in changes.items():
        old = getattr(item, field)
        if old != new:
            db.add(InvoiceComparisonEvent(
                comparison_id=line_id, field=field,
                old_value=str(old), new_value=str(new),
            ))
    _assign(item, body)
    db.commit()
    return _line_out(db, item, organization_id)


@router.get("/{line_id}/audit")
def comparison_history(line_id: int, db: Session = Depends(get_db),
                       organization_id: int = Depends(current_organization_id)):
    _owned_or_404(db, organization_id, line_id)
    events = db.scalars(
        select(InvoiceComparisonEvent)
        .where(InvoiceComparisonEvent.comparison_id == line_id)
        .order_by(InvoiceComparisonEvent.id)
    ).all()
    return [
        {"field": e.field, "old": e.old_value, "new": e.new_value, "at": e.created_at}
        for e in events
    ]


def _csv_text(value: str | None) -> str:
    """Make untrusted document text inert when a CSV is opened in Excel.

    CSV quoting alone does not stop spreadsheet formula injection. Preserve the
    visible source text while preventing formula interpretation by spreadsheet apps.
    """
    value = value or ""
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


@router.get("/{line_id}/report.csv")
def export_comparison_report(line_id: int, db: Session = Depends(get_db),
                             organization_id: int = Depends(current_organization_id)):
    """Download a human-reviewable snapshot, never a payment authorization."""
    item = _owned_or_404(db, organization_id, line_id)
    result = _line_out(db, item, organization_id)
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["TicketFlow invoice comparison — NOT approval for payment"])
    writer.writerow(["Invoice number", _csv_text(result.invoice_number)])
    writer.writerow(["Project ID", result.project_id])
    writer.writerow(["Supplier", _csv_text(result.supplier_name)])
    writer.writerow(["Material", _csv_text(result.material_name)])
    writer.writerow(["Delivery from", result.date_from.isoformat()])
    writer.writerow(["Delivery through", result.date_to.isoformat()])
    writer.writerow(["Comparison status", result.status])
    writer.writerow(["Invoice tons", str(result.billed_tons)])
    writer.writerow(["Invoice rate per ton", str(result.billed_unit_price)])
    writer.writerow(["Invoice material subtotal", str(result.billed_subtotal)])
    writer.writerow(["Billed tons times rate", str(result.billed_extension)])
    writer.writerow(["Invoice arithmetic difference", str(result.invoice_arithmetic_difference)])
    writer.writerow(["Verified tons", str(result.verified_tons)])
    writer.writerow(["Verified material subtotal",
                     str(result.verified_subtotal) if result.verified_subtotal is not None else "UNRESOLVED"])
    writer.writerow(["Billed minus verified tons", str(result.quantity_difference)])
    writer.writerow(["Billed minus verified subtotal",
                     str(result.subtotal_difference) if result.subtotal_difference is not None else "UNRESOLVED"])
    writer.writerow(["Verified ticket count", result.verified_count])
    writer.writerow(["Pending ticket count", result.pending_count])
    writer.writerow(["Unclassified ticket count", result.unclassified_count])
    writer.writerow(["Unpriced ticket count", result.unpriced_count])
    writer.writerow(["Rate mismatch count", result.rate_mismatch_count])
    writer.writerow([])
    writer.writerow(["Review warnings"])
    for warning in result.warnings:
        writer.writerow([warning])
    if not result.warnings:
        writer.writerow(["No arithmetic discrepancies detected; verify source completeness independently."])
    writer.writerow([])
    writer.writerow(["Tickets — potential matches must be classified before relying on totals"])
    writer.writerow(["Ticket ID", "Batch ID", "Ticket number", "Scope classification",
                     "Review status", "Ticket tons (not necessarily verified)", "Locked rate (if verified)"])
    for ticket in result.tickets:
        writer.writerow([
            ticket.ticket_id, ticket.batch_id, _csv_text(ticket.ticket_number),
            ticket.scope_match, ticket.review_status,
            str(ticket.tons) if ticket.tons is not None else "UNRESOLVED",
            str(ticket.confirmed_price) if ticket.confirmed_price is not None else "UNRESOLVED",
        ])
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="ticketflow-invoice-{line_id}.csv"',
                 "Cache-Control": "no-store"},
    )
