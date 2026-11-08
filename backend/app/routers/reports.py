"""Cross-batch views: filtered delivery records, dashboard totals, filtered Excel export."""
import csv
from collections import Counter
from datetime import date
from decimal import Decimal
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import false, func, or_, select
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
from ..services import pilot_report, project_report
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
    names = pricing_svc.catalog_names(db, organization_id)
    aliases = pricing_svc.alias_suggestions(db, organization_id, names)
    return [_out(db, t, organization_id, table, duplicate_index=index, catalog=names, aliases=aliases) for t in ts]


@router.get("/records/export.xlsx", dependencies=[Depends(limited("exports"))])
def export_records(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None, q: str | None = None,
    columns: str | None = None,
    db: Session = Depends(get_db),
    organization_id: int = Depends(current_organization_id),
):
    keys = [key.strip() for key in columns.split(",") if key.strip()] if columns else None
    if keys is not None and (not keys or set(keys) - set(export_svc.COLUMNS)):
        raise HTTPException(422, "Unknown export column.")
    filters = dict(organization_id=organization_id, project_id=project_id, supplier=supplier, material=material,
                   status=status, date_from=date_from, date_to=date_to, q=q)
    matching = db.scalar(filtered_query(select(func.count()), **filters))
    if matching > MAX_EXCEL_ROWS:
        raise HTTPException(413, f"{matching:,} records is too many for a spreadsheet to open comfortably. "
                                 "Narrow the filters, or use the CSV export, which handles any size.")
    table = pricing_svc.rate_table(db, organization_id)
    return Response(
        export_svc.build_workbook(export_svc.rows(db, filtered_query(export_svc.row_query(), **filters)),
                                  names_by_id(db, organization_id), keys, price_of=lambda t: pricing_svc.effective_price_for(t, table)[0]),
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
    }


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
    return Response(
        project_report.workbook(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="project-{project_id}-report.xlsx"', "Cache-Control": "no-store"})


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
    writer.writerow(["TicketFlow pilot report - counts of records, not an approval or a completeness claim"])
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
        "Content-Disposition": 'attachment; filename="ticketflow-pilot-report.csv"', "Cache-Control": "no-store"})


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
