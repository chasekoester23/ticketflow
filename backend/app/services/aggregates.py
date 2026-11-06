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


# Only approved, confirmed tickets count toward tons and cost.
COUNTED = and_(Ticket.status == "verified", Ticket.tonnage_confirmed.is_(True))


def counted_tons():
    return exact_sum(case((COUNTED, Ticket.tonnage), else_=0), 3)


def counted_cost():
    return exact_sum(case((COUNTED, Ticket.approved_cost), else_=0), 2)


def pending_tons():
    return exact_sum(case((COUNTED, 0), else_=func.coalesce(Ticket.tonnage, 0)), 3)


def unpriced():
    return func.coalesce(func.sum(case((and_(COUNTED, Ticket.approved_cost.is_(None)), 1), else_=0)), 0)


def counted_tickets():
    return func.coalesce(func.sum(case((COUNTED, 1), else_=0)), 0)
