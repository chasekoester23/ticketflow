"""Billing patterns worth a question, across a company's invoice lines.

Each is only a reason to look: a supplier may have a good explanation. Nothing
here changes a figure or marks anything as wrong.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import InvoiceComparison, InvoiceTicketRef

_MAX_SHOWN = 100


def find(db: Session, organization_id: int, items: list[InvoiceComparison]) -> list[dict]:
    by_id = {item.id: item for item in items}
    found: list[dict] = []

    # The same supplier ticket on more than one invoice line.
    listed: dict[tuple[str, str], dict] = {}
    for key, number, comparison_id, supplier_key in db.execute(
        select(InvoiceTicketRef.ticket_key, InvoiceTicketRef.ticket_number, InvoiceComparison.id, InvoiceComparison.supplier_key)
        .join(InvoiceComparison, InvoiceComparison.id == InvoiceTicketRef.comparison_id)
        .where(InvoiceComparison.organization_id == organization_id)
    ):
        entry = listed.setdefault((supplier_key, key), {"number": number, "lines": set()})
        entry["lines"].add(comparison_id)
    for (_supplier, _key), entry in sorted(listed.items()):
        if len(entry["lines"]) > 1:
            lines = sorted(entry["lines"])
            first = by_id[lines[0]]
            found.append({
                "kind": "ticket_on_two_invoices",
                "title": f"Ticket {entry['number']} is on {len(lines)} invoice lines",
                "detail": f"{first.supplier_name}: " + ", ".join(by_id[i].invoice_number for i in lines) + ". It may be billed twice.",
                "line_ids": lines,
            })

    # The same bill under a different invoice number: possibly sent again.
    same_bill: dict[tuple, list[InvoiceComparison]] = defaultdict(list)
    same_number: dict[tuple, list[InvoiceComparison]] = defaultdict(list)
    for item in items:
        # Same delivery window too. Fixed loads make equal amounts common across
        # different months, and a false alarm on every invoice gets ignored.
        same_bill[(item.supplier_key, item.material_key, item.date_from, item.date_to, item.billed_tons, item.billed_unit_price, item.billed_subtotal)].append(item)
        same_number[(item.supplier_key, item.material_key, item.invoice_number.strip().upper())].append(item)
    for group in same_bill.values():
        if len({item.invoice_number for item in group}) > 1:
            found.append({
                "kind": "same_amounts_new_number",
                "title": f"Identical amounts on {len(group)} invoice lines",
                "detail": f"{group[0].supplier_name}, {group[0].material_name}: {group[0].billed_tons} tons, ${group[0].billed_subtotal} on "
                          + ", ".join(item.invoice_number for item in group) + ". It may be the same bill sent again.",
                "line_ids": sorted(item.id for item in group),
            })
    for group in same_number.values():
        if len(group) > 1:
            found.append({
                "kind": "invoice_number_reused",
                "title": f"Invoice {group[0].invoice_number} was entered {len(group)} times for the same material",
                "detail": f"{group[0].supplier_name}, {group[0].material_name}. Check these aren't the same line recorded twice.",
                "line_ids": sorted(item.id for item in group),
            })
    return found[:_MAX_SHOWN]
