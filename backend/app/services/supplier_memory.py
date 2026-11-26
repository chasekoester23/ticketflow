"""What a catalog supplier's approved tickets always say, offered as a suggestion. No ML.

When a company's recent approved tickets from one catalog supplier all used the same
material spelling, unit or job location, a new ticket from that supplier is offered
that value. Only ever a suggestion a person applies: nothing is written to a ticket,
and a value read from the document is never replaced by one from history (a value
that differs only in spelling may be offered the usual spelling).

Company-scoped: only the company's own tickets are read. The cost is one grouped query
over at most RECENT approved tickets, so it doesn't grow with history or page size.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Ticket

FIELDS = ("material_type", "unit", "job_location")
KIND = "usual:"  # alias_suggestions key prefix: (KIND + field, supplier key) -> value
LABEL = "usual_"  # TicketOut.suggestions key prefix: "usual_material_type" and so on
MIN_APPROVED = 3  # fewer approved tickets than this isn't a pattern
RECENT = 2000  # approved tickets looked at, newest first


def usual_values(db: Session, organization_id: int, suppliers: set[str]) -> dict[tuple[str, str], str]:
    """{(KIND + field, supplier key): value} for each catalog supplier (normalized names)
    whose recent approved tickets, at least MIN_APPROVED with that field filled, all
    agree on it exactly."""
    if not suppliers:
        return {}
    recent = (select(Ticket.supplier_key, Ticket.material_type, Ticket.unit, Ticket.job_location)
              .where(Ticket.organization_id == organization_id, Ticket.status == "verified",
                     Ticket.supplier_key.is_not(None))
              .order_by(Ticket.id.desc()).limit(RECENT).subquery())
    rows = db.execute(select(recent.c.supplier_key, recent.c.material_type, recent.c.unit, recent.c.job_location,
                             func.count()).group_by(recent.c.supplier_key, recent.c.material_type, recent.c.unit,
                                                    recent.c.job_location))
    seen: dict[tuple[str, str], dict[str, int]] = {}
    for supplier_key, material, unit, location, count in rows:
        if supplier_key not in suppliers:
            continue
        for name, value in zip(FIELDS, (material, unit, location)):
            if value:
                counts = seen.setdefault((KIND + name, supplier_key), {})
                counts[value] = counts.get(value, 0) + count
    return {key: next(iter(counts)) for key, counts in seen.items()
            if len(counts) == 1 and next(iter(counts.values())) >= MIN_APPROVED}


def suggestions(t: Ticket, usual: dict[tuple[str, str], str], supplier_key: str, normalize) -> dict[str, str]:
    """{"usual_<field>": value} for this ticket. Offered only for an unapproved ticket, and
    only where the field is empty, or differs from the usual value only in spelling; the
    unit only when the document stated none."""
    if t.status in ("verified", "skipped"):
        return {}
    found = {}
    for name in ("material_type", "job_location"):
        value, current = usual.get((KIND + name, supplier_key)), getattr(t, name)
        if value and value != current and (not current or normalize(current) == normalize(value)):
            found[LABEL + name] = value
    value = usual.get((KIND + "unit", supplier_key))
    raw = t.raw_extraction if isinstance(t.raw_extraction, dict) else {}
    if value and value != (t.unit or "ton") and not raw.get("unit") and not t.source_unit:
        found[LABEL + "unit"] = value
    return found
