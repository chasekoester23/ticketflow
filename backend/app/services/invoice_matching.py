"""Ticket-by-ticket invoice checking: supplier invoice ticket numbers versus
TicketFlow delivery records.

Conservative by design:
- Ticket numbers match exactly, ignoring only letter case and surrounding
  spaces. Leading zeros are significant ("010482" is not "10482"); a
  difference only in leading zeros is reported as a hint, never counted.
- A number that fits more than one delivery record is ambiguous and is never
  matched to any of them.
- Only a verified, complete delivery inside the invoice line's project,
  supplier, material and dates can be a match. Nothing here edits tickets,
  fills in missing values, or approves an invoice.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Batch, InvoiceComparison, InvoiceTicketRef, Project, Ticket
from .pricing import norm

MAX_TICKET_REFS = 2000
MAX_TICKET_NUMBER = 100  # same as tickets.ticket_number
_MAX_TONS = Decimal("1000000")
_EXCEL_SCIENTIFIC = re.compile(r"^\d+(\.\d+)?E[+-]?\d+$", re.IGNORECASE)


class TicketListError(ValueError):
    """The pasted list cannot be read safely; nothing is saved."""


@dataclass(frozen=True)
class ParsedRef:
    position: int  # 1-based order on the invoice, as entered
    ticket_number: str
    billed_tons: Decimal | None


def ticket_key(value: str | None) -> str:
    """Match key: letter case and surrounding spaces ignored, nothing else."""
    return (value or "").strip().upper()


def _zero_insensitive(key: str) -> str:
    return key.lstrip("0")


def _cells(line: str) -> list[str]:
    # Excel copies rows as tab-separated cells; typed lists may use commas,
    # semicolons or spaces. Tabs win so a ticket cell may contain spaces.
    if "\t" in line:
        cells = line.split("\t")
    elif "," in line or ";" in line:
        cells = re.split(r"[,;]", line)
    else:
        cells = line.split()
    cells = [c.strip() for c in cells]
    while cells and not cells[-1]:
        cells.pop()  # Excel may add empty trailing cells
    return cells


def _tons(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise TicketListError(f"'{text}' is not a number of tons (enter it like 24.50)") from None
    if not value.is_finite() or value <= 0 or value >= _MAX_TONS:
        raise TicketListError(f"billed tons '{text}' must be a positive number")
    if value.as_tuple().exponent < -3:
        raise TicketListError(f"billed tons '{text}' has more than 3 decimal places")
    return value.quantize(Decimal("0.001"))  # stored as Numeric(12, 3)


def parse_ticket_list(text: str | None) -> list[ParsedRef]:
    """Read one ticket per line: ticket number, then optional billed tons.

    Blank lines are skipped. A first line whose ticket cell has no digits
    (a copied header such as "Ticket No. / Tons") is skipped. Any other
    unreadable line rejects the whole list with its line number, so a typo
    never silently drops a ticket.
    """
    refs: list[ParsedRef] = []
    problems: list[str] = []
    first_content_line = True
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        cells = _cells(raw)
        if not cells or not any(cells):
            continue
        is_first, first_content_line = first_content_line, False
        number = cells[0]
        if not re.search(r"\d", number):
            if is_first:
                continue  # header row
            problems.append(f"line {line_no}: '{number[:40]}' does not look like a ticket number")
            continue
        try:
            if len(cells) > 2:
                raise TicketListError(
                    "copy only two columns: ticket number, then billed tons (optional)")
            if len(number) > MAX_TICKET_NUMBER:
                raise TicketListError(f"ticket number is longer than {MAX_TICKET_NUMBER} characters")
            if _EXCEL_SCIENTIFIC.match(number):
                raise TicketListError(
                    f"'{number}' looks like an Excel number in scientific notation; "
                    "format the ticket column as Text and copy it again")
            tons = _tons(cells[1]) if len(cells) == 2 and cells[1] else None
        except TicketListError as exc:
            problems.append(f"line {line_no}: {exc}")
            continue
        refs.append(ParsedRef(len(refs) + 1, number, tons))
    if problems:
        shown = "; ".join(problems[:8])
        more = f" (and {len(problems) - 8} more)" if len(problems) > 8 else ""
        raise TicketListError(f"Ticket list needs fixes — {shown}{more}. Nothing was saved.")
    if len(refs) > MAX_TICKET_REFS:
        raise TicketListError(f"A ticket list can have at most {MAX_TICKET_REFS} tickets per invoice line.")
    return refs


def format_ticket_list(refs) -> str:
    """Stable text form used for the correction history."""
    return "; ".join(
        r.ticket_number + (f" {r.billed_tons} t" if r.billed_tons is not None else "")
        for r in refs
    )


# ---------------------------------------------------------------- matching

@dataclass
class TicketInfo:
    ticket_id: int
    batch_id: int
    ticket_number: str | None
    delivery_date: date | None
    supplier: str | None
    material: str | None
    tons: Decimal | None
    review_status: str
    project: str | None


@dataclass
class CheckRow:
    """One invoice ticket reference, or one in-scope delivery not on the invoice."""
    result: str
    line: int | None = None
    invoice_ticket_number: str | None = None
    billed_tons: Decimal | None = None
    ticket: TicketInfo | None = None
    candidates: list[TicketInfo] = field(default_factory=list)
    verified_tons: Decimal | None = None
    tons_difference: Decimal | None = None
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class TicketCheck:
    rows: list[CheckRow]
    counts: dict[str, int]
    itemized_billed_tons: Decimal | None
    warnings: list[str]
    incomplete: bool
    discrepancy: bool


RESULTS = ("matched", "tons_differ", "not_found", "outside_scope",
           "ambiguous", "needs_review", "not_on_invoice")
FLAGS = ("duplicate_on_invoice", "on_another_invoice")


def _info(t: Ticket, project: str | None) -> TicketInfo:
    return TicketInfo(
        ticket_id=t.id, batch_id=t.batch_id, ticket_number=t.ticket_number,
        delivery_date=t.delivery_date, supplier=t.supplier, material=t.material_type,
        tons=t.tonnage, review_status=t.status, project=project,
    )


def _conflicts(t: Ticket, project_id: int | None, item: InvoiceComparison,
               projects: dict[int, str]) -> list[str]:
    """Known facts that put this delivery outside the invoice line."""
    reasons = []
    if project_id != item.project_id:
        reasons.append(f"project {projects[project_id]}" if project_id in projects
                       else "no project assigned")
    if norm(t.supplier) and norm(t.supplier) != item.supplier_key:
        reasons.append(f"supplier {t.supplier}")
    if norm(t.material_type) and norm(t.material_type) != item.material_key:
        reasons.append(f"material {t.material_type}")
    if t.delivery_date is not None and not (item.date_from <= t.delivery_date <= item.date_to):
        reasons.append(f"delivered {t.delivery_date.isoformat()}")
    return reasons


def _gaps(t: Ticket) -> list[str]:
    """Why a delivery record cannot be relied on yet."""
    gaps = []
    if t.status != "verified":
        gaps.append("not verified (" + t.status.replace("_", " ") + ")")
    elif not t.tonnage_confirmed:
        gaps.append("tons not confirmed")
    for label, value in (("supplier", norm(t.supplier)), ("material", norm(t.material_type)),
                         ("delivery date", t.delivery_date), ("tons", t.tonnage)):
        if not value:
            gaps.append(f"{label} missing")
    return gaps


def check_tickets(
    db: Session, organization_id: int, item: InvoiceComparison,
    refs: list[InvoiceTicketRef], in_scope: list[Ticket],
) -> TicketCheck:
    keys = [ticket_key(r.ticket_number) for r in refs]
    loose_keys = {_zero_insensitive(k) for k in keys}
    projects = dict(db.execute(
        select(Project.id, Project.code).where(Project.organization_id == organization_id)
    ).all())

    # Every company delivery whose number could matter, in one query. The
    # SQL filter is deliberately loose (leading zeros ignored); exact keys
    # are compared in Python below.
    by_key: dict[str, list[tuple[Ticket, int | None]]] = defaultdict(list)
    by_loose: dict[str, list[tuple[Ticket, int | None]]] = defaultdict(list)
    if loose_keys:
        found = db.execute(
            select(Ticket, Batch.project_id).join(Batch, Batch.id == Ticket.batch_id).where(
                Batch.organization_id == organization_id,
                Ticket.status != "skipped",
                Ticket.ticket_number.is_not(None),
                func.ltrim(func.upper(func.trim(Ticket.ticket_number)), "0").in_(loose_keys),
            ).order_by(Ticket.id)
        ).all()
        for t, project_id in found:
            key = ticket_key(t.ticket_number)
            by_key[key].append((t, project_id))
            by_loose[_zero_insensitive(key)].append((t, project_id))

    # The same supplier ticket on another saved invoice line is possible
    # double billing (same company and supplier; any project).
    elsewhere: dict[str, list[str]] = defaultdict(list)
    if keys:
        for key, number in db.execute(
            select(InvoiceTicketRef.ticket_key, InvoiceComparison.invoice_number)
            .join(InvoiceComparison, InvoiceComparison.id == InvoiceTicketRef.comparison_id)
            .where(
                InvoiceComparison.organization_id == organization_id,
                InvoiceComparison.supplier_key == item.supplier_key,
                InvoiceComparison.id != item.id,
                InvoiceTicketRef.ticket_key.in_(set(keys)),
            ).order_by(InvoiceComparison.id)
        ).all():
            if number not in elsewhere[key]:
                elsewhere[key].append(number)

    repeats = Counter(keys)
    rows: list[CheckRow] = []
    referenced: set[int] = set()
    for ref, key in zip(refs, keys):
        row = CheckRow(result="", line=ref.position, invoice_ticket_number=ref.ticket_number,
                       billed_tons=ref.billed_tons)
        same = by_key.get(key, [])
        possible = [(t, p) for t, p in same if not _conflicts(t, p, item, projects)]
        if len(possible) > 1:
            row.result = "ambiguous"
            row.candidates = [_info(t, projects.get(p)) for t, p in possible]
            row.notes.append(f"{len(possible)} delivery records share this ticket number; "
                             "none is matched until the duplicates are resolved.")
        elif possible:
            t, p = possible[0]
            row.ticket = _info(t, projects.get(p))
            gaps = _gaps(t)
            if gaps:
                row.result = "needs_review"
                row.notes.append("Delivery record is " + ", ".join(gaps) + ".")
            else:
                row.verified_tons = t.tonnage
                if ref.billed_tons is None:
                    row.result = "matched"
                    row.notes.append("No billed tons entered for this ticket; quantity not compared.")
                else:
                    row.tons_difference = ref.billed_tons - t.tonnage
                    row.result = "tons_differ" if row.tons_difference else "matched"
        elif same:
            row.result = "outside_scope"
            row.candidates = [_info(t, projects.get(p)) for t, p in same]
            for t, p in same:
                row.notes.append("Ticket " + (t.ticket_number or "") + " is recorded with "
                                 + ", ".join(_conflicts(t, p, item, projects))
                                 + " — outside this invoice line.")
        else:
            row.result = "not_found"
            near = by_loose.get(_zero_insensitive(key), [])
            row.candidates = [_info(t, projects.get(p)) for t, p in near]
            if near:
                row.notes.append("Not counted: "
                                 + ", ".join("ticket " + (t.ticket_number or "") for t, _ in near)
                                 + " differs only in leading zeros. Compare both documents.")
            else:
                row.notes.append("No delivery record in TicketFlow has this ticket number.")
        if repeats[key] > 1:
            row.flags.append("duplicate_on_invoice")
            row.notes.append(f"This ticket number appears {repeats[key]} times on this invoice line.")
        if elsewhere.get(key):
            row.flags.append("on_another_invoice")
            row.notes.append("Also listed on invoice " + ", ".join(elsewhere[key]) + ".")
        # Ambiguous candidates are accounted for by this row. A leading-zero
        # near miss is NOT: that delivery still counts as missing below.
        if row.result == "ambiguous":
            referenced.update(c.ticket_id for c in row.candidates)
        if row.ticket:
            referenced.add(row.ticket.ticket_id)
        rows.append(row)

    # Deliveries in this line's scope that the invoice does not list.
    missing_verified = 0
    for t in in_scope:
        if t.id in referenced:
            continue
        row = CheckRow(result="not_on_invoice", ticket=_info(t, projects.get(item.project_id)))
        if t.status == "verified" and t.tonnage_confirmed:
            row.verified_tons = t.tonnage
            missing_verified += 1
        else:
            row.notes.append("Delivery is not verified yet.")
        if not t.ticket_number:
            row.notes.append("Delivery record has no ticket number, so it cannot be matched.")
        rows.append(row)

    counts = {name: 0 for name in RESULTS + FLAGS}
    for row in rows:
        counts[row.result] += 1
        for flag in row.flags:
            counts[flag] += 1

    itemized = None
    if refs and all(r.billed_tons is not None for r in refs):
        itemized = sum((r.billed_tons for r in refs), Decimal(0))

    warnings = []
    if counts["not_found"]:
        warnings.append(f"{counts['not_found']} invoice ticket number(s) have no matching delivery record.")
    if counts["outside_scope"]:
        warnings.append(f"{counts['outside_scope']} invoice ticket(s) match a delivery recorded "
                        "for a different project, supplier, material or date.")
    if counts["ambiguous"]:
        warnings.append(f"{counts['ambiguous']} invoice ticket number(s) match more than one "
                        "delivery record; resolve the duplicates before relying on this comparison.")
    if counts["needs_review"]:
        warnings.append(f"{counts['needs_review']} invoice ticket(s) match a delivery that is "
                        "not verified or is incomplete.")
    if counts["tons_differ"]:
        warnings.append(f"{counts['tons_differ']} ticket(s) are billed at different tons than verified.")
    if counts["duplicate_on_invoice"]:
        warnings.append(f"{counts['duplicate_on_invoice']} invoice line(s) repeat a ticket number.")
    if counts["on_another_invoice"]:
        warnings.append(f"{counts['on_another_invoice']} ticket(s) are also listed on another "
                        "saved invoice from this supplier.")
    if missing_verified:
        warnings.append(f"{missing_verified} verified delivery ticket(s) in this scope are not "
                        "listed on the invoice.")
    if itemized is not None and itemized != item.billed_tons:
        warnings.append(f"Itemized ticket tons add up to {itemized}, but the invoice line bills "
                        f"{item.billed_tons} tons.")

    return TicketCheck(
        rows=rows, counts=counts, itemized_billed_tons=itemized, warnings=warnings,
        incomplete=bool(counts["ambiguous"] or counts["needs_review"]),
        discrepancy=bool(
            counts["not_found"] or counts["outside_scope"] or counts["tons_differ"]
            or counts["duplicate_on_invoice"] or counts["on_another_invoice"]
            or missing_verified or (itemized is not None and itemized != item.billed_tons)
        ),
    )
