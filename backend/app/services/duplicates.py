"""Duplicate detection is advisory, never destructive.

Only rows that could match the tickets being shown are read, so the cost of a
person's edit or approval doesn't grow with the size of the company's history.
Comparison precedence and explanations match the original implementation.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Ticket
from .pricing import norm

MAX_REASONS = 10  # A reviewer can't act on more, and fixed-weight loads can match thousands.
_CHUNK = 400  # Stays under database bind limits on large batches.


class DuplicateIndex:
    """O(N) indexing; a bounded amount of work per ticket looked up."""

    def __init__(self, tickets: list[Ticket]):
        self.by_hash: dict[str, list[Ticket]] = defaultdict(list)
        self.by_number: dict[tuple[str, str], list[Ticket]] = defaultdict(list)
        self.by_load: dict[tuple[str, date, Decimal, str], list[Ticket]] = defaultdict(list)

        for ticket in sorted(tickets, key=lambda t: t.id):
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
        groups = []
        if ticket.crop_sha256:
            groups.append(self.by_hash.get(ticket.crop_sha256, ()))
        if ticket.supplier and ticket.ticket_number:
            groups.append(self.by_number.get((norm(ticket.supplier), ticket.ticket_number), ()))
        if (ticket.supplier and ticket.delivery_date and ticket.tonnage is not None
                and ticket.material_type):
            groups.append(self.by_load.get((
                norm(ticket.supplier), ticket.delivery_date, ticket.tonnage,
                norm(ticket.material_type),
            ), ()))

        # Each group is in id order, so the first few of each include the
        # lowest ids overall: the same ones the full comparison would list first.
        matches: dict[int, Ticket] = {}
        largest = 0
        for group in groups:
            # A stored ticket that isn't skipped is always in its own groups.
            largest = max(largest, len(group) - (0 if ticket.status == "skipped" else 1))
            matches.update({t.id: t for t in group[:MAX_REASONS + 2]})
        matches.pop(ticket.id, None)

        reasons: list[str] = []
        # Preserve SQL's previous primary-key ordering and reason precedence.
        for match_id in sorted(matches)[:MAX_REASONS]:
            other = matches[match_id]
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
        if largest > MAX_REASONS:
            reasons.append(f"...and at least {largest - MAX_REASONS} more with the same details")
        return reasons


def _chunks(values: list, size: int = _CHUNK) -> Iterable[list]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def load_index(db: Session, organization_id: int, tickets: Iterable[Ticket]) -> DuplicateIndex:
    """An index holding every stored ticket that could match ``tickets``."""
    tickets = list(tickets)
    hashes = sorted({t.crop_sha256 for t in tickets if t.crop_sha256})
    numbers = sorted({t.ticket_number.strip().upper() for t in tickets if t.supplier and t.ticket_number})
    loads = {(t.delivery_date, t.tonnage) for t in tickets
             if t.supplier and t.delivery_date and t.tonnage is not None and t.material_type}
    dates = sorted({d for d, _ in loads})
    tons = sorted({t for _, t in loads})
    # Separate lookups, each using its own index. A single OR of these scans the table.
    conditions = [Ticket.crop_sha256.in_(chunk) for chunk in _chunks(hashes)]
    conditions += [Ticket.number_key.in_(chunk) for chunk in _chunks(numbers)]
    conditions += [Ticket.delivery_date.in_(date_chunk) & Ticket.tonnage.in_(tons_chunk)
                   for date_chunk in _chunks(dates) for tons_chunk in _chunks(tons)]
    found: dict[int, Ticket] = {}
    for condition in conditions:
        found.update({t.id: t for t in db.scalars(select(Ticket).where(
            Ticket.organization_id == organization_id, Ticket.status != "skipped", condition))})
    return DuplicateIndex(list(found.values()))


def find_duplicates(db: Session, ticket: Ticket, organization_id: int) -> list[str]:
    """Compatibility entry point for single-ticket reads and edits."""
    return load_index(db, organization_id, [ticket]).find(ticket)
