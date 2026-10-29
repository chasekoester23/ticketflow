import uuid
import logging
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_db
from ..models import AuditEvent, Batch, Project, Ticket, UploadedFile
from ..schemas import BatchOut, TicketOut, TicketPatch
from ..services import export as export_svc
from ..services import duplicates as dup_svc
from ..services import pipeline
from ..services import pricing as pricing_svc
from ..services.extraction import ExtractionError, get_extractor
from ..services.validation import compute_flags

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

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
    """Process in a separate DB session; failures remain visible and retryable."""
    db = SessionLocal()
    try:
        try:
            extractor = get_extractor()
        except ExtractionError as exc:
            batch = db.get(Batch, batch_id)
            if batch is None:
                return
            for file in batch.files:
                if not only_failed or file.status in ("failed", "uploaded", "processing"):
                    file.status, file.error = "failed", str(exc)
            batch.status = "failed"
            db.commit()
            return
        pipeline.process_batch(db, batch_id, extractor, only_failed=only_failed)
    except Exception:
        logger.exception("Unexpected error while processing batch %s", batch_id)
        db.rollback()
        # An unexpected exception must never leave a batch stuck in processing
        # forever with no actionable retry path.
        batch = db.get(Batch, batch_id)
        if batch is not None:
            for file in batch.files:
                if file.status in ("uploaded", "processing"):
                    file.status = "failed"
                    file.error = "Processing interrupted unexpectedly. Please retry."
            batch.status = "failed"
            db.commit()
    finally:
        db.close()

@router.post("/batches", response_model=BatchOut)
async def create_batch(
    background: BackgroundTasks,
    layout: str = Form("single"),
    project_id: int | None = Form(None),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    if layout not in ("single", "grid_2x2"):
        raise HTTPException(422, "layout must be 'single' or 'grid_2x2'")
    if project_id is not None:
        project = db.get(Project, project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        if project.status != "active":
            raise HTTPException(409, "Project is archived; unarchive it to upload more tickets")
    # Validate the entire selection BEFORE creating a batch or storing any
    # files. Silently dropping an invalid file is dangerous: a contractor could
    # believe every delivery ticket in the scan was recorded.
    validated: list[tuple[UploadFile, str, str, str]] = []
    rejected: list[str] = []
    for up in files:
        original_name = Path(up.filename or "").name or "(unnamed file)"
        ext = Path(original_name).suffix.lower()
        data = await up.read(settings.max_upload_bytes + 1)
        if ext not in ALLOWED_EXT:
            rejected.append(f"{original_name}: unsupported file type (PDF, JPG, PNG only)")
        elif not data:
            rejected.append(f"{original_name}: file is empty")
        elif len(data) > settings.max_upload_bytes:
            rejected.append(f"{original_name}: exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB file limit")
        elif not _sniff_ok(data, ext):
            rejected.append(f"{original_name}: content does not match its file extension")
        else:
            validated.append((up, original_name, ext, pipeline.sha256_bytes(data)))
        await up.seek(0)

    if rejected:
        raise HTTPException(400, "Upload rejected; no files were processed. Remove or replace: " + "; ".join(rejected))
    if not validated:
        raise HTTPException(400, "No valid files were provided")

    batch = Batch(layout=layout, status="uploaded", project_id=project_id)
    upload_dir = None
    try:
        db.add(batch)
        db.flush()
        upload_dir = settings.storage_dir / "uploads" / str(batch.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        for up, original_name, ext, checksum in validated:
            internal = upload_dir / f"{uuid.uuid4().hex}{ext}"
            # Read each validated file individually so large multi-file
            # selections aren't retained in application memory simultaneously.
            internal.write_bytes(await up.read())
            db.add(UploadedFile(
                batch_id=batch.id, original_name=original_name,
                storage_path=str(internal), sha256=checksum,
            ))
        db.commit()
    except Exception:
        db.rollback()
        if upload_dir is not None:
            shutil.rmtree(upload_dir, ignore_errors=True)
        raise

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
    if batch.status != "failed":
        raise HTTPException(409, "Only failed batches can be retried; wait until processing finishes")
    if not any(f.status in ("failed", "uploaded", "processing") for f in batch.files):
        raise HTTPException(409, "No failed or interrupted files to retry")
    # Lock out repeated retry clicks before the background task is scheduled.
    batch.status = "processing"
    db.commit()
    background.add_task(_run_batch, batch_id, True)
    db.refresh(batch)
    return batch


@router.get("/batches/{batch_id}/tickets", response_model=list[TicketOut])
def list_tickets(batch_id: int, db: Session = Depends(get_db)):
    q = select(Ticket).where(Ticket.batch_id == batch_id).order_by(Ticket.file_id, Ticket.source_page, Ticket.position)
    table = pricing_svc.rate_table(db)
    index = dup_svc.load_index(db)
    names = pricing_svc.catalog_names(db)
    return [
        _out(db, ticket, table, duplicate_index=index, catalog=names)
        for ticket in db.scalars(q).all()
    ]


def _out(
    db: Session, t: Ticket, table=None, *,
    duplicate_index: dup_svc.DuplicateIndex | None = None,
    catalog: tuple[set[str], set[str]] | None = None,
) -> TicketOut:
    """Ticket plus pricing and duplicates, reusing per-list data when supplied."""
    table = table if table is not None else pricing_svc.rate_table(db)
    price, source = pricing_svc.effective_price_for(t, table)
    if t.status == "verified" and t.price_snapshot_state is None:
        notes = ["Legacy verified ticket: its historical rate is unknown. Enter the documented rate as an override, then re-verify to lock its cost."]
    elif t.status == "verified" and t.price_snapshot_state == "unpriced":
        notes = ["No rate was available at verification. Enter the documented rate as an override and re-verify this ticket."]
    elif price is None:
        notes = pricing_svc.unresolved_names(db, t, catalog) or ["No rate configured for this supplier and material"]
    else:
        notes = []
    out = TicketOut.model_validate(t)
    return out.model_copy(update=dict(
        unit_price=price, price_source=source, cost=pricing_svc.cost_for(t, price),
        duplicates=(duplicate_index.find(t) if duplicate_index is not None
                    else dup_svc.find_duplicates(db, t)),
        pricing_notes=notes,
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
    changed = False
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
        changed = True
        db.add(AuditEvent(ticket_id=t.id, field=field, old_value=None if old is None else str(old),
                          new_value=None if new is None else str(new)))
        setattr(t, field, new)
        if field == "tonnage":  # a changed quantity must be re-confirmed
            t.tonnage_confirmed = False
        if t.status == "verified":
            # Any changed field (including price) invalidates the previous approval.
            t.status = "needs_review"
            t.tonnage_confirmed = False
    if t.status == "failed" and changed:
        t.status, t.error = "needs_review", None  # manual entry recovers a failed extraction
    if changed:
        # Any edit invalidates a previously applied historical price; the
        # updated rate must be explicitly captured during re-verification.
        t.price_snapshot_state = "pending"
        t.applied_unit_price = None
        t.applied_price_source = None
    _refresh_flags(t)
    db.commit()
    return _out(db, t)


@router.post("/tickets/{ticket_id}/verify", response_model=TicketOut)
def verify_ticket(ticket_id: int, db: Session = Depends(get_db)):
    t = _ticket_or_404(db, ticket_id)
    # Validate the current values rather than trusting stale extraction flags.
    _refresh_flags(t)
    # Unusually high tonnage is a warning, not an absolute rejection: some
    # legitimate delivery tickets may be for a bulk shipment.
    blocking = [f for f in t.flags if not f.startswith("Tonnage unusually large")]
    if blocking:
        raise HTTPException(422, "Resolve ticket issues before verification: " + "; ".join(blocking))
    if t.status == "verified":
        if t.price_snapshot_state is None:
            raise HTTPException(409, "Historical unit price is unknown. Enter the documented rate as a ticket override and verify again.")
        if t.price_snapshot_state in ("priced", "unpriced"):
            return _out(db, t)  # repeated verify must not reprice an approved ticket

    price, source = pricing_svc.unit_price_for(t, pricing_svc.rate_table(db))
    db.add(AuditEvent(ticket_id=t.id, field="status", old_value=t.status, new_value="verified"))
    db.add(AuditEvent(
        ticket_id=t.id, field="verified_unit_price",
        old_value=None, new_value=None if price is None else str(price),
    ))
    t.applied_unit_price = price
    t.applied_price_source = source
    t.price_snapshot_state = "priced" if price is not None else "unpriced"
    t.status, t.tonnage_confirmed = "verified", True
    db.commit()
    return _out(db, t)


@router.post("/tickets/{ticket_id}/flag", response_model=TicketOut)
def flag_ticket(ticket_id: int, db: Session = Depends(get_db)):
    """Defer a questionable ticket without approving its quantity or cost."""
    t = _ticket_or_404(db, ticket_id)
    if t.status != "flagged":
        db.add(AuditEvent(ticket_id=t.id, field="status", old_value=t.status, new_value="flagged"))
    t.status = "flagged"
    t.tonnage_confirmed = False
    t.price_snapshot_state = "pending"
    t.applied_unit_price = None
    t.applied_price_source = None
    db.commit()
    return _out(db, t)


@router.post("/tickets/{ticket_id}/skip", response_model=TicketOut)
def skip_ticket(ticket_id: int, db: Session = Depends(get_db)):
    t = _ticket_or_404(db, ticket_id)
    db.add(AuditEvent(ticket_id=t.id, field="status", old_value=t.status, new_value="skipped"))
    t.status = "skipped"
    t.tonnage_confirmed = False
    t.price_snapshot_state = "pending"
    t.applied_unit_price = None
    t.applied_price_source = None
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
    prices = {t.id: p for t in tickets if (p := pricing_svc.effective_price_for(t, table)[0]) is not None}
    data = export_svc.build_workbook(list(tickets), prices)
    return Response(
        data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="delivery-records-batch-{batch_id}.xlsx"'})
