"""A person's confirmation that a verified delivery is billed in full by one
invoice line. See docs/engineering/invoice-ticket-evidence-spec.md.

Reads tickets; never changes them. Never approves an invoice.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import InvoiceComparison, InvoiceComparisonEvent, InvoiceTicketLink, Ticket, as_utc, utcnow
from .invoice_matching import ticket_key
from .pricing import norm


class ClaimConflict(Exception):
    """Another line holds one of these tickets; nothing was linked."""


def active_links(db: Session, comparison_id: int) -> list[InvoiceTicketLink]:
    return list(db.scalars(select(InvoiceTicketLink).where(
        InvoiceTicketLink.comparison_id == comparison_id, InvoiceTicketLink.released_at.is_(None),
    ).order_by(InvoiceTicketLink.id)))


def links_elsewhere(db: Session, comparison_id: int, ticket_ids: set[int]) -> dict[int, str]:
    """{ticket id: invoice number} for active links held by other lines."""
    if not ticket_ids:
        return {}
    return dict(db.execute(
        select(InvoiceTicketLink.ticket_id, InvoiceComparison.invoice_number)
        .join(InvoiceComparison, InvoiceComparison.id == InvoiceTicketLink.comparison_id)
        .where(InvoiceTicketLink.ticket_id.in_(ticket_ids), InvoiceTicketLink.released_at.is_(None),
               InvoiceTicketLink.comparison_id != comparison_id)
    ).all())


def changes_since_link(link: InvoiceTicketLink, ticket: Ticket, item: InvoiceComparison) -> list[str]:
    """Why a link can no longer be relied on. Empty means it still holds."""
    reasons = []
    if ticket.status != "verified" or not ticket.tonnage_confirmed:
        reasons.append("the delivery is no longer approved")
    elif as_utc(ticket.verified_at) != as_utc(link.ticket_verified_at):
        reasons.append("the delivery was approved again after this link")
    if ticket.tonnage != link.verified_tons:
        reasons.append("its tons changed")
    if ticket_key(ticket.ticket_number) != ticket_key(link.listed_number):
        reasons.append("its ticket number changed")
    if norm(ticket.supplier) != item.supplier_key:
        reasons.append("its supplier changed")
    if norm(ticket.material_type) != item.material_key:
        reasons.append("its material changed")
    if ticket.delivery_date is None or not item.date_from <= ticket.delivery_date <= item.date_to:
        reasons.append("its delivery date is outside this line")
    return reasons


def record(db: Session, comparison_id: int, actor_id: int | None, old: str | None, new: str | None) -> None:
    db.add(InvoiceComparisonEvent(comparison_id=comparison_id, actor_user_id=actor_id,
                                  field="ticket_evidence", old_value=old, new_value=new))


def release(db: Session, link: InvoiceTicketLink, actor_id: int | None, reason: str) -> None:
    link.released_at, link.released_by_user_id, link.release_reason = utcnow(), actor_id, reason[:200]
    record(db, link.comparison_id, actor_id, f"linked ticket {link.listed_number}", f"released: {reason}")


def claim(db: Session, comparison_id: int, tickets: list[tuple[Ticket, str]], actor_id: int | None) -> None:
    """Insert links in the caller's transaction, all or none. The database's
    partial unique index is the guard; this function doesn't check first."""
    try:
        for ticket, listed_number in tickets:
            db.add(InvoiceTicketLink(
                comparison_id=comparison_id, ticket_id=ticket.id, listed_number=listed_number,
                verified_tons=ticket.tonnage, ticket_verified_at=ticket.verified_at,
                confirmed_by_user_id=actor_id,
            ))
            record(db, comparison_id, actor_id, None, f"linked ticket {listed_number}")
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ClaimConflict("Another invoice line just linked one of these tickets. Nothing was linked.") from None
