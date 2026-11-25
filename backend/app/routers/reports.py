"""Cross-batch views: filtered delivery records, dashboard totals, filtered Excel export."""
import csv
from collections import Counter
from datetime import date
from decimal import Decimal
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, false, func, or_, select
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..config import settings
from ..db import get_db
from ..models import AuditEvent, Batch, InvoiceComparison, Project, ReviewTiming, Ticket, UploadedFile, User, as_utc
from ..permissions import require
from ..rate_limit import limited
from ..schemas import TicketOut
from ..services import aggregates
from ..services import export as export_svc
from ..services import duplicates as dup_svc
from ..services import consistency, pilot_report, project_report, reconciliation
from ..services.invoice_context import LineContext
from ..services import pricing as pricing_svc
from ..services.accounts import names_by_id
from .api import EDITABLE, _out
from .invoices import _line_out

router = APIRouter(prefix="/api")

VALID_STATUS = {"needs_review", "verified", "skipped", "failed", "flagged"}


def filtered_query(
    query, *, organization_id: int, project_id: int | None, supplier: str | None, material: str | None,
    status: str | None, date_from: date | None, date_to: date | None, q: str | None = None,
    include_skipped: bool = False,
):
    """Add the record filters to ``query``. One definition, so a list, its totals
    and its exports can never disagree about which tickets are included."""
    query = query.select_from(Ticket).where(Ticket.organization_id == organization_id)
    if project_id is not None:
        query = query.where(Ticket.batch_id.in_(select(Batch.id).where(
            Batch.organization_id == organization_id, Batch.project_id == project_id)))
    if supplier:
        query = query.where(Ticket.supplier.icontains(supplier.strip(), autoescape=True))
    if material:
        query = query.where(Ticket.material_type.icontains(material.strip(), autoescape=True))
    if q and q.strip():
        text = q.strip()
        query = query.where(or_(
            Ticket.ticket_number.icontains(text, autoescape=True), Ticket.supplier.icontains(text, autoescape=True),
            Ticket.material_type.icontains(text, autoescape=True), Ticket.job_location.icontains(text, autoescape=True),
        ))
    if status in VALID_STATUS:
        query = query.where(Ticket.status == status)
    elif status == "-none-":
        query = query.where(false())
    elif not include_skipped:
        query = query.where(Ticket.status != "skipped")
    if date_from:
        query = query.where(Ticket.delivery_date >= date_from)
    if date_to:
        query = query.where(Ticket.delivery_date <= date_to)
    return query


def filtered_tickets(
    db: Session, *, organization_id: int, project_id: int | None, supplier: str | None, material: str | None,
    status: str | None, date_from: date | None, date_to: date | None, include_skipped: bool = False, q: str | None = None,
) -> list[Ticket]:
    query = filtered_query(
        select(Ticket), organization_id=organization_id, project_id=project_id,
        supplier=supplier, material=material, status=status, date_from=date_from, date_to=date_to, q=q,
        include_skipped=include_skipped,
    ).order_by(Ticket.delivery_date, Ticket.id)
    return list(db.scalars(query).all())


MAX_UNPAGED_RECORDS = 5000
MAX_EXCEL_ROWS = 200_000


@router.get("/records", response_model=list[TicketOut])
def records(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None,
    db: Session = Depends(get_db),
    organization_id: int = Depends(current_organization_id),
):
    """Every matching record at once, for small result sets. /records/search pages."""
    filters = dict(organization_id=organization_id, project_id=project_id, supplier=supplier, material=material,
                   status=status, date_from=date_from, date_to=date_to)
    matching = db.scalar(filtered_query(select(func.count()), **filters))
    if matching > MAX_UNPAGED_RECORDS:
        raise HTTPException(413, f"{matching:,} records match. Use /api/records/search, which pages through them.")
    table = pricing_svc.rate_table(db, organization_id)
    ts = filtered_tickets(db, **filters)
    index = dup_svc.load_index(db, organization_id, ts)
    # Shared by every row: one catalog read, and the recent ticket numbers for the job-number check.
    similar = consistency.similar_names(db, organization_id)
    names = similar.normalized()
    numbers = consistency.ticket_numbers(db, organization_id)
    aliases = pricing_svc.alias_suggestions(db, organization_id, names)
    return [_out(db, t, organization_id, table, duplicate_index=index, catalog=names, aliases=aliases,
                 similar=similar, numbers=numbers) for t in ts]


@router.get("/records/export.xlsx", dependencies=[Depends(limited("exports"))])
def export_records(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None, q: str | None = None,
    columns: str | None = None, view_id: int | None = None,
    db: Session = Depends(get_db),
    organization_id: int = Depends(current_organization_id),
):
    from .records import view_choice
    keys, labels = view_choice(db, organization_id, view_id, columns)
    filters = dict(organization_id=organization_id, project_id=project_id, supplier=supplier, material=material,
                   status=status, date_from=date_from, date_to=date_to, q=q)
    matching = db.scalar(filtered_query(select(func.count()), **filters))
    if matching > MAX_EXCEL_ROWS:
        raise HTTPException(413, f"{matching:,} records is too many for a spreadsheet to open comfortably. "
                                 "Narrow the filters, or use the CSV export, which handles any size.")
    table = pricing_svc.rate_table(db, organization_id)
    return Response(
        export_svc.build_workbook(export_svc.rows(db, filtered_query(export_svc.row_query(), **filters)),
                                  names_by_id(db, organization_id), keys, price_of=lambda t: pricing_svc.effective_price_for(t, table)[0],
                                  labels=labels),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="delivery-records.xlsx"'})


@router.get("/dashboard")
def dashboard(project_id: int | None = None, db: Session = Depends(get_db),
              organization_id: int = Depends(current_organization_id)):
    # Added up by the database. Only approved quantities belong in actuals: unverified
    # extractions are estimates and must not silently inflate project totals.
    filters = dict(organization_id=organization_id, project_id=project_id, supplier=None, material=None, status=None,
                   date_from=None, date_to=None)
    by_status = dict(db.execute(filtered_query(select(Ticket.status, func.count()), **filters).group_by(Ticket.status)).all())
    total = db.execute(filtered_query(select(
        aggregates.counted_tons().label("tons"), aggregates.counted_cost().label("cost"), aggregates.pending_tons().label("pending"),
        aggregates.unpriced().label("unpriced"), aggregates.counted_tickets().label("counted")), **filters)).one()

    def group(column):
        label = func.coalesce(func.nullif(column, ""), "(unknown)")
        rows = db.execute(filtered_query(select(
            label.label("name"), func.count().label("tickets"), aggregates.counted_tons().label("tons"),
            aggregates.counted_cost().label("cost")), **filters).group_by(label))
        # Sorted here: each database's own text ordering puts "(unknown)" somewhere else.
        return [{"name": r.name, "tickets": r.tickets, "tons": str(aggregates.as_decimal(r.tons, 3)),
                 "cost": str(aggregates.as_decimal(r.cost, 2))} for r in sorted(rows, key=lambda r: r.name)]

    bq = select(Batch).where(Batch.organization_id == organization_id).order_by(Batch.id.desc()).limit(8)
    if project_id is not None:
        bq = bq.where(Batch.project_id == project_id)
    recent = list(db.scalars(bq))
    file_counts = dict(db.execute(select(UploadedFile.batch_id, func.count()).where(
        UploadedFile.batch_id.in_([b.id for b in recent])).group_by(UploadedFile.batch_id)).all()) if recent else {}
    projects = {p.id: p.name for p in db.scalars(select(Project).where(
        Project.organization_id == organization_id
    ))}
    tickets = sum(by_status.values())
    return {
        "tickets": tickets,
        "failed": by_status.get("failed", 0),
        "needs_review": sum(by_status.get(name, 0) for name in ("needs_review", "failed", "flagged")),
        "flagged": by_status.get("flagged", 0),
        "verified": by_status.get("verified", 0),
        # Strings: FastAPI would send Decimals in a plain dict as lossy floats.
        "total_tons": str(aggregates.as_decimal(total.tons, 3)),
        "pending_tons": str(aggregates.as_decimal(total.pending, 3)),
        "total_cost": str(aggregates.as_decimal(total.cost, 2)),
        "unpriced_tickets": int(total.unpriced),
        "pending_cost_tickets": tickets - int(total.counted),
        "by_supplier": group(Ticket.supplier),
        "by_material": group(Ticket.material_type),
        "recent_batches": [
            {"id": b.id, "status": b.status, "created_at": as_utc(b.created_at), "files": file_counts.get(b.id, 0),
             "project": projects.get(b.project_id)} for b in recent
        ],
        "projects": _active_projects(db, organization_id, project_id),
        "activity": _recent_activity(db, organization_id, project_id),
        "hauling": _hauling(db, organization_id, project_id, aggregates.as_decimal(total.cost, 2)),
    }


OPEN_STATUSES = ("needs_review", "flagged", "failed")
ACTIVITY = {"verified": "approved", "flagged": "flagged", "skipped": "skipped"}


QUEUE_LIMIT = 200
QUEUE_KINDS = ("tickets", "failed_files", "truck_shifts", "invoice_lines")


@router.get("/review-queue")
def review_queue(project_id: int | None = None, kind: str | None = None, db: Session = Depends(get_db),
                 organization_id: int = Depends(current_organization_id)):
    """Everything waiting for a person, in one list: ticket batches with tickets to check,
    files that couldn't be read, truck shifts to approve and invoice lines drafted from a
    PDF. Each item links to the screen where the work is done; nothing is approved here.

    Oldest first, because a queue is worked from the front. A fixed number of grouped
    queries; at most QUEUE_LIMIT items of each kind, with the true totals in ``summary``.
    """
    from ..models import Hauler, InvoiceDocument, InvoiceDraftLine, Truck, TruckShift
    if kind is not None and kind not in QUEUE_KINDS:
        raise HTTPException(422, f"kind must be one of: {', '.join(QUEUE_KINDS)}")
    names = {p.id: (p.code, p.name) for p in db.scalars(select(Project).where(Project.organization_id == organization_id))}
    if project_id is not None and project_id not in names:
        raise HTTPException(404, "Project not found")

    def project(pid):
        return {"id": pid, "code": names[pid][0], "name": names[pid][1]} if pid in names else None

    items: list[dict] = []
    summary = {k: 0 for k in QUEUE_KINDS}

    if kind in (None, "tickets"):
        open_by_batch = (select(Batch.id, Batch.project_id, Batch.created_at, Batch.intake_mode,
                                func.sum(case((Ticket.status == "needs_review", 1), else_=0)).label("to_check"),
                                func.sum(case((Ticket.status == "flagged", 1), else_=0)).label("flagged"),
                                func.sum(case((Ticket.status == "failed", 1), else_=0)).label("failed"),
                                func.min(Ticket.created_at).label("oldest"))
                         .join(Ticket, Ticket.batch_id == Batch.id)
                         .where(Batch.organization_id == organization_id, Ticket.organization_id == organization_id,
                                Ticket.status.in_(OPEN_STATUSES))
                         .group_by(Batch.id, Batch.project_id, Batch.created_at, Batch.intake_mode))
        if project_id is not None:
            open_by_batch = open_by_batch.where(Batch.project_id == project_id)
        rows = sorted(db.execute(open_by_batch).all(), key=lambda r: (as_utc(r.created_at), r.id))
        summary["tickets"] = sum(int(r.to_check) + int(r.flagged) + int(r.failed) for r in rows)
        for r in rows[:QUEUE_LIMIT]:
            parts = [f"{int(r.to_check)} to check" if r.to_check else None, f"{int(r.flagged)} flagged" if r.flagged else None,
                     f"{int(r.failed)} couldn't be read" if r.failed else None]
            items.append({"kind": "tickets", "id": r.id, "title": f"Delivery tickets · batch #{r.id}",
                          "detail": " · ".join(p for p in parts if p), "count": int(r.to_check) + int(r.flagged) + int(r.failed),
                          "attention": bool(r.flagged or r.failed), "project": project(r.project_id),
                          "created_at": as_utc(r.created_at), "href": f"/upload?batch={r.id}"})

    if kind in (None, "failed_files"):
        failed = (select(UploadedFile.id, UploadedFile.original_name, UploadedFile.error, Batch.id.label("batch_id"),
                         Batch.project_id, Batch.created_at)
                  .join(Batch, Batch.id == UploadedFile.batch_id)
                  .where(Batch.organization_id == organization_id, UploadedFile.status == "failed")
                  .order_by(Batch.created_at, UploadedFile.id))
        if project_id is not None:
            failed = failed.where(Batch.project_id == project_id)
        rows = db.execute(failed).all()
        summary["failed_files"] = len(rows)
        for r in rows[:QUEUE_LIMIT]:
            items.append({"kind": "failed_files", "id": r.id, "title": f"{r.original_name} couldn't be processed",
                          "detail": (r.error or "The file couldn't be read.")[:240], "count": 1, "attention": True,
                          "project": project(r.project_id), "created_at": as_utc(r.created_at),
                          "href": f"/upload?batch={r.batch_id}"})

    if kind in (None, "truck_shifts"):
        shifts = (select(TruckShift.id, TruckShift.project_id, TruckShift.work_date, TruckShift.created_at, TruckShift.driver_name,
                         TruckShift.start_at, TruckShift.end_at, Truck.identifier, Hauler.name.label("hauler"))
                  .join(Truck, Truck.id == TruckShift.truck_id).join(Hauler, Hauler.id == TruckShift.hauler_id)
                  .where(TruckShift.organization_id == organization_id, TruckShift.status == "needs_review")
                  .order_by(TruckShift.work_date, TruckShift.id))
        if project_id is not None:
            shifts = shifts.where(TruckShift.project_id == project_id)
        rows = db.execute(shifts).all()
        summary["truck_shifts"] = len(rows)
        for r in rows[:QUEUE_LIMIT]:
            on_site = (r.end_at - r.start_at).total_seconds() / 3600
            items.append({"kind": "truck_shifts", "id": r.id, "title": f"Truck {r.identifier} · {r.hauler} · {r.work_date.isoformat()}",
                          "detail": f"{on_site:.2f} h on site" + (f" · {r.driver_name}" if r.driver_name else ""), "count": 1,
                          "attention": False, "project": project(r.project_id), "created_at": as_utc(r.created_at),
                          "href": f"/trucking?shift_id={r.id}"})

    if kind in (None, "invoice_lines"):
        drafts = (select(InvoiceDocument.id, InvoiceDocument.original_name, InvoiceDocument.project_id, InvoiceDocument.created_at,
                         func.count(InvoiceDraftLine.id).label("lines"))
                  .join(InvoiceDraftLine, InvoiceDraftLine.document_id == InvoiceDocument.id)
                  .where(InvoiceDocument.organization_id == organization_id, InvoiceDraftLine.state == "pending_review")
                  .group_by(InvoiceDocument.id, InvoiceDocument.original_name, InvoiceDocument.project_id, InvoiceDocument.created_at)
                  .order_by(InvoiceDocument.created_at, InvoiceDocument.id))
        if project_id is not None:
            drafts = drafts.where(InvoiceDocument.project_id == project_id)
        rows = db.execute(drafts).all()
        summary["invoice_lines"] = sum(int(r.lines) for r in rows)
        for r in rows[:QUEUE_LIMIT]:
            items.append({"kind": "invoice_lines", "id": r.id, "title": f"Supplier invoice · {r.original_name}",
                          "detail": f"{int(r.lines)} drafted line{'s' if r.lines != 1 else ''} to confirm", "count": int(r.lines),
                          "attention": False, "project": project(r.project_id), "created_at": as_utc(r.created_at),
                          "href": f"/invoices/documents?id={r.id}"})

    items.sort(key=lambda i: (i["created_at"].timestamp() if i["created_at"] else 0, i["kind"], i["id"]))
    return {"summary": {**summary, "total": sum(summary.values())}, "items": items}


def _summary(db: Session, organization_id: int, project_id: int | None, date_from: date | None, date_to: date | None, period: str) -> dict:
    from ..services import cost_reports
    from .invoices import invoice_register
    if project_id is not None and not db.scalar(select(Project.id).where(Project.id == project_id, Project.organization_id == organization_id)):
        raise HTTPException(404, "Project not found")
    if period not in cost_reports.PERIODS:
        raise HTTPException(422, "period must be day, week or month")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "The start date is after the end date.")
    report = cost_reports.build(db, organization_id, project_id, date_from, date_to, period)
    # Invoices whose billing period overlaps the dates asked for.
    rows = [r for r in invoice_register(project_id, db, organization_id)["invoices"]
            if (not date_from or r["period_to"] >= date_from.isoformat()) and (not date_to or r["period_from"] <= date_to.isoformat())]
    report["invoices"] = {
        "invoices": len(rows), "billed": str(sum((Decimal(r["billed"]) for r in rows), Decimal(0))),
        "matched": sum(1 for r in rows if r["status"] == "matched"), "with_differences": sum(1 for r in rows if r["status"] == "discrepancy"),
        "incomplete": sum(1 for r in rows if r["status"] == "incomplete"),
        "difference": str(sum((Decimal(r["difference"]) for r in rows if r["difference"] is not None and r["status"] == "discrepancy"), Decimal(0))),
        "totals_only": sum(1 for r in rows if not r["ticket_level"]),
        "rows": rows,
    }
    return report


@router.get("/reports/summary", dependencies=[Depends(limited("reports"))])
def report_summary(project_id: int | None = None, date_from: date | None = None, date_to: date | None = None, period: str = "week",
                   db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Material, trucking, project cost and invoice reports for one project or all, over a date range.
    Approved figures only; a missing price is counted, never shown as $0. Not a payment approval."""
    return _summary(db, organization_id, project_id, date_from, date_to, period)


@router.get("/reports/summary.xlsx", dependencies=[Depends(limited("exports"))])
def report_summary_xlsx(project_id: int | None = None, date_from: date | None = None, date_to: date | None = None, period: str = "week",
                        db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font
    report = _summary(db, organization_id, project_id, date_from, date_to, period)
    safe = export_svc._safe_excel_text
    num = lambda v: float(v) if v is not None else None  # noqa: E731  (cells; the JSON keeps exact strings)
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Summary"
    scope = "All projects"
    if project_id is not None:
        p = db.get(Project, project_id)
        scope = f"{p.code} · {p.name}"
    for row in [["Quarvera cost report: approved figures only. Not an approval for payment."], ["Project", safe(scope)],
                ["Dates", f"{report['date_from'] or 'start'} to {report['date_to'] or 'today'}"], [],
                ["Verified tons", num(report["material"]["tons"])], ["Material cost", num(report["material"]["cost"])],
                ["Approved tickets without a rate (cost incomplete)", report["material"]["unpriced"]],
                ["Tickets waiting for review (not included)", report["material"]["waiting"]],
                ["Approved trucking hours", num(report["trucking"]["hours"])], ["Trucking cost", num(report["trucking"]["cost"])],
                ["Total verified cost", num(report["costs"]["total"])],
                ["Invoices in these dates", report["invoices"]["invoices"]], ["Invoices with differences", report["invoices"]["with_differences"]]]:
        sheet.append(row)
    sheet["A1"].font = Font(bold=True)

    def table(title, headers, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append(r)

    m = report["material"]
    table("Material by material", ["Material", "Tickets", "Verified tons", "Material cost", "Approved without a rate"],
          [[safe(r["name"]), r["tickets"], num(r["tons"]), num(r["cost"]), r["unpriced"]] for r in m["by_material"]])
    table("Material by supplier", ["Supplier", "Tickets", "Verified tons", "Material cost", "Approved without a rate"],
          [[safe(r["name"]), r["tickets"], num(r["tons"]), num(r["cost"]), r["unpriced"]] for r in m["by_supplier"]])
    table("Trucking by hauler", ["Hauler", "Approved shifts", "Approved hours", "Loads", "Trucking cost"],
          [[safe(r["name"]), r["shifts"], num(r["hours"]), r["loads"], num(r["cost"])] for r in report["trucking"]["by_hauler"]])
    table(f"By {period}", [period.capitalize(), "Tickets", "Verified tons", "Material cost", "Without a rate", "Truck hours", "Loads",
                            "Trucking cost", "Total"],
          [[r["period"], r["tickets"], num(r["tons"]), num(r["material"]), r["unpriced"], num(r["hours"]), r["loads"], num(r["trucking"]),
            num(r["total"])] for r in report["by_period"]])
    table("Invoices", ["Type", "Invoice", "Company", "Project", "From", "To", "Billed", "Approved records", "Difference", "Result", "Compared"],
          [[r["kind"], safe(r["invoice_number"]), safe(r["company"]), safe(r["project"]["code"]) if r["project"] else None, r["period_from"],
            r["period_to"], num(r["billed"]), num(r["compared"]), num(r["difference"]), r["status"],
            "Ticket by ticket" if r["ticket_level"] else "Totals only"] for r in report["invoices"]["rows"]])
    out = BytesIO()
    wb.save(out)
    return Response(out.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": 'attachment; filename="quarvera-cost-report.xlsx"', "Cache-Control": "no-store"})


def _hauling(db: Session, organization_id: int, project_id: int | None, material: Decimal) -> dict:
    """Approved hourly trucking, kept apart from material cost: different work, often a
    different company. Per project through each shift's allocations, so a shift split
    across jobs counts once, in its parts."""
    from ..models import ShiftAllocation, TruckShift
    allocations = select(aggregates.exact_sum(ShiftAllocation.cost, 2), aggregates.exact_sum(ShiftAllocation.hours, 2)).join(
        TruckShift, TruckShift.id == ShiftAllocation.shift_id).where(
        TruckShift.organization_id == organization_id, TruckShift.status == "approved")
    shifts = select(func.count()).select_from(TruckShift).where(TruckShift.organization_id == organization_id)
    if project_id is not None:
        allocations = allocations.where(ShiftAllocation.project_id == project_id)
        shifts = shifts.where(TruckShift.project_id == project_id)
    cost, hours = db.execute(allocations).one()
    hauling = aggregates.as_decimal(cost, 2)
    return {"approved_cost": str(hauling), "approved_hours": str(aggregates.as_decimal(hours, 2)),
            # Added here, exactly: approved material and approved hauling, each counted once.
            "approved_with_material": str(material + hauling),
            "awaiting_approval": db.scalar(shifts.where(TruckShift.status == "needs_review")),
            "disputed": db.scalar(shifts.where(TruckShift.status == "disputed"))}


def _active_projects(db: Session, organization_id: int, project_id: int | None, limit: int = 8) -> list[dict]:
    """Each active job: its tickets, what's still open, approved tons and the last upload."""
    query = select(Project).where(Project.organization_id == organization_id, Project.status == "active")
    if project_id is not None:
        query = query.where(Project.id == project_id)
    jobs = list(db.scalars(query))
    if not jobs:
        return []
    ids = [job.id for job in jobs]
    counts = {row.project_id: row for row in db.execute(
        select(Batch.project_id, func.count(Ticket.id).label("tickets"),
               func.sum(case((Ticket.status.in_(OPEN_STATUSES), 1), else_=0)).label("open"),
               aggregates.counted_tons().label("tons"))
        .select_from(Ticket).join(Batch, Batch.id == Ticket.batch_id)
        .where(Ticket.organization_id == organization_id, Batch.project_id.in_(ids))
        .group_by(Batch.project_id))}
    uploads = dict(db.execute(select(Batch.project_id, func.max(Batch.created_at)).where(
        Batch.organization_id == organization_id, Batch.project_id.in_(ids)).group_by(Batch.project_id)).all())
    rows = [{
        "id": job.id, "code": job.code, "name": job.name,
        "tickets": counts[job.id].tickets if job.id in counts else 0,
        "open": int(counts[job.id].open or 0) if job.id in counts else 0,
        "verified_tons": str(aggregates.as_decimal(counts[job.id].tons if job.id in counts else 0, 3)),
        "last_upload": as_utc(uploads[job.id]) if uploads.get(job.id) else None,
    } for job in jobs]
    # Most recently worked on first; jobs with no uploads yet after, by number.
    rows.sort(key=lambda r: (r["last_upload"] is None, -(r["last_upload"].timestamp()) if r["last_upload"] else 0, r["code"]))
    return rows[:limit]


def _recent_activity(db: Session, organization_id: int, project_id: int | None, limit: int = 8) -> list[dict]:
    """Who approved, flagged or skipped which ticket, most recent first, from the audit trail."""
    query = (select(AuditEvent.created_at, AuditEvent.new_value, Ticket.id, Ticket.ticket_number, Ticket.batch_id,
                    User.display_name, User.username)
             .join(Ticket, Ticket.id == AuditEvent.ticket_id)
             .outerjoin(User, User.id == AuditEvent.actor_user_id)
             .where(Ticket.organization_id == organization_id, AuditEvent.field == "status",
                    AuditEvent.new_value.in_(tuple(ACTIVITY)))
             .order_by(AuditEvent.id.desc()).limit(limit))
    if project_id is not None:
        query = query.where(Ticket.batch_id.in_(select(Batch.id).where(
            Batch.organization_id == organization_id, Batch.project_id == project_id)))
    return [{"at": as_utc(row.created_at), "action": ACTIVITY[row.new_value], "ticket_id": row.id,
             "ticket_number": row.ticket_number, "batch_id": row.batch_id,
             "by": row.display_name or row.username} for row in db.execute(query)]


def _pilot(db: Session, organization_id: int, project_id: int | None) -> dict:
    query = select(InvoiceComparison).where(InvoiceComparison.organization_id == organization_id)
    if project_id is not None:
        query = query.where(InvoiceComparison.project_id == project_id)
    items = list(db.scalars(query))
    context = LineContext(db, organization_id, items)
    statuses = Counter(_line_out(db, item, organization_id, context).status for item in items)
    return pilot_report.build_report(db, organization_id, project_id, statuses)


@router.get("/reports/project/{project_id}", dependencies=[Depends(limited("reports"))])
def project_report_view(project_id: int, db: Session = Depends(get_db),
                        organization_id: int = Depends(current_organization_id)):
    return _project_report(db, organization_id, project_id)


@router.get("/reports/project/{project_id}/report.xlsx", dependencies=[Depends(limited("exports"))])
def project_report_xlsx(project_id: int, db: Session = Depends(get_db),
                        organization_id: int = Depends(current_organization_id)):
    report = _project_report(db, organization_id, project_id)
    costs = _summary(db, organization_id, project_id, None, None, "month")
    people = names_by_id(db, organization_id)
    deliveries = [
        [export_svc._safe_excel_text(t.ticket_number), t.delivery_date, export_svc._safe_excel_text(t.supplier),
         export_svc._safe_excel_text(t.material_type), t.tonnage, t.applied_unit_price, t.approved_cost,
         people.get(t.verified_by_user_id), t.verified_at.replace(tzinfo=None) if t.verified_at else None]
        for t in db.scalars(filtered_query(select(Ticket), organization_id=organization_id, project_id=project_id, supplier=None,
                                           material=None, status="verified", date_from=None, date_to=None)
                            .where(aggregates.COUNTED)  # the same rule as every total, so the rows add up to them
                            .order_by(Ticket.delivery_date, Ticket.id))]
    return Response(
        project_report.workbook(report, costs, deliveries),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="project-{project_id}-report.xlsx"', "Cache-Control": "no-store"})


def _owned_project(db: Session, organization_id: int, project_id: int) -> Project:
    project = db.scalar(select(Project).where(Project.id == project_id, Project.organization_id == organization_id))
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/reports/project/{project_id}/unbilled", dependencies=[Depends(limited("reports"))])
def unbilled(project_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Approved deliveries no invoice line lists or covers yet. Totals cover every one;
    the rows stop at 500."""
    return reconciliation.unbilled(db, organization_id, _owned_project(db, organization_id, project_id))


@router.get("/reports/project/{project_id}/unbilled.csv", dependencies=[Depends(limited("exports"))])
def unbilled_csv(project_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    from .invoices import _csv_text
    project = _owned_project(db, organization_id, project_id)

    def text(value) -> str:
        return _csv_text(value) if value is not None else ""

    def stream():
        buffer = StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["Ticket ID", "Batch ID", "Ticket number", "Delivery date", "Supplier", "Material", "Approved tons",
                         "Material cost at locked rate (not a bill)"])
        for number, row in enumerate(reconciliation.unbilled_rows(db, organization_id, project.id), start=1):
            writer.writerow([row["ticket_id"], row["batch_id"], text(row["ticket_number"]), row["delivery_date"] or "",
                             text(row["supplier"]), text(row["material"]), row["tons"] or "", row["cost"] or ""])
            if number % 1000 == 0:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()

    return StreamingResponse(stream(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="unbilled-deliveries-project-{project.id}.csv"', "Cache-Control": "no-store"})


def _project_report(db: Session, organization_id: int, project_id: int) -> dict:
    project = db.scalar(select(Project).where(Project.id == project_id, Project.organization_id == organization_id))
    if project is None:
        raise HTTPException(404, "Project not found")
    items = list(db.scalars(select(InvoiceComparison).where(
        InvoiceComparison.organization_id == organization_id, InvoiceComparison.project_id == project_id,
    ).order_by(InvoiceComparison.id)))
    context = LineContext(db, organization_id, items)
    return project_report.build(db, organization_id, project, [_line_out(db, item, organization_id, context) for item in items])


def _project_filter(db: Session, organization_id: int, project_id: int | None) -> None:
    if project_id is not None and db.scalar(select(Project.id).where(
        Project.id == project_id, Project.organization_id == organization_id,
    )) is None:
        raise HTTPException(404, "Project not found")


@router.get("/reports/pilot", dependencies=[Depends(limited("reports"))])
def pilot(project_id: int | None = None, db: Session = Depends(get_db),
          organization_id: int = Depends(current_organization_id)):
    _project_filter(db, organization_id, project_id)
    return _pilot(db, organization_id, project_id)


@router.get("/reports/pilot.csv", dependencies=[Depends(limited("exports"))])
def pilot_csv(project_id: int | None = None, db: Session = Depends(get_db),
              organization_id: int = Depends(current_organization_id)):
    _project_filter(db, organization_id, project_id)
    report = _pilot(db, organization_id, project_id)
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Quarvera pilot report - counts of records, not an approval or a completeness claim"])
    writer.writerow(["Unresolved items", report["unresolved_items"]])
    for section in ("intake", "review", "reconciliation"):
        writer.writerow([])
        writer.writerow([section.title()])
        for name, value in report[section].items():
            if isinstance(value, dict):
                for key, count in sorted(value.items()):
                    writer.writerow([f"{name}: {key}", count])
            else:
                writer.writerow([name, value])
    writer.writerow([])
    writer.writerow(["Batches needing attention"])
    for row in report["batches_needing_attention"]:
        writer.writerow([f"Batch {row['batch_id']}", "; ".join(row["reasons"])])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="quarvera-pilot-report.csv"', "Cache-Control": "no-store"})


class TimingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: int
    seconds: Decimal = Field(gt=0, le=3600, max_digits=8, decimal_places=1)


@router.post("/review-timings", status_code=204)
def record_timing(body: TimingIn, db: Session = Depends(get_db), user: User = Depends(require("tickets.edit"))):
    """Opt-in: only stored when REVIEW_TIMING_ENABLED is on. The server decides
    whether fields were corrected, from the audit trail."""
    if not settings.review_timing_enabled:
        raise HTTPException(404, "Review timing is not enabled.")
    row = db.execute(select(Ticket.id, Batch.id, Batch.intake_mode).join(Batch, Batch.id == Ticket.batch_id).where(
        Ticket.id == body.ticket_id, Batch.organization_id == user.organization_id)).first()
    if row is None:
        raise HTTPException(404, "Ticket not found")
    corrected = len(db.scalars(select(AuditEvent.id).where(
        AuditEvent.ticket_id == row[0], AuditEvent.field.in_(EDITABLE))).all())
    db.add(ReviewTiming(organization_id=user.organization_id, batch_id=row[1], intake_mode=row[2],
                        seconds=body.seconds, fields_corrected=corrected))
    db.commit()
