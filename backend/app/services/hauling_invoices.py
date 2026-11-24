"""Hauling invoices against approved truck shifts: what was billed, what the approved
shifts support, the difference, and why. Pure and deterministic; nothing is decided.

Matching: an invoice line for hours, standby or a minimum charge matches the shift for
that hauler, truck and date. A line with no truck matches only if the hauler had one
shift that day; with several it needs a person.

Money, counted once:
- The first line billing a shift's hours (or standby, or minimum) is compared with the
  approved figures. Its difference (billed amount - approved hours x approved rate) is
  split into an hours effect at the billed rate, a rate effect on the approved hours,
  and the invoice's own arithmetic, adding up to exactly that difference.
- A later line billing the same shift's same time again is a repeat: its whole amount
  is unsupported, and nothing else is counted for it.
- A fee line (fuel surcharge, mobilization, ...) is unsupported until a person confirms
  the agreement allows it; there's no fee schedule to check it against yet.
- Billed time with no shift record is unsupported in full.
- An approved shift inside an invoice's period that no line bills is reported as not
  invoiced: owed later, never an overcharge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .pricing import to_cents

KIND_LABEL = {"hours": "working hours", "standby": "standby", "minimum": "minimum charge", "fee": "fee"}


@dataclass(frozen=True)
class ShiftFacts:
    id: int
    hauler_id: int
    truck_key: str
    truck: str
    work_date: date
    project_id: int
    status: str
    work: Decimal | None = None
    standby: Decimal | None = None
    top_up: Decimal | None = None
    hourly: Decimal | None = None
    standby_rate: Decimal | None = None


@dataclass(frozen=True)
class LineFacts:
    invoice_id: int
    invoice_number: str
    hauler_id: int
    hauler: str
    project_id: int | None
    period_from: date
    period_to: date
    line_no: int
    kind: str
    truck_key: str | None
    truck: str | None
    work_date: date | None
    hours: Decimal | None
    rate: Decimal | None
    amount: Decimal
    description: str | None = None


@dataclass
class Finding:
    key: str
    category: str
    title: str
    explanation: str
    project_id: int | None
    hauler: str
    invoice_id: int | None
    invoice_number: str | None
    line_no: int | None = None
    shift_id: int | None = None
    amount: Decimal | None = None        # this cause's share of billed minus approved
    billed: Decimal | None = None        # the whole line, for context
    expected: Decimal | None = None
    group: str | None = None             # all causes of one line share it, so they're capped at its difference


@dataclass
class LineResult:
    invoice_id: int
    line_no: int
    result: str  # matched | differs | repeat | no_shift | not_approved | ambiguous | fee
    shift_id: int | None
    expected: Decimal | None
    difference: Decimal | None
    notes: list[str] = field(default_factory=list)


def _approved(shift: ShiftFacts, kind: str) -> tuple[Decimal, Decimal | None]:
    if kind == "hours":
        return shift.work or Decimal(0), shift.hourly
    if kind == "standby":
        return shift.standby or Decimal(0), shift.standby_rate
    return shift.top_up or Decimal(0), shift.hourly


def reconcile(lines: list[LineFacts], shifts: list[ShiftFacts]) -> tuple[list[LineResult], list[Finding]]:
    results: list[LineResult] = []
    findings: list[Finding] = []
    billed: dict[tuple[int, str], LineFacts] = {}
    by_day: dict[tuple[int, date], list[ShiftFacts]] = {}
    for s in shifts:
        by_day.setdefault((s.hauler_id, s.work_date), []).append(s)

    for line in sorted(lines, key=lambda x: (x.invoice_id, x.line_no)):
        base = dict(project_id=line.project_id, hauler=line.hauler, invoice_id=line.invoice_id, invoice_number=line.invoice_number,
                    line_no=line.line_no, billed=line.amount)
        where = f"invoice {line.invoice_number}, line {line.line_no}"
        if line.kind == "fee":
            what = line.description or "a fee"
            findings.append(Finding(key=f"H{line.invoice_id}:{line.line_no}:fee", category="unsupported_fee",
                                    title=f"Fee not in an agreement: {where}", amount=line.amount, expected=Decimal(0),
                                    explanation=f"{what} for ${line.amount}. No agreement on file allows it. Confirm it was agreed, "
                                                "or ask the hauler to remove it.", **base))
            results.append(LineResult(line.invoice_id, line.line_no, "fee", None, Decimal(0), line.amount))
            continue
        day = by_day.get((line.hauler_id, line.work_date), []) if line.work_date else []
        candidates = [s for s in day if s.truck_key == line.truck_key] if line.truck_key else day
        if not candidates:
            truck = f"truck {line.truck}" if line.truck else "a truck"
            findings.append(Finding(key=f"H{line.invoice_id}:{line.line_no}:missing", category="missing_shift",
                                    title=f"Billed truck time with no shift: {where}", amount=line.amount, expected=Decimal(0),
                                    explanation=f"{KIND_LABEL[line.kind].capitalize()} for {truck} on "
                                                f"{line.work_date.isoformat() if line.work_date else 'no date'}: no truck shift is recorded. "
                                                "Find the time sheet, or ask the hauler for it.", **base))
            results.append(LineResult(line.invoice_id, line.line_no, "no_shift", None, Decimal(0), line.amount))
            continue
        if len(candidates) > 1:
            findings.append(Finding(key=f"H{line.invoice_id}:{line.line_no}:ambiguous", category="needs_review",
                                    title=f"Which truck? {where}", explanation="The line names no truck, and the hauler had "
                                    f"{len(candidates)} shifts that day. Ask which truck, or enter the truck number.", **base))
            results.append(LineResult(line.invoice_id, line.line_no, "ambiguous", None, None, None))
            continue
        shift = candidates[0]
        base["project_id"] = line.project_id or shift.project_id
        if shift.status != "approved":
            findings.append(Finding(key=f"H{line.invoice_id}:{line.line_no}:unapproved", category="needs_review", shift_id=shift.id,
                                    title=f"Shift not approved yet: {where}",
                                    explanation=f"Truck {shift.truck}'s shift on {shift.work_date.isoformat()} is "
                                                f"{'in dispute' if shift.status == 'disputed' else 'waiting for approval'}. "
                                                "The line can be checked once it's approved.", **base))
            results.append(LineResult(line.invoice_id, line.line_no, "not_approved", shift.id, None, None))
            continue
        first = billed.get((shift.id, line.kind))
        if first is not None:
            findings.append(Finding(key=f"H{line.invoice_id}:{line.line_no}:repeat", category="duplicate", shift_id=shift.id,
                                    title=f"Billed again: {where}", amount=line.amount, expected=Decimal(0),
                                    explanation=f"Truck {shift.truck}'s {KIND_LABEL[line.kind]} on {shift.work_date.isoformat()} "
                                                f"was already billed on invoice {first.invoice_number}, line {first.line_no}.", **base))
            results.append(LineResult(line.invoice_id, line.line_no, "repeat", shift.id, Decimal(0), line.amount))
            continue
        billed[(shift.id, line.kind)] = line
        hours, rate = _approved(shift, line.kind)
        expected = to_cents(hours * rate) if rate is not None else Decimal(0)
        difference = line.amount - expected
        group = f"H{line.invoice_id}:{line.line_no}"
        causes: list[tuple[str, str, Decimal, str]] = []
        if line.hours is not None and line.rate is not None:
            hours_effect = to_cents((line.hours - hours) * line.rate)
            rate_effect = to_cents(hours * (line.rate - (rate or Decimal(0))))
            if hours_effect:
                causes.append(("hours", "Hours differ", hours_effect,
                               f"Billed {line.hours} h of {KIND_LABEL[line.kind]}; {hours} h were approved for truck {shift.truck} "
                               f"on {shift.work_date.isoformat()}."))
            if rate_effect:
                causes.append(("rate", "Rate differs", rate_effect,
                               f"Billed ${line.rate}/h; the approved shift locked ${rate or 0}/h."))
            rest = difference - hours_effect - rate_effect  # the invoice's own arithmetic, and rounding
            if rest:
                causes.append(("arithmetic", "Invoice arithmetic", rest,
                               f"The line's amount isn't its hours times its rate ({line.hours} x ${line.rate})."))
        elif difference:
            causes.append(("hours", "Amount differs", difference,
                           f"Billed ${line.amount}; the approved {KIND_LABEL[line.kind]} comes to ${expected}. The line gives no hours or rate."))
        category = {"hours": "hours", "rate": "rate", "arithmetic": "invoice_arithmetic"}
        for kind, label, effect, text in causes:
            findings.append(Finding(key=f"{group}:{kind}", category=category[kind], title=f"{label}: {where}", explanation=text,
                                    shift_id=shift.id, amount=effect, expected=expected, group=group, **base))
        results.append(LineResult(line.invoice_id, line.line_no, "differs" if difference else "matched", shift.id, expected, difference))

    billed_shifts = {shift_id for shift_id, _kind in billed}
    periods: dict[int, list[LineFacts]] = {}
    for line in lines:
        periods.setdefault(line.hauler_id, []).append(line)
    for s in shifts:
        if s.status != "approved" or s.id in billed_shifts or s.hauler_id not in periods:
            continue
        covering = [line for line in periods[s.hauler_id] if line.period_from <= s.work_date <= line.period_to]
        if not covering:
            continue
        findings.append(Finding(key=f"HS{s.id}", category="not_invoiced", shift_id=s.id, project_id=s.project_id,
                                hauler=covering[0].hauler, invoice_id=covering[0].invoice_id, invoice_number=covering[0].invoice_number,
                                title=f"Approved shift not billed: truck {s.truck}, {s.work_date.isoformat()}",
                                explanation=f"Inside invoice {covering[0].invoice_number}'s period, but no line bills it. "
                                            "Expect it on a later invoice, or ask whether it was missed."))
    return results, findings


def load(db, organization_id: int) -> tuple[list[LineFacts], list[ShiftFacts]]:
    """Every hauling invoice line and truck shift of a company, as facts for reconcile()."""
    from sqlalchemy import select

    from ..models import Hauler, HaulingInvoice, HaulingInvoiceLine, Truck, TruckShift, normal_name
    haulers = dict(db.execute(select(Hauler.id, Hauler.name).where(Hauler.organization_id == organization_id)).all())
    trucks = dict(db.execute(select(Truck.id, Truck.identifier).where(Truck.organization_id == organization_id)).all())
    lines = [LineFacts(invoice_id=i.id, invoice_number=i.invoice_number, hauler_id=i.hauler_id, hauler=haulers.get(i.hauler_id, ""),
                       project_id=i.project_id, period_from=i.period_from, period_to=i.period_to, line_no=line.line_no, kind=line.kind,
                       truck_key=normal_name(line.truck_identifier) if line.truck_identifier else None, truck=line.truck_identifier,
                       work_date=line.work_date, hours=line.hours, rate=line.rate, amount=line.amount, description=line.description)
             for i, line in db.execute(select(HaulingInvoice, HaulingInvoiceLine).join(HaulingInvoiceLine, HaulingInvoiceLine.invoice_id == HaulingInvoice.id)
                                       .where(HaulingInvoice.organization_id == organization_id))]
    shifts = []
    for s in db.scalars(select(TruckShift).where(TruckShift.organization_id == organization_id)):
        approved = s.status == "approved"
        top_up = (s.approved_billable_hours - s.approved_work_hours - s.approved_standby_hours) if approved else None
        shifts.append(ShiftFacts(id=s.id, hauler_id=s.hauler_id, truck_key=normal_name(trucks.get(s.truck_id, "")), truck=trucks.get(s.truck_id, ""),
                                 work_date=s.work_date, project_id=s.project_id, status=s.status,
                                 work=s.approved_work_hours, standby=s.approved_standby_hours, top_up=top_up,
                                 hourly=s.applied_hourly_rate, standby_rate=s.applied_standby_rate))
    return lines, shifts
