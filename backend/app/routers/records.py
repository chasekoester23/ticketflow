"""Searching, tidying and exporting delivery records at scale.

The register reads one page at a time, and its totals are computed separately,
so neither depends on how many rows a browser can draw. Bulk changes only touch
tickets nobody has approved: an approved quantity stays exactly as approved until
a person opens that one ticket.
"""
from __future__ import annotations

import csv
import difflib
from collections import Counter
from datetime import date
from decimal import Decimal
from io import StringIO
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..models import Batch, Material, Project, SavedView, Supplier, Ticket, User, as_utc
from ..permissions import has_permission, require
from ..schemas import TicketOut
from ..services import aggregates
from ..services import duplicates as dup_svc
from ..services import export as export_svc
from ..services import pricing as pricing_svc
from ..services.accounts import names_by_id
from ..services.pricing import norm
from .api import apply_review_edit, _out
from .reports import filtered_query

router = APIRouter(prefix="/api")

SORTS = {
    "date": Ticket.delivery_date, "ticket_number": Ticket.ticket_number, "tons": Ticket.tonnage,
    "supplier": Ticket.supplier, "material": Ticket.material_type, "status": Ticket.status, "job": Ticket.job_location,
}
# Applied once per batch to save typing. Ticket numbers and tons are per ticket,
# so bulk changes can't touch them.
BULK_FIELDS = ("supplier", "material_type", "job_location", "delivery_date")
BULK_EDITABLE = ("needs_review", "flagged")
MAX_BULK = 2000
MAX_VIEWS = 100
FILTER_KEYS = {"project_id", "supplier", "material", "status", "date_from", "date_to", "q"}


class RecordsSummary(BaseModel):
    records: int
    by_status: dict[str, int]
    verified_tons: Decimal
    verified_cost: Decimal
    verified_unpriced: int


class RecordsPage(BaseModel):
    rows: list[TicketOut]
    total: int
    page: int
    page_size: int
    summary: RecordsSummary


@router.get("/records/search", response_model=RecordsPage)
def search_records(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None, q: str | None = None,
    sort: Literal["date", "ticket_number", "tons", "supplier", "material", "status", "job"] = "date",
    direction: Literal["asc", "desc"] = "desc",
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id),
):
    filters = dict(organization_id=organization_id, project_id=project_id, supplier=supplier, material=material,
                   status=status, date_from=date_from, date_to=date_to, q=q)
    # Same filters as the rows and the exports, so these can never disagree.
    by_status = dict(db.execute(filtered_query(select(Ticket.status, func.count()), **filters)
                                .group_by(Ticket.status)).all())
    total = sum(by_status.values())
    totals = db.execute(filtered_query(select(
        aggregates.counted_tons().label("tons"), aggregates.counted_cost().label("cost"), aggregates.unpriced().label("unpriced")),
        **filters)).one()
    tons, cost = aggregates.as_decimal(totals.tons, 3), aggregates.as_decimal(totals.cost, 2)
    unpriced = int(totals.unpriced)

    # Blank values go last whichever way it's sorted, on every database.
    column = SORTS[sort]
    order = (column.asc().nulls_last(), Ticket.id.asc()) if direction == "asc" else (column.desc().nulls_last(), Ticket.id.desc())
    tickets = list(db.scalars(filtered_query(select(Ticket), **filters).order_by(*order)
                              .limit(page_size).offset((page - 1) * page_size)))
    table = pricing_svc.rate_table(db, organization_id)
    index = dup_svc.load_index(db, organization_id, tickets)
    names = pricing_svc.catalog_names(db, organization_id)
    people = names_by_id(db, organization_id)
    aliases = pricing_svc.alias_suggestions(db, organization_id, names)
    return RecordsPage(
        rows=[_out(db, t, organization_id, table, duplicate_index=index, catalog=names, people=people, aliases=aliases)
              for t in tickets],
        total=total, page=page, page_size=page_size,
        summary=RecordsSummary(records=total, by_status=by_status, verified_tons=tons, verified_cost=cost,
                               verified_unpriced=unpriced),
    )


def _columns(columns: str | None) -> list[str] | None:
    if not columns:
        return None
    keys = [key.strip() for key in columns.split(",") if key.strip()]
    unknown = [key for key in keys if key not in export_svc.COLUMNS]
    if unknown or not keys:
        raise HTTPException(422, "Unknown export column: " + ", ".join(unknown or ["(none)"]))
    return keys


@router.get("/records/export.csv")
def export_records_csv(
    project_id: int | None = None, supplier: str | None = None, material: str | None = None,
    status: str | None = None, date_from: date | None = None, date_to: date | None = None, q: str | None = None,
    columns: str | None = None, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id),
):
    """Exact decimal text, for import into an accounting system. Skipped tickets are left out."""
    keys = _columns(columns) or list(export_svc.DEFAULT_COLUMNS)
    table = pricing_svc.rate_table(db, organization_id)
    approvers = names_by_id(db, organization_id)
    query = filtered_query(export_svc.row_query(), organization_id=organization_id, project_id=project_id, supplier=supplier,
                           material=material, status=status, date_from=date_from, date_to=date_to, q=q)
    price_of = lambda t: pricing_svc.effective_price_for(t, table)[0]  # noqa: E731

    def stream():
        buffer = StringIO(newline="")
        writer = csv.writer(buffer)
        for number, row in enumerate(export_svc.csv_lines(export_svc.rows(db, query), keys, approvers, price_of), start=1):
            writer.writerow(row)
            if number % 1000 == 0:  # One network chunk per row would be far slower.
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()

    return StreamingResponse(stream(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="delivery-records.csv"', "Cache-Control": "no-store"})


class BulkEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_ids: list[int] = Field(min_length=1, max_length=MAX_BULK)
    field: Literal["supplier", "material_type", "job_location", "delivery_date"]
    value: str = Field(min_length=1, max_length=200)


def _bulk_value(field: str, value: str):
    value = value.strip()
    if not value:
        raise HTTPException(422, "Enter a value to apply.")
    if field == "delivery_date":
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise HTTPException(422, "Enter the date as YYYY-MM-DD.") from None
    return value


def _apply(db: Session, tickets: list[Ticket], field: str, value, user: User) -> dict:
    changed = unchanged = 0
    left: Counter = Counter()
    for ticket in tickets:
        if ticket.status not in BULK_EDITABLE:
            left[ticket.status] += 1
        elif apply_review_edit(db, ticket, user, field, value):
            changed += 1
        else:
            unchanged += 1
    db.commit()
    return {"changed": changed, "unchanged": unchanged, "left_alone": dict(left)}


@router.post("/tickets/bulk-edit")
def bulk_edit(body: BulkEditIn, db: Session = Depends(get_db), user: User = Depends(require("tickets.edit"))):
    """One value on many unapproved tickets, audited per ticket. Approved, skipped
    and failed tickets are left alone and reported. Fails closed on any unknown id."""
    wanted = set(body.ticket_ids)
    tickets = list(db.scalars(select(Ticket).join(Batch, Batch.id == Ticket.batch_id).where(
        Batch.organization_id == user.organization_id, Ticket.id.in_(wanted))))
    if len(tickets) != len(wanted):
        raise HTTPException(404, "Some of those tickets weren't found. Nothing was changed.")
    return _apply(db, tickets, body.field, _bulk_value(body.field, body.value), user)


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["supplier", "material_type"]
    from_value: str = Field(min_length=1, max_length=200)
    to_value: str = Field(min_length=1, max_length=200)
    project_id: int | None = None


@router.post("/records/rename")
def rename(body: RenameIn, db: Session = Depends(get_db), user: User = Depends(require("tickets.edit"))):
    """Change one exact spelling to a catalog name on unapproved tickets. Only
    catalog names are accepted, so a typo can't spread across hundreds of tickets."""
    model = Supplier if body.field == "supplier" else Material
    target = db.scalar(select(model.name).where(
        model.organization_id == user.organization_id, model.name == body.to_value.strip()))
    if target is None:
        raise HTTPException(422, "Choose a name from your supplier or material list.")
    query = select(Ticket).join(Batch, Batch.id == Ticket.batch_id).where(
        Batch.organization_id == user.organization_id, getattr(Ticket, body.field) == body.from_value)
    if body.project_id is not None:
        query = query.where(Batch.project_id == body.project_id)
    return _apply(db, list(db.scalars(query)), body.field, target, user)


@router.get("/records/name-variants")
def name_variants(field: Literal["supplier", "material"], project_id: int | None = None,
                  db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Every spelling on record, with whether your catalog knows it. A closest
    catalog name is only ever a suggestion for a person to accept."""
    column = Ticket.supplier if field == "supplier" else Ticket.material_type
    query = select(column, Ticket.status, func.count()).where(
        Ticket.organization_id == organization_id, column.is_not(None), Ticket.status != "skipped")
    if project_id is not None:
        query = query.where(Ticket.batch_id.in_(select(Batch.id).where(
            Batch.organization_id == organization_id, Batch.project_id == project_id)))
    totals: dict[str, dict[str, int]] = {}
    for value, status, count in db.execute(query.group_by(column, Ticket.status)):
        row = totals.setdefault(value, {"tickets": 0, "verified": 0})
        row["tickets"] += count
        row["verified"] += count if status == "verified" else 0

    model = Supplier if field == "supplier" else Material
    catalog = list(db.scalars(select(model.name).where(model.organization_id == organization_id).order_by(model.name)))
    by_key = {norm(name): name for name in catalog}
    aliases = pricing_svc.alias_suggestions(db, organization_id)
    rows = []
    for value, count in totals.items():
        key = norm(value)
        alias = aliases.get((field, key))
        if value in catalog:
            state, target = "catalog", value
        elif key in by_key:
            state, target = "same_name", by_key[key]
        elif alias:
            state, target = "alias", alias
        else:
            close = difflib.get_close_matches(key, list(by_key), n=1, cutoff=0.75)
            state, target = "unknown", by_key[close[0]] if close else None
        rows.append({"value": value, "state": state, "target": target, "tickets": count["tickets"],
                     "verified": count["verified"], "unverified": count["tickets"] - count["verified"]})
    order = {"unknown": 0, "alias": 1, "same_name": 2, "catalog": 3}
    rows.sort(key=lambda r: (order[r["state"]], -r["tickets"], r["value"]))
    return {"field": field, "catalog": catalog, "rows": rows}


class SavedViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    filters: dict[str, str] = {}
    columns: list[str] = []


def _view_out(view: SavedView, people: dict[int, str]) -> dict:
    return {"id": view.id, "name": view.name, "filters": view.filters, "columns": view.columns,
            "created_by": people.get(view.created_by_user_id), "created_at": as_utc(view.created_at)}


@router.get("/saved-views")
def list_views(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    people = names_by_id(db, organization_id)
    return [_view_out(v, people) for v in db.scalars(select(SavedView).where(
        SavedView.organization_id == organization_id).order_by(SavedView.name))]


@router.post("/saved-views", status_code=201)
def create_view(body: SavedViewIn, db: Session = Depends(get_db), user: User = Depends(require("records.read"))):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Give this view a name.")
    if set(body.filters) - FILTER_KEYS or any(len(v) > 200 for v in body.filters.values()):
        raise HTTPException(422, "A saved view can only hold the record filters.")
    if set(body.columns) - set(export_svc.COLUMNS):
        raise HTTPException(422, "A saved view can only hold known export columns.")
    if db.scalar(select(func.count()).select_from(SavedView).where(
            SavedView.organization_id == user.organization_id)) >= MAX_VIEWS:
        raise HTTPException(409, f"This company already has {MAX_VIEWS} saved views. Remove one first.")
    view = SavedView(organization_id=user.organization_id, name=name, filters=body.filters, columns=body.columns,
                     created_by_user_id=user.id)
    db.add(view)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A saved view with that name already exists.") from None
    return _view_out(view, names_by_id(db, user.organization_id))


@router.delete("/saved-views/{view_id}", status_code=204)
def delete_view(view_id: int, db: Session = Depends(get_db), user: User = Depends(require("records.read"))):
    view = db.scalar(select(SavedView).where(
        SavedView.id == view_id, SavedView.organization_id == user.organization_id))
    if view is None:
        raise HTTPException(404, "Saved view not found")
    if view.created_by_user_id != user.id and not has_permission(user, "projects.manage"):
        raise HTTPException(403, "Only the person who saved this view, or a manager, can remove it.")
    db.delete(view)
    db.commit()
