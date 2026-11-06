"""Excel and CSV exports of delivery records, written row by row so memory stays flat."""
import io
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Batch, Project, Ticket, UploadedFile
from .pricing import to_cents

LEGACY_APPROVER = "Not recorded (approved before individual accounts)"
STATUS_LABEL = {"verified": "Verified", "needs_review": "Needs review", "failed": "Extraction failed", "skipped": "Skipped", "flagged": "Flagged for review"}
HEADER_FILL = PatternFill("solid", fgColor="1F2937")


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


def ticket_cost(t, unit_price: Decimal | None) -> Decimal | None:
    """Exact decimal math, available only for currently verified tickets."""
    if t.status != "verified" or unit_price is None or t.tonnage is None or not t.tonnage_confirmed:
        return None
    return to_cents(t.tonnage * unit_price)


def _source_ref(t) -> str:
    return f"{t.file_name} (page {t.source_page}, ticket {t.position + 1})"


def _approval(t, approvers: dict[int, str]) -> tuple[str | None, object]:
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


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


@dataclass(frozen=True)
class Column:
    header: str
    width: int
    value: Callable[[Any, Decimal | None, Decimal | None, dict], object]
    number_format: str | None = None


# Keyed for export configurations. The default set and its order never change,
# so existing spreadsheets and macros keep working.
COLUMNS: dict[str, Column] = {
    "ticket_number": Column("Ticket Number", 16, lambda t, *_: _safe_excel_text(t.ticket_number)),
    "delivery_date": Column("Delivery Date", 14, lambda t, *_: t.delivery_date, "yyyy-mm-dd"),
    "material": Column("Material Type", 28, lambda t, *_: _safe_excel_text(t.material_type)),
    "tons": Column("Tonnage", 12, lambda t, *_: _money(t.tonnage), "#,##0.000"),
    "supplier": Column("Supplier", 24, lambda t, *_: _safe_excel_text(t.supplier)),
    "job_location": Column("Job Location", 24, lambda t, *_: _safe_excel_text(t.job_location)),
    "unit_price": Column("Unit Price", 12, lambda t, price, *_: _money(price), '"$"#,##0.00##'),
    "cost": Column("Calculated Cost", 16, lambda t, price, cost, _a: _money(cost), '"$"#,##0.00'),
    "status": Column("Verification Status", 20, lambda t, *_: STATUS_LABEL.get(t.status, t.status)),
    "source": Column("Source File Reference", 44, lambda t, *_: _safe_excel_text(_source_ref(t))),
    "approved_by": Column("Approved By", 26, lambda t, p, c, approvers: _approval(t, approvers)[0]),
    "approved_at": Column("Approved At (UTC)", 18, lambda t, p, c, approvers: _approval(t, approvers)[1], "yyyy-mm-dd hh:mm"),
    "project": Column("Project", 16, lambda t, *_: _safe_excel_text(t.project_code)),
    "batch": Column("Batch", 8, lambda t, *_: t.batch_id),
}
DEFAULT_COLUMNS = ("ticket_number", "delivery_date", "material", "tons", "supplier", "job_location", "unit_price",
                   "cost", "status", "source", "approved_by", "approved_at")
HEADERS = [COLUMNS[key].header for key in DEFAULT_COLUMNS]
_ROW_COLUMNS = (
    Ticket.id, Ticket.batch_id, Ticket.source_page, Ticket.position, Ticket.ticket_number, Ticket.delivery_date, Ticket.material_type,
    Ticket.tonnage, Ticket.supplier, Ticket.job_location, Ticket.status, Ticket.tonnage_confirmed, Ticket.unit_price_override,
    Ticket.price_snapshot_state, Ticket.applied_unit_price, Ticket.applied_price_source, Ticket.verified_by_user_id, Ticket.verified_at,
)


def row_query():
    """Exactly what an export needs, as plain rows. File and project names come from
    correlated lookups, so they cost one small read per row at most, and whole Ticket
    objects are never built for a large export."""
    file_name = select(UploadedFile.original_name).where(UploadedFile.id == Ticket.file_id).scalar_subquery()
    project_code = select(Project.code).join(Batch, Batch.project_id == Project.id).where(Batch.id == Ticket.batch_id).scalar_subquery()
    return select(*_ROW_COLUMNS, file_name.label("file_name"), project_code.label("project_code"))


def rows(db: Session, query, order=(Ticket.delivery_date, Ticket.id)) -> Iterator:
    """Read in chunks: a large export holds a little in memory at a time."""
    yield from db.execute(query.order_by(*order).execution_options(yield_per=2000))


def build_workbook(tickets: Iterable, approvers: dict[int, str] | None = None, columns: list[str] | None = None, *,
                   price_of: Callable[[Any], Decimal | None]) -> bytes:
    """``approvers`` maps user id -> display name for the exporting company. Rows go
    straight to a temporary file as they're written, so memory stays flat."""
    approvers = approvers or {}
    keys = columns or list(DEFAULT_COLUMNS)
    chosen = [COLUMNS[key] for key in keys]
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Delivery Records")
    for index, column in enumerate(chosen, start=1):
        ws.column_dimensions[get_column_letter(index)].width = column.width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(chosen))}1048576"

    def header(cells):
        styled = []
        for text in cells:
            cell = WriteOnlyCell(ws, value=text)
            cell.font, cell.fill = Font(bold=True, color="FFFFFF"), HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            styled.append(cell)
        return styled

    ws.append(header([column.header for column in chosen]))
    formatted = [column.number_format for column in chosen]
    summary = _Summary()
    for t in tickets:
        if t.status == "skipped":
            continue
        price = price_of(t)
        cost = ticket_cost(t, price)
        summary.add(t, cost)
        out = []
        for column, number_format in zip(chosen, formatted):
            value = column.value(t, price, cost, approvers)
            if number_format and value is not None:
                cell = WriteOnlyCell(ws, value=value)
                cell.number_format = number_format
                value = cell
            out.append(value)
        ws.append(out)
    summary.write(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_lines(tickets: Iterable, keys: list[str], approvers: dict[int, str], price_of: Callable[[Any], Decimal | None]) -> Iterator[list]:
    """Rows for a CSV: exact decimal text, never floats, and formula-safe text."""
    exact = {"tons": lambda t, _p, _c: t.tonnage, "unit_price": lambda t, p, _c: p, "cost": lambda t, _p, c: c}
    yield [COLUMNS[key].header for key in keys]
    for t in tickets:
        if t.status == "skipped":
            continue
        price = price_of(t)
        cost = ticket_cost(t, price)
        row = []
        for key in keys:
            value = exact[key](t, price, cost) if key in exact else COLUMNS[key].value(t, price, cost, approvers)
            row.append("" if value is None else value.isoformat() if hasattr(value, "isoformat") else str(value))
        yield row


class _Summary:
    """Totals for the Summary sheet, added up as rows go by."""

    def __init__(self) -> None:
        self.total = self.needs_review = self.unpriced = 0
        self.tons = self.pending = self.cost = Decimal(0)
        self.groups: dict[str, dict] = {"Totals by supplier": defaultdict(lambda: [0, Decimal(0), Decimal(0)]),
                                        "Totals by material type": defaultdict(lambda: [0, Decimal(0), Decimal(0)]),
                                        "Totals by job location": defaultdict(lambda: [0, Decimal(0), Decimal(0)])}

    def add(self, t, cost: Decimal | None) -> None:
        self.total += 1
        counted = t.status == "verified" and t.tonnage_confirmed
        if t.status == "verified" and t.tonnage_confirmed and t.tonnage is not None:
            self.tons += t.tonnage
        if t.status != "verified":
            self.needs_review += 1
            if t.tonnage is not None:
                self.pending += t.tonnage
        if cost is None:
            self.unpriced += 1
        else:
            self.cost += cost
        for title, name in (("Totals by supplier", t.supplier), ("Totals by material type", t.material_type), ("Totals by job location", t.job_location)):
            group = self.groups[title][name or "(unknown)"]
            group[0] += 1
            if counted:
                group[1] += t.tonnage or Decimal(0)
            group[2] += cost or Decimal(0)

    def write(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Summary")
        ws.column_dimensions["A"].width = 38
        for col in "BCD":
            ws.column_dimensions[col].width = 18

        def cell(value, *, bold=False, size=None, italic=False, number_format=None, header=False, color=None):
            item = WriteOnlyCell(ws, value=value)
            if header:
                item.font, item.fill = Font(bold=True, color="FFFFFF"), HEADER_FILL
            elif bold or size or italic or color:
                item.font = Font(bold=bold, size=size, italic=italic, color=color)
            if number_format:
                item.number_format = number_format
            return item

        ws.append([cell("Summary", bold=True, size=14)])
        ws.append([])
        for label, value, fmt in [
            ("Total tickets", self.total, "0"),
            ("Verified tons", float(self.tons), "#,##0.000"),
            ("Total calculated material cost", float(self.cost), '"$"#,##0.00'),
            ("Records requiring review", self.needs_review, "0"),
            ("Records without a calculated cost", self.unpriced, "0"),
            ("Unverified tons (excluded from actuals)", float(self.pending), "#,##0.000"),
        ]:
            ws.append([cell(label, bold=True), cell(value, number_format=fmt)])
        ws.append([])
        ws.append([cell("Note: calculated cost covers verified material only (tons x unit price). Delivery charges, taxes and other invoice items are not included.",
                        italic=True, color="6B7280")])
        for title, groups in self.groups.items():
            ws.append([])
            ws.append([cell(title, bold=True, size=12)])
            ws.append([cell(text, header=True) for text in ("Name", "Tickets", "Tons", "Calculated cost")])
            for name in sorted(groups):
                count, tons, cost = groups[name]
                ws.append([_safe_excel_text(name), count, cell(float(tons), number_format="#,##0.000"), cell(float(cost), number_format='"$"#,##0.00')])
