"""First-run setup: the first company and its first owner, from the browser.

Public, because nobody can sign in yet. So it's guarded: it works only while no user
exists at all, only from this machine unless ALLOW_REMOTE_SETUP=true, and two people
trying at once can't both succeed. Afterwards it answers 409 forever, and people are
added from the Team page or the terminal scripts.
"""
from __future__ import annotations

import ipaddress
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import MAX_PASSWORD_LENGTH, Credentials, login, normalize_username, password_hash
from ..config import settings
from ..db import get_db
from ..models import AccountEvent, Organization, User
from ..services.locks import take_turn
from .auth import account_profile

router = APIRouter(prefix="/api/setup")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FORWARDED_FOR = re.compile(r'for="?\[?([^\]";,]+)', re.IGNORECASE)


class SetupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_name: str = Field(min_length=1, max_length=200)
    username: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


def _loopback(address: str) -> bool:
    address = address.strip()
    if address.count(":") == 1:  # host:port
        address = address.split(":")[0]
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def from_this_machine(request: Request) -> bool:
    """The connection is from this machine, and so is everyone a proxy says it passed
    the request on for. A proxy on this machine makes every visitor look local, so a
    forwarded address that isn't this machine counts against it."""
    if request.client is None or not _loopback(request.client.host):
        return False
    claimed = []
    for name in ("x-forwarded-for", "x-real-ip"):
        claimed += [part for part in request.headers.get(name, "").split(",") if part.strip()]
    claimed += _FORWARDED_FOR.findall(request.headers.get("forwarded", ""))
    return all(_loopback(address) for address in claimed)


def _needed(db: Session) -> bool:
    return not db.scalar(select(func.count()).select_from(User))


@router.get("/status")
def setup_status(db: Session = Depends(get_db)):
    """Only whether setup is still needed. Reveals nothing else."""
    return {"needed": _needed(db)}


def _clean(value: str, label: str, limit: int) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or _CONTROL.search(value.replace("\t", " ")) or len(cleaned) > limit:
        raise HTTPException(422, f"Enter a {label} of 1 to {limit} ordinary characters.")
    return cleaned


@router.post("")
def first_setup(body: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not settings.allow_remote_setup and not from_this_machine(request):
        raise HTTPException(403, {"code": "setup_local_only", "message": "First-time setup only works from the computer TicketFlow runs on. "
                                  "Open it there, or set ALLOW_REMOTE_SETUP=true for this one step."})
    if not _needed(db):
        raise HTTPException(409, {"code": "already_set_up", "message": "TicketFlow is already set up. Sign in, or ask an owner to add you."})
    company = _clean(body.company_name, "company name", 200)
    display = _clean(body.display_name, "name", 100)
    try:
        username = normalize_username(body.username)
        if body.password.strip().casefold() == username:
            raise ValueError("Password cannot be your username.")
        encoded = password_hash(body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    # Two people setting up at once: the second waits, then finds a user and stops.
    take_turn(db, "first-setup")
    if not _needed(db):
        db.rollback()
        raise HTTPException(409, {"code": "already_set_up", "message": "TicketFlow was just set up by someone else. Sign in, or ask them to add you."})
    # The migrations create one placeholder company. With no users yet, it's this one.
    organization = db.scalar(select(Organization).order_by(Organization.id).limit(1))
    if organization is None:
        organization = Organization(name=company)
        db.add(organization)
    organization.name = company
    db.flush()
    owner = User(organization_id=organization.id, username=username, display_name=display, password_hash=encoded, role="owner",
                 active=True, must_change_password=False)
    db.add(owner)
    db.flush()
    db.add(AccountEvent(organization_id=organization.id, actor_user_id=owner.id, target_user_id=owner.id, action="workspace_set_up", new_value="owner"))
    db.commit()
    return account_profile(db, login(request, response, Credentials(username=username, password=body.password), db))
