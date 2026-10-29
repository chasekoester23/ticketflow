"""Cross-batch views: filtered delivery records, dashboard totals, filtered Excel export."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Batch, Project, Ticket
from ..schemas import TicketOut
from ..services import export as export_svc
from ..services import pricing as pricing_svc
from .api import _out

router = APIRouter(prefix="/api")

VALID_STATUS = {"needs_review", "verified", "skipped", "failed"}


def filtered_tickets(
    db: Session, *, project_id: int | None, supplier: str | None, material: str | None,
    status: str | None, date_from: date | None, date_to: date | None, include_skipped: bool = False,
) -> list[Ticket]:
    q = select(Ticket).join(Batch, Batch.id == Ticket.batch_id)
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
    return list(db.scalars(q.order_by(Ticket.delivery_date, Ticket.id)).all())


@router.get("/records", response_model=list[TicketOut])
def records(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None,
    db: Session = Depends(get_db),
):
    table = pricing_svc.rate_table(db)
    ts = filtered_tickets(db, project_id=project_id, supplier=supplier, material=material,
                          status=status, date_from=date_from, date_to=date_to)
    return [_out(db, t, table) for t in ts]


@router.get("/records/export.xlsx")
def export_records(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None,
    db: Session = Depends(get_db),
):
    table = pricing_svc.rate_table(db)
    ts = filtered_tickets(db, project_id=project_id, supplier=supplier, material=material,
                          status=status, date_from=date_from, date_to=date_to)
    prices = {t.id: p for t in ts if (p := pricing_svc.unit_price_for(t, table)[0]) is not None}
    return Response(
        export_svc.build_workbook(ts, prices),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="delivery-records.xlsx"'})


@router.get("/dashboard")
def dashboard(project_id: int | None = None, db: Session = Depends(get_db)):
    table = pricing_svc.rate_table(db)
    all_ts = filtered_tickets(db, project_id=project_id, supplier=None, material=None, status=None,
                              date_from=None, date_to=None, include_skipped=True)
    ts = [t for t in all_ts if t.status != "skipped"]

    # Only approved quantities belong in actuals. Unverified extractions are
    # estimates and must not silently inflate project totals.
    verified = [t for t in ts if t.status == "verified" and t.tonnage_confirmed]
    pending = [t for t in ts if t.status != "verified" or not t.tonnage_confirmed]
    total_tons = sum((t.tonnage for t in verified if t.tonnage is not None), Decimal(0))
    pending_tons = sum((t.tonnage for t in pending if t.tonnage is not None), Decimal(0))
    prices = [pricing_svc.unit_price_for(t, table)[0] for t in ts]
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
        return [{"name": k, "tickets": v[0], "tons": v[1], "cost": v[2]} for k, v in sorted(g.items())]

    bq = select(Batch).order_by(Batch.id.desc()).limit(8)
    if project_id is not None:
        bq = bq.where(Batch.project_id == project_id)
    projects = {p.id: p.name for p in db.scalars(select(Project))}
    return {
        "tickets": len(ts),
        "needs_review": sum(1 for t in ts if t.status in ("needs_review", "failed")),
        "verified": sum(1 for t in ts if t.status == "verified"),
        "total_tons": total_tons,
        "pending_tons": pending_tons,
        "total_cost": total_cost,
        "unpriced_tickets": sum(1 for t, p in zip(ts, prices)
                                if t.status == "verified" and t.tonnage_confirmed and p is None),
        "pending_cost_tickets": len(pending),
        "by_supplier": group(lambda t: t.supplier),
        "by_material": group(lambda t: t.material_type),
        "recent_batches": [
            {"id": b.id, "status": b.status, "created_at": b.created_at, "files": len(b.files),
             "project": projects.get(b.project_id)} for b in db.scalars(bq)
        ],
    }
