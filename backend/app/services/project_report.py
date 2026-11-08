"""What a project's delivery records and invoices add up to.

Only approved quantities count toward tons and cost. Everything still unapproved
is shown beside them and never folded in. A figure that can't be computed stays
unresolved and is counted as such: it never becomes zero.
"""
from __future__ import annotations

import io
from collections import defaultdict
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from ..models import Batch, Project, Ticket
from . import aggregates
from .export import _safe_excel_text as safe

NOT_ENTERED = "(not entered)"


class _Group:
    __slots__ = ("tickets", "verified_tickets", "verified_tons", "cost", "pending_tickets", "pending_tons", "unpriced")

    def __init__(self) -> None:
        self.tickets = self.verified_tickets = self.pending_tickets = self.unpriced = 0
        self.verified_tons = self.cost = self.pending_tons = Decimal(0)

    def add(self, row) -> None:
        self.tickets += row.tickets
        self.verified_tickets += int(row.counted)
        self.pending_tickets += row.tickets - int(row.counted)
        self.unpriced += int(row.unpriced)
        self.verified_tons += aggregates.as_decimal(row.tons, 3)
        self.cost += aggregates.as_decimal(row.cost, 2)
        self.pending_tons += aggregates.as_decimal(row.pending, 3)

    def out(self) -> dict:
        return {"tickets": self.tickets, "verified_tickets": self.verified_tickets,
                "verified_tons": str(self.verified_tons), "cost": str(self.cost), "unpriced": self.unpriced,
                "pending_tickets": self.pending_tickets, "pending_tons": str(self.pending_tons)}


def build(db: Session, organization_id: int, project: Project, lines: list) -> dict:
    """``lines`` are this project's computed invoice comparisons. The database adds up
    each supplier, material and month, so this reads a few hundred rows at most."""
    supplier = func.coalesce(func.nullif(Ticket.supplier, ""), NOT_ENTERED)
    material = func.coalesce(func.nullif(Ticket.material_type, ""), NOT_ENTERED)
    month = func.coalesce(func.substr(cast(Ticket.delivery_date, String), 1, 7), NOT_ENTERED)
    rows = db.execute(select(
        supplier.label("supplier"), material.label("material"), month.label("month"),
        func.count().label("tickets"), aggregates.counted_tickets().label("counted"), aggregates.counted_tons().label("tons"),
        aggregates.counted_cost().label("cost"), aggregates.pending_tons().label("pending"), aggregates.unpriced().label("unpriced"),
    ).where(
        Ticket.organization_id == organization_id, Ticket.status != "skipped",
        Ticket.batch_id.in_(select(Batch.id).where(Batch.organization_id == organization_id, Batch.project_id == project.id)),
    ).group_by(supplier, material, month))
    total = _Group()
    pairs: dict[tuple[str, str], _Group] = defaultdict(_Group)
    months: dict[str, _Group] = defaultdict(_Group)
    for row in rows:
        total.add(row)
        pairs[(row.supplier, row.material)].add(row)
        months[row.month].add(row)
    first, last = db.execute(select(func.min(Ticket.delivery_date), func.max(Ticket.delivery_date)).where(
        Ticket.organization_id == organization_id, Ticket.status != "skipped",
        Ticket.batch_id.in_(select(Batch.id).where(Batch.organization_id == organization_id, Batch.project_id == project.id)))).one()

    invoice_rows = []
    billed = verified = Decimal(0)
    unresolved = 0
    by_status: dict[str, int] = defaultdict(int)
    for line in lines:
        by_status[line.status] += 1
        billed += line.billed_subtotal
        if line.verified_subtotal is None:
            unresolved += 1
        else:
            verified += line.verified_subtotal
        invoice_rows.append({
            "id": line.id, "invoice_number": line.invoice_number, "supplier": line.supplier_name,
            "material": line.material_name, "date_from": line.date_from.isoformat(), "date_to": line.date_to.isoformat(),
            "status": line.status, "comparison_mode": line.comparison_mode,
            "billed_tons": str(line.billed_tons), "verified_tons": str(line.verified_tons),
            "tons_difference": str(line.quantity_difference), "billed_subtotal": str(line.billed_subtotal),
            "verified_subtotal": None if line.verified_subtotal is None else str(line.verified_subtotal),
            "subtotal_difference": None if line.subtotal_difference is None else str(line.subtotal_difference),
        })
    return {
        "project": {"id": project.id, "code": project.code, "name": project.name, "status": project.status},
        "first_delivery": first.isoformat() if first else None,
        "last_delivery": last.isoformat() if last else None,
        "totals": total.out(),
        "by_supplier_material": [{"supplier": s, "material": m, **pairs[(s, m)].out()} for s, m in sorted(pairs)],
        "by_month": [{"month": name, **months[name].out()} for name in sorted(months)],
        "invoices": {
            "lines": invoice_rows, "by_status": dict(by_status), "billed_total": str(billed),
            # Over lines with a known figure only. `lines_unresolved` says how many that leaves out.
            "verified_total": str(verified), "lines_unresolved": unresolved,
        },
    }


_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_TONS = "#,##0.000"
_MONEY = '"$"#,##0.00'


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list], formats: dict[int, str], widths: list[int]):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), _HEADER_FILL
    for row in rows:
        ws.append(row)
    for column, fmt in formats.items():
        for r in range(2, ws.max_row + 1):
            ws.cell(r, column).number_format = fmt
    for i, width in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = width
    ws.freeze_panes = "A2"
    return ws


def workbook(report: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    project, totals = report["project"], report["totals"]
    rows = [
        ("Project", safe(f"{project['code']} - {project['name']}")),
        ("First delivery", report["first_delivery"] or "None dated"),
        ("Last delivery", report["last_delivery"] or "None dated"),
        ("Tickets on record", totals["tickets"]),
        ("Approved tickets", totals["verified_tickets"]),
        ("Approved tons", Decimal(totals["verified_tons"])),
        ("Material cost of approved tickets", Decimal(totals["cost"])),
        ("Approved tickets with no locked price", totals["unpriced"]),
        ("Tickets not yet approved (excluded above)", totals["pending_tickets"]),
        ("Tons not yet approved (excluded above)", Decimal(totals["pending_tons"])),
        ("Invoice material billed", Decimal(report["invoices"]["billed_total"])),
        ("Approved cost on invoice lines with a known figure", Decimal(report["invoices"]["verified_total"])),
        ("Invoice lines with an unresolved figure", report["invoices"]["lines_unresolved"]),
    ]
    for label, value in rows:
        ws.append([label, value])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        if "tons" in label.lower():
            ws.cell(ws.max_row, 2).number_format = _TONS
        elif "cost" in label.lower() or "billed" in label.lower():
            ws.cell(ws.max_row, 2).number_format = _MONEY
    ws.append([])
    ws.append(["Material cost only (tons x locked rate). Delivery charges, taxes and other invoice items are not included. Not an approval for payment."])
    ws.cell(ws.max_row, 1).font = Font(italic=True, color="6B7280")
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 26

    amounts = lambda r: [r["tickets"], r["verified_tickets"], Decimal(r["verified_tons"]), Decimal(r["cost"]), r["unpriced"],  # noqa: E731
                         r["pending_tickets"], Decimal(r["pending_tons"])]
    tail = ["Tickets", "Approved tickets", "Approved tons", "Material cost", "No locked price", "Not yet approved", "Tons not yet approved"]
    _sheet(wb, "By supplier and material", ["Supplier", "Material"] + tail,
           [[safe(r["supplier"]), safe(r["material"])] + amounts(r) for r in report["by_supplier_material"]],
           {5: _TONS, 6: _MONEY, 9: _TONS}, [28, 28, 10, 12, 14, 14, 12, 12, 14])
    _sheet(wb, "By month", ["Delivery month"] + tail,
           [[r["month"]] + amounts(r) for r in report["by_month"]],
           {4: _TONS, 5: _MONEY, 8: _TONS}, [16, 10, 12, 14, 14, 12, 12, 14])
    number = lambda v: None if v is None else Decimal(v)  # noqa: E731
    _sheet(wb, "Invoice lines",
           ["Invoice", "Supplier", "Material", "From", "Through", "Result", "Billed tons", "Approved tons", "Tons difference",
            "Billed subtotal", "Approved subtotal", "Subtotal difference"],
           [[safe(r["invoice_number"]), safe(r["supplier"]), safe(r["material"]), r["date_from"], r["date_to"], r["status"],
             Decimal(r["billed_tons"]), Decimal(r["verified_tons"]), Decimal(r["tons_difference"]),
             Decimal(r["billed_subtotal"]), number(r["verified_subtotal"]) if r["verified_subtotal"] else "UNRESOLVED",
             number(r["subtotal_difference"]) if r["subtotal_difference"] else "UNRESOLVED"]
            for r in report["invoices"]["lines"]],
           {7: _TONS, 8: _TONS, 9: _TONS, 10: _MONEY, 11: _MONEY, 12: _MONEY},
           [16, 26, 24, 12, 12, 18, 12, 12, 12, 14, 14, 14])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
