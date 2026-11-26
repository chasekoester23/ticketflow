from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..permissions import require
from ..models import AuditEvent, Batch, Material, NameAlias, PricingEvent, Project, Supplier, SupplierRate, Ticket, User, as_utc, utcnow
from ..services.accounts import names_by_id
from ..services import rate_sheet
from ..services.locks import take_turn
from ..services import pricing as pricing_svc
from ..services import units
from ..services.pricing import norm

router = APIRouter(prefix="/api")


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    contact: str | None = None


class MaterialIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    unit: str = "ton"


class RateIn(BaseModel):
    supplier_id: int
    material_id: int
    unit_price: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    # Blank: company-wide. Set: this project's agreement, preferred for its tickets.
    project_id: int | None = Field(default=None, gt=0)
    # Blank: no start date. Set: applies to deliveries on or after this date.
    effective_from: date | None = None
    # Blank: open-ended. Set: the last day the price is valid (a quote's expiry).
    effective_to: date | None = None
    # Does the price include trucking? "included": no hauling charge is added for deliveries
    # it prices. "separate" or blank (not stated): a hauler's per-ton or per-load rate applies.
    freight: Literal["included", "separate"] | None = None
    # What the price is per. It prices only deliveries measured in the same unit. Blank: per ton.
    unit: Literal["ton", "cy", "load", "each", "gal"] | None = None

    @model_validator(mode="after")
    def _ends_after_it_starts(self):
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("The rate's last day is before its first day.")
        return self


class AliasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["supplier", "material"]
    alias: str = Field(min_length=1, max_length=200)
    target_id: int


class RateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    supplier_id: int
    material_id: int
    unit_price: Decimal
    project_id: int | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    freight: str | None = None
    unit: str = "ton"


def _dump(o, *fields):
    return {f: getattr(o, f) for f in fields}


PRICE_PLACES = Decimal("0.0001")


def _price(value: Decimal | None) -> str | None:
    return None if value is None else str(Decimal(value).quantize(PRICE_PLACES))


def _event(db: Session, user: User, action: str, *, supplier: str | None = None,
           material: str | None = None, old: Decimal | None = None, new: Decimal | None = None,
           scope: str | None = None) -> None:
    """Record a pricing change in the caller's transaction (rolled back with it)."""
    db.add(PricingEvent(
        organization_id=user.organization_id, actor_user_id=user.id, action=action,
        supplier_name=supplier, material_name=material, old_value=_price(old), new_value=_price(new), scope=scope,
    ))


def _scope_text(db: Session, project_id: int | None, effective_from: date | None) -> str:
    """How history names a rate's scope: snapshotted text, readable after a rename."""
    where = "Company-wide"
    if project_id is not None:
        project = db.get(Project, project_id)
        where = f"Project {project.code} ({project.name})" if project else f"Project #{project_id}"
    return where + (f", from {effective_from.isoformat()}" if effective_from else "")


def _project_or_404(db: Session, organization_id: int, project_id: int | None) -> None:
    if project_id is not None and db.scalar(select(Project.id).where(
            Project.id == project_id, Project.organization_id == organization_id)) is None:
        raise HTTPException(404, "Project not found")


def _same_scope(project_id: int | None, effective_from: date | None):
    return ((SupplierRate.project_id.is_(None) if project_id is None else SupplierRate.project_id == project_id),
            (SupplierRate.effective_from.is_(None) if effective_from is None else SupplierRate.effective_from == effective_from))


def _remove_rates(db: Session, user: User, rates) -> None:
    for r in rates:
        _event(db, user, "rate_removed", supplier=r.supplier.name, material=r.material.name, old=r.unit_price,
               scope=_scope_text(db, r.project_id, r.effective_from))
        db.delete(r)


@router.get("/suppliers")
def list_suppliers(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    return [_dump(s, "id", "name", "contact") for s in db.scalars(select(Supplier).where(Supplier.organization_id == organization_id).order_by(Supplier.name))]


def _end_aliases(db: Session, user: User, kind: str, target_key: str) -> None:
    """A removed catalog entry takes its aliases with it; the rows stay as history."""
    for alias in db.scalars(select(NameAlias).where(
        NameAlias.organization_id == user.organization_id, NameAlias.kind == kind,
        NameAlias.target_key == target_key, NameAlias.removed_at.is_(None),
    )):
        alias.removed_at, alias.removed_by_user_id = utcnow(), user.id


def _name_is_an_alias(db: Session, organization_id: int, kind: str, name: str) -> bool:
    """One spelling must not mean two things in the same company."""
    return db.scalar(select(NameAlias.id).where(
        NameAlias.organization_id == organization_id, NameAlias.kind == kind,
        NameAlias.alias_key == norm(name), NameAlias.removed_at.is_(None),
    ).limit(1)) is not None


def _same_catalog_name(db: Session, model, organization_id: int, name: str) -> str | None:
    """Rates are found by normalized name, so 'Sand' and 'SAND' would be one
    name with two prices. Returns the existing spelling, if there is one."""
    key = norm(name)
    return next((existing for existing in db.scalars(select(model.name).where(model.organization_id == organization_id))
                 if norm(existing) == key), None)


def _refuse_same_name(db: Session, model, organization_id: int, name: str, kind: str) -> None:
    existing = _same_catalog_name(db, model, organization_id, name)
    if existing is None:
        return
    if existing == name.strip():
        raise HTTPException(409, f"A {kind} with that name already exists")
    raise HTTPException(409, f"'{existing}' is already in the catalog, and tickets can't tell it apart from "
                             f"'{name.strip()}'. Use '{existing}' instead.")


@router.post("/suppliers", status_code=201)
def create_supplier(body: SupplierIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    _refuse_same_name(db, Supplier, user.organization_id, body.name, "supplier")
    if _name_is_an_alias(db, user.organization_id, "supplier", body.name):
        raise HTTPException(409, "That name is already saved as another spelling of a supplier. Remove the spelling first.")
    s = Supplier(organization_id=user.organization_id, name=body.name.strip(), contact=body.contact)
    db.add(s)
    _event(db, user, "supplier_added", supplier=s.name)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A supplier with that name already exists")
    return _dump(s, "id", "name", "contact")


@router.delete("/suppliers/{sid}", status_code=204)
def delete_supplier(sid: int, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    organization_id = user.organization_id
    s = db.scalar(select(Supplier).where(Supplier.id == sid, Supplier.organization_id == organization_id))
    if not s:
        raise HTTPException(404, "Supplier not found")
    _remove_rates(db, user, db.scalars(select(SupplierRate).where(
        SupplierRate.supplier_id == sid, SupplierRate.organization_id == organization_id)))
    _event(db, user, "supplier_removed", supplier=s.name)
    _end_aliases(db, user, "supplier", norm(s.name))
    db.delete(s)
    db.commit()


@router.get("/materials")
def list_materials(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    return [_dump(m, "id", "name", "unit") for m in db.scalars(select(Material).where(Material.organization_id == organization_id).order_by(Material.name))]


@router.post("/materials", status_code=201)
def create_material(body: MaterialIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    _refuse_same_name(db, Material, user.organization_id, body.name, "material")
    if _name_is_an_alias(db, user.organization_id, "material", body.name):
        raise HTTPException(409, "That name is already saved as another spelling of a material. Remove the spelling first.")
    m = Material(organization_id=user.organization_id, name=body.name.strip(), unit=body.unit)
    db.add(m)
    _event(db, user, "material_added", material=m.name)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A material with that name already exists")
    return _dump(m, "id", "name", "unit")


@router.delete("/materials/{mid}", status_code=204)
def delete_material(mid: int, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    organization_id = user.organization_id
    m = db.scalar(select(Material).where(Material.id == mid, Material.organization_id == organization_id))
    if not m:
        raise HTTPException(404, "Material not found")
    _remove_rates(db, user, db.scalars(select(SupplierRate).where(
        SupplierRate.material_id == mid, SupplierRate.organization_id == organization_id)))
    _event(db, user, "material_removed", material=m.name)
    _end_aliases(db, user, "material", norm(m.name))
    db.delete(m)
    db.commit()


@router.get("/name-aliases")
def list_aliases(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    people = names_by_id(db, organization_id)
    rows = db.scalars(select(NameAlias).where(NameAlias.organization_id == organization_id).order_by(NameAlias.id.desc()))
    return [{"id": a.id, "kind": a.kind, "alias": a.alias_text, "target": a.target_name,
             "added_by": people.get(a.created_by_user_id), "added_at": as_utc(a.created_at),
             "removed_by": people.get(a.removed_by_user_id), "removed_at": as_utc(a.removed_at)} for a in rows]


@router.post("/name-aliases", status_code=201)
def add_alias(body: AliasIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    organization_id = user.organization_id
    model = Supplier if body.kind == "supplier" else Material
    target = db.scalar(select(model).where(model.id == body.target_id, model.organization_id == organization_id))
    if target is None:
        raise HTTPException(404, f"{body.kind.title()} not found")
    text, key = body.alias.strip(), norm(body.alias)
    if not key:
        raise HTTPException(422, "Enter the spelling as it appears on tickets.")
    names = db.scalars(select(model.name).where(model.organization_id == organization_id))
    if any(norm(name) == key for name in names):
        raise HTTPException(409, f"That is already a {body.kind} name in your list, so no alias is needed.")
    if _name_is_an_alias(db, organization_id, body.kind, text):
        raise HTTPException(409, "That spelling is already saved. Remove it first to change what it means.")
    alias = NameAlias(organization_id=organization_id, kind=body.kind, alias_text=text, alias_key=key,
                      target_name=target.name, target_key=norm(target.name), created_by_user_id=user.id)
    db.add(alias)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That spelling is already saved.") from None
    return {"id": alias.id, "kind": alias.kind, "alias": alias.alias_text, "target": alias.target_name}


@router.delete("/name-aliases/{alias_id}", status_code=204)
def remove_alias(alias_id: int, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    alias = db.scalar(select(NameAlias).where(
        NameAlias.id == alias_id, NameAlias.organization_id == user.organization_id))
    if alias is None:
        raise HTTPException(404, "Alias not found")
    if alias.removed_at is None:
        alias.removed_at, alias.removed_by_user_id = utcnow(), user.id
        db.commit()


@router.get("/rates", response_model=list[RateOut])
def list_rates(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    return db.scalars(select(SupplierRate).where(SupplierRate.organization_id == organization_id)).all()


PREVIEW_ROWS = 200


EXPIRY_WARNING_DAYS = 30


@router.get("/rates/timeline")
def rates_timeline(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Each supplier and material's rates in date order, per scope, with the gaps,
    overlaps and expiries a person should know about, and how many tickets waiting for
    approval can't be priced because of them. Explains the rule; never changes a price."""
    from datetime import timedelta
    today = date.today()
    codes = dict(db.execute(select(Project.id, Project.code).where(Project.organization_id == organization_id)).all())
    rows = db.execute(select(SupplierRate, Supplier.name, Material.name)
                      .join(Supplier, Supplier.id == SupplierRate.supplier_id).join(Material, Material.id == SupplierRate.material_id)
                      .where(SupplierRate.organization_id == organization_id)).all()
    pairs: dict[tuple[str, str], dict] = {}
    for rate, supplier, material in rows:
        pair = pairs.setdefault((supplier, material), {"supplier": supplier, "material": material, "scopes": {}})
        pair["scopes"].setdefault(rate.project_id, []).append(rate)
    book = pricing_svc.rate_table(db, organization_id)
    waiting = db.scalars(select(Ticket).where(Ticket.organization_id == organization_id, Ticket.status.in_(("needs_review", "flagged")),
                                              Ticket.unit_price_override.is_(None)))
    blocked: dict[tuple[str, str], int] = {}
    for t in waiting:
        reason = book.for_ticket(t).reason
        if reason and "ended on" in reason:
            key = (norm(t.supplier), norm(t.material_type))
            blocked[key] = blocked.get(key, 0) + 1
    out = []
    for (supplier, material), pair in sorted(pairs.items()):
        scopes = []
        for project_id, rates in sorted(pair["scopes"].items(), key=lambda kv: (kv[0] is not None, codes.get(kv[0], ""))):
            rates.sort(key=lambda r: r.effective_from or date.min)
            segments, warnings = [], []
            label = "Company rate" if project_id is None else f"{codes.get(project_id, project_id)} agreement"
            for n, rate in enumerate(rates):
                following = rates[n + 1] if n + 1 < len(rates) else None
                ends = rate.effective_to
                if following is not None:
                    day_before = following.effective_from - timedelta(days=1)
                    if rate.effective_to is not None and rate.effective_to >= following.effective_from:
                        warnings.append({"kind": "overlap", "text": f"{label}: the rate from {_day(rate.effective_from)} runs to "
                                         f"{rate.effective_to.isoformat()}, but the next starts {following.effective_from.isoformat()}. "
                                         "The later one applies from its start."})
                    if ends is None or ends > day_before:
                        ends = day_before
                    elif ends < day_before:
                        warnings.append({"kind": "gap", "text": f"{label}: no price from {(ends + timedelta(days=1)).isoformat()} "
                                         f"to {day_before.isoformat()}. Tickets delivered then stay unpriced."})
                elif rate.effective_to is not None:
                    fallback = " The company rate is not used instead." if project_id is not None else ""
                    if rate.effective_to < today:
                        warnings.append({"kind": "expired", "text": f"{label}: expired {rate.effective_to.isoformat()}. "
                                         f"Tickets delivered after that stay unpriced until a new rate is set.{fallback}"})
                    elif rate.effective_to <= today + timedelta(days=EXPIRY_WARNING_DAYS):
                        warnings.append({"kind": "expiring", "text": f"{label}: expires {rate.effective_to.isoformat()}. "
                                         f"Ask the supplier for the next quote.{fallback}"})
                starts = rate.effective_from
                segments.append({
                    "rate_id": rate.id, "price": _price(rate.unit_price), "from": starts.isoformat() if starts else None,
                    "to": ends.isoformat() if ends else None, "declared_to": rate.effective_to.isoformat() if rate.effective_to else None,
                    "state": "past" if ends is not None and ends < today else "future" if starts is not None and starts > today else "current",
                })
            scopes.append({"project_id": project_id, "project": codes.get(project_id), "label": label,
                           "segments": segments, "warnings": warnings})
        out.append({"supplier": supplier, "material": material, "scopes": scopes,
                    "waiting_unpriced": blocked.get((norm(supplier), norm(material)), 0),
                    "warnings": sum(len(scope["warnings"]) for scope in scopes)})
    return {"today": today.isoformat(), "pairs": out}


def _day(value: date | None) -> str:
    return value.isoformat() if value else "the start"


@router.post("/rates/preview")
def preview_rate(body: RateIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    """What saving this rate would do to tickets not yet approved: which would change
    price, from what to what. Saves nothing. Approved tickets keep their locked price
    whatever happens, so they're never in this list."""
    organization_id = user.organization_id
    supplier = db.scalar(select(Supplier).where(Supplier.id == body.supplier_id, Supplier.organization_id == organization_id))
    material = db.scalar(select(Material).where(Material.id == body.material_id, Material.organization_id == organization_id))
    if supplier is None or material is None:
        raise HTTPException(404, "Supplier or material not found")
    _project_or_404(db, organization_id, body.project_id)
    current = pricing_svc.rate_table(db, organization_id)
    key = (norm(supplier.name), norm(material.name))
    same = [r for r in current.rows.get(key, []) if r.project_id == body.project_id and r.effective_from == body.effective_from]
    proposed_rows = [r for r in current.rows.get(key, []) if r not in same]
    proposed_rows.append(pricing_svc.RateRow(body.unit_price, body.project_id, body.effective_from, body.effective_to,
                                             unit=body.unit or (same[0].unit if same else units.DEFAULT)))
    projects_by_batch = current.projects_by_batch
    if body.project_id is not None and not projects_by_batch:
        projects_by_batch = dict(db.execute(select(Batch.id, Batch.project_id).where(Batch.organization_id == organization_id)).all())
    proposed = pricing_svc.RateBook({**current.rows, key: proposed_rows}, projects_by_batch)
    current = pricing_svc.RateBook(current.rows, projects_by_batch)
    tickets = db.scalars(select(Ticket).where(
        Ticket.organization_id == organization_id, Ticket.status.in_(("needs_review", "flagged")),
        Ticket.supplier_key == key[0], Ticket.material_key == key[1], Ticket.unit_price_override.is_(None)))
    codes = dict(db.execute(select(Project.id, Project.code).where(Project.organization_id == organization_id)).all())
    changed = []
    for t in tickets:
        before = current.for_ticket(t).price
        after = proposed.for_ticket(t).price
        if before == after:
            continue
        changed.append({
            "ticket_id": t.id, "batch_id": t.batch_id, "ticket_number": t.ticket_number,
            "delivery_date": t.delivery_date.isoformat() if t.delivery_date else None,
            "project": codes.get(proposed.project_of(t)), "tons": format(t.tonnage, ".3f") if t.tonnage is not None else None,
            "price_now": _price(before), "price_after": _price(after),
            "cost_change": str(pricing_svc.to_cents(t.tonnage * ((after or Decimal(0)) - (before or Decimal(0)))))
            if t.tonnage is not None and before is not None and after is not None else None,
        })
    changed.sort(key=lambda row: (row["delivery_date"] or "", row["ticket_id"]))
    total = sum((Decimal(row["cost_change"]) for row in changed if row["cost_change"] is not None), Decimal(0))
    return {"scope": _scope_text(db, body.project_id, body.effective_from), "tickets": len(changed),
            "newly_priced": sum(1 for row in changed if row["price_now"] is None),
            "cost_change": str(total), "rows": changed[:PREVIEW_ROWS], "truncated": len(changed) > PREVIEW_ROWS,
            "note": "Tickets not yet approved only. Approved tickets keep the price they were approved at."}


@router.put("/rates", response_model=RateOut)
def upsert_rate(body: RateIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    """Set the price for one supplier and material in one scope: company-wide or one
    project, from a date or with no start date. Locked prices are never touched."""
    organization_id = user.organization_id
    supplier = db.scalar(select(Supplier).where(
        Supplier.id == body.supplier_id, Supplier.organization_id == organization_id))
    material = db.scalar(select(Material).where(
        Material.id == body.material_id, Material.organization_id == organization_id))
    if supplier is None or material is None:
        raise HTTPException(404, "Supplier or material not found")
    _project_or_404(db, organization_id, body.project_id)
    take_turn(db, "rate", organization_id, body.supplier_id, body.material_id)
    rate = db.scalar(select(SupplierRate).where(
        SupplierRate.supplier_id == body.supplier_id,
        SupplierRate.material_id == body.material_id,
        SupplierRate.organization_id == organization_id,
        *_same_scope(body.project_id, body.effective_from)))
    scope = _scope_text(db, body.project_id, body.effective_from)
    if rate:
        if rate.unit_price != body.unit_price:
            _event(db, user, "rate_changed", supplier=supplier.name, material=material.name,
                   old=rate.unit_price, new=body.unit_price, scope=scope)
            rate.unit_price = body.unit_price
        _set_expiry(db, user, rate, body.effective_to, supplier.name, material.name, scope)
        if "freight" in body.model_fields_set and rate.freight != body.freight:
            # Changes which deliveries carry a hauling charge from now on; approved ones keep theirs.
            db.add(PricingEvent(organization_id=organization_id, actor_user_id=user.id, action="freight_changed",
                                supplier_name=supplier.name, material_name=material.name, scope=scope,
                                old_value=rate.freight or "not stated", new_value=body.freight or "not stated"))
            rate.freight = body.freight
        if body.unit is not None and rate.unit != body.unit:
            # Which deliveries it can price from now on; approved ones keep their locked price.
            db.add(PricingEvent(organization_id=organization_id, actor_user_id=user.id, action="unit_changed",
                                supplier_name=supplier.name, material_name=material.name, scope=scope,
                                old_value=f"per {units.singular(rate.unit)}", new_value=f"per {units.singular(body.unit)}"))
            rate.unit = body.unit
    else:
        rate = SupplierRate(organization_id=organization_id, **body.model_dump(exclude={"unit"}), unit=body.unit or units.DEFAULT)
        db.add(rate)
        _event(db, user, "rate_set", supplier=supplier.name, material=material.name, new=body.unit_price,
               scope=scope + (f", until {body.effective_to.isoformat()}" if body.effective_to else ""))
    db.commit()
    return rate


def _set_expiry(db: Session, user: User, rate: SupplierRate, effective_to: date | None, supplier: str, material: str, scope: str) -> None:
    """A quote's expiry changes which tickets it can price, so it's history like a price."""
    if rate.effective_to == effective_to:
        return
    if effective_to is not None and rate.effective_from is not None and effective_to < rate.effective_from:
        raise HTTPException(422, "The rate's last day is before its first day.")
    db.add(PricingEvent(organization_id=user.organization_id, actor_user_id=user.id, action="rate_validity_changed",
                        supplier_name=supplier, material_name=material, scope=scope,
                        old_value=rate.effective_to.isoformat() if rate.effective_to else "open-ended",
                        new_value=effective_to.isoformat() if effective_to else "open-ended"))
    rate.effective_to = effective_to


class RateSheetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500_000)
    # False previews the plan and saves nothing.
    apply: bool = False
    # A supplier's quote for one job, from a date: every row of the sheet gets this scope.
    project_id: int | None = Field(default=None, gt=0)
    effective_from: date | None = None
    # The quote's expiry, for every row. Blank: open-ended.
    effective_to: date | None = None

    @model_validator(mode="after")
    def _ends_after_it_starts(self):
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("The quote's last day is before its first day.")
        return self


@router.post("/rates/import")
def import_rate_sheet(body: RateSheetIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    """Paste a rate sheet, see exactly what it would change, then apply it. Locked
    prices on approved tickets are never touched, and every change is recorded."""
    organization_id = user.organization_id
    if body.apply:
        take_turn(db, "rate-sheet", organization_id)
    try:
        rows = rate_sheet.parse(body.text)
    except rate_sheet.RateSheetError as exc:
        raise HTTPException(422, str(exc)) from None
    suppliers = {norm(s.name): s for s in db.scalars(select(Supplier).where(Supplier.organization_id == organization_id))}
    materials = {norm(m.name): m for m in db.scalars(select(Material).where(Material.organization_id == organization_id))}
    _project_or_404(db, organization_id, body.project_id)
    existing = {(r.supplier_id, r.material_id): r for r in db.scalars(select(SupplierRate).where(
        SupplierRate.organization_id == organization_id, *_same_scope(body.project_id, body.effective_from)))}
    scope = _scope_text(db, body.project_id, body.effective_from)
    problems = []
    seen_pairs: dict[tuple[str, str], int] = {}
    for row in rows:
        for kind, name, known in (("supplier", row.supplier, suppliers), ("material", row.material, materials)):
            if not norm(name):
                problems.append(f"line {row.line}: '{name}' isn't a usable name")
            elif norm(name) not in known and _name_is_an_alias(db, organization_id, kind, name):
                problems.append(f"line {row.line}: '{name}' is saved as another spelling of a {kind}, so use the catalog name")
        pair = (norm(row.supplier), norm(row.material))
        if pair in seen_pairs:
            problems.append(f"line {row.line} is the same supplier and material as line {seen_pairs[pair]}")
        seen_pairs[pair] = row.line
    if problems:
        raise HTTPException(422, "Rate sheet needs fixes: " + "; ".join(problems[:8]) + ". Nothing was saved.")

    plan, new_suppliers, new_materials = [], {}, {}
    for row in rows:
        supplier, material = suppliers.get(norm(row.supplier)), materials.get(norm(row.material))
        if supplier is None:
            new_suppliers.setdefault(norm(row.supplier), row.supplier)
        if material is None:
            new_materials.setdefault(norm(row.material), row.material)
        rate = existing.get((supplier.id, material.id)) if supplier and material else None
        # A different expiry is a change too: pasting a sheet with none would end an expiry.
        action = "add" if rate is None else "unchanged" if rate.unit_price == row.price and rate.effective_to == body.effective_to else "change"
        plan.append({"line": row.line, "supplier": supplier.name if supplier else row.supplier,
                     "material": material.name if material else row.material, "price": str(row.price),
                     "old_price": str(rate.unit_price) if rate else None, "action": action,
                     "new_supplier": supplier is None, "new_material": material is None})
    summary = {"plan": plan, "new_suppliers": sorted(new_suppliers.values()), "new_materials": sorted(new_materials.values()),
               "adds": sum(p["action"] == "add" for p in plan), "changes": sum(p["action"] == "change" for p in plan),
               "unchanged": sum(p["action"] == "unchanged" for p in plan), "applied": False, "scope": scope}
    if not body.apply:
        return summary

    for key, name in new_suppliers.items():
        suppliers[key] = Supplier(organization_id=organization_id, name=name)
        db.add(suppliers[key])
        _event(db, user, "supplier_added", supplier=name)
    for key, name in new_materials.items():
        materials[key] = Material(organization_id=organization_id, name=name)
        db.add(materials[key])
        _event(db, user, "material_added", material=name)
    db.flush()
    for row in rows:
        supplier, material = suppliers[norm(row.supplier)], materials[norm(row.material)]
        rate = existing.get((supplier.id, material.id))
        if rate is None:
            db.add(SupplierRate(organization_id=organization_id, supplier_id=supplier.id, material_id=material.id, unit_price=row.price,
                                project_id=body.project_id, effective_from=body.effective_from, effective_to=body.effective_to))
            _event(db, user, "rate_set", supplier=supplier.name, material=material.name, new=row.price,
                   scope=scope + (f", until {body.effective_to.isoformat()}" if body.effective_to else ""))
        else:
            if rate.unit_price != row.price:
                _event(db, user, "rate_changed", supplier=supplier.name, material=material.name, old=rate.unit_price, new=row.price, scope=scope)
                rate.unit_price = row.price
            _set_expiry(db, user, rate, body.effective_to, supplier.name, material.name, scope)
    db.commit()
    return {**summary, "applied": True}


@router.delete("/rates/{rid}", status_code=204)
def delete_rate(rid: int, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    r = db.scalar(select(SupplierRate).where(
        SupplierRate.id == rid, SupplierRate.organization_id == user.organization_id
    ))
    if not r:
        raise HTTPException(404, "Rate not found")
    _remove_rates(db, user, [r])
    db.commit()


@router.get("/pricing/history")
def pricing_history(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Newest 200 supplier/material/rate changes in this company.

    Readable by every role: anyone relying on a ticket's cost can see when and
    by whom the rate behind it changed. Changes before this history existed
    were not recorded and are not reconstructed.
    """
    people = names_by_id(db, organization_id)
    events = db.scalars(
        select(PricingEvent).where(PricingEvent.organization_id == organization_id)
        .order_by(PricingEvent.id.desc()).limit(200)
    ).all()
    return [
        {"id": e.id, "action": e.action, "supplier": e.supplier_name, "material": e.material_name,
         "old": e.old_value, "new": e.new_value, "scope": e.scope, "at": as_utc(e.created_at),
         "actor": people.get(e.actor_user_id) if e.actor_user_id else None}
        for e in events
    ]


class ApplyRatesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # False: say what would happen and change nothing. True: price them and record it.
    apply: bool = False


@router.post("/rates/apply-to-unpriced")
def apply_rates_to_unpriced(body: ApplyRatesIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    """Price approved deliveries that were approved before their rate existed.

    A contractor can approve a first batch before entering contract rates. Those
    tickets are counted as "no rate" (never $0). Once the rates are entered, this
    prices each one with the rate that applies to it on its delivery date, in its unit,
    exactly as approval would have. It never changes a price already locked, never
    touches an override or a legacy ticket, and records each change in the ticket's
    history. A ticket whose new rate includes freight while hauling was already charged
    on it is left for a person: re-approving settles which one pays.
    """
    organization_id = user.organization_id
    table = pricing_svc.rate_table(db, organization_id)
    tickets = list(db.scalars(select(Ticket).where(
        Ticket.organization_id == organization_id, Ticket.status == "verified", Ticket.tonnage_confirmed.is_(True),
        Ticket.price_snapshot_state == "unpriced", Ticket.unit_price_override.is_(None)).order_by(Ticket.id)))
    priced, left = [], []
    total = Decimal(0)
    for t in tickets:
        found = table.for_ticket(t)
        if found.price is None or t.tonnage is None:
            left.append({"ticket_id": t.id, "ticket_number": t.ticket_number,
                         "reason": found.reason or "No rate on file for this supplier and material"})
            continue
        if found.freight == "included" and t.haul_billing in ("per_ton", "per_load"):
            left.append({"ticket_id": t.id, "ticket_number": t.ticket_number,
                         "reason": "The rate includes freight, but hauling was already charged on this delivery. "
                                   "Withdraw and re-approve it to settle which one applies."})
            continue
        cost = pricing_svc.to_cents(t.tonnage * found.price)
        total += cost
        priced.append({"ticket_id": t.id, "batch_id": t.batch_id, "ticket_number": t.ticket_number,
                       "delivery_date": t.delivery_date.isoformat() if t.delivery_date else None,
                       "price": str(found.price), "cost": str(cost)})
        if body.apply:
            db.add(AuditEvent(ticket_id=t.id, actor_user_id=user.id, field="verified_unit_price", old_value=None,
                              new_value=f"{found.price} (rate entered after approval)"))
            t.applied_unit_price, t.applied_price_source = found.price, found.source
            t.price_snapshot_state, t.approved_cost = "priced", cost
    if body.apply and priced:
        db.add(PricingEvent(organization_id=organization_id, actor_user_id=user.id, action="rates_applied_after_approval",
                            old_value=None, new_value=f"{len(priced)} approved deliveries priced, ${total}"))
        db.commit()
    return {"applied": body.apply, "priced": len(priced), "cost": str(total), "rows": priced[:200],
            "still_unpriced": len(left), "left": left[:200]}
