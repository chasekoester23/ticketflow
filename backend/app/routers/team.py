"""Owner-only team management for ONE company.

Every query is scoped by the signed-in owner's organization. A user ID from
another company is indistinguishable from a missing one (404).
Temporary passwords are returned exactly once and never stored in plain text.
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import normalize_username
from ..db import get_db
from ..models import AccountEvent, User, as_utc
from ..permissions import ROLE_LABELS, require
from ..services import accounts

router = APIRouter(prefix="/api/team")

Role = Literal["owner", "manager", "clerk", "viewer"]
_manage = require("team.manage")


class MemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    display_name: str | None = Field(default=None, max_length=100)
    role: Role

    @field_validator("username")
    @classmethod
    def canonical_username(cls, value: str) -> str:
        return normalize_username(value)


class MemberPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role | None = None
    active: bool | None = None
    display_name: str | None = Field(default=None, max_length=100)


class MemberOut(BaseModel):
    id: int
    username: str
    display_name: str | None
    role: str
    role_label: str
    active: bool
    must_change_password: bool
    created_at: datetime | None
    is_you: bool


class MemberWithPassword(BaseModel):
    member: MemberOut
    temporary_password: str


def _out(user: User, me: User) -> MemberOut:
    return MemberOut(
        id=user.id, username=user.username, display_name=user.display_name,
        role=user.role, role_label=ROLE_LABELS.get(user.role, user.role),
        active=user.active, must_change_password=user.must_change_password,
        created_at=as_utc(user.created_at), is_you=user.id == me.id,
    )


def _member_or_404(db: Session, owner: User, member_id: int) -> User:
    member = db.scalar(select(User).where(
        User.id == member_id, User.organization_id == owner.organization_id,
    ))
    if member is None:
        raise HTTPException(404, "Team member not found")
    return member


def _fail(db: Session, exc: accounts.AccountError):
    db.rollback()
    raise HTTPException(exc.status, str(exc)) from exc


@router.get("/members", response_model=list[MemberOut])
def list_members(owner: User = Depends(_manage), db: Session = Depends(get_db)):
    members = db.scalars(
        select(User).where(User.organization_id == owner.organization_id)
        .order_by(User.active.desc(), User.username)
    ).all()
    return [_out(m, owner) for m in members]


@router.post("/members", response_model=MemberWithPassword, status_code=201)
def add_member(body: MemberIn, owner: User = Depends(_manage), db: Session = Depends(get_db)):
    try:
        member, temporary = accounts.create_member(
            db, actor=owner, username=body.username, role=body.role,
            display_name=body.display_name,
        )
        db.commit()
    except accounts.AccountError as exc:
        _fail(db, exc)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That username is not available. Choose another.")
    return MemberWithPassword(member=_out(member, owner), temporary_password=temporary)


@router.patch("/members/{member_id}", response_model=MemberOut)
def update_member(member_id: int, body: MemberPatch, owner: User = Depends(_manage),
                  db: Session = Depends(get_db)):
    member = _member_or_404(db, owner, member_id)
    fields = body.model_dump(exclude_unset=True)
    try:
        accounts.update_member(
            db, actor=owner, target=member,
            role=fields.get("role"), active=fields.get("active"),
            display_name=fields.get("display_name"),
            display_name_set="display_name" in fields,
        )
        db.commit()
    except accounts.AccountError as exc:
        _fail(db, exc)
    return _out(member, owner)


@router.post("/members/{member_id}/reset-password", response_model=MemberWithPassword)
def reset_password(member_id: int, owner: User = Depends(_manage), db: Session = Depends(get_db)):
    member = _member_or_404(db, owner, member_id)
    if member.id == owner.id:
        raise HTTPException(409, "Use Account settings to change your own password.")
    temporary = accounts.reset_member_password(db, actor=owner, target=member)
    db.commit()
    return MemberWithPassword(member=_out(member, owner), temporary_password=temporary)


@router.get("/events")
def account_events(owner: User = Depends(_manage), db: Session = Depends(get_db)):
    """Most recent 200 team changes in this company, newest first."""
    names = accounts.names_by_id(db, owner.organization_id)
    events = db.scalars(
        select(AccountEvent).where(AccountEvent.organization_id == owner.organization_id)
        .order_by(AccountEvent.id.desc()).limit(200)
    ).all()
    return [
        {
            "id": e.id, "action": e.action, "old": e.old_value, "new": e.new_value,
            "at": as_utc(e.created_at),
            "actor": names.get(e.actor_user_id) if e.actor_user_id else None,
            "actor_kind": "person" if e.actor_user_id else "terminal",
            "target": names.get(e.target_user_id),
        }
        for e in events
    ]
