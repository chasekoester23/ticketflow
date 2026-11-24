"""Hourly trucking: a truck's shift, the agreement that prices it, and what it costs.

Pure rules, no database, so every financial case can be tested exactly. Money is
Decimal and rounded half up to the cent, as everywhere else (pricing.to_cents).

A shift is its own record, separate from the material deliveries its truck carried.
Deliveries can be linked to it, but the hourly cost belongs to the shift alone, so a
truck that hauls eight loads in a nine-hour shift costs nine hours, not eight charges.

Hours
- On site = end - start - unpaid break. Start and end are date-times, so a shift
  that crosses midnight just works. Hours are kept to the hundredth, half up.
- Of the time on site, standby (waiting) is separate from working time.
- Billable hours are what a person approves. The time-sheet figures and the hauler's
  claim are evidence beside them, never silently used in their place.

Price (explicit; nothing is assumed)
- Agreement: the most specific one for the hauler that has started by the shift's
  date: project and equipment type, then project, then equipment type, then any.
  Within that, the latest start applies. If it ended before the shift (an expired
  quote, or a gap before the next one), the shift is unpriced, and a narrower
  expired agreement does not fall back to a broader one.
- Working hours x hourly rate.
- Standby hours x the agreement's standby rate. Standby with no standby rate in the
  agreement leaves the shift unpriced: how standby is paid is the agreement's to say.
- Minimum hours: if working + standby is below the minimum, the difference is charged
  at the hourly rate, as its own line.
- Each line is rounded to the cent; the total is their sum, as an invoice adds lines.

Billing increments (the agreement's, e.g. quarter hours): time on site is rounded to
the step, "up" or to the "nearest" (half a step rounds up), before standby is taken
off. Approved hours must be whole steps, and may be up to the rounded time on site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from .pricing import to_cents

HOURS = Decimal("0.01")
MAX_SHIFT = timedelta(hours=24)
EQUIPMENT_TYPES = ("tandem", "tri_axle", "quad_axle", "semi_end_dump", "belly_dump", "other")


class ShiftError(ValueError):
    """A shift's times or hours that can't be right. The message is for a person."""


def hours_between(start: datetime, end: datetime, break_minutes: int = 0) -> Decimal:
    """Time on site, less the unpaid break."""
    if end <= start:
        raise ShiftError("The shift ends before it starts. For a shift past midnight, give the next day's date for the end.")
    if end - start > MAX_SHIFT:
        raise ShiftError("The shift is longer than 24 hours. Check the dates.")
    if break_minutes < 0:
        raise ShiftError("An unpaid break can't be negative.")
    minutes = Decimal(int((end - start).total_seconds() // 60) - break_minutes)
    if minutes < 0:
        raise ShiftError("The unpaid break is longer than the shift.")
    return (minutes / 60).quantize(HOURS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class HaulRate:
    id: int
    hauler_id: int
    hourly_rate: Decimal
    project_id: int | None = None
    equipment_type: str | None = None
    minimum_hours: Decimal | None = None
    standby_rate: Decimal | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    billing_increment: Decimal | None = None
    increment_rounding: str | None = None  # "up" | "nearest"


ROUNDINGS = ("up", "nearest")


def to_increment(hours: Decimal, increment: Decimal | None, rounding: str | None) -> Decimal:
    """Hours in the agreement's billing steps. No increment: unchanged."""
    if increment is None:
        return hours
    if increment <= 0 or rounding not in ROUNDINGS:
        raise ShiftError("A billing increment needs a positive step and a rounding rule (up or nearest).")
    steps = (hours / increment).to_integral_value(rounding=ROUND_CEILING if rounding == "up" else ROUND_HALF_UP)
    return (steps * increment).quantize(HOURS)


def in_steps(hours: Decimal, increment: Decimal | None) -> bool:
    return increment is None or (hours / increment) == (hours / increment).to_integral_value()


@dataclass(frozen=True)
class RateChoice:
    rate: HaulRate | None
    reason: str | None = None


def choose_rate(rates: list[HaulRate], *, hauler_id: int, project_id: int | None, equipment_type: str | None,
                on: date) -> RateChoice:
    mine = [r for r in rates if r.hauler_id == hauler_id]
    if not mine:
        return RateChoice(None, "No hourly rate agreement with this hauler.")
    tiers = [(project_id, equipment_type), (project_id, None), (None, equipment_type), (None, None)]
    seen: set[tuple] = set()
    for project, equipment in tiers:
        if (project, equipment) in seen:
            continue  # with no project or no truck type, some tiers are the same one
        seen.add((project, equipment))
        tier = [r for r in mine if r.project_id == project and r.equipment_type == equipment]
        if not tier:
            continue
        started = [r for r in tier if r.effective_from is None or r.effective_from <= on]
        if not started:
            continue  # this agreement hasn't started yet: a broader one may apply
        newest = max(started, key=lambda r: r.effective_from or date.min)
        same_start = {(r.hourly_rate, r.standby_rate, r.minimum_hours) for r in started if r.effective_from == newest.effective_from}
        if len(same_start) > 1:
            return RateChoice(None, "Two agreements start on the same day with different terms. Remove one on Trucking.")
        if newest.effective_to is not None and newest.effective_to < on:
            return RateChoice(None, f"The hourly agreement that applies ended on {newest.effective_to.isoformat()}. "
                                    "Set the rate for this date, or extend the agreement.")
        return RateChoice(newest)
    return RateChoice(None, "No hourly agreement with this hauler covers this project and truck type.")


@dataclass(frozen=True)
class CostLine:
    label: str
    hours: Decimal
    rate: Decimal
    amount: Decimal


@dataclass(frozen=True)
class ShiftCost:
    lines: list[CostLine] = field(default_factory=list)
    total: Decimal | None = None
    billable_hours: Decimal | None = None
    reason: str | None = None


def price_shift(work_hours: Decimal, standby_hours: Decimal, rate: HaulRate | None) -> ShiftCost:
    if work_hours < 0 or standby_hours < 0:
        raise ShiftError("Hours can't be negative.")
    if rate is None:
        return ShiftCost(reason="No hourly agreement applies.")
    if standby_hours > 0 and rate.standby_rate is None:
        return ShiftCost(reason="The agreement doesn't say how standby is paid. Add a standby rate, or approve the "
                                "standby time as working hours if that is what was agreed.")
    lines = [CostLine("Working hours", work_hours, rate.hourly_rate, to_cents(work_hours * rate.hourly_rate))]
    if standby_hours > 0:
        lines.append(CostLine("Standby", standby_hours, rate.standby_rate, to_cents(standby_hours * rate.standby_rate)))
    paid = work_hours + standby_hours
    if rate.minimum_hours is not None and paid < rate.minimum_hours:
        top_up = rate.minimum_hours - paid
        lines.append(CostLine(f"Minimum charge ({rate.minimum_hours} h)", top_up, rate.hourly_rate,
                              to_cents(top_up * rate.hourly_rate)))
        paid = rate.minimum_hours
    return ShiftCost(lines=lines, total=sum((line.amount for line in lines), Decimal(0)), billable_hours=paid)


def allocate(total: Decimal, shares: list[Decimal]) -> list[Decimal]:
    """Split a cost across allocations in proportion to their hours, to the cent, so the
    parts add up to exactly the total: the cents left after rounding down go to the
    largest remainders, ties to the first."""
    if not shares or any(s < 0 for s in shares) or sum(shares) <= 0:
        raise ShiftError("Allocate a positive number of hours.")
    whole = sum(shares)
    cents = int((total * 100).to_integral_value())
    exact = [Decimal(cents) * s / whole for s in shares]
    floors = [int(e) for e in exact]
    left = cents - sum(floors)
    order = sorted(range(len(shares)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:left]:
        floors[i] += 1
    return [Decimal(f) / 100 for f in floors]
