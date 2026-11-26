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

Per ton and per load (line kinds "tons" and "loads"): the line is compared with the
approved deliveries whose hauling charge this hauler holds (locked on each ticket at
approval, see haul_billing.py). The deliveries are the ticket numbers the line lists,
or else the hauler's deliveries that day (and truck, if named) on the invoice's
project. Its difference splits the same way: a quantity effect (tons or loads) at the
billed rate, a rate effect on the approved quantity, and the arithmetic. A delivery
billed a second time, on another project, paid by the hour, or with its freight in the
material price is never supported here, so no hauling is paid twice.

Not billed at all: approved hauling (shifts, and per-ton or per-load deliveries) that
no invoice covers is one "not billed yet" finding per hauler and project, so nothing
approved is forgotten and nothing is counted as owed twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .pricing import to_cents

KIND_LABEL = {"hours": "working hours", "standby": "standby", "minimum": "minimum charge", "fee": "fee", "credit": "credit",
              "tons": "tons hauled", "loads": "loads hauled"}
UNIT_KINDS = {"tons": "per_ton", "loads": "per_load"}


def ticket_keys(text: str | None) -> tuple[str, ...]:
    """Ticket numbers as the delivery records key them: trimmed, upper case, in order."""
    import re
    return tuple(dict.fromkeys(part.strip().upper() for part in re.split(r"[\s,;]+", text or "") if part.strip()))


@dataclass(frozen=True)
class DeliveryFacts:
    """An approved-or-not delivery ticket that names a hauler, with its locked hauling charge."""
    id: int
    hauler_id: int
    number_key: str | None
    ticket_number: str | None
    truck_key: str | None
    delivery_date: date | None
    project_id: int | None
    counted: bool            # approved, quantity confirmed: in project cost
    method: str | None       # per_ton | per_load | hourly | freight_included | unpriced | ambiguous
    quantity: Decimal | None
    unit_rate: Decimal | None
    cost: Decimal | None
    hauler: str = ""
    loads: Decimal = Decimal(1)  # a ticket measured in loads counts its quantity; any other is one truckload


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
    hauler: str = ""
    # Every project the shift's hours are charged to: its own, and any it was split onto.
    projects: frozenset = frozenset()


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
    quantity: Decimal | None = None          # tons or loads, for the unit kinds
    tickets: tuple[str, ...] = ()            # ticket number keys the line lists
    chosen_shift_id: int | None = None       # a person's choice when several shifts matched


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
    material: str = "Hourly trucking"    # what the exceptions center files it under


@dataclass
class LineResult:
    invoice_id: int
    line_no: int
    result: str  # matched | differs | repeat | no_shift | no_delivery | not_approved | ambiguous | fee | credit
    shift_id: int | None
    expected: Decimal | None
    difference: Decimal | None
    notes: list[str] = field(default_factory=list)
    ticket_ids: list[int] = field(default_factory=list)
    candidates: list[int] = field(default_factory=list)   # shifts a person can choose between


def _approved(shift: ShiftFacts, kind: str) -> tuple[Decimal, Decimal | None]:
    if kind == "hours":
        return shift.work or Decimal(0), shift.hourly
    if kind == "standby":
        return shift.standby or Decimal(0), shift.standby_rate
    return shift.top_up or Decimal(0), shift.hourly


def reconcile(lines: list[LineFacts], shifts: list[ShiftFacts],
              deliveries: list[DeliveryFacts] | tuple = ()) -> tuple[list[LineResult], list[Finding]]:
    results: list[LineResult] = []
    findings: list[Finding] = []
    billed: dict[tuple[int, str], LineFacts] = {}
    billed_deliveries: dict[int, LineFacts] = {}
    by_day: dict[tuple[int, date], list[ShiftFacts]] = {}
    for s in shifts:
        by_day.setdefault((s.hauler_id, s.work_date), []).append(s)

    for line in sorted(lines, key=lambda x: (x.invoice_id, x.line_no)):
        base = dict(project_id=line.project_id, hauler=line.hauler, invoice_id=line.invoice_id, invoice_number=line.invoice_number,
                    line_no=line.line_no, billed=line.amount)
        where = f"invoice {line.invoice_number}, line {line.line_no}"
        if line.kind == "credit":
            # A credit the hauler gives back: it lowers what's billed, and is nothing to dispute.
            results.append(LineResult(line.invoice_id, line.line_no, "credit", None, line.amount, Decimal(0)))
            continue
        if line.kind == "fee":
            what = line.description or "a fee"
            findings.append(Finding(key=f"H{line.invoice_id}:{line.line_no}:fee", category="unsupported_fee",
                                    title=f"Fee not in an agreement: {where}", amount=line.amount, expected=Decimal(0),
                                    explanation=f"{what} for ${line.amount}. No agreement on file allows it. Confirm it was agreed, "
                                                "or ask the hauler to remove it.", **base))
            results.append(LineResult(line.invoice_id, line.line_no, "fee", None, Decimal(0), line.amount))
            continue
        if line.kind in UNIT_KINDS:
            _unit_line(line, base, where, deliveries, billed_deliveries, results, findings)
            continue
        day = by_day.get((line.hauler_id, line.work_date), []) if line.work_date else []
        candidates = [s for s in day if s.truck_key == line.truck_key] if line.truck_key else day
        if line.chosen_shift_id is not None:
            # A person chose the shift; it must still be this hauler's, that day.
            candidates = [s for s in day if s.id == line.chosen_shift_id]
        if line.project_id is not None and candidates:
            # A project's invoice bills that project's truck time: its shifts, or shifts split onto it.
            on_project = [s for s in candidates if line.project_id in (s.projects or {s.project_id})]
            if not on_project:
                other = candidates[0]
                findings.append(Finding(key=f"H{line.invoice_id}:{line.line_no}:wrong_project", category="wrong_project",
                                        shift_id=other.id, title=f"Billed against another project: {where}",
                                        amount=line.amount, expected=Decimal(0),
                                        explanation=f"Truck {other.truck}'s shift on {other.work_date.isoformat()} was worked for another "
                                                    "project than this invoice's. Ask the hauler to bill it on that project's invoice.",
                                        **base))
                results.append(LineResult(line.invoice_id, line.line_no, "differs", other.id, Decimal(0), line.amount))
                continue
            candidates = on_project
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
            results.append(LineResult(line.invoice_id, line.line_no, "ambiguous", None, None, None,
                                      candidates=[c.id for c in candidates]))
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
    hauler_names = {**{s.hauler_id: s.hauler for s in shifts}, **{d.hauler_id: d.hauler for d in deliveries},
                    **{line.hauler_id: line.hauler for line in lines}}
    not_covered: dict[tuple[int, int | None], list[tuple[date | None, str]]] = {}
    for d in deliveries:
        if not d.counted or d.method not in UNIT_KINDS.values() or d.id in billed_deliveries:
            continue
        covering = [line for line in periods.get(d.hauler_id, []) if d.delivery_date and line.period_from <= d.delivery_date <= line.period_to
                    and (line.project_id is None or line.project_id == d.project_id)]
        if covering:
            findings.append(Finding(key=f"HD{d.id}", category="not_invoiced", project_id=d.project_id, hauler=covering[0].hauler,
                                    invoice_id=covering[0].invoice_id, invoice_number=covering[0].invoice_number,
                                    material=_unit_material(d.method),
                                    title=f"Approved delivery's hauling not billed: ticket {d.ticket_number or d.id}, "
                                          f"{d.delivery_date.isoformat() if d.delivery_date else 'no date'}",
                                    explanation=f"Inside invoice {covering[0].invoice_number}'s period, but no line bills its hauling "
                                                f"(${d.cost} locked at approval). Expect it on a later invoice, or ask whether it was missed."))
        else:
            not_covered.setdefault((d.hauler_id, d.project_id), []).append((d.delivery_date, "delivery"))
    for s in shifts:
        if s.status != "approved" or s.id in billed_shifts:
            continue
        if s.hauler_id not in periods:
            not_covered.setdefault((s.hauler_id, s.project_id), []).append((s.work_date, "shift"))
            continue
        # An invoice for one project covers only that project's shifts: a hauler billing each job
        # separately hasn't left another job's shift off this one.
        covering = [line for line in periods[s.hauler_id] if line.period_from <= s.work_date <= line.period_to
                    and (line.project_id is None or line.project_id == s.project_id)]
        if not covering:
            not_covered.setdefault((s.hauler_id, s.project_id), []).append((s.work_date, "shift"))
            continue
        findings.append(Finding(key=f"HS{s.id}", category="not_invoiced", shift_id=s.id, project_id=s.project_id,
                                hauler=covering[0].hauler, invoice_id=covering[0].invoice_id, invoice_number=covering[0].invoice_number,
                                title=f"Approved shift not billed: truck {s.truck}, {s.work_date.isoformat()}",
                                explanation=f"Inside invoice {covering[0].invoice_number}'s period, but no line bills it. "
                                            "Expect it on a later invoice, or ask whether it was missed."))
    # Approved hauling no invoice covers yet: one finding per hauler and project, so the
    # list stays short and nothing approved is forgotten. Owed later, never an overcharge.
    for (hauler_id, project_id), items in sorted(not_covered.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        dates = sorted(d for d, _ in items if d is not None)
        shifts_n = sum(1 for _, what in items if what == "shift")
        loads_n = len(items) - shifts_n
        parts = ([f"{shifts_n} approved shift{'s' if shifts_n != 1 else ''}"] if shifts_n else []) + \
                ([f"{loads_n} approved deliver{'ies' if loads_n != 1 else 'y'} hauled per ton or per load"] if loads_n else [])
        span = f"{dates[0].isoformat()} to {dates[-1].isoformat()}" if dates else "undated"
        findings.append(Finding(key=f"HU:{hauler_id}:{project_id}", category="not_invoiced", project_id=project_id,
                                hauler=hauler_names.get(hauler_id, ""),
                                invoice_id=None, invoice_number=None, material="Trucking",
                                title=f"Hauling not billed yet: {' and '.join(parts)}, {span}",
                                explanation="No hauler invoice covers these yet. Expect a bill, or ask whether one was missed."))
    return results, findings


def _unit_material(method: str | None) -> str:
    return "Hauling per ton" if method == "per_ton" else "Hauling per load" if method == "per_load" else "Trucking"


def _unit_line(line: LineFacts, base: dict, where: str, deliveries, billed_deliveries: dict, results: list, findings: list) -> None:
    """A line billing tons or loads hauled, against approved deliveries carrying this hauler's charge."""
    method = UNIT_KINDS[line.kind]
    material = _unit_material(method)
    unit = "t" if line.kind == "tons" else "loads"
    mine = [d for d in deliveries if d.hauler_id == line.hauler_id]
    group = f"H{line.invoice_id}:{line.line_no}"
    problems: list[tuple[str, str, str]] = []   # (category, title, explanation) with no money
    if line.tickets:
        by_number: dict[str, list[DeliveryFacts]] = {}
        for d in mine:
            if d.number_key:
                by_number.setdefault(d.number_key, []).append(d)
        chosen = []
        for number in line.tickets:
            found = by_number.get(number, [])
            if not found:
                problems.append(("missing_ticket", f"Billed delivery with no record: {where}",
                                 f"Ticket {number} isn't on record as hauled by {line.hauler}. Find the ticket, or ask the hauler."))
                continue
            if len(found) > 1:
                problems.append(("needs_review", f"Which delivery? {where}",
                                 f"Ticket {number} matches {len(found)} deliveries hauled by {line.hauler}. Check which one was billed."))
                continue
            chosen.append(found[0])
    else:
        chosen = [d for d in mine if line.work_date is not None and d.delivery_date == line.work_date
                  and (not line.truck_key or d.truck_key == line.truck_key)
                  and (line.project_id is None or d.project_id == line.project_id)
                  and d.counted and d.method == method and d.id not in billed_deliveries]
        if not chosen:
            truck = f"truck {line.truck}" if line.truck else "this hauler"
            findings.append(Finding(key=f"{group}:missing", category="missing_ticket", amount=line.amount, expected=Decimal(0),
                                    material=material, title=f"Billed hauling with no approved delivery: {where}",
                                    explanation=f"{KIND_LABEL[line.kind].capitalize()} for {truck} on "
                                                f"{line.work_date.isoformat() if line.work_date else 'no date'}: no approved delivery "
                                                f"hauled {method.replace('_', ' ')} is on record. List the ticket numbers, or ask the hauler.",
                                    **base))
            results.append(LineResult(line.invoice_id, line.line_no, "no_delivery", None, Decimal(0), line.amount))
            return
    supported: list[DeliveryFacts] = []
    for d in chosen:
        number = d.ticket_number or str(d.id)
        if d.id in billed_deliveries:
            first = billed_deliveries[d.id]
            problems.append(("duplicate", f"Billed again: {where}",
                             f"Ticket {number}'s hauling was already billed on invoice {first.invoice_number}, line {first.line_no}."))
        elif line.project_id is not None and d.project_id != line.project_id:
            problems.append(("wrong_project", f"Billed against another project: {where}",
                             f"Ticket {number} was delivered to another project than this invoice's. Ask the hauler to bill it there."))
        elif not d.counted:
            problems.append(("needs_review", f"Delivery not approved yet: {where}",
                             f"Ticket {number} isn't approved yet. The line can be checked once it is."))
        elif d.method == "hourly":
            problems.append(("duplicate", f"Already paid by the hour: {where}",
                             f"Ticket {number} was carried on an hourly truck shift, which pays for it. Billing it {method.replace('_', ' ')} too would pay twice."))
        elif d.method == "freight_included":
            problems.append(("duplicate", f"Freight already in the material price: {where}",
                             f"Ticket {number}'s material price includes freight, so its hauling is already paid through the supplier."))
        elif d.method in ("unpriced", "ambiguous"):
            problems.append(("needs_review", f"No agreed hauling rate: {where}",
                             f"Ticket {number} has no {'single ' if d.method == 'ambiguous' else ''}hauling rate on file, so what it "
                             "should cost isn't known. Add the hauler's rate on Rates & Pricing."))
        elif d.method != method:
            problems.append(("rate", f"Billed {method.replace('_', ' ')}, agreed {d.method.replace('_', ' ')}: {where}",
                             f"Ticket {number}'s hauling is agreed {d.method.replace('_', ' ')}, not {method.replace('_', ' ')}."))
        else:
            supported.append(d)
    # When nothing on the line can be paid, its whole amount is explained by the first problem that
    # isn't "not approved yet": one exception per problem, carrying its dollars, not a second vague one.
    carries = None if supported else next((i for i, (c, _, _) in enumerate(problems) if c != "needs_review"), None)
    for i, (category, title, text) in enumerate(problems):
        money = dict(amount=line.amount, expected=Decimal(0), group=group) if i == carries else {}
        findings.append(Finding(key=f"{group}:{category}:{len(findings)}", category=category, title=title, explanation=text,
                                material=material, **money, **base))
    for d in supported:
        billed_deliveries[d.id] = line
    ids = [d.id for d in chosen]
    if not supported and problems and all(c in ("needs_review",) for c, _, _ in problems):
        results.append(LineResult(line.invoice_id, line.line_no, "not_approved", None, None, None, ticket_ids=ids))
        return
    expected = sum((d.cost for d in supported if d.cost is not None), Decimal(0))
    approved_qty = sum((d.quantity or Decimal(0) for d in supported) if line.kind == "tons" else (d.loads for d in supported), Decimal(0))
    rates = {d.unit_rate for d in supported}
    rate = rates.pop() if len(rates) == 1 else None
    difference = line.amount - expected
    causes: list[tuple[str, str, Decimal, str]] = []
    if supported and line.quantity is not None and line.rate is not None and rate is not None:
        quantity_effect = to_cents((line.quantity - approved_qty) * line.rate)
        rate_effect = to_cents(approved_qty * (line.rate - rate))
        if quantity_effect:
            causes.append(("quantity", "Quantity hauled differs", quantity_effect,
                           f"Billed {line.quantity} {unit}; the approved deliveries add up to {approved_qty} {unit}."))
        if rate_effect:
            causes.append(("rate", "Hauling rate differs", rate_effect,
                           f"Billed ${line.rate} per {'ton' if line.kind == 'tons' else 'load'}; the approved deliveries locked ${rate}."))
        rest = difference - quantity_effect - rate_effect
        if rest:
            causes.append(("arithmetic", "Invoice arithmetic", rest,
                           f"The line's amount isn't its quantity times its rate ({line.quantity} x ${line.rate})."))
    elif difference and carries is None:
        causes.append(("quantity", "Amount differs", difference,
                       f"Billed ${line.amount}; the approved deliveries' hauling comes to ${expected}."))
    category = {"quantity": "quantity", "rate": "rate", "arithmetic": "invoice_arithmetic"}
    for kind, label, effect, text in causes:
        findings.append(Finding(key=f"{group}:{kind}", category=category[kind], title=f"{label}: {where}", explanation=text,
                                amount=effect, expected=expected, group=group, material=material, **base))
    if not supported and any(c in ("duplicate",) for c, _, _ in problems):
        results.append(LineResult(line.invoice_id, line.line_no, "repeat", None, Decimal(0), line.amount, ticket_ids=ids))
        return
    results.append(LineResult(line.invoice_id, line.line_no, "differs" if difference or problems else "matched", None, expected,
                              difference, ticket_ids=ids))


def load_all(db, organization_id: int) -> tuple[list[LineFacts], list[ShiftFacts], list[DeliveryFacts]]:
    """Lines, shifts and the deliveries that name a hauler: everything reconcile() needs."""
    from sqlalchemy import and_, select

    from ..models import Batch, Hauler, Ticket, normal_name
    from .haul_billing import loads_of
    lines, shifts = load(db, organization_id)
    names = dict(db.execute(select(Hauler.id, Hauler.name).where(Hauler.organization_id == organization_id)).all())
    counted = and_(Ticket.status == "verified", Ticket.tonnage_confirmed.is_(True))
    deliveries = [DeliveryFacts(id=t.id, hauler_id=t.hauler_id, number_key=t.number_key, ticket_number=t.ticket_number,
                                truck_key=normal_name(t.truck_number) if t.truck_number else None, delivery_date=t.delivery_date,
                                project_id=project_id, counted=bool(is_counted), method=t.haul_billing, quantity=t.tonnage,
                                unit_rate=t.haul_unit_rate, cost=t.haul_cost, hauler=names.get(t.hauler_id, ""),
                                loads=loads_of(t) or Decimal(1))
                  for t, project_id, is_counted in db.execute(
                      select(Ticket, Batch.project_id, counted).join(Batch, Batch.id == Ticket.batch_id)
                      .where(Ticket.organization_id == organization_id, Ticket.hauler_id.is_not(None), Ticket.status != "skipped"))]
    return lines, shifts, deliveries


def load(db, organization_id: int) -> tuple[list[LineFacts], list[ShiftFacts]]:
    """Every hauling invoice line and truck shift of a company, as facts for reconcile()."""
    from sqlalchemy import select

    from ..models import Hauler, HaulingInvoice, HaulingInvoiceLine, ShiftAllocation, Truck, TruckShift, normal_name
    haulers = dict(db.execute(select(Hauler.id, Hauler.name).where(Hauler.organization_id == organization_id)).all())
    trucks = dict(db.execute(select(Truck.id, Truck.identifier).where(Truck.organization_id == organization_id)).all())
    lines = [LineFacts(invoice_id=i.id, invoice_number=i.invoice_number, hauler_id=i.hauler_id, hauler=haulers.get(i.hauler_id, ""),
                       project_id=i.project_id, period_from=i.period_from, period_to=i.period_to, line_no=line.line_no, kind=line.kind,
                       truck_key=normal_name(line.truck_identifier) if line.truck_identifier else None, truck=line.truck_identifier,
                       work_date=line.work_date, hours=line.hours, rate=line.rate, amount=line.amount, description=line.description,
                       quantity=line.quantity, tickets=ticket_keys(line.tickets), chosen_shift_id=line.shift_id)
             for i, line in db.execute(select(HaulingInvoice, HaulingInvoiceLine).join(HaulingInvoiceLine, HaulingInvoiceLine.invoice_id == HaulingInvoice.id)
                                       .where(HaulingInvoice.organization_id == organization_id))]
    split: dict[int, set[int]] = {}
    for shift_id, project_id in db.execute(select(ShiftAllocation.shift_id, ShiftAllocation.project_id)
                                           .join(TruckShift, TruckShift.id == ShiftAllocation.shift_id)
                                           .where(TruckShift.organization_id == organization_id)):
        split.setdefault(shift_id, set()).add(project_id)
    shifts = []
    for s in db.scalars(select(TruckShift).where(TruckShift.organization_id == organization_id)):
        approved = s.status == "approved"
        top_up = (s.approved_billable_hours - s.approved_work_hours - s.approved_standby_hours) if approved else None
        shifts.append(ShiftFacts(id=s.id, hauler_id=s.hauler_id, truck_key=normal_name(trucks.get(s.truck_id, "")), truck=trucks.get(s.truck_id, ""),
                                 work_date=s.work_date, project_id=s.project_id, status=s.status,
                                 work=s.approved_work_hours, standby=s.approved_standby_hours, top_up=top_up,
                                 hourly=s.applied_hourly_rate, standby_rate=s.applied_standby_rate, hauler=haulers.get(s.hauler_id, ""),
                                 projects=frozenset({s.project_id, *split.get(s.id, ())})))
    return lines, shifts
