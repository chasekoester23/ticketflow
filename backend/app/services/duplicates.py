"""Duplicate detection is advisory, never destructive.

Build one index for batch/records views rather than re-reading every stored
ticket once per displayed row. Comparison precedence and explanations must
match the original single-ticket implementation.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Ticket
from .pricing import norm


class DuplicateIndex:
    """O(N) indexing; O(matches) per ticket rather than O(N) per ticket."""

    def __init__(self, tickets: list[Ticket]):
        self.by_hash: dict[str, list[Ticket]] = defaultdict(list)
        self.by_number: dict[tuple[str, str], list[Ticket]] = defaultdict(list)
        self.by_load: dict[tuple[str, date, Decimal, str], list[Ticket]] = defaultdict(list)

        for ticket in tickets:
            if ticket.status == "skipped":
                continue
            if ticket.crop_sha256:
                self.by_hash[ticket.crop_sha256].append(ticket)
            if ticket.supplier and ticket.ticket_number:
                self.by_number[(norm(ticket.supplier), ticket.ticket_number)].append(ticket)
            if (ticket.supplier and ticket.delivery_date and ticket.tonnage is not None
                    and ticket.material_type):
                self.by_load[(
                    norm(ticket.supplier), ticket.delivery_date, ticket.tonnage,
                    norm(ticket.material_type),
                )].append(ticket)

    def find(self, ticket: Ticket) -> list[str]:
        matches: dict[int, Ticket] = {}
        if ticket.crop_sha256:
            matches.update({t.id: t for t in self.by_hash.get(ticket.crop_sha256, ())})
        if ticket.supplier and ticket.ticket_number:
            matches.update({t.id: t for t in self.by_number.get(
                (norm(ticket.supplier), ticket.ticket_number), (),
            )})
        if (ticket.supplier and ticket.delivery_date and ticket.tonnage is not None
                and ticket.material_type):
            matches.update({t.id: t for t in self.by_load.get((
                norm(ticket.supplier), ticket.delivery_date, ticket.tonnage,
                norm(ticket.material_type),
            ), ())})

        reasons: list[str] = []
        # Preserve SQL's previous primary-key ordering and reason precedence.
        for match_id in sorted(matches):
            other = matches[match_id]
            if other.id == ticket.id:
                continue
            where = f"batch {other.batch_id}, page {other.source_page}, ticket {other.position + 1}"
            if ticket.crop_sha256 and other.crop_sha256 == ticket.crop_sha256:
                reasons.append(f"Identical scanned image to an existing ticket ({where})")
                continue
            same_supplier = bool(ticket.supplier) and norm(other.supplier) == norm(ticket.supplier)
            if same_supplier and ticket.ticket_number and other.ticket_number == ticket.ticket_number:
                reasons.append(
                    f"Same supplier and ticket number {ticket.ticket_number} "
                    f"as an existing ticket ({where})"
                )
                continue
            if (same_supplier and ticket.delivery_date and other.delivery_date == ticket.delivery_date
                    and ticket.tonnage is not None and other.tonnage == ticket.tonnage
                    and norm(other.material_type) == norm(ticket.material_type)
                    and ticket.material_type):
                reasons.append(
                    f"Same supplier, date, material and tonnage as an existing ticket "
                    f"({where}) with a different ticket number"
                )
        return reasons


def load_index(db: Session) -> DuplicateIndex:
    """One database scan per list response, regardless of displayed row count."""
    tickets = list(db.scalars(select(Ticket).where(Ticket.status != "skipped").order_by(Ticket.id)))
    return DuplicateIndex(tickets)


def find_duplicates(db: Session, ticket: Ticket) -> list[str]:
    """Compatibility entry point for single-ticket reads and edits."""
    return load_index(db).find(ticket)
