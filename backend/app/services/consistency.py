"""Two checks for a value typed or read into the wrong place. Both only ever suggest:
nothing is changed, and nothing is blocked.

- A ticket number that doesn't look like the supplier's other ticket numbers, or that
  matches the job number, is probably the job, PO or truck number.
- A supplier or material name that differs from one catalog name only by "Inc",
  "Trucking", "No." and the like probably means that catalog name.
"""
from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Material, Supplier, Ticket
from .pricing import norm

_WORD = re.compile(r"[a-z0-9]+")
COMPANY_WORDS = {"inc", "llc", "co", "company", "corp", "corporation", "ltd", "limited", "the", "and",
                 "trucking", "hauling", "transport", "transportation", "logistics"}
MATERIAL_WORDS = {"no", "num", "number"}


def _words(name: str | None) -> list[str]:
    return _WORD.findall((name or "").lower())


def supplier_key(name: str | None) -> tuple[str, ...]:
    return tuple(word for word in _words(name) if word not in COMPANY_WORDS)


def material_key(name: str | None) -> tuple[str, ...]:
    return tuple(word for word in _words(name) if word not in MATERIAL_WORDS)


def shape(number: str) -> str:
    """IV-52330 -> AA-99999: what a supplier's ticket numbers have in common."""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", number.strip()))


class SimilarNames:
    def __init__(self, suppliers: list[str], materials: list[str]):
        self.catalog = {"supplier": suppliers, "material": materials}
        self.exact = {kind: {norm(name) for name in names} for kind, names in self.catalog.items()}

    def normalized(self) -> tuple[set[str], set[str]]:
        """The same sets pricing.catalog_names would load, without reading them again."""
        return self.exact["supplier"], self.exact["material"]

    def suggest(self, kind: str, value: str | None) -> str | None:
        """The one catalog name this value most likely means, or None if it isn't clear."""
        if not value or norm(value) in self.exact[kind]:
            return None
        keyfn = supplier_key if kind == "supplier" else material_key
        key = keyfn(value)
        if not key:
            return None
        names = self.catalog[kind]
        found = {name for name in names if keyfn(name) == key}
        if not found and len(key) >= 2:
            # "Indiana Valley" and "Indiana Valley Trucking Co": one is the start of the other.
            found = {name for name in names if len(k := keyfn(name)) >= 2 and (k[:len(key)] == key or key[:len(k)] == k)}
        return next(iter(found)) if len(found) == 1 else None

    def for_ticket(self, ticket: Ticket) -> dict[str, str]:
        found = {}
        for field, kind in (("supplier", "supplier"), ("material_type", "material")):
            name = self.suggest(kind, getattr(ticket, field))
            if name:
                found[field] = name
        return found


def similar_names(db: Session, organization_id: int) -> SimilarNames:
    return SimilarNames(
        list(db.scalars(select(Supplier.name).where(Supplier.organization_id == organization_id))),
        list(db.scalars(select(Material.name).where(Material.organization_id == organization_id))),
    )


class TicketNumbers:
    """Recent ticket numbers by supplier, to spot one that doesn't belong."""

    def __init__(self, rows):
        self.by_supplier: dict[tuple[str, ...], list[tuple[int, str, str]]] = {}
        for ticket_id, supplier, number in rows:
            key = supplier_key(supplier)
            if key and number:
                self.by_supplier.setdefault(key, []).append((ticket_id, shape(number), number))

    def warning(self, ticket: Ticket) -> str | None:
        number = (ticket.ticket_number or "").strip()
        if not number:
            return None
        raw = ticket.raw_extraction if isinstance(ticket.raw_extraction, dict) else {}
        job = raw.get("job_number")
        if job and norm(str(job)) == norm(number):
            return "This is the job number on the ticket. The ticket number is usually printed or stamped near \"Ticket No.\""
        if ticket.job_location and re.search(rf"(?<![A-Za-z0-9]){re.escape(number)}(?![A-Za-z0-9])", ticket.job_location):
            return "This number is also in the job. Check it isn't the job number."
        others = [(s, n) for i, s, n in self.by_supplier.get(supplier_key(ticket.supplier), []) if i != ticket.id]
        if len(others) < 2:
            return None
        counts = Counter(s for s, _ in others)
        if counts[shape(number)]:
            return None
        common, seen = counts.most_common(1)[0]
        if seen / len(others) < 0.6:
            return None
        example = next(n for s, n in others if s == common)
        return f"Doesn't look like this supplier's other ticket numbers, such as {example}. Check it isn't the job, PO or truck number."


def ticket_numbers(db: Session, organization_id: int, limit: int = 2000) -> TicketNumbers:
    return TicketNumbers(db.execute(
        select(Ticket.id, Ticket.supplier, Ticket.ticket_number)
        .where(Ticket.organization_id == organization_id, Ticket.ticket_number.is_not(None), Ticket.status != "skipped")
        .order_by(Ticket.id.desc()).limit(limit)).all())
