"""First-run setup: the first company and its first owner, from the browser. Also, for a
signed-in company, its starting lists pasted in at once (see the end of this file).

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
from ..permissions import require
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
        raise HTTPException(403, {"code": "setup_local_only", "message": "First-time setup only works from the computer Quarvera runs on. "
                                  "Open it there, or set ALLOW_REMOTE_SETUP=true for this one step."})
    if not _needed(db):
        raise HTTPException(409, {"code": "already_set_up", "message": "Quarvera is already set up. Sign in, or ask an owner to add you."})
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
        raise HTTPException(409, {"code": "already_set_up", "message": "Quarvera was just set up by someone else. Sign in, or ask them to add you."})
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


# ---------------------------------------------------------------- lists pasted in at the start
# A contractor's suppliers, materials, haulers and projects, pasted from a spreadsheet (tabs),
# a CSV or one name per line. Preview first, then apply. Names already on file are left as
# they are; nothing is ever renamed or removed. Rates come later, from the rate sheet on
# Pricing: until then an approved ticket shows "rate missing", never $0. Each route needs
# the same permission as adding one record by hand, and is limited to the signed-in company.

MAX_LIST_ROWS = 500
_HEADERS = {"name", "names", "supplier", "suppliers", "supplier name", "vendor", "material", "materials", "material name",
            "product", "hauler", "haulers", "hauler name", "trucking company", "carrier", "project", "projects", "code",
            "project code", "project number", "project no", "job", "job number", "job no", "job #", "contact"}


class ListIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=200_000)
    # False previews what would be added and saves nothing.
    apply: bool = False


def _list_rows(text: str) -> list[tuple[int, list[str]]]:
    import csv

    lines = text.splitlines()
    delimiter = "\t" if any("\t" in line for line in lines) else ","
    rows = []
    for number, cells in enumerate(csv.reader(lines, delimiter=delimiter), start=1):
        cells = [" ".join(cell.split()) for cell in cells]
        if any(cells):
            rows.append((number, cells))
    if rows and rows[0][1][0].casefold().rstrip(":") in _HEADERS:
        rows = rows[1:]  # a heading row from the spreadsheet
    if not rows:
        raise HTTPException(422, "Paste at least one name. Nothing was saved.")
    if len(rows) > MAX_LIST_ROWS:
        raise HTTPException(422, f"Paste at most {MAX_LIST_ROWS} rows at a time. Nothing was saved.")
    return rows


def _list_plan(rows, kind: str, known: dict[str, str], *, alias_of=None, code_column: bool = False) -> tuple[list[dict], list[str]]:
    """What each row would do: add, already on file, or a repeat of an earlier row."""
    from ..models import normal_name

    plan, problems, seen = [], [], {}
    for line, cells in rows:
        if code_column:
            code, name = cells[0], (cells[1] if len(cells) > 1 else "")
            key, extra = code.casefold(), {"code": code, "job_location": cells[2] if len(cells) > 2 and cells[2] else None}
            if not code or len(code) > 50 or _CONTROL.search(code):
                problems.append(f"line {line}: a project number of 1 to 50 characters comes first")
            if not name or len(name) > 200:
                problems.append(f"line {line}: the project's name comes after its number (1 to 200 characters)")
        else:
            name = cells[0]
            key, extra = normal_name(name), {"contact": cells[1] if len(cells) > 1 and cells[1] else None}
            if not key or len(name) > 200:
                problems.append(f"line {line}: '{name[:60]}' isn't a usable {kind} name")
            elif alias_of and alias_of(name):
                problems.append(f"line {line}: '{name[:60]}' is saved as another spelling of a {kind}, so it's already on file")
            if extra["contact"] and len(extra["contact"]) > 200:
                problems.append(f"line {line}: the contact is longer than 200 characters")
        action = "exists" if key in known else "repeated" if key in seen else "add"
        plan.append({"line": line, "name": name, **extra, "action": action,
                     "existing": known.get(key), "same_as_line": seen.get(key) if action == "repeated" else None})
        seen.setdefault(key, line)
    return plan, problems


def _list_answer(db: Session, kind: str, plan: list[dict], problems: list[str], apply: bool, create) -> dict:
    if problems:
        raise HTTPException(422, "The list needs fixes: " + "; ".join(problems[:8]) + ". Nothing was saved.")
    summary = {"kind": kind, "rows": plan, "adds": sum(p["action"] == "add" for p in plan),
               "existing": sum(p["action"] == "exists" for p in plan), "repeated": sum(p["action"] == "repeated" for p in plan),
               "applied": False}
    if not apply:
        return summary
    for row in plan:
        if row["action"] == "add":
            create(row)
    db.commit()
    return {**summary, "applied": True}


@router.post("/import/suppliers")
def import_suppliers(body: ListIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    from ..models import Supplier, normal_name
    from .pricing import _event, _name_is_an_alias

    organization_id = user.organization_id
    if body.apply:
        take_turn(db, "setup-import", organization_id, "suppliers")
    known = {normal_name(s.name): s.name for s in db.scalars(select(Supplier).where(Supplier.organization_id == organization_id))}
    plan, problems = _list_plan(_list_rows(body.text), "supplier", known,
                                alias_of=lambda name: _name_is_an_alias(db, organization_id, "supplier", name))

    def create(row):
        db.add(Supplier(organization_id=organization_id, name=row["name"], contact=row["contact"]))
        _event(db, user, "supplier_added", supplier=row["name"])

    return _list_answer(db, "suppliers", plan, problems, body.apply, create)


@router.post("/import/materials")
def import_materials(body: ListIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    from ..models import Material, normal_name
    from .pricing import _event, _name_is_an_alias

    organization_id = user.organization_id
    if body.apply:
        take_turn(db, "setup-import", organization_id, "materials")
    known = {normal_name(m.name): m.name for m in db.scalars(select(Material).where(Material.organization_id == organization_id))}
    plan, problems = _list_plan(_list_rows(body.text), "material", known,
                                alias_of=lambda name: _name_is_an_alias(db, organization_id, "material", name))
    for row in plan:
        row.pop("contact", None)

    def create(row):
        db.add(Material(organization_id=organization_id, name=row["name"]))
        _event(db, user, "material_added", material=row["name"])

    return _list_answer(db, "materials", plan, problems, body.apply, create)


@router.post("/import/haulers")
def import_haulers(body: ListIn, db: Session = Depends(get_db), user: User = Depends(require("trucking.edit"))):
    from ..models import Hauler, normal_name

    organization_id = user.organization_id
    if body.apply:
        take_turn(db, "setup-import", organization_id, "haulers")
    known = {h.name_key: h.name for h in db.scalars(select(Hauler).where(Hauler.organization_id == organization_id))}
    plan, problems = _list_plan(_list_rows(body.text), "hauler", known)

    def create(row):
        db.add(Hauler(organization_id=organization_id, name=row["name"], name_key=normal_name(row["name"]), contact=row["contact"]))

    return _list_answer(db, "haulers", plan, problems, body.apply, create)


@router.post("/import/projects")
def import_projects(body: ListIn, db: Session = Depends(get_db), user: User = Depends(require("projects.manage"))):
    from ..models import Project

    organization_id = user.organization_id
    if body.apply:
        take_turn(db, "setup-import", organization_id, "projects")
    known = {p.code.casefold(): f"{p.code} · {p.name}" for p in db.scalars(select(Project).where(Project.organization_id == organization_id))}
    plan, problems = _list_plan(_list_rows(body.text), "project", known, code_column=True)

    def create(row):
        db.add(Project(organization_id=organization_id, code=row["code"], name=row["name"], job_location=row["job_location"]))

    return _list_answer(db, "projects", plan, problems, body.apply, create)
