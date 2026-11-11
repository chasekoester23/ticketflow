"""Installation health and first-run progress for the people running TicketFlow."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..models import Batch, InvoiceComparison, Material, Project, Supplier, SupplierRate, Ticket, User
from ..permissions import require
from ..services import system_health, usage

router = APIRouter(prefix="/api")


@router.get("/system/status")
def system_status(db: Session = Depends(get_db), user: User = Depends(require("system.read"))):
    return system_health.build(db, user.organization_id)


@router.get("/system/usage")
def usage_this_month(db: Session = Depends(get_db), user: User = Depends(require("usage.read"))):
    """This company's ticket reads, tokens and estimated cost this month. Cost is unknown
    unless MODEL_PRICES names every model used. Counts only, never a ticket's values."""
    return usage.report(db, user.organization_id)


def _count(db: Session, model, *conditions) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


@router.get("/onboarding")
def onboarding(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """How far this company has got, read from what actually exists. Stores nothing."""
    company = Batch.organization_id == organization_id
    steps = [
        ("project", "Create your first project", "Tickets and invoices are filed under your job numbers.",
         "/projects", _count(db, Project, Project.organization_id == organization_id) > 0),
        ("catalog", "Add suppliers, materials and rates", "Paste your rate sheet, so approved tickets lock in a price.",
         "/pricing", _count(db, SupplierRate, SupplierRate.organization_id == organization_id) > 0),
        ("team", "Invite your team", "Each person gets their own login, so approvals are attributed to a name.",
         "/team", _count(db, User, User.organization_id == organization_id, User.active.is_(True)) > 1),
        ("upload", "Upload your first scans", "A PDF of four tickets per page works, or single images.",
         "/upload", _count(db, Batch, company) > 0),
        ("approve", "Approve your first ticket", "Check each value against the scan. Only approved tickets count.",
         "/upload", db.scalar(select(func.count()).select_from(Ticket).join(Batch, Batch.id == Ticket.batch_id).where(
             company, Ticket.status == "verified")) > 0),
        ("invoice", "Compare your first invoice", "Upload a supplier invoice PDF, or type a line in.",
         "/invoices/documents", _count(db, InvoiceComparison, InvoiceComparison.organization_id == organization_id) > 0),
    ]
    return {
        "complete": all(step[4] for step in steps),
        "steps": [{"key": key, "title": title, "detail": detail, "href": href, "done": done}
                  for key, title, detail, href, done in steps],
        "suppliers": _count(db, Supplier, Supplier.organization_id == organization_id),
        "materials": _count(db, Material, Material.organization_id == organization_id),
    }
