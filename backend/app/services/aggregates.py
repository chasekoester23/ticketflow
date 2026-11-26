"""Exact totals, added up by the database instead of loading every ticket.

SQLite keeps decimals as floats, so a plain SUM can drift. Rounding each value to
a whole number of thousandths or cents first, then adding integers, gives the same
exact answer on SQLite and PostgreSQL. A test compares it with Decimal arithmetic.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, and_, case, cast, func

from ..models import Ticket


def exact_sum(column, scale: int):
    """SUM as an integer count of 10^-scale, for exact results on any database."""
    return func.coalesce(func.sum(cast(func.round(column * (10 ** scale)), BigInteger)), 0)


def as_decimal(units: int | None, scale: int) -> Decimal:
    return Decimal(int(units or 0)).scaleb(-scale)


# Only approved, confirmed tickets count toward quantities and cost.
COUNTED = and_(Ticket.status == "verified", Ticket.tonnage_confirmed.is_(True))
# Tons are tons: a delivery measured in cubic yards, loads, each or gallons is never added
# to them (services/units.py). Those quantities are totalled per unit by counted_quantity().
IN_TONS = Ticket.unit == "ton"


def counted_tons():
    return exact_sum(case((and_(COUNTED, IN_TONS), Ticket.tonnage), else_=0), 3)


def counted_quantity():
    """Approved quantity in whatever unit the query groups by (group by Ticket.unit)."""
    return exact_sum(case((COUNTED, Ticket.tonnage), else_=0), 3)


def counted_loads():
    """Loads delivered: a ticket measured in loads counts its quantity, any other ticket is one load."""
    return exact_sum(case((COUNTED, case((Ticket.unit == "load", Ticket.tonnage), else_=1)), else_=0), 3)


def counted_cost():
    return exact_sum(case((COUNTED, Ticket.approved_cost), else_=0), 2)


def pending_tons():
    return exact_sum(case((COUNTED, 0), (IN_TONS, func.coalesce(Ticket.tonnage, 0)), else_=0), 3)


def unpriced():
    return func.coalesce(func.sum(case((and_(COUNTED, Ticket.approved_cost.is_(None)), 1), else_=0)), 0)


def counted_tickets():
    return func.coalesce(func.sum(case((COUNTED, 1), else_=0)), 0)


# Hauling billed per ton or per load, locked on approved tickets (services/haul_billing.py).
# Hourly hauling is counted from approved shifts instead, so a delivery is never paid twice.
def counted_haul_cost():
    return exact_sum(case((COUNTED, func.coalesce(Ticket.haul_cost, 0)), else_=0), 2)


def haul_unpriced():
    """Approved deliveries with a hauler but no applicable hauling rate: missing, never $0."""
    return func.coalesce(func.sum(case((and_(COUNTED, Ticket.haul_billing.in_(("unpriced", "ambiguous"))), 1), else_=0)), 0)


# Select these, filter with OTHER_UNITS and group by Ticket.unit (and anything else), then
# pass the rows to other_units(): approved quantities that aren't tons, each in its own unit.
OTHER_UNIT_COLUMNS = (Ticket.unit.label("unit"), counted_quantity().label("quantity"), counted_tickets().label("counted"))
OTHER_UNITS = Ticket.unit != "ton"


def other_units(rows) -> list[dict]:
    totals: dict[str, list[int]] = {}
    for row in rows:
        if int(row.counted):
            entry = totals.setdefault(row.unit, [0, 0])
            entry[0] += int(row.quantity)
            entry[1] += int(row.counted)
    return [{"unit": unit, "quantity": str(as_decimal(quantity, 3)), "tickets": count}
            for unit, (quantity, count) in sorted(totals.items())]
