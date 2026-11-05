"""Cross-batch views: filtered delivery records, dashboard totals, filtered Excel export."""
import csv
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..config import settings
from ..db import get_db
from ..models import AuditEvent, Batch, InvoiceComparison, Project, ReviewTiming, Ticket, UploadedFile, User
from ..permissions import require
from ..schemas import TicketOut
from ..services import export as export_svc
from ..services import duplicates as dup_svc
from ..services import pilot_report
from ..services.invoice_context import LineContext
from ..services import pricing as pricing_svc
from ..services.accounts import names_by_id
from .api import EDITABLE, _out
from .invoices import _line_out

router = APIRouter(prefix="/api")

VALID_STATUS = {"needs_review", "verified", "skipped", "failed", "flagged"}


# What totals need. Whole Ticket objects cost several times as much to load,
# which matters on every dashboard view of a large company.
TOTALS_COLUMNS = (
    Ticket.id, Ticket.status, Ticket.tonnage, Ticket.tonnage_confirmed, Ticket.supplier, Ticket.material_type,
    Ticket.unit_price_override, Ticket.price_snapshot_state, Ticket.applied_unit_price, Ticket.applied_price_source,
)


def filtered_tickets(
    db: Session, *, organization_id: int, project_id: int | None, supplier: str | None, material: str | None,
    status: str | None, date_from: date | None, date_to: date | None, include_skipped: bool = False,
    columns: tuple | None = None,
) -> list:
    """Ticket objects, or lightweight rows with these attributes when ``columns`` is given."""
    q = (select(*columns) if columns else select(Ticket)).select_from(Ticket).join(Batch, Batch.id == Ticket.batch_id).where(
        Batch.organization_id == organization_id
    )
    if project_id is not None:
        q = q.where(Batch.project_id == project_id)
    if supplier:
        q = q.where(Ticket.supplier.ilike(f"%{supplier.strip()}%"))
    if material:
        q = q.where(Ticket.material_type.ilike(f"%{material.strip()}%"))
    if status in VALID_STATUS:
        q = q.where(Ticket.status == status)
    elif not include_skipped:
        q = q.where(Ticket.status != "skipped")
    if date_from:
        q = q.where(Ticket.delivery_date >= date_from)
    if date_to:
        q = q.where(Ticket.delivery_date <= date_to)
    q = q.order_by(Ticket.delivery_date, Ticket.id)
    return list(db.execute(q).all() if columns else db.scalars(q).all())


@router.get("/records", response_model=list[TicketOut])
def records(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None,
    db: Session = Depends(get_db),
    organization_id: int = Depends(current_organization_id),
):
    table = pricing_svc.rate_table(db, organization_id)
    ts = filtered_tickets(db, organization_id=organization_id, project_id=project_id, supplier=supplier, material=material,
                          status=status, date_from=date_from, date_to=date_to)
    index = dup_svc.load_index(db, organization_id, ts)
    names = pricing_svc.catalog_names(db, organization_id)
    aliases = pricing_svc.alias_suggestions(db, organization_id, names)
    return [_out(db, t, organization_id, table, duplicate_index=index, catalog=names, aliases=aliases) for t in ts]


@router.get("/records/export.xlsx")
def export_records(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None,
    db: Session = Depends(get_db),
    organization_id: int = Depends(current_organization_id),
):
    table = pricing_svc.rate_table(db, organization_id)
    ts = filtered_tickets(db, organization_id=organization_id, project_id=project_id, supplier=supplier, material=material,
                          status=status, date_from=date_from, date_to=date_to)
    prices = {t.id: p for t in ts if (p := pricing_svc.effective_price_for(t, table)[0]) is not None}
    return Response(
        export_svc.build_workbook(ts, prices, names_by_id(db, organization_id)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="delivery-records.xlsx"'})


@router.get("/dashboard")
def dashboard(project_id: int | None = None, db: Session = Depends(get_db),
              organization_id: int = Depends(current_organization_id)):
    table = pricing_svc.rate_table(db, organization_id)
    all_ts = filtered_tickets(db, organization_id=organization_id, project_id=project_id, supplier=None, material=None, status=None,
                              date_from=None, date_to=None, include_skipped=True, columns=TOTALS_COLUMNS)
    ts = [t for t in all_ts if t.status != "skipped"]

    # Only approved quantities belong in actuals. Unverified extractions are
    # estimates and must not silently inflate project totals.
    verified = [t for t in ts if t.status == "verified" and t.tonnage_confirmed]
    pending = [t for t in ts if t.status != "verified" or not t.tonnage_confirmed]
    total_tons = sum((t.tonnage for t in verified if t.tonnage is not None), Decimal(0))
    pending_tons = sum((t.tonnage for t in pending if t.tonnage is not None), Decimal(0))
    prices = [pricing_svc.effective_price_for(t, table)[0] for t in ts]
    costs = [pricing_svc.cost_for(t, p) for t, p in zip(ts, prices)]
    total_cost = sum((c for c in costs if c is not None), Decimal(0))

    def group(keyfn):
        g: dict[str, list] = defaultdict(lambda: [0, Decimal(0), Decimal(0)])
        for t, c in zip(ts, costs):
            row = g[keyfn(t) or "(unknown)"]
            row[0] += 1
            if t.status == "verified" and t.tonnage_confirmed:
                row[1] += t.tonnage or Decimal(0)
            row[2] += c or Decimal(0)
        return [{"name": k, "tickets": v[0], "tons": str(v[1]), "cost": str(v[2])} for k, v in sorted(g.items())]

    bq = select(Batch).where(Batch.organization_id == organization_id).order_by(Batch.id.desc()).limit(8)
    if project_id is not None:
        bq = bq.where(Batch.project_id == project_id)
    recent = list(db.scalars(bq))
    file_counts = dict(db.execute(select(UploadedFile.batch_id, func.count()).where(
        UploadedFile.batch_id.in_([b.id for b in recent])).group_by(UploadedFile.batch_id)).all()) if recent else {}
    projects = {p.id: p.name for p in db.scalars(select(Project).where(
        Project.organization_id == organization_id
    ))}
    return {
        "tickets": len(ts),
        "needs_review": sum(1 for t in ts if t.status in ("needs_review", "failed", "flagged")),
        "flagged": sum(1 for t in ts if t.status == "flagged"),
        "verified": sum(1 for t in ts if t.status == "verified"),
        # Strings: FastAPI would send Decimals in a plain dict as lossy floats.
        "total_tons": str(total_tons),
        "pending_tons": str(pending_tons),
        "total_cost": str(total_cost),
        "unpriced_tickets": sum(1 for t, p in zip(ts, prices)
                                if t.status == "verified" and t.tonnage_confirmed and p is None),
        "pending_cost_tickets": len(pending),
        "by_supplier": group(lambda t: t.supplier),
        "by_material": group(lambda t: t.material_type),
        "recent_batches": [
            {"id": b.id, "status": b.status, "created_at": b.created_at, "files": file_counts.get(b.id, 0),
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


def _project_filter(db: Session, organization_id: int, project_id: int | None) -> None:
    if project_id is not None and db.scalar(select(Project.id).where(
        Project.id == project_id, Project.organization_id == organization_id,
    )) is None:
        raise HTTPException(404, "Project not found")


@router.get("/reports/pilot")
def pilot(project_id: int | None = None, db: Session = Depends(get_db),
          organization_id: int = Depends(current_organization_id)):
    _project_filter(db, organization_id, project_id)
    return _pilot(db, organization_id, project_id)


@router.get("/reports/pilot.csv")
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
