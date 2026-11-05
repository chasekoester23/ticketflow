"""Why an invoice line does or doesn't match, in figures that add up exactly.

Starts from what's verified, walks through every difference, and lands on what was
billed. Whatever isn't explained is shown as unexplained: nothing is rounded away,
and nothing here changes a ticket or approves an invoice.
"""
from __future__ import annotations

from decimal import Decimal

from .pricing import to_cents

_MAX_LISTED = 25
_LABELS = {
    "not_found": ("Billed on tickets with no delivery record", "Find the paper for {list}, or ask the supplier for a copy."),
    "outside_scope": ("Billed on tickets recorded for another project, supplier, material or date", "Check where {list} were recorded: the delivery or the invoice line may be filed wrongly."),
    "unverified": ("Billed on deliveries not approved yet", "Review {list}, then this comparison updates by itself."),
    "ambiguous": ("Ticket numbers that fit more than one delivery", "Resolve the duplicate records for {list}."),
    "tons_differ": ("Billed tons differ from verified tons on the same ticket", "Compare the scan with the invoice for {list}."),
    "repeated": ("Tickets billed more than once on this line", "Ask the supplier about {list}: each appears more than once."),
    "not_on_invoice": ("Verified deliveries this invoice doesn't list", "Check whether {list} were billed elsewhere or not yet."),
}
_ORDER = tuple(_LABELS)


def _names(numbers: list[str]) -> str:
    shown = ", ".join(numbers[:6])
    return shown + (f" and {len(numbers) - 6} more" if len(numbers) > 6 else "")


def tons_bridge(*, verified_ids: set[int], verified_tons: Decimal, billed_tons: Decimal, rows: list) -> dict:
    """From verified tons to billed tons. Holds exactly when ``complete``:
    start + sum(items) + unexplained == billed."""
    groups: dict[str, dict] = {}
    consumed: set[int] = set()
    missing_tons = 0

    def add(kind: str, tons: Decimal, number: str | None) -> None:
        group = groups.setdefault(kind, {"kind": kind, "label": _LABELS[kind][0], "tons": Decimal(0), "tickets": []})
        group["tons"] += tons
        if number:
            group["tickets"].append(number)

    for row in rows:
        if row.line is None:
            continue
        tickets = [row.ticket] if row.ticket else (row.candidates if row.result == "ambiguous" else [])
        # Tons this invoice row accounts for, the first time it names a delivery.
        fresh = [t for t in tickets if t.ticket_id in verified_ids and t.ticket_id not in consumed]
        accounted = sum((t.tons or Decimal(0) for t in fresh), Decimal(0))
        repeated = bool(tickets) and not fresh and any(t.ticket_id in consumed for t in tickets)
        consumed.update(t.ticket_id for t in fresh)
        number = row.invoice_ticket_number
        if row.billed_tons is None:
            missing_tons += 1
            continue
        change = row.billed_tons - accounted
        if repeated or "duplicate_on_invoice" in row.flags and not fresh:
            add("repeated", change, number)
        elif row.result == "matched" and not change:
            continue
        elif row.result in ("matched", "tons_differ"):
            add("tons_differ", change, number)
        elif row.result == "not_found":
            add("not_found", change, number)
        elif row.result == "outside_scope":
            add("outside_scope", change, number)
        elif row.result == "ambiguous":
            add("ambiguous", change, number)
        else:
            add("unverified", change, number)
    for row in rows:
        if row.line is None and row.verified_tons is not None and row.ticket and row.ticket.ticket_id in verified_ids:
            add("not_on_invoice", -row.verified_tons, row.ticket.ticket_number or f"record #{row.ticket.ticket_id}")

    items = [{
        "kind": kind, "label": groups[kind]["label"], "tons": groups[kind]["tons"],
        "count": len(groups[kind]["tickets"]), "tickets": groups[kind]["tickets"][:_MAX_LISTED],
    } for kind in _ORDER if kind in groups]
    complete = missing_tons == 0
    explained = verified_tons + sum((item["tons"] for item in items), Decimal(0))
    return {
        "start": verified_tons, "items": items, "billed": billed_tons, "complete": complete,
        "rows_without_tons": missing_tons,
        "unexplained": billed_tons - explained if complete else None,
    }


def money_bridge(*, verified_rows: list, verified_subtotal: Decimal | None, billed_subtotal: Decimal,
                 billed_tons: Decimal, billed_rate: Decimal) -> dict | None:
    """From verified cost to billed subtotal. None when a verified ticket has no
    locked price, because the starting figure isn't known.
    start + quantity + rate + arithmetic + rounding == billed, exactly."""
    if verified_subtotal is None:
        return None
    verified_tons = sum((t.tonnage or Decimal(0) for t in verified_rows), Decimal(0))
    quantity_effect = to_cents((billed_tons - verified_tons) * billed_rate)
    by_rate: dict[Decimal, dict] = {}
    rate_effect = Decimal(0)
    for ticket in verified_rows:
        locked = ticket.applied_unit_price
        if locked == billed_rate:
            continue
        tons = ticket.tonnage or Decimal(0)
        effect = to_cents((billed_rate - locked) * tons)
        rate_effect += effect
        group = by_rate.setdefault(locked, {"locked_rate": locked, "tickets": 0, "tons": Decimal(0), "effect": Decimal(0)})
        group["tickets"] += 1
        group["tons"] += tons
        group["effect"] += effect
    arithmetic = billed_subtotal - to_cents(billed_tons * billed_rate)
    rounding = billed_subtotal - verified_subtotal - quantity_effect - rate_effect - arithmetic
    return {
        "start": verified_subtotal, "quantity_effect": quantity_effect, "rate_effect": rate_effect,
        "rate_groups": [by_rate[rate] for rate in sorted(by_rate)], "arithmetic_effect": arithmetic,
        "rounding": rounding, "billed": billed_subtotal,
    }


def actions(*, tons: dict | None, itemized: bool, pending: int, unclassified: int, unpriced: int, rate_mismatch: int,
            other_invoice: list[str], intake_issues: list[str], project_id: int, supplier: str) -> list[dict]:
    """What a person can do next, most useful first."""
    steps: list[dict] = []
    if pending or unclassified:
        count = pending + unclassified
        steps.append({"text": f"Review {count} delivery record(s) that could belong to this line.",
                      "href": f"/records?project_id={project_id}&status=needs_review"})
    for issue in intake_issues:
        steps.append({"text": issue, "href": "/upload"})
    if unpriced:
        steps.append({"text": f"Enter a rate and re-approve {unpriced} verified ticket(s) with no locked price.",
                      "href": f"/records?project_id={project_id}&status=verified"})
    if tons:
        for item in tons["items"]:
            if item["tickets"]:
                steps.append({"text": _LABELS[item["kind"]][1].format(list=_names(item["tickets"])),
                              "href": None})
    if rate_mismatch:
        steps.append({"text": f"Compare the invoice rate with the rate locked on {rate_mismatch} ticket(s), and your supplier's agreed price.",
                      "href": "/pricing"})
    if other_invoice:
        steps.append({"text": f"Ask {supplier} about {_names(other_invoice)}: also listed on another invoice.", "href": None})
    if not itemized:
        steps.append({"text": "Enter this invoice's ticket numbers to check each delivery, not only the totals.", "href": None})
    return steps


def headline(*, status: str, itemized: bool, quantity_difference: Decimal, subtotal_difference: Decimal | None,
             pending: int, unclassified: int, verified_count: int) -> str:
    """One plain sentence for the top of the panel."""
    if status == "no_verified_tickets":
        return "No approved deliveries match this line yet, so there's nothing to compare it with."
    if status == "matches":
        return ("Every listed ticket and the totals agree with approved deliveries." if itemized
                else "The totals agree, but individual tickets weren't checked.")
    if status == "incomplete":
        waiting = pending + unclassified
        if waiting:
            return f"Can't be settled yet: {waiting} delivery record(s) that may belong here still need a person."
        return "Can't be settled yet: something this comparison depends on still needs a person."
    parts = []
    if quantity_difference:
        parts.append(f"billed {abs(quantity_difference)} tons {'more' if quantity_difference > 0 else 'less'} than approved")
    if subtotal_difference:
        parts.append(f"${abs(subtotal_difference)} {'more' if subtotal_difference > 0 else 'less'} in material cost")
    if parts:
        return "This invoice line is " + " and ".join(parts) + "."
    return "The totals agree, but individual tickets don't." if itemized else "A figure on this invoice line doesn't agree."
