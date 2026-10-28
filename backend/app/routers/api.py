import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_db
from ..models import AuditEvent, Batch, Ticket, UploadedFile
from ..schemas import BatchOut, TicketOut, TicketPatch
from ..services import export as export_svc
from ..services import duplicates as dup_svc
from ..services import pipeline
from ..services import pricing as pricing_svc
from ..services.extraction import ExtractionError, get_extractor
from ..services.validation import compute_flags

router = APIRouter(prefix="/api")

ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
MAGIC = {b"%PDF": ".pdf", b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png"}
EDITABLE = ["ticket_number", "delivery_date", "material_type", "tonnage", "supplier", "job_location"]


def _sniff_ok(data: bytes, ext: str) -> bool:
    """Check real content, not just the filename."""
    for magic, kind in MAGIC.items():
        if data.startswith(magic):
            return kind == ext or (kind == ".jpg" and ext == ".jpeg")
    return False


def _run_batch(batch_id: int, only_failed: bool = False) -> None:
    """Background job: own DB session; extractor errors are recorded per ticket, config errors per file."""
    db = SessionLocal()
    try:
        try:
            extractor = get_extractor()
        except ExtractionError as exc:
            batch = db.get(Batch, batch_id)
            for f in batch.files:
                f.status, f.error = "failed", str(exc)
            batch.status = "failed"
            db.commit()
            return
        pipeline.process_batch(db, batch_id, extractor, only_failed=only_failed)
    finally:
        db.close()


@router.post("/batches", response_model=BatchOut)
async def create_batch(
    background: BackgroundTasks,
    layout: str = Form("single"),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    if layout not in ("single", "grid_2x2"):
        raise HTTPException(422, "layout must be 'single' or 'grid_2x2'")
    batch = Batch(layout=layout, status="uploaded")
    db.add(batch)
    db.flush()
    upload_dir = settings.storage_dir / "uploads" / str(batch.id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    rejected = []
    for up in files:
        ext = Path(up.filename or "").suffix.lower()
        data = await up.read(settings.max_upload_bytes + 1)
        if ext not in ALLOWED_EXT:
            rejected.append(f"{up.filename}: unsupported file type")
        elif not data:
            rejected.append(f"{up.filename}: empty file")
        elif len(data) > settings.max_upload_bytes:
            rejected.append(f"{up.filename}: file too large")
        elif not _sniff_ok(data, ext):
            rejected.append(f"{up.filename}: content does not match its extension")
        else:
            internal = upload_dir / f"{uuid.uuid4().hex}{ext}"  # never use the user's filename on disk
            internal.write_bytes(data)
            batch.files.append(UploadedFile(
                original_name=Path(up.filename).name, storage_path=str(internal), sha256=pipeline.sha256_bytes(data)))
    if not batch.files:
        db.rollback()
        raise HTTPException(400, "No valid files uploaded: " + "; ".join(rejected))
    db.commit()
    background.add_task(_run_batch, batch.id)
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=list[BatchOut])
def list_batches(db: Session = Depends(get_db)):
    return db.scalars(select(Batch).order_by(Batch.id.desc())).all()


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return batch


@router.post("/batches/{batch_id}/retry", response_model=BatchOut)
def retry_batch(batch_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    background.add_task(_run_batch, batch_id, True)
    return batch


@router.get("/batches/{batch_id}/tickets", response_model=list[TicketOut])
def list_tickets(batch_id: int, db: Session = Depends(get_db)):
    q = select(Ticket).where(Ticket.batch_id == batch_id).order_by(Ticket.file_id, Ticket.source_page, Ticket.position)
    table = pricing_svc.rate_table(db)
    return [_out(db, t, table) for t in db.scalars(q).all()]


def _out(db: Session, t: Ticket, table=None) -> TicketOut:
    """Ticket plus computed price, cost, duplicate and pricing notes."""
    table = table if table is not None else pricing_svc.rate_table(db)
    price, source = pricing_svc.unit_price_for(t, table)
    out = TicketOut.model_validate(t)
    return out.model_copy(update=dict(
        unit_price=price, price_source=source, cost=pricing_svc.cost_for(t, price),
        duplicates=dup_svc.find_duplicates(db, t),
        pricing_notes=[] if price is not None else (pricing_svc.unresolved_names(db, t) or ["No rate configured for this supplier and material"]),
    ))


def _ticket_or_404(db: Session, ticket_id: int) -> Ticket:
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    return t


def _refresh_flags(t: Ticket) -> None:
    t.flags = compute_flags(
        ticket_number=t.ticket_number, delivery_date=t.delivery_date, material_type=t.material_type,
        tonnage=t.tonnage, supplier=t.supplier, job_location=t.job_location, raw_date="x" if t.delivery_date else None)


@router.patch("/tickets/{ticket_id}", response_model=TicketOut)
def patch_ticket(ticket_id: int, patch: TicketPatch, db: Session = Depends(get_db)):
    t = _ticket_or_404(db, ticket_id)
    changes = patch.model_dump(exclude_unset=True)
    for field, new in changes.items():
        if isinstance(new, str):
            new = new.strip() or None
        if field == "tonnage" and new is not None:
            new = new.quantize(Decimal("0.001"))  # same precision as stored values
        if field == "unit_price_override" and new is not None:
            new = new.quantize(Decimal("0.0001"))
        old = getattr(t, field)
        if old == new:
            continue
        db.add(AuditEvent(ticket_id=t.id, field=field, old_value=None if old is None else str(old),
                          new_value=None if new is None else str(new)))
        setattr(t, field, new)
        if field == "tonnage":  # a changed quantity must be re-confirmed
            t.tonnage_confirmed = False
        if t.status == "verified":
            t.status = "needs_review"
    if t.status == "failed" and changes:
        t.status, t.error = "needs_review", None  # manual entry recovers a failed extraction
    _refresh_flags(t)
    db.commit()
    return _out(db, t)


@router.post("/tickets/{ticket_id}/verify", response_model=TicketOut)
def verify_ticket(ticket_id: int, db: Session = Depends(get_db)):
    t = _ticket_or_404(db, ticket_id)
    if t.tonnage is None or t.tonnage <= 0:
        raise HTTPException(422, "A positive tonnage is required before verifying")
    if not t.ticket_number:
        raise HTTPException(422, "A ticket number is required before verifying")
    db.add(AuditEvent(ticket_id=t.id, field="status", old_value=t.status, new_value="verified"))
    t.status, t.tonnage_confirmed = "verified", True
    db.commit()
    return _out(db, t)


@router.post("/tickets/{ticket_id}/skip", response_model=TicketOut)
def skip_ticket(ticket_id: int, db: Session = Depends(get_db)):
    t = _ticket_or_404(db, ticket_id)
    db.add(AuditEvent(ticket_id=t.id, field="status", old_value=t.status, new_value="skipped"))
    t.status = "skipped"
    db.commit()
    return _out(db, t)


@router.get("/tickets/{ticket_id}/image")
def ticket_image(ticket_id: int, db: Session = Depends(get_db)):
    t = _ticket_or_404(db, ticket_id)
    return FileResponse(t.crop_path, media_type="image/png")


@router.get("/tickets/{ticket_id}/audit")
def ticket_audit(ticket_id: int, db: Session = Depends(get_db)):
    events = db.scalars(select(AuditEvent).where(AuditEvent.ticket_id == ticket_id).order_by(AuditEvent.id)).all()
    return [{"field": e.field, "old": e.old_value, "new": e.new_value, "at": e.created_at} for e in events]


@router.get("/batches/{batch_id}/export.xlsx")
def export_batch(batch_id: int, db: Session = Depends(get_db)):
    if not db.get(Batch, batch_id):
        raise HTTPException(404, "Batch not found")
    tickets = db.scalars(
        select(Ticket).where(Ticket.batch_id == batch_id).order_by(Ticket.file_id, Ticket.source_page, Ticket.position)).all()
    table = pricing_svc.rate_table(db)
    prices = {t.id: p for t in tickets if (p := pricing_svc.unit_price_for(t, table)[0]) is not None}
    data = export_svc.build_workbook(list(tickets), prices)
    return Response(
        data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="delivery-records-batch-{batch_id}.xlsx"'})
