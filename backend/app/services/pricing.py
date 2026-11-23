"""Rate lookup and cost calculation. Money is always Decimal; we never invent a price."""
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Batch, Material, NameAlias, Supplier, SupplierRate, Ticket, normal_name

CENTS = Decimal("0.01")


def to_cents(amount: Decimal) -> Decimal:
    """Half a cent rounds up, as supplier invoices and spreadsheets do. Decimal's
    default rounds it to even, which made false one-cent invoice differences."""
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


@lru_cache(maxsize=4096)  # Pure, and called for every ticket on every list.
def norm(name: str | None) -> str:
    """Case/punctuation/space-insensitive key so 'ABC Aggregates.' matches 'abc  aggregates'."""
    return normal_name(name)


@dataclass(frozen=True)
class RateRow:
    price: Decimal
    project_id: int | None
    effective_from: date | None
    effective_to: date | None = None  # last day it's valid; None is open-ended


@dataclass(frozen=True)
class RateMatch:
    price: Decimal | None
    source: str | None  # "project_rate" | "rate" | None
    reason: str | None = None  # why there is no price, when the rates themselves are the reason


class RateBook:
    """Every rate a company has, and the rule for which one prices a ticket.

    1. The ticket's project agreement, if it has one that applies, else the company rate.
    2. Within one of those, the latest rate starting on or before the delivery date. A
       rate with no start date covers every date before the first dated one. A later
       rate supersedes an earlier one; an earlier one is never revived.
    3. A project agreement that hasn't started by the delivery date gives way to the
       company rate.
    4. If that latest rate ended (effective_to) before the delivery date, there is no
       valid price in that scope: an expired quote, or a gap before the next one. The
       ticket is unpriced with the reason. An expired project agreement does NOT fall
       back to the company rate: that would quietly put list price on contracted work.
    5. Never a guess: dated rates and no delivery date, or two prices for one name
       ("Sand" and "SAND"), leave the ticket unpriced with the reason.
    """

    def __init__(self, rows: dict[tuple[str, str], list[RateRow]], projects_by_batch: dict[int, int | None]):
        self.rows = rows
        self.projects_by_batch = projects_by_batch

    def __bool__(self) -> bool:
        return bool(self.rows)

    def project_of(self, t: Ticket) -> int | None:
        return self.projects_by_batch.get(t.batch_id) if t.batch_id is not None else None

    def match(self, supplier: str | None, material: str | None, project_id: int | None, on: date | None) -> RateMatch:
        rows = self.rows.get((norm(supplier), norm(material)))
        if not rows:
            return RateMatch(None, None)
        for scope in ([project_id] if project_id is not None else []) + [None]:
            scoped = [r for r in rows if r.project_id == scope]
            if not scoped:
                continue
            if any(r.effective_from is not None or r.effective_to is not None for r in scoped) and on is None:
                return RateMatch(None, None, "This supplier and material's rate changes over time. "
                                             "Enter the delivery date to price it.")
            started = [r for r in scoped if r.effective_from is None or on is None or r.effective_from <= on]
            if not started:
                continue  # the agreement starts after this delivery: the company rate applies
            newest = max(started, key=lambda r: r.effective_from or date.min)
            prices = {r.price for r in started if r.effective_from == newest.effective_from}
            if len(prices) > 1:
                return RateMatch(None, None, "Two catalog names that read the same have different rates. "
                                             "Remove the duplicate on Pricing.")
            if newest.effective_to is not None and on is not None and newest.effective_to < on:
                later = sorted(r.effective_from for r in scoped if r.effective_from is not None and r.effective_from > on)
                which = "The project agreement" if scope is not None else "The company rate"
                return RateMatch(None, None, f"{which} for this supplier and material ended on {newest.effective_to.isoformat()}"
                                 + (f", and the next one starts {later[0].isoformat()}" if later else "")
                                 + ". Set the price for this date on Pricing, or extend the rate.")
            return RateMatch(newest.price, "project_rate" if scope is not None else "rate")
        return RateMatch(None, None)

    def for_ticket(self, t: Ticket) -> RateMatch:
        return self.match(t.supplier, t.material_type, self.project_of(t), t.delivery_date)


def rate_table(db: Session, organization_id: int) -> RateBook:
    rows = db.execute(
        select(Supplier.name, Material.name, SupplierRate.unit_price, SupplierRate.project_id, SupplierRate.effective_from,
               SupplierRate.effective_to)
        .join(SupplierRate, SupplierRate.supplier_id == Supplier.id)
        .join(Material, Material.id == SupplierRate.material_id)
        .where(SupplierRate.organization_id == organization_id,
               Supplier.organization_id == organization_id,
               Material.organization_id == organization_id)
    ).all()
    book: dict[tuple[str, str], list[RateRow]] = {}
    for s, m, p, project_id, effective_from, effective_to in rows:
        book.setdefault((norm(s), norm(m)), []).append(RateRow(p, project_id, effective_from, effective_to))
    # A ticket's project comes from its batch. Read only when some rate is per project.
    projects_by_batch: dict[int, int | None] = {}
    if any(r.project_id is not None for found in book.values() for r in found):
        projects_by_batch = dict(db.execute(select(Batch.id, Batch.project_id).where(
            Batch.organization_id == organization_id)).all())
    return RateBook(book, projects_by_batch)


def unit_price_for(t: Ticket, table: RateBook) -> tuple[Decimal | None, str | None]:
    """Returns (price, source) where source is 'override' | 'project_rate' | 'rate' | None."""
    if t.unit_price_override is not None:
        return t.unit_price_override, "override"
    if not isinstance(table, RateBook):
        return None, None
    found = table.for_ticket(t)
    return found.price, found.source


def effective_price_for(t: Ticket, table: RateBook) -> tuple[Decimal | None, str | None]:
    """Return the stored rate for verified tickets, a live preview otherwise.

    A verified ticket with an unpriced/legacy snapshot must never silently
    inherit today's supplier rate. The user must explicitly edit and re-verify.
    """
    if t.status == "verified":
        if t.price_snapshot_state == "priced":
            return t.applied_unit_price, t.applied_price_source
        return None, None
    return unit_price_for(t, table)


def unpriced_reason(t: Ticket, table: RateBook) -> str | None:
    """Why the rates themselves leave this ticket without a price, if they do."""
    if t.unit_price_override is not None or not isinstance(table, RateBook):
        return None
    return table.for_ticket(t).reason


def cost_for(t: Ticket, price: Decimal | None) -> Decimal | None:
    """Cost only for a currently verified ticket with confirmed tonnage and a rate."""
    if t.status != "verified" or price is None or t.tonnage is None or not t.tonnage_confirmed:
        return None
    return to_cents(t.tonnage * price)


def alias_suggestions(
    db: Session, organization_id: int, catalog: tuple[set[str], set[str]] | None = None,
) -> dict[tuple[str, str], str]:
    """{(kind, alias key): catalog name} for active aliases whose target still
    exists. One query per list, however many tickets are displayed."""
    catalog = catalog if catalog is not None else catalog_names(db, organization_id)
    current = {"supplier": catalog[0], "material": catalog[1]}
    rows = db.execute(select(NameAlias.kind, NameAlias.alias_key, NameAlias.target_key, NameAlias.target_name).where(
        NameAlias.organization_id == organization_id, NameAlias.removed_at.is_(None)))
    return {(kind, key): name for kind, key, target, name in rows if target in current.get(kind, ())}


def suggestions_for(t: Ticket, aliases: dict[tuple[str, str], str]) -> dict[str, str]:
    """Exact alias matches only: never fuzzy, and only ever a suggestion."""
    found = {}
    for field, kind, value in (("supplier", "supplier", t.supplier), ("material_type", "material", t.material_type)):
        name = aliases.get((kind, norm(value))) if value else None
        if name:
            found[field] = name
    return found


def catalog_names(db: Session, organization_id: int) -> tuple[set[str], set[str]]:
    """Load normalized supplier/material names once for an entire review queue."""
    return (
        {norm(n) for n in db.scalars(select(Supplier.name).where(Supplier.organization_id == organization_id))},
        {norm(n) for n in db.scalars(select(Material.name).where(Material.organization_id == organization_id))},
    )


def unresolved_names(
    db: Session, t: Ticket, organization_id: int,
    names: tuple[set[str], set[str]] | None = None,
) -> list[str]:
    """Why a rate is missing, with optional cached catalog name sets."""
    notes = []
    suppliers, materials = names if names is not None else catalog_names(db, organization_id)
    if t.supplier and norm(t.supplier) not in suppliers:
        notes.append(f"Supplier '{t.supplier}' is not in your supplier list")
    if t.material_type and norm(t.material_type) not in materials:
        notes.append(f"Material '{t.material_type}' is not in your material list")
    return notes
