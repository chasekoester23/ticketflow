"""Excel export with openpyxl. Phase 1: unit price / cost columns exist but stay blank until Phase 2 pricing."""
import io
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterator

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Batch, Project, Ticket

LEGACY_APPROVER = "Not recorded (approved before individual accounts)"
STATUS_LABEL = {"verified": "Verified", "needs_review": "Needs review", "failed": "Extraction failed", "skipped": "Skipped", "flagged": "Flagged for review"}
HEADER_FILL = PatternFill("solid", fgColor="1F2937")
CENTS = Decimal("0.01")


def _safe_excel_text(value: str | None) -> str | None:
    """Prevent supplier-controlled text becoming a spreadsheet formula.

    openpyxl treats leading '=' as a formula. Prefix any spreadsheet formula
    trigger (including leading whitespace) with a literal apostrophe. Numeric
    tonnage/prices remain numbers and are never passed through this function.
    """
    if value is None:
        return None
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def ticket_cost(t: Ticket, unit_price: Decimal | None) -> Decimal | None:
    """Exact decimal math, available only for currently verified tickets."""
    if t.status != "verified" or unit_price is None or t.tonnage is None or not t.tonnage_confirmed:
        return None
    return (t.tonnage * unit_price).quantize(CENTS)


def _source_ref(t: Ticket) -> str:
    return f"{t.file.original_name} (page {t.source_page}, ticket {t.position + 1})"


def _approval(t: Ticket, approvers: dict[int, str]) -> tuple[str | None, object]:
    """Who approved the CURRENT verification, and when (naive UTC for Excel).

    Blank for unapproved tickets. A verified ticket without a recorded
    approver predates individual accounts and says so; it is never guessed.
    """
    if t.status != "verified":
        return None, None
    if t.verified_by_user_id is None:
        return LEGACY_APPROVER, None
    name = approvers.get(t.verified_by_user_id, "Unknown account")
    at = t.verified_at.replace(tzinfo=None) if t.verified_at is not None else None
    return _safe_excel_text(name), at


@dataclass
class ExportContext:
    unit_prices: dict[int, Decimal]
    approvers: dict[int, str]
    projects: dict[int, str]


@dataclass(frozen=True)
class Column:
    header: str
    width: int
    value: Callable[[Ticket, Decimal | None, Decimal | None, ExportContext], object]
    number_format: str | None = None


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


# Keyed for export configurations. The default set and its order never change,
# so existing spreadsheets and macros keep working.
COLUMNS: dict[str, Column] = {
    "ticket_number": Column("Ticket Number", 16, lambda t, *_: _safe_excel_text(t.ticket_number)),
    "delivery_date": Column("Delivery Date", 14, lambda t, *_: t.delivery_date, "yyyy-mm-dd"),
    "material": Column("Material Type", 28, lambda t, *_: _safe_excel_text(t.material_type)),
    "tons": Column("Tonnage", 12, lambda t, *_: _money(t.tonnage), "#,##0.00"),
    "supplier": Column("Supplier", 24, lambda t, *_: _safe_excel_text(t.supplier)),
    "job_location": Column("Job Location", 24, lambda t, *_: _safe_excel_text(t.job_location)),
    "unit_price": Column("Unit Price", 12, lambda t, price, *_: _money(price), '"$"#,##0.00'),
    "cost": Column("Calculated Cost", 16, lambda t, price, cost, _c: _money(cost), '"$"#,##0.00'),
    "status": Column("Verification Status", 20, lambda t, *_: STATUS_LABEL.get(t.status, t.status)),
    "source": Column("Source File Reference", 44, lambda t, *_: _safe_excel_text(_source_ref(t))),
    "approved_by": Column("Approved By", 26, lambda t, p, c, ctx: _approval(t, ctx.approvers)[0]),
    "approved_at": Column("Approved At (UTC)", 18, lambda t, p, c, ctx: _approval(t, ctx.approvers)[1], "yyyy-mm-dd hh:mm"),
    "project": Column("Project", 16, lambda t, p, c, ctx: _safe_excel_text(ctx.projects.get(t.id))),
    "batch": Column("Batch", 8, lambda t, *_: t.batch_id),
}
DEFAULT_COLUMNS = ("ticket_number", "delivery_date", "material", "tons", "supplier", "job_location", "unit_price",
                   "cost", "status", "source", "approved_by", "approved_at")
HEADERS = [COLUMNS[key].header for key in DEFAULT_COLUMNS]


def project_codes(db: Session, organization_id: int) -> "_ProjectCodes":
    return _ProjectCodes(db, organization_id)


class _ProjectCodes:
    """Ticket id to project code, read only if a Project column was asked for."""

    def __init__(self, db: Session, organization_id: int):
        self.db, self.organization_id = db, organization_id
        self._by_ticket: dict[int, str] | None = None

    def get(self, ticket_id: int) -> str | None:
        if self._by_ticket is None:
            self._by_ticket = dict(self.db.execute(
                select(Ticket.id, Project.code).join(Batch, Batch.id == Ticket.batch_id)
                .join(Project, Project.id == Batch.project_id).where(Batch.organization_id == self.organization_id)
            ).all())
        return self._by_ticket.get(ticket_id)


def build_workbook(tickets: list[Ticket], unit_prices: dict[int, Decimal] | None = None,
                   approvers: dict[int, str] | None = None, columns: list[str] | None = None,
                   projects=None) -> bytes:
    """``approvers`` maps user id -> display name for the exporting company."""
    context = ExportContext(unit_prices or {}, approvers or {}, projects or {})
    keys = columns or list(DEFAULT_COLUMNS)
    chosen = [COLUMNS[key] for key in keys]
    tickets = [t for t in tickets if t.status != "skipped"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Delivery Records"
    ws.append([column.header for column in chosen])
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    rows = []
    for t in tickets:
        price = context.unit_prices.get(t.id)
        cost = ticket_cost(t, price)
        rows.append((t, price, cost))
        ws.append([column.value(t, price, cost, context) for column in chosen])
    for index, column in enumerate(chosen, start=1):
        if column.number_format:
            for r in range(2, ws.max_row + 1):
                ws.cell(r, index).number_format = column.number_format
        ws.column_dimensions[get_column_letter(index)].width = column.width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(chosen))}{max(ws.max_row, 2)}"

    _summary_sheet(wb, rows)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_lines(tickets: list[Ticket], keys: list[str], context: ExportContext) -> Iterator[list]:
    """Rows for a CSV: exact decimal text, never floats, and formula-safe text."""
    exact = {"tons": lambda t, _p, _c: t.tonnage, "unit_price": lambda t, p, _c: p, "cost": lambda t, _p, c: c}
    yield [COLUMNS[key].header for key in keys]
    for t in tickets:
        if t.status == "skipped":
            continue
        price = context.unit_prices.get(t.id)
        cost = ticket_cost(t, price)
        row = []
        for key in keys:
            value = exact[key](t, price, cost) if key in exact else COLUMNS[key].value(t, price, cost, context)
            row.append("" if value is None else value.isoformat() if hasattr(value, "isoformat") else str(value))
        yield row


def _summary_sheet(wb: Workbook, rows) -> None:
    ws = wb.create_sheet("Summary")
    bold = Font(bold=True)
    total_tons = sum((t.tonnage for t, _, _ in rows if t.status == "verified"
                      and t.tonnage_confirmed and t.tonnage is not None), Decimal(0))
    pending_tons = sum((t.tonnage for t, _, _ in rows if t.status != "verified"
                        and t.tonnage is not None), Decimal(0))
    total_cost = sum((c for _, _, c in rows if c is not None), Decimal(0))
    unpriced = sum(1 for _, _, c in rows if c is None)
    needs_review = sum(1 for t, _, _ in rows if t.status != "verified")

    ws.append(["Summary"]); ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for label, value, fmt in [
        ("Total tickets", len(rows), "0"),
        ("Verified tons", float(total_tons), "#,##0.00"),
        ("Total calculated material cost", float(total_cost), '"$"#,##0.00'),
        ("Records requiring review", needs_review, "0"),
        ("Records without a calculated cost", unpriced, "0"),
    ]:
        ws.append([label, value]); ws.cell(ws.max_row, 2).number_format = fmt; ws.cell(ws.max_row, 1).font = bold
    ws.append(["Unverified tons (excluded from actuals)", float(pending_tons)])
    ws.cell(ws.max_row, 2).number_format = "#,##0.00"
    ws.cell(ws.max_row, 1).font = bold
    ws.append([])
    ws.append(["Note: calculated cost covers verified material only (tons x unit price). Delivery charges, taxes and other invoice items are not included."])
    ws.cell(ws.max_row, 1).font = Font(italic=True, color="6B7280")

    for title, key in [("Totals by supplier", lambda t: t.supplier or "(unknown)"),
                       ("Totals by material type", lambda t: t.material_type or "(unknown)"),
                       ("Totals by job location", lambda t: t.job_location or "(unknown)")]:
        groups: dict[str, list] = defaultdict(lambda: [0, Decimal(0), Decimal(0)])
        for t, _, c in rows:
            g = groups[key(t)]
            g[0] += 1
            if t.status == "verified" and t.tonnage_confirmed:
                g[1] += t.tonnage or Decimal(0)
            g[2] += c or Decimal(0)
        ws.append([])
        ws.append([title]); ws.cell(ws.max_row, 1).font = Font(bold=True, size=12)
        ws.append(["Name", "Tickets", "Tons", "Calculated cost"])
        for c in ws[ws.max_row]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), HEADER_FILL
        for name in sorted(groups):
            n, tons, cost = groups[name]
            ws.append([_safe_excel_text(name), n, float(tons), float(cost)])
            ws.cell(ws.max_row, 3).number_format = "#,##0.00"
            ws.cell(ws.max_row, 4).number_format = '"$"#,##0.00'
    ws.column_dimensions["A"].width = 38
    for col in "BCD":
        ws.column_dimensions[col].width = 18
