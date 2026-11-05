"""Rate lookup and cost calculation. Money is always Decimal; we never invent a price."""
import re
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Material, NameAlias, Supplier, SupplierRate, Ticket

CENTS = Decimal("0.01")


def to_cents(amount: Decimal) -> Decimal:
    """Half a cent rounds up, as supplier invoices and spreadsheets do. Decimal's
    default rounds it to even, which made false one-cent invoice differences."""
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


@lru_cache(maxsize=4096)  # Pure, and called for every ticket on every list.
def norm(name: str | None) -> str:
    """Case/punctuation/space-insensitive key so 'ABC Aggregates.' matches 'abc  aggregates'."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def rate_table(db: Session, organization_id: int) -> dict[tuple[str, str], Decimal]:
    rows = db.execute(
        select(Supplier.name, Material.name, SupplierRate.unit_price)
        .join(SupplierRate, SupplierRate.supplier_id == Supplier.id)
        .join(Material, Material.id == SupplierRate.material_id)
        .where(SupplierRate.organization_id == organization_id,
               Supplier.organization_id == organization_id,
               Material.organization_id == organization_id)
    ).all()
    return {(norm(s), norm(m)): p for s, m, p in rows}


def unit_price_for(t: Ticket, table: dict[tuple[str, str], Decimal]) -> tuple[Decimal | None, str | None]:
    """Returns (price, source) where source is 'override' | 'rate' | None."""
    if t.unit_price_override is not None:
        return t.unit_price_override, "override"
    price = table.get((norm(t.supplier), norm(t.material_type)))
    return (price, "rate") if price is not None else (None, None)


def effective_price_for(t: Ticket, table: dict[tuple[str, str], Decimal]) -> tuple[Decimal | None, str | None]:
    """Return the stored rate for verified tickets, a live preview otherwise.

    A verified ticket with an unpriced/legacy snapshot must never silently
    inherit today's supplier rate. The user must explicitly edit and re-verify.
    """
    if t.status == "verified":
        if t.price_snapshot_state == "priced":
            return t.applied_unit_price, t.applied_price_source
        return None, None
    return unit_price_for(t, table)


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
