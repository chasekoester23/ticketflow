"""Import a supplier's delivery spreadsheet into the normal ticket review.

Upload (tickets.upload) -> map columns and preview (tickets.upload) -> import
(tickets.edit). Imported rows are ordinary unapproved tickets: a person reviews and
approves each one as usual, and approval prices and locks it as usual. Nothing here
approves, prices or totals anything. See docs/engineering/delivery-imports.md.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..db import get_db
from ..models import AuditEvent, Batch, DeliveryImport, Project, ScanEvent, Ticket, UploadedFile, User, as_utc
from ..permissions import require
from ..rate_limit import limited
from ..services import delivery_import as svc
from ..services import storage
from ..services.accounts import names_by_id

router = APIRouter(prefix="/api/delivery-imports")

MEDIA = {"csv": "text/csv; charset=utf-8", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _import_or_404(db: Session, organization_id: int, import_id: int, *, lock: bool = False) -> DeliveryImport:
    query = select(DeliveryImport).where(DeliveryImport.id == import_id, DeliveryImport.organization_id == organization_id)
    if lock:
        query = query.with_for_update()
    found = db.scalar(query)
    if found is None:
        raise HTTPException(404, "Import not found")
    return found


def _rows_of(imp: DeliveryImport) -> list[list]:
    try:
        data = storage.current().read(imp.storage_path)
    except OSError:
        raise HTTPException(409, "The stored spreadsheet is missing. Upload it again.") from None
    try:
        return svc.read_rows(imp.file_kind, data)
    except svc.SheetError as error:
        raise HTTPException(409, str(error)) from None


def _revision(imp: DeliveryImport) -> str:
    """Changes whenever the mapping does, so an import applies what the person previewed."""
    return hashlib.sha256(json.dumps([imp.header_row, imp.mapping, imp.defaults], sort_keys=True).encode()).hexdigest()[:16]


def _row_out(result: svc.RowResult) -> dict:
    v = result.values
    action = "problem" if result.errors else "duplicate" if result.duplicate else "import"
    return {
        "row": result.row, "action": action, "errors": result.errors, "warnings": result.warnings, "duplicate": result.duplicate,
        "ticket_number": v["ticket_number"], "delivery_date": v["delivery_date"].isoformat() if v["delivery_date"] else None,
        "material_type": v["material_type"], "quantity": None if v["tonnage"] is None else str(v["tonnage"]), "unit": v["unit"],
        "source_quantity": None if v["source_quantity"] is None else str(v["source_quantity"]), "source_unit": v["source_unit"],
        "supplier": v["supplier"], "job_location": v["job_location"], "truck_number": v["truck_number"],
    }


def _results(db: Session, imp: DeliveryImport, rows: list[list]) -> list[svc.RowResult]:
    results = svc.interpret_all(rows, imp.header_row - 1, imp.mapping or {}, imp.defaults or {})
    svc.mark_duplicates(db, imp.organization_id, results)
    return results


def _detail(db: Session, imp: DeliveryImport, *, rows: list[list] | None = None, results: list[svc.RowResult] | None = None) -> dict:
    project = db.scalar(select(Project).where(Project.id == imp.project_id, Project.organization_id == imp.organization_id)) \
        if imp.project_id else None
    out = {
        "id": imp.id, "original_name": imp.original_name, "file_kind": imp.file_kind, "status": imp.status,
        "header_row": imp.header_row, "rows": imp.rows, "project_id": imp.project_id,
        "project_code": project.code if project else None, "project_name": project.name if project else None,
        "mapping": imp.mapping, "defaults": imp.defaults, "revision": _revision(imp),
        "created_at": as_utc(imp.created_at), "committed_at": as_utc(imp.committed_at), "batch_id": imp.batch_id,
        "result": None if imp.status != "imported" else {
            "imported": imp.imported, "skipped_duplicates": imp.skipped_duplicates, "error_rows": imp.error_rows,
            "batch_id": imp.batch_id},
        "fields": [{"key": k, "label": svc.LABELS[k], "required": k in svc.REQUIRED} for k in svc.FIELDS],
        "limits": {"bytes": svc.MAX_BYTES, "rows": svc.MAX_ROWS, "columns": svc.MAX_COLUMNS},
    }
    if rows is None and imp.status != "imported":
        rows = _rows_of(imp)
    if rows is not None:
        header = imp.header_row - 1
        columns = svc.columns_of(rows, header)
        mapping, unit = svc.suggest(columns)
        data = [r for r in rows[header + 1:] if not svc._blank(r)]
        out.update(columns=columns, suggested={"mapping": mapping, "unit": unit},
                   sample=[[svc._text(v) for v in r[:len(columns)]] for r in data[:svc.SAMPLE_ROWS]],
                   header_candidates=[{"row": i + 1, "cells": [svc._text(v) for v in r[:8]]}
                                      for i, r in enumerate(rows[:svc.HEADER_SEARCH]) if not svc._blank(r)])
    if imp.status == "mapped":
        results = results if results is not None else _results(db, imp, rows)
        shown = [_row_out(r) for r in results]
        out["preview"] = {"rows": shown, "counts": {
            "rows": len(shown), "ready": sum(1 for r in shown if r["action"] == "import"),
            "problems": sum(1 for r in shown if r["errors"]),
            "duplicates_in_file": sum(1 for r in results if r.duplicate and r.duplicate["kind"] == "file"),
            "duplicates_existing": sum(1 for r in results if r.duplicate and r.duplicate["kind"] == "existing"),
            "warnings": sum(1 for r in shown if r["warnings"])}}
    return out


@router.post("", status_code=201, dependencies=[Depends(limited("uploads"))])
def upload_spreadsheet(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(require("tickets.upload"))):
    """Keep the original file and say what's in it: the header row, the columns, a few rows and a suggested mapping."""
    name = Path(file.filename or "").name or "deliveries"
    if len(name) > 255:
        raise HTTPException(400, "The file name is over 255 characters. Rename the file before uploading it.")
    data = file.file.read(svc.MAX_BYTES + 1)
    try:
        kind = svc.kind_of(name, data)
        rows = svc.read_rows(kind, data)
    except svc.SheetError as error:
        raise HTTPException(400, str(error)) from None
    header = svc.find_header_row(rows)
    data_rows = sum(1 for r in rows[header + 1:] if not svc._blank(r))
    if data_rows == 0:
        raise HTTPException(400, "The file has a header but no delivery rows under it.")
    store = storage.current()
    key = store.save(f"uploads/imports/{user.organization_id}/{uuid.uuid4().hex}.{kind}", data)
    try:
        imp = DeliveryImport(organization_id=user.organization_id, original_name=name, storage_path=key,
                             sha256=hashlib.sha256(data).hexdigest(), file_kind=kind, header_row=header + 1, rows=data_rows,
                             status="uploaded", created_by_user_id=user.id, imported=0, skipped_duplicates=0, error_rows=0)
        db.add(imp)
        db.commit()
    except BaseException:
        db.rollback()
        store.delete(key)
        raise
    return _detail(db, imp, rows=rows)


@router.get("")
def list_imports(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Every import, newest first, with what it did."""
    imports = list(db.scalars(select(DeliveryImport).where(DeliveryImport.organization_id == organization_id)
                              .order_by(DeliveryImport.id.desc()).limit(200)))
    projects = {p.id: p for p in db.scalars(select(Project).where(
        Project.organization_id == organization_id, Project.id.in_({i.project_id for i in imports if i.project_id})))}
    people = names_by_id(db, organization_id)
    return [{
        "id": i.id, "original_name": i.original_name, "file_kind": i.file_kind, "status": i.status, "rows": i.rows,
        "imported": i.imported, "skipped_duplicates": i.skipped_duplicates, "error_rows": i.error_rows,
        "batch_id": i.batch_id, "project_id": i.project_id,
        "project_code": projects[i.project_id].code if i.project_id in projects else None,
        "created_at": as_utc(i.created_at), "created_by": people.get(i.created_by_user_id),
        "committed_at": as_utc(i.committed_at), "committed_by": people.get(i.committed_by_user_id),
    } for i in imports]


@router.get("/{import_id}")
def get_import(import_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    return _detail(db, _import_or_404(db, organization_id, import_id))


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header_row: int | None = Field(default=None, ge=1, le=svc.HEADER_SEARCH)
    # {field: column index}. Fields left out, or null, aren't in the file.
    columns: dict[Literal[svc.FIELDS], int | None] = Field(default_factory=dict)  # type: ignore[valid-type]
    project_id: int | None = Field(default=None, gt=0)
    # For the whole file, when it isn't a column (or a row leaves the cell blank).
    supplier: str | None = Field(default=None, max_length=200)
    unit: Literal["ton", "cy", "load", "each", "gal", "lb"] | None = None
    job_location: str | None = Field(default=None, max_length=200)


@router.put("/{import_id}/mapping")
def map_columns(import_id: int, body: MappingIn, db: Session = Depends(get_db), user: User = Depends(require("tickets.upload"))):
    """Save which column is which, and preview every row exactly as it would be imported."""
    imp = _import_or_404(db, user.organization_id, import_id)
    if imp.status == "imported":
        raise HTTPException(409, "This spreadsheet has already been imported. Upload it again to import it differently.")
    if body.project_id is not None:
        project = db.scalar(select(Project).where(Project.id == body.project_id, Project.organization_id == user.organization_id))
        if project is None:
            raise HTTPException(404, "Project not found")
        if project.status != "active":
            raise HTTPException(409, "Project is archived; unarchive it to import tickets into it")
    rows = _rows_of(imp)
    header_row = body.header_row or imp.header_row
    if header_row > len(rows):
        raise HTTPException(422, "That header row is past the end of the file.")
    columns = svc.columns_of(rows, header_row - 1)
    mapping = {name: index for name, index in body.columns.items() if index is not None}
    if any(index < 0 or index >= len(columns) for index in mapping.values()):
        raise HTTPException(422, "A chosen column isn't in the file.")
    if len(set(mapping.values())) != len(mapping):
        raise HTTPException(422, "Each column can be used for one thing only.")
    missing = [svc.LABELS[name] for name in svc.REQUIRED if name not in mapping]
    if missing:
        raise HTTPException(422, "Choose the column for: " + ", ".join(missing) + ".")
    if "unit" not in mapping and body.unit is None:
        raise HTTPException(422, "Choose the unit for the whole file, or the column that says it.")
    defaults = {"supplier": (body.supplier or "").strip() or None, "unit": body.unit,
                "job_location": (body.job_location or "").strip() or None}
    claimed = db.execute(update(DeliveryImport).where(
        DeliveryImport.id == imp.id, DeliveryImport.organization_id == user.organization_id,
        DeliveryImport.status != "imported").values(
        header_row=header_row, mapping=mapping, defaults=defaults, project_id=body.project_id, status="mapped",
        rows=sum(1 for r in rows[header_row:] if not svc._blank(r))))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "This spreadsheet has just been imported.")
    db.commit()
    db.refresh(imp)
    return _detail(db, imp, rows=rows)


class CommitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Row number -> what to do with a duplicate. Every duplicate not listed is skipped.
    duplicates: dict[int, Literal["skip", "import"]] = Field(default_factory=dict, max_length=svc.MAX_ROWS)
    # Rows with a problem come in for a person to fix on the review screen, instead of being left out.
    include_problem_rows: bool = False
    # From the preview: refuse if the mapping changed since.
    revision: str | None = Field(default=None, max_length=32)


@router.post("/{import_id}/commit")
def commit_import(import_id: int, body: CommitIn, db: Session = Depends(get_db), user: User = Depends(require("tickets.edit"))):
    """Create one unapproved ticket per chosen row, in a new batch. Never approves anything."""
    organization_id = user.organization_id
    imp = _import_or_404(db, organization_id, import_id, lock=True)
    if imp.status == "imported":
        raise HTTPException(409, "This spreadsheet has already been imported.")
    if imp.status != "mapped":
        raise HTTPException(409, "Choose the columns and check the preview first.")
    if body.revision is not None and body.revision != _revision(imp):
        raise HTTPException(409, "The column choices changed since this preview. Check the preview again.")
    project = None
    if imp.project_id is not None:
        project = db.scalar(select(Project).where(Project.id == imp.project_id, Project.organization_id == organization_id))
        if project is None or project.status != "active":
            raise HTTPException(409, "The project chosen for this import is archived or gone. Choose another.")
    rows = _rows_of(imp)
    results = _results(db, imp, rows)
    chosen, skipped, left_out, duplicate_notes = [], 0, 0, {}
    for result in results:
        if result.errors and not body.include_problem_rows:
            left_out += 1
            continue
        if result.duplicate:
            if body.duplicates.get(result.row, "skip") != "import":
                skipped += 1
                continue
            duplicate_notes[result.row] = (f"imported alongside ticket {result.duplicate['ticket_id']}"
                                           if result.duplicate["kind"] == "existing"
                                           else f"imported alongside row {result.duplicate['row']} of this file")
        chosen.append(result)
    if not chosen:
        raise HTTPException(422, "Nothing to import: every row is a duplicate you chose to skip or has a problem.")
    # Taken now, in this transaction, so a second click or another person can't import it twice.
    claimed = db.execute(update(DeliveryImport).where(
        DeliveryImport.id == imp.id, DeliveryImport.organization_id == organization_id,
        DeliveryImport.status == "mapped").values(status="imported"))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "This spreadsheet has already been imported.")

    store = storage.current()
    folder = None
    try:
        batch = Batch(organization_id=organization_id, project_id=imp.project_id, layout="single", intake_mode="import",
                      status="ready_for_review", created_by_user_id=user.id, expected_ticket_count=len(chosen))
        db.add(batch)
        db.flush()
        folder = f"crops/{batch.id}"
        source = UploadedFile(batch_id=batch.id, original_name=imp.original_name, storage_path=imp.storage_path,
                              sha256=imp.sha256, status="ready_for_review")
        db.add(source)
        db.flush()
        columns = svc.columns_of(rows, imp.header_row - 1)
        tickets = []
        for result in chosen:
            # The row's picture stands where a scan's crop would: page = the file's row, one "crop" per row.
            image, box = svc.row_image(imp.original_name, imp.sha256, result.row, columns, result.cells)
            key = store.save(f"{folder}/import_r{result.row}.png", image)
            v = result.values
            ticket = Ticket(
                batch_id=batch.id, organization_id=organization_id, file_id=source.id, source_page=result.row, position=0,
                crop_box=box, crop_path=key, crop_sha256=svc.sha256_hex(image),
                ticket_number=v["ticket_number"], delivery_date=v["delivery_date"], material_type=v["material_type"],
                tonnage=v["tonnage"], unit=v["unit"], source_quantity=v["source_quantity"], source_unit=v["source_unit"],
                supplier=v["supplier"], job_location=v["job_location"], truck_number=v["truck_number"],
                flags=result.flags, status="needs_review", tonnage_confirmed=False, price_snapshot_state="pending")
            db.add(ticket)
            tickets.append((ticket, result))
        db.flush()
        for ticket, result in tickets:
            db.add(AuditEvent(ticket_id=ticket.id, actor_user_id=user.id, field="imported_from",
                              new_value=f"{imp.original_name}, row {result.row}"))
            if result.row in duplicate_notes:
                db.add(AuditEvent(ticket_id=ticket.id, actor_user_id=user.id, field="duplicate_import",
                                  new_value=duplicate_notes[result.row]))
        db.add(ScanEvent(batch_id=batch.id, actor_user_id=user.id, action="spreadsheet_imported",
                         new_value=f"{len(chosen)} rows from {imp.original_name}"[:100]))
        imp.batch_id, imp.imported, imp.skipped_duplicates, imp.error_rows = batch.id, len(chosen), skipped, left_out
        imp.committed_by_user_id, imp.committed_at = user.id, datetime.now(timezone.utc)
        db.commit()
    except BaseException:
        db.rollback()
        if folder is not None:
            store.delete_prefix(folder)
        raise
    db.refresh(imp)
    return _detail(db, imp)


@router.get("/{import_id}/file")
def original_file(import_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """The spreadsheet exactly as it was uploaded."""
    imp = _import_or_404(db, organization_id, import_id)
    served = storage.current().serve(imp.storage_path, MEDIA[imp.file_kind])
    if served is None:
        raise HTTPException(404, "The stored file is missing.")
    safe = "".join(ch if ch.isalnum() or ch in "-_. " else "_" for ch in imp.original_name)[:120] or f"import-{imp.id}"
    served.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return served
