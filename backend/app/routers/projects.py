from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..permissions import require
from ..rate_limit import limited
from ..models import Batch, Project, ShiftAllocation, Ticket, TruckShift, User, as_utc
from ..services import aggregates

router = APIRouter(prefix="/api/projects")


class ProjectIn(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    job_location: str | None = None
    description: str | None = None


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    job_location: str | None = None
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    job_location: str | None
    description: str | None
    status: str


@router.get("", response_model=list[ProjectOut])
def list_projects(include_archived: bool = False, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    q = select(Project).where(Project.organization_id == organization_id).order_by(Project.name)
    if not include_archived:
        q = q.where(Project.status == "active")
    return db.scalars(q).all()


OPEN_STATUSES = ("needs_review", "flagged", "failed")


@router.get("/directory", dependencies=[Depends(limited("reports"))])
def project_directory(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Every project of the company, archived ones too, with what it holds.

    Material cost counts approved tickets with a locked price only; ``unpriced`` says how
    many approved tickets have none, so a missing rate never reads as a zero cost.
    Trucking cost is approved hourly trucking through each shift's allocations, plus per-ton and
    per-load hauling locked on approved tickets.
    A fixed number of grouped queries, whatever the number of projects.
    """
    jobs = list(db.scalars(select(Project).where(Project.organization_id == organization_id)))
    tickets = {row.project_id: row for row in db.execute(
        select(Batch.project_id, func.count(Ticket.id).label("tickets"),
               func.sum(case((Ticket.status.in_(OPEN_STATUSES), 1), else_=0)).label("open"),
               aggregates.counted_tickets().label("verified"), aggregates.counted_tons().label("tons"),
               aggregates.counted_cost().label("cost"), aggregates.unpriced().label("unpriced"),
               aggregates.counted_haul_cost().label("haul"), aggregates.haul_unpriced().label("haul_missing"),
               func.max(Ticket.created_at).label("last_ticket"))
        .select_from(Ticket).join(Batch, Batch.id == Ticket.batch_id)
        .where(Ticket.organization_id == organization_id, Batch.organization_id == organization_id)
        .group_by(Batch.project_id))}
    uploads = dict(db.execute(select(Batch.project_id, func.max(Batch.created_at)).where(
        Batch.organization_id == organization_id).group_by(Batch.project_id)).all())
    trucking = {row.project_id: row for row in db.execute(
        select(ShiftAllocation.project_id, aggregates.exact_sum(ShiftAllocation.cost, 2).label("cost"))
        .join(TruckShift, TruckShift.id == ShiftAllocation.shift_id)
        .where(TruckShift.organization_id == organization_id, TruckShift.status == "approved")
        .group_by(ShiftAllocation.project_id))}
    shifts = {row.project_id: row for row in db.execute(
        select(TruckShift.project_id, func.sum(case((TruckShift.status == "needs_review", 1), else_=0)).label("open"),
               func.max(TruckShift.created_at).label("last"))
        .where(TruckShift.organization_id == organization_id).group_by(TruckShift.project_id))}

    def latest(*moments):
        known = [as_utc(m) for m in moments if m is not None]
        return max(known) if known else None

    rows = []
    for job in jobs:
        t, tr, sh = tickets.get(job.id), trucking.get(job.id), shifts.get(job.id)
        material = aggregates.as_decimal(t.cost if t else 0, 2)
        # Hourly shifts plus per-ton and per-load hauling locked on approved tickets: each paid once.
        hauling = aggregates.as_decimal(int(tr.cost if tr else 0) + int(t.haul if t else 0), 2)
        rows.append({
            "id": job.id, "code": job.code, "name": job.name, "job_location": job.job_location,
            "description": job.description, "status": job.status, "created_at": as_utc(job.created_at),
            "tickets": t.tickets if t else 0,
            "open_tickets": int(t.open or 0) if t else 0,
            "open_shifts": int(sh.open or 0) if sh else 0,
            "verified_tickets": int(t.verified or 0) if t else 0,
            "verified_tons": str(aggregates.as_decimal(t.tons if t else 0, 3)),
            "material_cost": str(material),
            "unpriced_tickets": int(t.unpriced or 0) if t else 0,
            "trucking_cost": str(hauling),
            "hauling_rate_missing": int(t.haul_missing or 0) if t else 0,
            "verified_cost": str(material + hauling),
            "last_activity": latest(uploads.get(job.id), t.last_ticket if t else None, sh.last if sh else None),
        })
    rows.sort(key=lambda r: (r["status"] != "active", r["last_activity"] is None,
                             -(r["last_activity"].timestamp()) if r["last_activity"] else 0, r["code"]))
    return rows


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db), user: User = Depends(require("projects.manage"))):
    organization_id = user.organization_id
    p = Project(organization_id=organization_id, code=body.code.strip(), name=body.name.strip(), job_location=body.job_location, description=body.description)
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A project with that ID already exists")
    return p


@router.patch("/{pid}", response_model=ProjectOut)
def update_project(pid: int, body: ProjectPatch, db: Session = Depends(get_db), user: User = Depends(require("projects.manage"))):
    organization_id = user.organization_id
    p = db.scalar(select(Project).where(Project.id == pid, Project.organization_id == organization_id))
    if not p:
        raise HTTPException(404, "Project not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.commit()
    return p
