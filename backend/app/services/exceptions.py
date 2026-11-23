"""Billing exceptions: every reason to question an invoice, in one list.

Nothing here is stored or decided. Each exception is recomputed from the company's
invoice lines, delivery tickets and locked prices by the comparison engine that
already explains each line (reconciliation.differences), plus the cross-invoice
duplicate checks (billing_exceptions) and the unbilled-delivery report. A person's
handling of it (owner, status, credit received) is kept separately, found again by
the exception's key.

Money is never counted twice. On an invoice line whose approved cost is known,
reconciliation.differences splits billed-minus-approved into causes (tickets' tons,
rates, the invoice's own arithmetic) that add up to exactly the line's difference.
Each exception carries one of those causes, and nothing else carries money:
duplicates point at tons already inside a line's causes, and unbilled deliveries are
a separate figure (owed to the supplier later), never added to overcharges.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from ..models import InvoiceComparison, Project
from . import billing_exceptions, reconciliation

CATEGORIES = {
    "rate": "Rate differs from agreement",
    "quantity": "Billed tons differ from deliveries",
    "duplicate": "Possible duplicate charge",
    "missing_ticket": "Billed delivery with no matching record",
    "not_invoiced": "Approved delivery not billed",
    "unmatched_line": "Invoice line with nothing to compare",
    "needs_review": "Needs a person to decide",
    "invoice_arithmetic": "Invoice arithmetic",
}
SEVERITIES = ("high", "medium", "low")
# Billed above approved by at least this much in one cause is high severity.
HIGH_AMOUNT = Decimal("250.00")

_KIND_CATEGORY = {
    "tons_differ": "quantity", "quantity": "quantity", "rate": "rate", "arithmetic": "invoice_arithmetic",
    "not_found": "missing_ticket", "outside_scope": "missing_ticket", "repeated": "duplicate",
    "ambiguous": "needs_review", "unverified": "needs_review", "not_on_invoice": "not_invoiced",
}


@dataclass
class Detected:
    key: str
    category: str
    severity: str
    title: str
    explanation: str
    project_id: int | None
    supplier: str
    material: str | None = None
    invoice_numbers: list[str] = field(default_factory=list)
    line_ids: list[int] = field(default_factory=list)
    ticket_number: str | None = None
    ticket_ids: list[int] = field(default_factory=list)
    billed_tons: Decimal | None = None
    approved_tons: Decimal | None = None
    # This cause's share of the line's billed minus approved. None when it isn't known.
    amount: Decimal | None = None
    # The whole line, for context: what it billed, and what approved deliveries support.
    line_billed: Decimal | None = None
    line_approved: Decimal | None = None
    # Approved deliveries not billed (tickets, tons, cost): owed later, never an overcharge.
    unbilled_tickets: int | None = None
    unbilled_cost: Decimal | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _severity(category: str, amount: Decimal | None) -> str:
    if category == "duplicate" or (amount is not None and amount >= HIGH_AMOUNT):
        return "high"
    if (amount is not None and amount > 0) or category in ("missing_ticket", "unmatched_line"):
        return "medium"
    return "low"


def _ticket_ids(line, number: str | None) -> list[int]:
    if not number:
        return []
    found = []
    for row in line.ticket_check:
        if row.invoice_ticket_number == number or (row.ticket and row.ticket.ticket_number == number):
            if row.ticket:
                found.append(row.ticket.ticket_id)
            found.extend(c.ticket_id for c in row.candidates)
    return sorted(set(found))


def _from_line(line) -> list[Detected]:
    """One exception per cause on one invoice line."""
    found: list[Detected] = []
    common = dict(project_id=line.project_id, supplier=line.supplier_name, material=line.material_name,
                  invoice_numbers=[line.invoice_number], line_ids=[line.id],
                  line_billed=line.billed_subtotal, line_approved=line.verified_subtotal)
    rate_index = 0
    for entry in reconciliation.differences(line):
        kind = entry["kind"]
        if kind in ("rounding", "on_another_invoice"):
            continue  # rounding is expected; a ticket on two invoices is one cross-invoice exception
        category = _KIND_CATEGORY[kind]
        number = entry["ticket_number"]
        if kind == "rate":
            key = f"L{line.id}:rate:{rate_index}"
            rate_index += 1
        else:
            key = f"L{line.id}:{kind}:{number or ''}"
        label = reconciliation.KINDS[kind]
        title = f"{label}: invoice {line.invoice_number}" + (f", ticket {number}" if number else "")
        found.append(Detected(
            key=key, category=category, severity=_severity(category, entry["dollar_effect"]), title=title,
            explanation=entry["check"].strip(), ticket_number=number, ticket_ids=_ticket_ids(line, number),
            billed_tons=entry["billed_tons"], approved_tons=entry["approved_tons"], amount=entry["dollar_effect"],
            **common))
    if line.status in ("no_verified_tickets", "incomplete") and not any(
            d.category in ("missing_ticket", "needs_review") for d in found):
        category = "unmatched_line" if line.status == "no_verified_tickets" else "needs_review"
        found.append(Detected(
            key=f"L{line.id}:status", category=category, severity=_severity(category, None),
            title=(f"Invoice {line.invoice_number}: no approved deliveries to compare with" if category == "unmatched_line"
                   else f"Invoice {line.invoice_number} can't be checked yet"),
            explanation=" ".join(line.warnings[:4]), **common))
    return found


def detect(db: Session, organization_id: int, items: list[InvoiceComparison], lines: list) -> list[Detected]:
    found: list[Detected] = []
    for line in lines:
        found.extend(_from_line(line))
    by_id = {line.id: line for line in lines}
    for pattern in billing_exceptions.find(db, organization_id, items):
        group = [by_id[i] for i in pattern["line_ids"] if i in by_id]
        if not group:
            continue
        found.append(Detected(
            key=f"D:{pattern['kind']}:" + ",".join(str(i) for i in pattern["line_ids"]), category="duplicate", severity="high",
            title=pattern["title"], explanation=pattern["detail"],
            project_id=group[0].project_id if len({g.project_id for g in group}) == 1 else None,
            supplier=group[0].supplier_name, material=group[0].material_name,
            invoice_numbers=sorted({g.invoice_number for g in group}), line_ids=pattern["line_ids"]))
    gaps = reconciliation.unbilled_gaps_by_project(db, organization_id)
    for project in db.query(Project).filter(Project.organization_id == organization_id, Project.id.in_(list(gaps))):
        for gap in gaps[project.id]:
            found.append(Detected(
                key=f"U:{project.id}:{gap['supplier']}:{gap['material']}:{gap['date_from']}", category="not_invoiced",
                severity="low",
                title=f"Not billed yet: {gap['supplier']}, {gap['material']}, {gap['date_from']} to {gap['date_to']}",
                explanation=(f"{gap['tickets']} approved deliver{'y' if gap['tickets'] == 1 else 'ies'} ({gap['tons']} t) in "
                             f"{project.code} that no invoice line lists or covers. Expect a bill, or ask whether one was missed."),
                project_id=project.id, supplier=gap["supplier"], material=gap["material"],
                approved_tons=Decimal(gap["tons"]), unbilled_tickets=gap["tickets"]))
    return found


def summarize(open_items: list[dict], all_reviews: list[dict]) -> dict:
    """Totals a person can rely on: potential overcharge is only causes with a known,
    positive effect, each counted once. Recovery is only what people recorded."""
    # Per invoice line, never more than the line actually billed above approved: the causes
    # leave out rounding, so their sum can run a cent over it.
    per_line: dict[int, Decimal] = {}
    caps: dict[int, Decimal] = {}
    loose: list[Decimal] = []
    for i in open_items:
        if i["amount"] is None or i["amount"] <= 0:
            continue
        if len(i["line_ids"]) == 1 and i["line_billed"] is not None and i["line_approved"] is not None:
            line = i["line_ids"][0]
            per_line[line] = per_line.get(line, Decimal(0)) + i["amount"]
            caps[line] = max(Decimal(0), i["line_billed"] - i["line_approved"])
        else:
            loose.append(i["amount"])
    positive = [min(total, caps[line]) for line, total in per_line.items()] + loose
    negative = [i["amount"] for i in open_items if i["amount"] is not None and i["amount"] < 0]
    disputed = [i["amount"] for i in open_items if i["status"] == "disputed" and i["amount"] is not None and i["amount"] > 0]
    by_category = {key: sum(1 for i in open_items if i["category"] == key) for key in CATEGORIES}
    return {
        "open": len(open_items),
        "by_category": by_category,
        "by_severity": {s: sum(1 for i in open_items if i["severity"] == s) for s in SEVERITIES},
        "potential_overcharge": str(sum(positive, Decimal(0))),
        "billed_below_approved": str(-sum(negative, Decimal(0))),
        "disputed_amount": str(sum(disputed, Decimal(0))),
        "money_unknown": sum(1 for i in open_items if i["amount"] is None and i["category"] in ("quantity", "rate", "missing_ticket")),
        "recovered": str(sum((Decimal(r["recovered_amount"]) for r in all_reviews
                              if r["status"] == "recovered" and r["recovered_amount"] is not None), Decimal(0))),
        "unassigned": sum(1 for i in open_items if i["assignee_user_id"] is None),
    }
