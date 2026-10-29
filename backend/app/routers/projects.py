from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..models import Project

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


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    p = Project(organization_id=organization_id, code=body.code.strip(), name=body.name.strip(), job_location=body.job_location, description=body.description)
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A project with that ID already exists")
    return p


@router.patch("/{pid}", response_model=ProjectOut)
def update_project(pid: int, body: ProjectPatch, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    p = db.scalar(select(Project).where(Project.id == pid, Project.organization_id == organization_id))
    if not p:
        raise HTTPException(404, "Project not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.commit()
    return p
