from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Material, Supplier, SupplierRate

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


class RateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    supplier_id: int
    material_id: int
    unit_price: Decimal


def _dump(o, *fields):
    return {f: getattr(o, f) for f in fields}


@router.get("/suppliers")
def list_suppliers(db: Session = Depends(get_db)):
    return [_dump(s, "id", "name", "contact") for s in db.scalars(select(Supplier).order_by(Supplier.name))]


@router.post("/suppliers", status_code=201)
def create_supplier(body: SupplierIn, db: Session = Depends(get_db)):
    s = Supplier(name=body.name.strip(), contact=body.contact)
    db.add(s)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A supplier with that name already exists")
    return _dump(s, "id", "name", "contact")


@router.delete("/suppliers/{sid}", status_code=204)
def delete_supplier(sid: int, db: Session = Depends(get_db)):
    s = db.get(Supplier, sid)
    if not s:
        raise HTTPException(404, "Supplier not found")
    for r in db.scalars(select(SupplierRate).where(SupplierRate.supplier_id == sid)):
        db.delete(r)
    db.delete(s)
    db.commit()


@router.get("/materials")
def list_materials(db: Session = Depends(get_db)):
    return [_dump(m, "id", "name", "unit") for m in db.scalars(select(Material).order_by(Material.name))]


@router.post("/materials", status_code=201)
def create_material(body: MaterialIn, db: Session = Depends(get_db)):
    m = Material(name=body.name.strip(), unit=body.unit)
    db.add(m)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A material with that name already exists")
    return _dump(m, "id", "name", "unit")


@router.delete("/materials/{mid}", status_code=204)
def delete_material(mid: int, db: Session = Depends(get_db)):
    m = db.get(Material, mid)
    if not m:
        raise HTTPException(404, "Material not found")
    for r in db.scalars(select(SupplierRate).where(SupplierRate.material_id == mid)):
        db.delete(r)
    db.delete(m)
    db.commit()


@router.get("/rates", response_model=list[RateOut])
def list_rates(db: Session = Depends(get_db)):
    return db.scalars(select(SupplierRate)).all()


@router.put("/rates", response_model=RateOut)
def upsert_rate(body: RateIn, db: Session = Depends(get_db)):
    if not db.get(Supplier, body.supplier_id) or not db.get(Material, body.material_id):
        raise HTTPException(404, "Supplier or material not found")
    rate = db.scalar(select(SupplierRate).where(
        SupplierRate.supplier_id == body.supplier_id, SupplierRate.material_id == body.material_id))
    if rate:
        rate.unit_price = body.unit_price
    else:
        rate = SupplierRate(**body.model_dump())
        db.add(rate)
    db.commit()
    return rate


@router.delete("/rates/{rid}", status_code=204)
def delete_rate(rid: int, db: Session = Depends(get_db)):
    r = db.get(SupplierRate, rid)
    if not r:
        raise HTTPException(404, "Rate not found")
    db.delete(r)
    db.commit()
