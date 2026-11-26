"""The hauling charge for one delivery, when a hauler bills per ton or per load.

Rules (docs/engineering/trucking-billing.md), so each delivery's hauling is paid once:
1. No hauler on the ticket: no hauling charge is recorded for it.
2. Hauled under an hourly shift: the shift pays for the truck ("hourly"), never per ton too.
3. The material rate that priced it includes freight: nothing extra ("freight_included").
4. Otherwise the hauler's per-ton or per-load rate: the project's rate if it has one that
   applies, else the company's; within either, the latest started on or before the
   delivery date. An expired rate doesn't fall back to a broader one.
5. No applicable rate: "unpriced" (missing, never $0). A per-ton and a per-load rate
   applying at once: "ambiguous" (nobody guesses).
The result is locked on the ticket at approval and a database trigger keeps it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HaulingRate, Ticket
from .pricing import to_cents

UNIT_METHODS = ("per_ton", "per_load")
COUNTED_METHODS = UNIT_METHODS  # the only kinds that carry a cost on the ticket itself


@dataclass(frozen=True)
class HaulCharge:
    billing: str | None
    rate_id: int | None = None
    unit_rate: Decimal | None = None
    cost: Decimal | None = None
    reason: str | None = None


def unit_rates(db: Session, organization_id: int) -> list[HaulingRate]:
    return list(db.scalars(select(HaulingRate).where(HaulingRate.organization_id == organization_id,
                                                     HaulingRate.billing_method.in_(UNIT_METHODS))))


def _choose(rates: list[HaulingRate], hauler_id: int, project_id: int | None, on: date | None) -> HaulCharge | list[HaulingRate]:
    mine = [r for r in rates if r.hauler_id == hauler_id]
    for scope in ([project_id] if project_id is not None else []) + [None]:
        scoped = [r for r in mine if r.project_id == scope]
        if not scoped:
            continue
        if on is None and any(r.effective_from or r.effective_to for r in scoped):
            return HaulCharge("unpriced", reason="This hauler's rate changes over time. Enter the delivery date to price the hauling.")
        started = [r for r in scoped if r.effective_from is None or on is None or r.effective_from <= on]
        if not started:
            continue  # the project rate starts after this delivery: the company rate applies
        newest_start = max(r.effective_from or date.min for r in started)
        newest = [r for r in started if (r.effective_from or date.min) == newest_start]
        if any(r.effective_to is not None and on is not None and r.effective_to < on for r in newest):
            return HaulCharge("unpriced", reason="The hauler's rate for these dates has ended. Add the current rate on Rates & Pricing.")
        return newest
    return HaulCharge("unpriced", reason="No per-ton or per-load rate for this hauler. Add one on Rates & Pricing.")


def charge_for(t: Ticket, *, project_id: int | None, freight: str | None, rates: list[HaulingRate]) -> HaulCharge:
    """What hauling this delivery costs, if anything. ``freight`` is the material rate's."""
    if t.hauler_id is None:
        return HaulCharge(None)
    if t.truck_shift_id is not None:
        return HaulCharge("hourly", reason="Hauled under an hourly truck shift; the shift pays for it.")
    if freight == "included":
        return HaulCharge("freight_included", reason="The material price includes freight.")
    chosen = _choose(rates, t.hauler_id, project_id, t.delivery_date)
    if isinstance(chosen, HaulCharge):
        return chosen
    methods = {r.billing_method for r in chosen}
    if len(methods) > 1 or len({r.unit_rate for r in chosen}) > 1:
        return HaulCharge("ambiguous", reason="More than one hauling rate applies to this delivery. Remove the extra one on Rates & Pricing.")
    rate = chosen[0]
    if rate.billing_method == "per_ton":
        if t.tonnage is None:
            return HaulCharge("unpriced", reason="Per-ton hauling needs the delivery's tons.")
        return HaulCharge("per_ton", rate.id, rate.unit_rate, to_cents(t.tonnage * rate.unit_rate))
    return HaulCharge("per_load", rate.id, rate.unit_rate, to_cents(rate.unit_rate))
