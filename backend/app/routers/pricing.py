from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..permissions import require
from ..models import Material, NameAlias, PricingEvent, Supplier, SupplierRate, User, as_utc, utcnow
from ..services.accounts import names_by_id
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


def _dump(o, *fields):
    return {f: getattr(o, f) for f in fields}


PRICE_PLACES = Decimal("0.0001")


def _price(value: Decimal | None) -> str | None:
    return None if value is None else str(Decimal(value).quantize(PRICE_PLACES))


def _event(db: Session, user: User, action: str, *, supplier: str | None = None,
           material: str | None = None, old: Decimal | None = None, new: Decimal | None = None) -> None:
    """Record a pricing change in the caller's transaction (rolled back with it)."""
    db.add(PricingEvent(
        organization_id=user.organization_id, actor_user_id=user.id, action=action,
        supplier_name=supplier, material_name=material, old_value=_price(old), new_value=_price(new),
    ))


def _remove_rates(db: Session, user: User, rates) -> None:
    for r in rates:
        _event(db, user, "rate_removed", supplier=r.supplier.name, material=r.material.name, old=r.unit_price)
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


@router.post("/suppliers", status_code=201)
def create_supplier(body: SupplierIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
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


@router.put("/rates", response_model=RateOut)
def upsert_rate(body: RateIn, db: Session = Depends(get_db), user: User = Depends(require("pricing.manage"))):
    organization_id = user.organization_id
    supplier = db.scalar(select(Supplier).where(
        Supplier.id == body.supplier_id, Supplier.organization_id == organization_id))
    material = db.scalar(select(Material).where(
        Material.id == body.material_id, Material.organization_id == organization_id))
    if supplier is None or material is None:
        raise HTTPException(404, "Supplier or material not found")
    rate = db.scalar(select(SupplierRate).where(
        SupplierRate.supplier_id == body.supplier_id,
        SupplierRate.material_id == body.material_id,
        SupplierRate.organization_id == organization_id))
    if rate:
        if rate.unit_price != body.unit_price:
            _event(db, user, "rate_changed", supplier=supplier.name, material=material.name,
                   old=rate.unit_price, new=body.unit_price)
            rate.unit_price = body.unit_price
    else:
        rate = SupplierRate(organization_id=organization_id, **body.model_dump())
        db.add(rate)
        _event(db, user, "rate_set", supplier=supplier.name, material=material.name, new=body.unit_price)
    db.commit()
    return rate


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
         "old": e.old_value, "new": e.new_value, "at": as_utc(e.created_at),
         "actor": people.get(e.actor_user_id) if e.actor_user_id else None}
        for e in events
    ]
