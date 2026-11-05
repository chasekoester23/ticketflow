"""Invoice LINE reconciliation against verified delivery tickets (never payment approval).

One manually entered line per project/supplier/material/date window. Values
come from the supplier's invoice, NOT guessed by an AI model. Recompute against
currently approved Ticket rows on every read, using each ticket's locked rate.

A line may also list the invoice's individual ticket numbers. Those are
checked one by one (services/invoice_matching.py). A line without them is a
totals-only comparison and is always labelled as such.
"""
import csv
from collections import Counter
from datetime import date, datetime
from io import StringIO
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..permissions import require
from ..models import (
    InvoiceComparison, InvoiceComparisonEvent, InvoiceTicketLink, InvoiceTicketRef,
    Project, Ticket, User, as_utc,
)
from ..services import billing_exceptions, invoice_bridge, ticket_evidence
from ..services.accounts import names_by_id
from ..services.invoice_context import LineContext
from ..services.invoice_matching import (
    ParsedRef, TicketListError, check_tickets, format_ticket_list, parse_ticket_list, ticket_key,
)
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
    # Optional: the invoice's ticket numbers, one per line, each optionally
    # followed by billed tons (pasted from Excel). Empty = totals-only.
    # The whole list is replaced on correction.
    ticket_list: str = Field(default="", max_length=200_000)

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


class CheckedTicket(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ticket_id: int
    batch_id: int
    ticket_number: str | None
    delivery_date: date | None
    supplier: str | None
    material: str | None
    tons: Decimal | None
    review_status: str
    project: str | None


class TicketCheckRow(BaseModel):
    """One invoice ticket line (line is set) or one delivery the invoice omits."""
    model_config = ConfigDict(from_attributes=True)
    result: Literal["matched", "tons_differ", "not_found", "outside_scope",
                    "ambiguous", "needs_review", "not_on_invoice"]
    line: int | None
    invoice_ticket_number: str | None
    billed_tons: Decimal | None
    ticket: CheckedTicket | None
    candidates: list[CheckedTicket]
    verified_tons: Decimal | None
    tons_difference: Decimal | None
    flags: list[Literal["duplicate_on_invoice", "on_another_invoice"]]
    notes: list[str]
    # unmatched | candidate | confirmed | conflicting | invalidated
    evidence: str = "unmatched"
    link_id: int | None = None


class TicketLinkOut(BaseModel):
    id: int
    ticket_id: int
    batch_id: int
    listed_number: str
    verified_tons: Decimal
    state: Literal["confirmed", "invalidated"]
    reasons: list[str]
    confirmed_by: str | None
    confirmed_at: datetime


class EvidenceSummary(BaseModel):
    listed_tickets: int
    confirmed: int
    candidates: int
    conflicting: int
    invalidated: int
    unmatched: int
    confirmed_tons: Decimal


class TicketLinksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_ids: list[int] = Field(min_length=1, max_length=2000)


class BridgeItem(BaseModel):
    kind: str
    label: str
    tons: Decimal
    count: int
    tickets: list[str]


class TonsBridge(BaseModel):
    start: Decimal
    items: list[BridgeItem]
    billed: Decimal
    complete: bool
    rows_without_tons: int
    unexplained: Decimal | None


class RateGroup(BaseModel):
    locked_rate: Decimal
    tickets: int
    tons: Decimal
    effect: Decimal


class MoneyBridge(BaseModel):
    start: Decimal
    quantity_effect: Decimal
    rate_effect: Decimal
    rate_groups: list[RateGroup]
    arithmetic_effect: Decimal
    rounding: Decimal
    billed: Decimal


class NextStep(BaseModel):
    text: str
    href: str | None


class Diagnosis(BaseModel):
    """Why this result, in figures that add up exactly. Explains; never decides."""
    headline: str
    tons: TonsBridge | None
    money: MoneyBridge | None
    money_unresolved_reason: str | None
    next_steps: list[NextStep]


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
    # "totals_only": no ticket numbers entered, individual tickets NOT checked.
    comparison_mode: Literal["itemized", "totals_only"]
    warnings: list[str]
    tickets: list[TicketMatch]
    ticket_check: list[TicketCheckRow]
    ticket_check_counts: dict[str, int]
    itemized_billed_tons: Decimal | None
    ticket_links: list[TicketLinkOut] = []
    evidence: EvidenceSummary | None = None
    diagnosis: Diagnosis | None = None
    # The uploaded invoice this line was confirmed from, if any.
    source_document_id: int | None = None
    source_page: int | None = None
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


def _parse_tickets(body: InvoiceLineIn) -> list[ParsedRef]:
    try:
        return parse_ticket_list(body.ticket_list)
    except TicketListError as exc:
        raise HTTPException(422, str(exc)) from None


def _refs(db: Session, comparison_id: int) -> list[InvoiceTicketRef]:
    return list(db.scalars(
        select(InvoiceTicketRef)
        .where(InvoiceTicketRef.comparison_id == comparison_id)
        .order_by(InvoiceTicketRef.position)
    ))


def _replace_refs(db: Session, comparison_id: int, parsed: list[ParsedRef]) -> None:
    # Executed immediately, so the new rows never collide with the old positions.
    db.execute(delete(InvoiceTicketRef).where(InvoiceTicketRef.comparison_id == comparison_id))
    db.add_all(
        InvoiceTicketRef(
            comparison_id=comparison_id, position=ref.position, ticket_number=ref.ticket_number,
            ticket_key=ticket_key(ref.ticket_number), billed_tons=ref.billed_tons,
        )
        for ref in parsed
    )


def _assign(item: InvoiceComparison, body: InvoiceLineIn) -> None:
    for field, value in body.model_dump(exclude={"ticket_list"}).items():
        setattr(item, field, value)
    item.supplier_key = norm(body.supplier_name)
    item.material_key = norm(body.material_name)


def add_comparison(db: Session, body: InvoiceLineIn, user: User) -> InvoiceComparison:
    """The one way a comparison is created, typed or confirmed from a draft, so
    both paths get the same checks and the same math. Caller commits."""
    _validate_scope(db, user.organization_id, body)
    parsed = _parse_tickets(body)
    item = InvoiceComparison(organization_id=user.organization_id, created_by_user_id=user.id)
    _assign(item, body)
    db.add(item)
    db.flush()
    _replace_refs(db, item.id, parsed)
    return item


def _line_out(db: Session, item: InvoiceComparison, organization_id: int,
              context: LineContext | None = None) -> InvoiceLineOut:
    """One line's comparison, always recomputed from current tickets. Pass one
    LineContext across a list, so each line doesn't re-read the same rows."""
    context = context or LineContext(db, organization_id, [item])
    all_tickets = [
        t for t in context.in_scope(item.project_id, item.supplier_key, item.material_key)
        if t.delivery_date is not None and item.date_from <= t.delivery_date <= item.date_to
    ]
    verified = [
        t for t in all_tickets
        if t.status == "verified" and t.tonnage_confirmed
    ]
    verified_ids = {t.id for t in verified}
    pending = [t for t in all_tickets if t.id not in verified_ids]
    # A ticket still under review might lack a date, supplier or material,
    # so it cannot be definitively assigned to an invoice scope. Never show
    # a clean match while a potentially relevant unknown ticket is present.
    # If any KNOWN attribute contradicts the invoice scope, exclude it.
    scoped_ids = {t.id for t in all_tickets}
    unclassified = []
    for ticket in context.unreviewed(item.project_id):
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

    refs = context.refs(item.id)
    check = check_tickets(db, organization_id, item, refs, all_tickets, context.numbers) if refs else None

    # Links people confirmed, checked against the delivery as it is now.
    links = context.links(item.id)
    linked_tickets = context.linked_tickets
    people = context.people if links else {}
    link_changes = {link.id: ticket_evidence.changes_since_link(link, linked_tickets[link.ticket_id], item)
                    for link in links}
    links_by_ticket = {link.ticket_id: link for link in links}
    row_tickets = {row.ticket.ticket_id for row in check.rows if row.ticket} if check else set()
    held_elsewhere = context.links_elsewhere(item.id, row_tickets)
    rows_out: list[TicketCheckRow] = []
    for row in (check.rows if check else []):
        out = TicketCheckRow.model_validate(row)
        link = links_by_ticket.get(row.ticket.ticket_id) if row.ticket else None
        elsewhere = held_elsewhere.get(row.ticket.ticket_id) if row.ticket else None
        if link is not None:
            out.link_id = link.id
            out.evidence = "invalidated" if link_changes[link.id] else "confirmed"
            if link_changes[link.id]:
                out.notes.append("Linked earlier, but " + ", ".join(link_changes[link.id]) + ".")
        elif elsewhere:
            out.evidence = "conflicting"
            out.notes.append(f"Already linked to invoice {elsewhere}.")
        elif row.result in ("ambiguous", "tons_differ") or row.flags:
            out.evidence = "conflicting"
        elif row.result == "matched":
            out.evidence = "candidate"
        rows_out.append(out)

    # Tickets that never became records can't appear in any total above.
    intake_issues = context.intake_issues(item.project_id)

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
    if check:
        warnings.extend(check.warnings)
    warnings.extend(intake_issues)
    invalidated = [link for link in links if link_changes[link.id]]
    if invalidated:
        warnings.append(f"{len(invalidated)} confirmed ticket link(s) no longer hold because the delivery "
                        "changed. Re-confirm or release them.")
    newly_conflicting = sum(1 for row in rows_out if row.ticket and row.ticket.ticket_id in held_elsewhere)
    if newly_conflicting:
        warnings.append(f"{newly_conflicting} ticket(s) on this line are already linked to another invoice line.")

    if not verified:
        status = "no_verified_tickets"
    elif pending or unclassified or unpriced or intake_issues or invalidated or (check and check.incomplete):
        status = "incomplete"
    elif (quantity_difference or arithmetic_difference or subtotal_difference or rate_mismatch
          or newly_conflicting or (check and check.discrepancy)):
        status = "discrepancy"
    else:
        status = "matches"

    source = context.source(item.id)
    tons_detail = invoice_bridge.tons_bridge(
        verified_ids=verified_ids, verified_tons=verified_tons, billed_tons=item.billed_tons, rows=check.rows,
    ) if check else None
    money_detail = invoice_bridge.money_bridge(
        verified_rows=verified, verified_subtotal=verified_subtotal, billed_subtotal=item.billed_subtotal,
        billed_tons=item.billed_tons, billed_rate=item.billed_unit_price,
    )
    diagnosis = Diagnosis(
        headline=invoice_bridge.headline(
            status=status, itemized=bool(check), quantity_difference=quantity_difference,
            subtotal_difference=subtotal_difference, pending=len(pending), unclassified=len(unclassified),
            verified_count=len(verified)),
        tons=tons_detail, money=money_detail,
        money_unresolved_reason=None if money_detail else (
            f"{len(unpriced)} verified ticket(s) have no locked price, so the approved cost isn't known." if unpriced
            else "No approved deliveries to compare with."),
        next_steps=invoice_bridge.actions(
            tons=tons_detail, itemized=bool(check), pending=len(pending), unclassified=len(unclassified),
            unpriced=len(unpriced), rate_mismatch=len(rate_mismatch),
            other_invoice=[row.invoice_ticket_number for row in rows_out if "on_another_invoice" in row.flags],
            intake_issues=intake_issues, project_id=item.project_id, supplier=item.supplier_name),
    )

    return InvoiceLineOut(
        id=item.id, project_id=item.project_id,
        source_document_id=source[0] if source else None, source_page=source[1] if source else None,
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
        status=status, comparison_mode="itemized" if check else "totals_only",
        warnings=warnings, diagnosis=diagnosis,
        ticket_check=rows_out,
        ticket_links=[TicketLinkOut(
            id=link.id, ticket_id=link.ticket_id, batch_id=linked_tickets[link.ticket_id].batch_id,
            listed_number=link.listed_number, verified_tons=link.verified_tons,
            state="invalidated" if link_changes[link.id] else "confirmed", reasons=link_changes[link.id],
            confirmed_by=people.get(link.confirmed_by_user_id), confirmed_at=as_utc(link.confirmed_at),
        ) for link in links],
        evidence=EvidenceSummary(
            listed_tickets=sum(1 for row in rows_out if row.line is not None),
            confirmed=sum(1 for row in rows_out if row.evidence == "confirmed"),
            candidates=sum(1 for row in rows_out if row.evidence == "candidate"),
            conflicting=sum(1 for row in rows_out if row.evidence == "conflicting"),
            invalidated=sum(1 for row in rows_out if row.evidence == "invalidated"),
            unmatched=sum(1 for row in rows_out if row.evidence == "unmatched"),
            confirmed_tons=sum((link.verified_tons for link in links if not link_changes[link.id]), Decimal(0)),
        ) if check else None,
        ticket_check_counts=check.counts if check else {},
        itemized_billed_tons=check.itemized_billed_tons if check else None,
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


@router.get("/overview")
def overview(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Across all lines: what's billed against what's approved, and billing
    patterns worth a question. Possible problems for a person to check, never findings."""
    items = list(db.scalars(select(InvoiceComparison).where(InvoiceComparison.organization_id == organization_id)))
    context = LineContext(db, organization_id, items)
    lines = [_line_out(db, item, organization_id, context) for item in items]
    by_supplier: dict[str, dict] = {}
    statuses: Counter = Counter()
    for line in lines:
        statuses[line.status] += 1
        group = by_supplier.setdefault(norm(line.supplier_name), {
            "supplier": line.supplier_name, "lines": 0, "billed": Decimal(0), "verified": Decimal(0),
            "lines_unresolved": 0, "open": 0})
        group["lines"] += 1
        group["billed"] += line.billed_subtotal
        group["open"] += 1 if line.status != "matches" else 0
        if line.verified_subtotal is None:
            group["lines_unresolved"] += 1
        else:
            group["verified"] += line.verified_subtotal
    return {
        "lines": len(lines), "by_status": dict(statuses),
        "billed": str(sum((line.billed_subtotal for line in lines), Decimal(0))),
        "verified": str(sum((line.verified_subtotal or Decimal(0) for line in lines), Decimal(0))),
        "lines_unresolved": sum(1 for line in lines if line.verified_subtotal is None),
        "by_supplier": [{**group, "billed": str(group["billed"]), "verified": str(group["verified"])}
                        for _, group in sorted(by_supplier.items())],
        "exceptions": billing_exceptions.find(db, organization_id, items),
    }


@router.get("", response_model=list[InvoiceLineOut])
def list_comparisons(db: Session = Depends(get_db),
                     organization_id: int = Depends(current_organization_id)):
    items = db.scalars(select(InvoiceComparison).where(
        InvoiceComparison.organization_id == organization_id
    ).order_by(InvoiceComparison.id.desc())).all()
    context = LineContext(db, organization_id, items)
    return [_line_out(db, item, organization_id, context) for item in items]


@router.post("", response_model=InvoiceLineOut, status_code=201)
def create_comparison(body: InvoiceLineIn, db: Session = Depends(get_db),
                      user: User = Depends(require("invoices.manage"))):
    item = add_comparison(db, body, user)
    db.commit()
    return _line_out(db, item, user.organization_id)


@router.get("/{line_id}", response_model=InvoiceLineOut)
def get_comparison(line_id: int, db: Session = Depends(get_db),
                   organization_id: int = Depends(current_organization_id)):
    return _line_out(db, _owned_or_404(db, organization_id, line_id), organization_id)


@router.put("/{line_id}", response_model=InvoiceLineOut)
def correct_comparison(line_id: int, body: InvoiceLineIn, db: Session = Depends(get_db),
                       user: User = Depends(require("invoices.manage"))):
    organization_id = user.organization_id
    item = _owned_or_404(db, organization_id, line_id)
    _validate_scope(db, organization_id, body, exclude_id=line_id)
    parsed = _parse_tickets(body)
    changes = body.model_dump(exclude={"ticket_list"})
    for field, new in changes.items():
        old = getattr(item, field)
        if old != new:
            db.add(InvoiceComparisonEvent(
                comparison_id=line_id, actor_user_id=user.id, field=field,
                old_value=str(old), new_value=str(new),
            ))
    old_list, new_list = format_ticket_list(_refs(db, line_id)), format_ticket_list(parsed)
    if old_list != new_list:
        # The full previous list stays in the history; nothing is lost.
        db.add(InvoiceComparisonEvent(
            comparison_id=line_id, actor_user_id=user.id, field="ticket_list",
            old_value=old_list or "(totals only)", new_value=new_list or "(totals only)",
        ))
        _replace_refs(db, line_id, parsed)
    _assign(item, body)
    db.flush()
    # A link only stands while this line still lists and scopes that delivery.
    result = _line_out(db, item, organization_id)
    listed = {row.ticket.ticket_id for row in result.ticket_check if row.line is not None and row.ticket}
    released = False
    for link in ticket_evidence.active_links(db, line_id):
        if link.ticket_id not in listed:
            ticket_evidence.release(db, link, user.id, "invoice line corrected")
            released = True
    db.commit()
    return _line_out(db, item, organization_id) if released else result


@router.post("/{line_id}/ticket-links", response_model=InvoiceLineOut)
def confirm_ticket_links(line_id: int, body: TicketLinksIn, db: Session = Depends(get_db),
                         user: User = Depends(require("invoices.manage"))):
    """A manager confirms deliveries are billed in full by this line. All or
    nothing. Only clean matches qualify, so partial billing can't be linked."""
    organization_id = user.organization_id
    item = _owned_or_404(db, organization_id, line_id)
    rows = {row.ticket.ticket_id: row for row in _line_out(db, item, organization_id).ticket_check
            if row.ticket and row.line is not None}
    refused, wanted, stale = [], [], []
    for ticket_id in dict.fromkeys(body.ticket_ids):
        row = rows.get(ticket_id)
        if row is None:
            refused.append(f"#{ticket_id}: not listed on this invoice line")
        elif row.evidence == "confirmed":
            continue  # already linked: repeating is harmless
        elif row.result == "matched" and not row.flags and row.evidence in ("candidate", "invalidated"):
            wanted.append(row)
            if row.evidence == "invalidated":
                stale.append(row.link_id)
        else:
            refused.append(f"{row.invoice_ticket_number}: not a clean match ({row.evidence})")
    if refused:
        raise HTTPException(409, "Nothing was linked. " + "; ".join(refused[:10]) + ".")
    for link in ticket_evidence.active_links(db, line_id):
        if link.id in stale:
            ticket_evidence.release(db, link, user.id, "re-confirmed by a person")
    db.flush()
    tickets = {t.id: t for t in db.scalars(select(Ticket).where(Ticket.id.in_([r.ticket.ticket_id for r in wanted])))}
    try:
        ticket_evidence.claim(db, line_id, [(tickets[r.ticket.ticket_id], r.invoice_ticket_number) for r in wanted], user.id)
    except ticket_evidence.ClaimConflict as exc:
        raise HTTPException(409, str(exc)) from None
    db.commit()
    return _line_out(db, item, organization_id)


@router.post("/{line_id}/ticket-links/{link_id}/release", response_model=InvoiceLineOut)
def release_ticket_link(line_id: int, link_id: int, db: Session = Depends(get_db),
                        user: User = Depends(require("invoices.manage"))):
    organization_id = user.organization_id
    item = _owned_or_404(db, organization_id, line_id)
    link = db.scalar(select(InvoiceTicketLink).where(
        InvoiceTicketLink.id == link_id, InvoiceTicketLink.comparison_id == line_id))
    if link is None:
        raise HTTPException(404, "Ticket link not found")
    if link.released_at is None:
        ticket_evidence.release(db, link, user.id, "released by a person")
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
    people = names_by_id(db, organization_id)
    return [
        {"field": e.field, "old": e.old_value, "new": e.new_value, "at": as_utc(e.created_at),
         "actor": people.get(e.actor_user_id) if e.actor_user_id else None}
        for e in events
    ]


MODE_LABELS = {
    "itemized": "Itemized - each invoice ticket number checked",
    "totals_only": "Totals-only comparison - individual tickets not checked",
}
RESULT_LABELS = {
    "matched": "Matched",
    "tons_differ": "Tons differ",
    "not_found": "Not found in TicketFlow",
    "outside_scope": "Outside this invoice line",
    "ambiguous": "Ambiguous - several records",
    "needs_review": "Delivery not verified or incomplete",
    "not_on_invoice": "Delivery not on invoice",
    "duplicate_on_invoice": "Repeated on this invoice",
    "on_another_invoice": "Also on another invoice",
}


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
    writer.writerow(["Comparison type", MODE_LABELS[result.comparison_mode]])
    writer.writerow(["Source invoice document",
                     f"Document {result.source_document_id}, page {result.source_page}"
                     if result.source_document_id else "Entered by hand"])
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
    if result.comparison_mode == "itemized":
        writer.writerow(["Invoice ticket lines", sum(1 for r in result.ticket_check if r.line is not None)])
        writer.writerow(["Itemized billed tons",
                         str(result.itemized_billed_tons) if result.itemized_billed_tons is not None
                         else "NOT ENTERED FOR EVERY TICKET"])
        for name, count in result.ticket_check_counts.items():
            writer.writerow([RESULT_LABELS[name] + " count", count])
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
    if result.ticket_links:
        writer.writerow([])
        writer.writerow(["Ticket evidence confirmed by people"])
        writer.writerow(["Ticket number", "TicketFlow ticket ID", "Tons when confirmed", "State", "Confirmed by", "Confirmed at (UTC)", "Reasons"])
        for link in result.ticket_links:
            writer.writerow([_csv_text(link.listed_number), link.ticket_id, str(link.verified_tons), link.state,
                             _csv_text(link.confirmed_by or "Not recorded"), link.confirmed_at.isoformat(),
                             _csv_text("; ".join(link.reasons))])
    if result.comparison_mode == "itemized":
        writer.writerow([])
        writer.writerow(["Ticket-by-ticket check"])
        writer.writerow(["Invoice line", "Invoice ticket number", "Billed tons", "Result", "Flags",
                         "TicketFlow ticket ID", "Batch ID", "Recorded ticket number", "Delivery date",
                         "Review status", "Verified tons", "Billed minus verified tons", "Notes"])
        for row in result.ticket_check:
            t = row.ticket
            writer.writerow([
                row.line if row.line is not None else "NOT ON INVOICE",
                _csv_text(row.invoice_ticket_number),
                str(row.billed_tons) if row.billed_tons is not None else "",
                RESULT_LABELS[row.result],
                "; ".join(RESULT_LABELS[f] for f in row.flags),
                t.ticket_id if t else "; ".join(str(c.ticket_id) for c in row.candidates),
                t.batch_id if t else "",
                _csv_text(t.ticket_number if t else None),
                t.delivery_date.isoformat() if t and t.delivery_date else "",
                t.review_status if t else "",
                str(row.verified_tons) if row.verified_tons is not None else "",
                str(row.tons_difference) if row.tons_difference is not None else "",
                _csv_text(" ".join(row.notes)),
            ])
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="ticketflow-invoice-{line_id}.csv"',
                 "Cache-Control": "no-store"},
    )
