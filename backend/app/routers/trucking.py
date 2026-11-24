"""Hourly trucking: haulers, trucks, hourly agreements and truck shifts.

The rules live in services/trucking.py. Here: who may do what, the company boundary,
revision checks, and history. A shift's price is locked only by a person's approval,
which names the hours they approve; time-sheet figures and the hauler's claim are
evidence beside them. Nothing here changes a material ticket's quantity or price.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..models import (
    Hauler, HaulingRate, Project, ShiftAllocation, Ticket, Truck, TruckShift, TruckShiftEvent, User, as_utc, normal_name,
)
from ..permissions import require
from ..services.accounts import names_by_id
from ..services.locks import take_turn
from ..services.trucking import EQUIPMENT_TYPES, HaulRate, ShiftError, allocate, choose_rate, hours_between, price_shift

router = APIRouter(prefix="/api")
HOURS = Field(ge=0, le=24, max_digits=6, decimal_places=2)


# ---------------------------------------------------------------- haulers and trucks

class HaulerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    contact: str | None = Field(default=None, max_length=200)


class TruckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hauler_id: int = Field(gt=0)
    identifier: str = Field(min_length=1, max_length=50)
    equipment_type: str

    @field_validator("equipment_type")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in EQUIPMENT_TYPES:
            raise ValueError("Unknown truck type.")
        return value


def _hauler_or_404(db: Session, organization_id: int, hauler_id: int) -> Hauler:
    hauler = db.scalar(select(Hauler).where(Hauler.id == hauler_id, Hauler.organization_id == organization_id))
    if hauler is None:
        raise HTTPException(404, "Hauler not found")
    return hauler


def _project_or_404(db: Session, organization_id: int, project_id: int) -> Project:
    project = db.scalar(select(Project).where(Project.id == project_id, Project.organization_id == organization_id))
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/haulers")
def list_haulers(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    haulers = db.scalars(select(Hauler).where(Hauler.organization_id == organization_id).order_by(Hauler.name)).all()
    trucks = db.scalars(select(Truck).where(Truck.organization_id == organization_id).order_by(Truck.identifier)).all()
    return [{"id": h.id, "name": h.name, "contact": h.contact,
             "trucks": [{"id": t.id, "identifier": t.identifier, "equipment_type": t.equipment_type, "active": t.active}
                        for t in trucks if t.hauler_id == h.id]} for h in haulers]


@router.post("/haulers", status_code=201)
def create_hauler(body: HaulerIn, db: Session = Depends(get_db), user: User = Depends(require("trucking.edit"))):
    name = body.name.strip()
    if not normal_name(name):
        raise HTTPException(422, "Enter the hauler's name.")
    hauler = Hauler(organization_id=user.organization_id, name=name, name_key=normal_name(name), contact=body.contact)
    db.add(hauler)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A hauler with that name is already on the list.") from None
    return {"id": hauler.id, "name": hauler.name, "contact": hauler.contact, "trucks": []}


@router.post("/trucks", status_code=201)
def create_truck(body: TruckIn, db: Session = Depends(get_db), user: User = Depends(require("trucking.edit"))):
    _hauler_or_404(db, user.organization_id, body.hauler_id)
    truck = Truck(organization_id=user.organization_id, hauler_id=body.hauler_id, identifier=body.identifier.strip(),
                  equipment_type=body.equipment_type)
    db.add(truck)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That hauler already has a truck with that number.") from None
    return {"id": truck.id, "hauler_id": truck.hauler_id, "identifier": truck.identifier, "equipment_type": truck.equipment_type}


# ---------------------------------------------------------------- hourly agreements

class HaulingRateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hauler_id: int = Field(gt=0)
    project_id: int | None = Field(default=None, gt=0)
    equipment_type: str | None = None
    hourly_rate: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    minimum_hours: Decimal | None = Field(default=None, gt=0, le=24, max_digits=6, decimal_places=2)
    standby_rate: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)
    effective_from: date | None = None
    effective_to: date | None = None

    @model_validator(mode="after")
    def _sensible(self):
        if self.equipment_type is not None and self.equipment_type not in EQUIPMENT_TYPES:
            raise ValueError("Unknown truck type.")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("The agreement's last day is before its first day.")
        return self


def _money(value: Decimal | None, places: str = "0.01") -> str | None:
    return None if value is None else str(Decimal(value).quantize(Decimal(places)))


def _rate_out(r: HaulingRate) -> dict:
    return {"id": r.id, "hauler_id": r.hauler_id, "project_id": r.project_id, "equipment_type": r.equipment_type,
            "hourly_rate": _money(r.hourly_rate, "0.0001"), "minimum_hours": _money(r.minimum_hours),
            "standby_rate": _money(r.standby_rate, "0.0001"),
            "effective_from": r.effective_from.isoformat() if r.effective_from else None,
            "effective_to": r.effective_to.isoformat() if r.effective_to else None}


@router.get("/hauling-rates")
def list_hauling_rates(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    rows = db.scalars(select(HaulingRate).where(HaulingRate.organization_id == organization_id)
                      .order_by(HaulingRate.hauler_id, HaulingRate.effective_from)).all()
    return [_rate_out(r) for r in rows]


@router.post("/hauling-rates", status_code=201)
def create_hauling_rate(body: HaulingRateIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    _hauler_or_404(db, user.organization_id, body.hauler_id)
    if body.project_id is not None:
        _project_or_404(db, user.organization_id, body.project_id)
    rate = HaulingRate(organization_id=user.organization_id, created_by_user_id=user.id, **body.model_dump())
    db.add(rate)
    db.commit()
    return _rate_out(rate)


@router.delete("/hauling-rates/{rate_id}", status_code=204)
def delete_hauling_rate(rate_id: int, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    rate = db.scalar(select(HaulingRate).where(HaulingRate.id == rate_id, HaulingRate.organization_id == user.organization_id))
    if rate is None:
        raise HTTPException(404, "Agreement not found")
    db.delete(rate)  # approved shifts keep the rate they were approved at
    db.commit()


# ---------------------------------------------------------------- truck shifts

class ShiftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    truck_id: int = Field(gt=0)
    start_at: datetime
    end_at: datetime
    unpaid_break_minutes: int = Field(default=0, ge=0, le=600)
    standby_hours: Decimal = Field(default=Decimal(0), ge=0, le=24, max_digits=6, decimal_places=2)
    claimed_hours: Decimal | None = Field(default=None, ge=0, le=24, max_digits=6, decimal_places=2)
    loads: int | None = Field(default=None, ge=0, le=500)
    driver_name: str | None = Field(default=None, max_length=100)
    field_signoff: str | None = Field(default=None, max_length=100)
    cost_code: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("start_at", "end_at")
    @classmethod
    def _local(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=None)  # times as written on the sheet, in the job's local time


class AllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    cost_code: str | None = Field(default=None, max_length=50)
    hours: Decimal = Field(gt=0, le=24, max_digits=6, decimal_places=2)


class ApproveIn(BaseModel):
    """A person states the hours they approve. Nothing is filled in for them."""
    model_config = ConfigDict(extra="forbid")
    work_hours: Decimal = HOURS
    standby_hours: Decimal = HOURS
    allocations: list[AllocationIn] | None = Field(default=None, max_length=20)


class ReopenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


class DeliveriesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_ids: list[int] = Field(max_length=500)


def _rates(db: Session, organization_id: int) -> list[HaulRate]:
    return [HaulRate(id=r.id, hauler_id=r.hauler_id, hourly_rate=r.hourly_rate, project_id=r.project_id,
                     equipment_type=r.equipment_type, minimum_hours=r.minimum_hours, standby_rate=r.standby_rate,
                     effective_from=r.effective_from, effective_to=r.effective_to)
            for r in db.scalars(select(HaulingRate).where(HaulingRate.organization_id == organization_id))]


def _shift_or_404(db: Session, organization_id: int, shift_id: int) -> TruckShift:
    shift = db.scalar(select(TruckShift).where(TruckShift.id == shift_id, TruckShift.organization_id == organization_id))
    if shift is None:
        raise HTTPException(404, "Truck shift not found")
    return shift


def _require_revision(shift: TruckShift, if_match: str | None) -> None:
    if if_match is None:
        raise HTTPException(428, "Reload and try again: this needs the version of the shift you were looking at.")
    if if_match.strip().strip('"') != str(shift.lock_version):
        raise HTTPException(409, {"code": "shift_changed",
                                  "message": "Someone else changed this shift while you had it open. Nothing was saved. Reload to see theirs."})


def _event(db: Session, shift: TruckShift, user: User, field: str, old, new) -> None:
    db.add(TruckShiftEvent(shift_id=shift.id, actor_user_id=user.id, field=field,
                           old_value=None if old is None else str(old), new_value=None if new is None else str(new)))


def _view(db: Session, shift: TruckShift, truck: Truck, hauler: Hauler, rates: list[HaulRate], codes: dict[int, str],
          people: dict[int, str], deliveries: list[Ticket]) -> dict:
    """A shift as a reviewer sees it: what the sheet says, what that works out to, the
    agreement and price that would apply (or are locked), and what doesn't add up."""
    flags, on_site, work, preview, choice = [], None, None, None, None
    try:
        on_site = hours_between(shift.start_at, shift.end_at, shift.unpaid_break_minutes)
        work = on_site - shift.standby_hours
        if work < 0:
            flags.append("Standby is longer than the time on site.")
            work = None
    except ShiftError as exc:
        flags.append(str(exc))
    if shift.status != "approved":
        choice = choose_rate(rates, hauler_id=shift.hauler_id, project_id=shift.project_id, equipment_type=truck.equipment_type,
                             on=shift.work_date)
        if work is not None:
            preview = price_shift(work, shift.standby_hours, choice.rate)
        if choice.reason:
            flags.append(choice.reason)
        elif preview and preview.reason:
            flags.append(preview.reason)
    if shift.claimed_hours is not None and on_site is not None and shift.claimed_hours != on_site:
        flags.append(f"The hauler claims {shift.claimed_hours} h; the time sheet's times give {on_site} h on site.")
    if not shift.field_signoff:
        flags.append("No field sign-off recorded from the time sheet.")
    tons = [t.tonnage for t in deliveries]
    all_tons = bool(deliveries) and all(v is not None for v in tons)
    linked_tons = sum(tons, Decimal(0)) if all_tons else None
    cost = shift.approved_cost if shift.status == "approved" else (preview.total if preview else None)
    return {
        "id": shift.id, "revision": str(shift.lock_version), "status": shift.status,
        "project_id": shift.project_id, "project": codes.get(shift.project_id), "work_date": shift.work_date.isoformat(),
        "truck_id": truck.id, "truck": truck.identifier, "equipment_type": truck.equipment_type,
        "hauler_id": hauler.id, "hauler": hauler.name, "driver_name": shift.driver_name,
        "start_at": shift.start_at.isoformat(timespec="minutes"), "end_at": shift.end_at.isoformat(timespec="minutes"),
        "unpaid_break_minutes": shift.unpaid_break_minutes, "standby_hours": _money(shift.standby_hours),
        "claimed_hours": _money(shift.claimed_hours), "loads": shift.loads, "field_signoff": shift.field_signoff,
        "cost_code": shift.cost_code, "notes": shift.notes,
        "on_site_hours": _money(on_site), "work_hours": _money(work),
        "rate": None if shift.status == "approved" or choice is None or choice.rate is None else {
            "id": choice.rate.id, "hourly_rate": _money(choice.rate.hourly_rate, "0.0001"),
            "standby_rate": _money(choice.rate.standby_rate, "0.0001"), "minimum_hours": _money(choice.rate.minimum_hours)},
        "cost_lines": [] if preview is None or shift.status == "approved" else [
            {"label": line.label, "hours": _money(line.hours), "rate": _money(line.rate, "0.0001"), "amount": _money(line.amount)}
            for line in preview.lines],
        "cost": _money(cost), "cost_is_locked": shift.status == "approved",
        "approved": None if shift.status != "approved" else {
            "work_hours": _money(shift.approved_work_hours), "standby_hours": _money(shift.approved_standby_hours),
            "billable_hours": _money(shift.approved_billable_hours), "hourly_rate": _money(shift.applied_hourly_rate, "0.0001"),
            "standby_rate": _money(shift.applied_standby_rate, "0.0001"), "minimum_hours": _money(shift.applied_minimum_hours),
            "by": people.get(shift.approved_by_user_id), "at": as_utc(shift.approved_at)},
        "flags": flags,
        "deliveries": {"count": len(deliveries), "tons": _money(linked_tons, "0.001"),
                       "ticket_ids": [t.id for t in deliveries],
                       # Only when every linked delivery is in tons and the cost is locked: never a guess.
                       "cost_per_ton": _money(shift.approved_cost / linked_tons) if shift.status == "approved" and linked_tons else None},
    }


def _views(db: Session, organization_id: int, shifts: list[TruckShift]) -> list[dict]:
    trucks = {t.id: t for t in db.scalars(select(Truck).where(Truck.organization_id == organization_id))}
    haulers = {h.id: h for h in db.scalars(select(Hauler).where(Hauler.organization_id == organization_id))}
    codes = dict(db.execute(select(Project.id, Project.code).where(Project.organization_id == organization_id)).all())
    people = names_by_id(db, organization_id)
    rates = _rates(db, organization_id)
    linked: dict[int, list[Ticket]] = {}
    ids = [s.id for s in shifts]
    if ids:
        for t in db.scalars(select(Ticket).where(Ticket.organization_id == organization_id, Ticket.truck_shift_id.in_(ids))):
            linked.setdefault(t.truck_shift_id, []).append(t)
    return [_view(db, s, trucks[s.truck_id], haulers[s.hauler_id], rates, codes, people, linked.get(s.id, [])) for s in shifts]


@router.get("/truck-shifts")
def list_shifts(project_id: int | None = None, hauler_id: int | None = None, truck_id: int | None = None,
                status: str | None = None, date_from: date | None = None, date_to: date | None = None,
                db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    query = select(TruckShift).where(TruckShift.organization_id == organization_id)
    for column, value in ((TruckShift.project_id, project_id), (TruckShift.hauler_id, hauler_id),
                          (TruckShift.truck_id, truck_id), (TruckShift.status, status)):
        if value is not None:
            query = query.where(column == value)
    if date_from:
        query = query.where(TruckShift.work_date >= date_from)
    if date_to:
        query = query.where(TruckShift.work_date <= date_to)
    shifts = db.scalars(query.order_by(TruckShift.work_date.desc(), TruckShift.id.desc()).limit(500)).all()
    views = _views(db, organization_id, list(shifts))
    approved = [v for v in views if v["status"] == "approved"]
    return {
        "shifts": views,
        "summary": {
            "shifts": len(views), "awaiting_approval": sum(1 for v in views if v["status"] == "needs_review"),
            "disputed": sum(1 for v in views if v["status"] == "disputed"),
            "approved_hours": _money(sum((Decimal(v["approved"]["billable_hours"]) for v in approved), Decimal(0))),
            "approved_cost": _money(sum((Decimal(v["cost"]) for v in approved), Decimal(0))),
            "loads": sum(v["loads"] or 0 for v in views),
            "with_flags": sum(1 for v in views if v["flags"] and v["status"] != "approved"),
        },
        "truncated": len(views) == 500,
    }


def _validated(db: Session, organization_id: int, body: ShiftIn) -> Truck:
    _project_or_404(db, organization_id, body.project_id)
    truck = db.scalar(select(Truck).where(Truck.id == body.truck_id, Truck.organization_id == organization_id))
    if truck is None:
        raise HTTPException(404, "Truck not found")
    try:
        on_site = hours_between(body.start_at, body.end_at, body.unpaid_break_minutes)
    except ShiftError as exc:
        raise HTTPException(422, str(exc)) from None
    if body.standby_hours > on_site:
        raise HTTPException(422, "Standby is longer than the time on site.")
    return truck


FIELDS = ("project_id", "truck_id", "start_at", "end_at", "unpaid_break_minutes", "standby_hours", "claimed_hours", "loads",
          "driver_name", "field_signoff", "cost_code", "notes")


@router.post("/truck-shifts", status_code=201)
def create_shift(body: ShiftIn, db: Session = Depends(get_db), user: User = Depends(require("trucking.edit"))):
    truck = _validated(db, user.organization_id, body)
    shift = TruckShift(organization_id=user.organization_id, hauler_id=truck.hauler_id, work_date=body.start_at.date(),
                       created_by_user_id=user.id, **body.model_dump())
    db.add(shift)
    db.flush()
    _event(db, shift, user, "created", None, f"{truck.identifier}, {body.start_at:%Y-%m-%d %H:%M} to {body.end_at:%Y-%m-%d %H:%M}")
    db.commit()
    return _views(db, user.organization_id, [shift])[0]


@router.put("/truck-shifts/{shift_id}")
def edit_shift(shift_id: int, body: ShiftIn, db: Session = Depends(get_db), user: User = Depends(require("trucking.edit")),
               if_match: str | None = Header(None)):
    take_turn(db, "truck-shift", user.organization_id, shift_id)
    shift = _shift_or_404(db, user.organization_id, shift_id)
    _require_revision(shift, if_match)
    if shift.status == "approved":
        raise HTTPException(409, "This shift is approved. A manager reopens it first, so its locked cost isn't changed by accident.")
    truck = _validated(db, user.organization_id, body)
    for name in FIELDS:
        new = getattr(body, name)
        if getattr(shift, name) != new:
            _event(db, shift, user, name, getattr(shift, name), new)
            setattr(shift, name, new)
    shift.hauler_id, shift.work_date = truck.hauler_id, body.start_at.date()
    shift.lock_version += 1
    db.commit()
    return _views(db, user.organization_id, [shift])[0]


@router.post("/truck-shifts/{shift_id}/approve")
def approve_shift(shift_id: int, body: ApproveIn, db: Session = Depends(get_db), user: User = Depends(require("trucking.approve")),
                  if_match: str | None = Header(None)):
    """Lock the hours this person approves, the agreement's rates, the cost, and where it goes."""
    organization_id = user.organization_id
    take_turn(db, "truck-shift", organization_id, shift_id)
    shift = _shift_or_404(db, organization_id, shift_id)
    _require_revision(shift, if_match)
    if shift.status == "approved":
        raise HTTPException(409, "This shift is already approved.")
    truck = db.get(Truck, shift.truck_id)
    on_site = hours_between(shift.start_at, shift.end_at, shift.unpaid_break_minutes)
    if body.work_hours + body.standby_hours > on_site:
        raise HTTPException(422, f"Approved hours ({body.work_hours + body.standby_hours}) are more than the time on site "
                                 f"on the time sheet ({on_site}). Correct the times first, so the record explains the hours.")
    choice = choose_rate(_rates(db, organization_id), hauler_id=shift.hauler_id, project_id=shift.project_id,
                         equipment_type=truck.equipment_type, on=shift.work_date)
    if choice.rate is None:
        raise HTTPException(422, choice.reason)
    cost = price_shift(body.work_hours, body.standby_hours, choice.rate)
    if cost.total is None:
        raise HTTPException(422, cost.reason)
    wanted = body.allocations or [AllocationIn(project_id=shift.project_id, cost_code=shift.cost_code, hours=cost.billable_hours)]
    if sum(a.hours for a in wanted) != cost.billable_hours:
        raise HTTPException(422, f"Allocations add up to {sum(a.hours for a in wanted)} h; the shift bills {cost.billable_hours} h.")
    for a in wanted:
        _project_or_404(db, organization_id, a.project_id)
    parts = allocate(cost.total, [a.hours for a in wanted])
    shift.status = "approved"
    shift.approved_work_hours, shift.approved_standby_hours = body.work_hours, body.standby_hours
    shift.approved_billable_hours, shift.approved_cost = cost.billable_hours, cost.total
    shift.applied_rate_id, shift.applied_hourly_rate = choice.rate.id, choice.rate.hourly_rate
    shift.applied_standby_rate, shift.applied_minimum_hours = choice.rate.standby_rate, choice.rate.minimum_hours
    shift.approved_by_user_id, shift.approved_at = user.id, datetime.now(timezone.utc)
    shift.lock_version += 1
    db.flush()
    for a, part in zip(wanted, parts):
        db.add(ShiftAllocation(shift_id=shift.id, project_id=a.project_id, cost_code=a.cost_code, hours=a.hours, cost=part))
    _event(db, shift, user, "status", "needs_review", "approved")
    _event(db, shift, user, "approved", None, f"{body.work_hours} h working, {body.standby_hours} h standby, "
                                               f"{cost.billable_hours} h billable at ${choice.rate.hourly_rate}/h: ${cost.total}")
    db.commit()
    return _views(db, organization_id, [shift])[0]


@router.post("/truck-shifts/{shift_id}/reopen")
def reopen_shift(shift_id: int, body: ReopenIn, db: Session = Depends(get_db), user: User = Depends(require("trucking.approve")),
                 if_match: str | None = Header(None)):
    """Withdraw an approval, with the reason. The history keeps what was approved."""
    take_turn(db, "truck-shift", user.organization_id, shift_id)
    shift = _shift_or_404(db, user.organization_id, shift_id)
    _require_revision(shift, if_match)
    if shift.status != "approved":
        raise HTTPException(409, "Only an approved shift can be reopened.")
    shift.status = "needs_review"  # first: the database allows the figures to change only once it's no longer approved
    db.flush()
    for name in ("approved_work_hours", "approved_standby_hours", "approved_billable_hours", "applied_rate_id",
                 "applied_hourly_rate", "applied_standby_rate", "applied_minimum_hours", "approved_cost",
                 "approved_by_user_id", "approved_at"):
        setattr(shift, name, None)
    for allocation in db.scalars(select(ShiftAllocation).where(ShiftAllocation.shift_id == shift.id)):
        db.delete(allocation)
    shift.lock_version += 1
    _event(db, shift, user, "status", "approved", "needs_review")
    _event(db, shift, user, "reopened", None, body.reason.strip())
    db.commit()
    return _views(db, user.organization_id, [shift])[0]


@router.put("/truck-shifts/{shift_id}/deliveries")
def link_deliveries(shift_id: int, body: DeliveriesIn, db: Session = Depends(get_db), user: User = Depends(require("trucking.edit"))):
    """The material deliveries this truck carried during the shift. Linking adds no cost:
    the shift's hourly cost is the shift's, however many loads it carried."""
    organization_id = user.organization_id
    take_turn(db, "truck-shift", organization_id, shift_id)
    shift = _shift_or_404(db, organization_id, shift_id)
    wanted = set(body.ticket_ids)
    found = {t.id: t for t in db.scalars(select(Ticket).where(Ticket.organization_id == organization_id, Ticket.id.in_(wanted)))} if wanted else {}
    missing = sorted(wanted - set(found))
    if missing:
        raise HTTPException(404, "; ".join(f"#{i}: no such ticket" for i in missing))
    elsewhere = [t for t in found.values() if t.truck_shift_id not in (None, shift.id)]
    if elsewhere:
        raise HTTPException(409, "Already linked to another shift: " + ", ".join(t.ticket_number or f"#{t.id}" for t in elsewhere))
    current = {t.id: t for t in db.scalars(select(Ticket).where(Ticket.truck_shift_id == shift.id))}
    for t in current.values():
        if t.id not in wanted:
            t.truck_shift_id = None
    for t in found.values():
        t.truck_shift_id = shift.id
    added, removed = sorted(wanted - set(current)), sorted(set(current) - wanted)
    if added or removed:
        _event(db, shift, user, "deliveries", ",".join(map(str, removed)) or None, ",".join(map(str, added)) or None)
    db.commit()
    return _views(db, organization_id, [shift])[0]


@router.get("/truck-shifts/{shift_id}/history")
def shift_history(shift_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    shift = _shift_or_404(db, organization_id, shift_id)
    people = names_by_id(db, organization_id)
    events = db.scalars(select(TruckShiftEvent).where(TruckShiftEvent.shift_id == shift.id).order_by(TruckShiftEvent.id))
    return [{"field": e.field, "old": e.old_value, "new": e.new_value, "at": as_utc(e.created_at),
             "actor": people.get(e.actor_user_id) if e.actor_user_id else None} for e in events]
