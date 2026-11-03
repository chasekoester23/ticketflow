"""Excel export with openpyxl. Phase 1: unit price / cost columns exist but stay blank until Phase 2 pricing."""
import io
from collections import defaultdict
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..models import Ticket

HEADERS = [
    "Ticket Number", "Delivery Date", "Material Type", "Tonnage", "Supplier", "Job Location",
    "Unit Price", "Calculated Cost", "Verification Status", "Source File Reference",
    # Appended (not inserted) so existing column positions never move.
    "Approved By", "Approved At (UTC)",
]
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


def build_workbook(tickets: list[Ticket], unit_prices: dict[int, Decimal] | None = None,
                   approvers: dict[int, str] | None = None) -> bytes:
    """``approvers`` maps user id -> display name for the exporting company."""
    unit_prices = unit_prices or {}
    approvers = approvers or {}
    tickets = [t for t in tickets if t.status != "skipped"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Delivery Records"
    ws.append(HEADERS)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    rows = []
    for t in tickets:
        price = unit_prices.get(t.id)
        cost = ticket_cost(t, price)
        rows.append((t, price, cost))
        approved_by, approved_at = _approval(t, approvers)
        ws.append([
            _safe_excel_text(t.ticket_number), t.delivery_date, _safe_excel_text(t.material_type),
            float(t.tonnage) if t.tonnage is not None else None,
            _safe_excel_text(t.supplier), _safe_excel_text(t.job_location),
            float(price) if price is not None else None,
            float(cost) if cost is not None else None,
            STATUS_LABEL.get(t.status, t.status), _safe_excel_text(_source_ref(t)),
            approved_by, approved_at,
        ])
    for r in range(2, ws.max_row + 1):
        ws.cell(r, 2).number_format = "yyyy-mm-dd"
        ws.cell(r, 4).number_format = "#,##0.00"
        ws.cell(r, 7).number_format = '"$"#,##0.00'
        ws.cell(r, 8).number_format = '"$"#,##0.00'
        ws.cell(r, 12).number_format = "yyyy-mm-dd hh:mm"
    for i, w in enumerate([16, 14, 28, 12, 24, 24, 12, 16, 20, 44, 26, 18], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{max(ws.max_row, 2)}"

    _summary_sheet(wb, rows)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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
