"""What's been delivered but not billed, and every difference on what has been.

Both only describe. Nothing here approves a delivery, an invoice or a payment, and
nothing changes a record. The figures are exact: tons and cents are added up as
whole numbers, and every dollar effect on an invoice line adds up to exactly that
line's difference between what was billed and what's approved.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterator

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from ..models import Batch, InvoiceComparison, InvoiceTicketLink, InvoiceTicketRef, Project, Ticket
from . import aggregates, invoice_bridge
from .pricing import to_cents

MAX_ROWS = 500
NOT_ENTERED = "(not entered)"
UNBILLED_NOTE = ("Approved deliveries that no saved invoice line lists by ticket number or covers by its dates. "
                 "Each may still be billed later. This is not a bill and not an approval to pay.")


# ------------------------------------------------------------------ unbilled deliveries

def _unbilled(organization_id: int, project_id: int):
    """Approved deliveries in this project that no invoice line lists or covers, and that
    no person has linked to a line."""
    in_project = select(Batch.id).where(Batch.organization_id == organization_id, Batch.project_id == project_id)
    covered = select(InvoiceComparison.id).where(
        InvoiceComparison.organization_id == organization_id, InvoiceComparison.project_id == project_id,
        InvoiceComparison.supplier_key == Ticket.supplier_key, InvoiceComparison.material_key == Ticket.material_key,
        InvoiceComparison.date_from <= Ticket.delivery_date, InvoiceComparison.date_to >= Ticket.delivery_date).exists()
    # Listed by number on any line from this supplier, whatever its project or dates: that
    # one is billed, even if it was billed under the wrong job.
    listed = select(InvoiceTicketRef.id).join(InvoiceComparison, InvoiceComparison.id == InvoiceTicketRef.comparison_id).where(
        InvoiceComparison.organization_id == organization_id, InvoiceComparison.supplier_key == Ticket.supplier_key,
        InvoiceTicketRef.ticket_key == Ticket.number_key).exists()
    linked = select(InvoiceTicketLink.id).where(InvoiceTicketLink.ticket_id == Ticket.id, InvoiceTicketLink.released_at.is_(None)).exists()
    return and_(Ticket.organization_id == organization_id, Ticket.batch_id.in_(in_project), aggregates.COUNTED, ~covered, ~listed, ~linked)


_ROW_COLUMNS = (Ticket.id, Ticket.batch_id, Ticket.ticket_number, Ticket.delivery_date, Ticket.supplier, Ticket.material_type,
                Ticket.tonnage, Ticket.approved_cost)


def _row(row) -> dict:
    return {"ticket_id": row.id, "batch_id": row.batch_id, "ticket_number": row.ticket_number,
            "delivery_date": row.delivery_date.isoformat() if row.delivery_date else None, "supplier": row.supplier,
            "material": row.material_type, "tons": format(row.tonnage, ".3f") if row.tonnage is not None else None,
            "cost": format(row.approved_cost, ".2f") if row.approved_cost is not None else None}


def _ordered(query):
    return query.order_by(Ticket.delivery_date.asc().nulls_last(), Ticket.id)


def unbilled(db: Session, organization_id: int, project: Project) -> dict:
    condition = _unbilled(organization_id, project.id)
    totals = db.execute(select(func.count().label("tickets"), aggregates.exact_sum(Ticket.tonnage, 3).label("tons"),
                               aggregates.exact_sum(Ticket.approved_cost, 2).label("cost"),
                               func.coalesce(func.sum(case((Ticket.approved_cost.is_(None), 1), else_=0)), 0).label("without_cost"))
                        .where(condition)).one()
    rows = [_row(row) for row in db.execute(_ordered(select(*_ROW_COLUMNS).where(condition)).limit(MAX_ROWS))]
    return {
        "project": {"id": project.id, "code": project.code, "name": project.name},
        "total_tickets": int(totals.tickets),
        "total_tons": str(aggregates.as_decimal(totals.tons, 3)),
        # Approved tickets with no locked price aren't in this figure. They're counted
        # below rather than treated as costing nothing.
        "total_cost": str(aggregates.as_decimal(totals.cost, 2)),
        "tickets_without_cost": int(totals.without_cost),
        "rows": rows,
        "truncated": int(totals.tickets) > len(rows),
        "gaps": _gaps(db, organization_id, project.id, condition),
        "note": UNBILLED_NOTE,
    }


def unbilled_rows(db: Session, organization_id: int, project_id: int) -> Iterator[dict]:
    """Every row, read a chunk at a time, for the CSV."""
    query = _ordered(select(*_ROW_COLUMNS).where(_unbilled(organization_id, project_id))).execution_options(yield_per=2000)
    for row in db.execute(query):
        yield _row(row)


def _gaps(db: Session, organization_id: int, project_id: int, condition) -> list[dict]:
    """Runs of unbilled delivery dates, per supplier and material, that no invoice line's
    dates cover. A run ends wherever a line's dates fall between two deliveries."""
    days = db.execute(select(Ticket.supplier_key, Ticket.material_key, Ticket.delivery_date, func.min(Ticket.supplier).label("supplier"),
                             func.min(Ticket.material_type).label("material"), func.count().label("tickets"),
                             aggregates.exact_sum(Ticket.tonnage, 3).label("tons"))
                      .where(condition, Ticket.delivery_date.is_not(None))
                      .group_by(Ticket.supplier_key, Ticket.material_key, Ticket.delivery_date)).all()
    windows: dict[tuple, list[tuple[date, date]]] = {}
    for supplier_key, material_key, start, end in db.execute(select(
            InvoiceComparison.supplier_key, InvoiceComparison.material_key, InvoiceComparison.date_from, InvoiceComparison.date_to).where(
            InvoiceComparison.organization_id == organization_id, InvoiceComparison.project_id == project_id)):
        windows.setdefault((supplier_key, material_key), []).append((start, end))
    by_scope: dict[tuple, list] = {}
    for day in days:
        by_scope.setdefault((day.supplier_key, day.material_key), []).append(day)
    gaps = []
    for scope, entries in by_scope.items():
        entries.sort(key=lambda entry: entry.delivery_date)
        run: list = []
        for entry in entries:
            if run and any(run[-1].delivery_date < start <= entry.delivery_date for start, _end in windows.get(scope, ())):
                gaps.append(_gap(run))
                run = []
            run.append(entry)
        gaps.append(_gap(run))
    return sorted(gaps, key=lambda gap: (gap["supplier"], gap["material"], gap["date_from"]))


def _gap(run: list) -> dict:
    return {"supplier": run[0].supplier or NOT_ENTERED, "material": run[0].material or NOT_ENTERED,
            "date_from": run[0].delivery_date.isoformat(), "date_to": run[-1].delivery_date.isoformat(),
            "tickets": sum(entry.tickets for entry in run),
            "tons": str(sum((aggregates.as_decimal(entry.tons, 3) for entry in run), Decimal(0)))}


# ------------------------------------------------------------------ differences, for a supplier

KINDS = {
    "tons_differ": "Tons differ", "not_found": "No delivery record", "outside_scope": "Recorded for another job, material or date",
    "ambiguous": "Fits more than one delivery", "unverified": "Delivery not approved yet", "repeated": "Billed more than once",
    "not_on_invoice": "Approved delivery not on this invoice", "on_another_invoice": "Also on another invoice",
    "quantity": "Quantity", "rate": "Rate", "arithmetic": "Invoice arithmetic", "rounding": "Rounding",
}
HEADERS = ["Invoice", "Supplier", "Material", "Kind", "Ticket number", "Billed tons", "Approved tons", "Tons difference",
           "Dollar effect (billed minus approved)", "What to check (not an approval to pay)"]


def differences(line) -> list[dict]:
    """Every difference on one computed invoice line. When its money is known, the dollar
    effects add up to exactly the line's billed subtotal minus its approved subtotal:
    ticket-level effects at the billed rate, then rates, the invoice's own arithmetic,
    and the rounding that's left."""
    money = line.diagnosis.money.model_dump() if line.diagnosis and line.diagnosis.money else None
    known = money is not None and line.subtotal_difference is not None
    rate = line.billed_unit_price
    found: list[dict] = []

    def add(kind, *, number=None, billed=None, approved=None, tons=None, effect=None, check=""):
        found.append({"kind": kind, "ticket_number": number, "billed_tons": billed, "approved_tons": approved,
                      "tons_difference": tons, "dollar_effect": effect if known else None, "check": check})

    explained_tons = Decimal(0)
    for change in invoice_bridge.tons_changes(verified_ids=line._verified_ids, rows=line.ticket_check):
        row, kind, tons = change["row"], change["kind"], change["tons"]
        if "on_another_invoice" in row.flags:
            add("on_another_invoice", number=row.invoice_ticket_number, billed=row.billed_tons,
                check="It's also listed on " + "; ".join(note for note in row.notes if note.startswith("Also listed")) + " Check it isn't billed twice.")
        if kind is None:
            continue
        if tons is not None:
            explained_tons += tons
        add(kind, number=change["number"], billed=row.billed_tons, approved=row.verified_tons, tons=tons,
            effect=to_cents(tons * rate) if tons is not None else None,
            check=invoice_bridge._LABELS[kind][1].format(list=change["number"]))
    remainder = line.quantity_difference - explained_tons
    if remainder:
        itemized = line.comparison_mode == "itemized"
        add("quantity", billed=None if itemized else line.billed_tons, approved=None if itemized else line.verified_tons, tons=remainder,
            effect=to_cents(remainder * rate),
            check="The line's total tons differ from its tickets' tons." if itemized
            else "The invoice bills a different total than approved deliveries add up to. Ask for its ticket list to see which.")
    if known:
        for group in money["rate_groups"]:
            add("rate", approved=group["tons"], effect=group["effect"],
                check=f"{group['tickets']} ticket(s) locked at ${group['locked_rate']}/ton, billed at ${rate}/ton. Check the agreed price.")
        if money["arithmetic_effect"]:
            add("arithmetic", effect=money["arithmetic_effect"], check="The line's subtotal isn't its billed tons times its rate.")
        rounding = line.subtotal_difference - sum((entry["dollar_effect"] for entry in found if entry["dollar_effect"] is not None), Decimal(0))
        if rounding:
            add("rounding", effect=rounding, check="Each ticket's cost is rounded to the cent, and the invoice rounds once.")
    elif found or line.quantity_difference:
        reason = line.diagnosis.money_unresolved_reason if line.diagnosis else None
        for entry in found:
            entry["check"] += f" Dollar effect unknown: {reason}" if reason else ""
    return found


def discrepancy_lines(lines) -> Iterator[list]:
    """CSV rows, after the header."""
    from ..routers.invoices import _csv_text  # The one formula-neutralizing rule for invoice CSVs.

    def text(value) -> str:
        return _csv_text(value) if value is not None else ""

    def number(value) -> str:
        return "" if value is None else str(value)

    for line in lines:
        for entry in differences(line):
            yield [text(line.invoice_number), text(line.supplier_name), text(line.material_name), KINDS[entry["kind"]],
                   text(entry["ticket_number"]), number(entry["billed_tons"]), number(entry["approved_tons"]),
                   number(entry["tons_difference"]), number(entry["dollar_effect"]), text(entry["check"])]
