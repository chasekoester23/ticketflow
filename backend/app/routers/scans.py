"""Scan check: reconcile scanned pages and slots against tickets on record."""
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..config import settings
from ..db import get_db
from ..models import AuditEvent, Batch, ScanEvent, ScanSlot, Ticket, UploadedFile, User, as_utc
from ..permissions import require
from ..services import pipeline, scan_manifest, splitter, storage
from ..services.accounts import names_by_id

router = APIRouter(prefix="/api")

_BUSY = "This batch is still processing. Wait until it finishes."


class ExpectedCount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_ticket_count: int | None = Field(ge=0, le=100_000)


class Rotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Clockwise from the original scan; 0 restores it.
    rotation: int


def _batch_or_404(db: Session, batch_id: int, organization_id: int) -> Batch:
    batch = db.scalar(select(Batch).where(Batch.id == batch_id, Batch.organization_id == organization_id))
    if batch is None:
        raise HTTPException(404, "Batch not found")
    return batch


def _slot_or_404(db: Session, slot_id: int, organization_id: int) -> tuple[ScanSlot, Batch]:
    row = db.execute(
        select(ScanSlot, Batch).join(Batch, Batch.id == ScanSlot.batch_id).where(
            ScanSlot.id == slot_id, Batch.organization_id == organization_id)
    ).first()
    if row is None:
        raise HTTPException(404, "Scan slot not found")
    return row[0], row[1]


def _ticket_at(db: Session, slot: ScanSlot) -> Ticket | None:
    return db.scalar(select(Ticket).where(
        Ticket.file_id == slot.file_id, Ticket.source_page == slot.page, Ticket.position == slot.position))


def _require_idle(batch: Batch) -> None:
    if batch.status in ("uploaded", "processing"):
        raise HTTPException(409, _BUSY)


@router.get("/batches/{batch_id}/manifest")
def get_manifest(batch_id: int, db: Session = Depends(get_db),
                 organization_id: int = Depends(current_organization_id)):
    return scan_manifest.build_manifest(db, _batch_or_404(db, batch_id, organization_id))


@router.get("/batches/{batch_id}/scan-events")
def scan_events(batch_id: int, db: Session = Depends(get_db),
                organization_id: int = Depends(current_organization_id)):
    _batch_or_404(db, batch_id, organization_id)
    people = names_by_id(db, organization_id)
    events = db.scalars(select(ScanEvent).where(ScanEvent.batch_id == batch_id).order_by(ScanEvent.id))
    return [{"action": e.action, "slot_id": e.slot_id, "old": e.old_value, "new": e.new_value,
             "at": as_utc(e.created_at),
             "actor": people.get(e.actor_user_id) if e.actor_user_id else None} for e in events]


@router.put("/batches/{batch_id}/expected-count")
def set_expected_count(batch_id: int, body: ExpectedCount, db: Session = Depends(get_db),
                       user: User = Depends(require("tickets.upload"))):
    batch = _batch_or_404(db, batch_id, user.organization_id)
    if batch.expected_ticket_count != body.expected_ticket_count:
        scan_manifest.record_event(
            db, batch.id, user.id, "expected_count",
            old=None if batch.expected_ticket_count is None else str(batch.expected_ticket_count),
            new=None if body.expected_ticket_count is None else str(body.expected_ticket_count))
        batch.expected_ticket_count = body.expected_ticket_count
        db.commit()
    return scan_manifest.build_manifest(db, batch)


@router.post("/batches/{batch_id}/manifest/rebuild")
def rebuild_manifest(batch_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require("tickets.upload"))):
    """Build slots for older batches. Adds slot records only; tickets are untouched."""
    batch = _batch_or_404(db, batch_id, user.organization_id)
    _require_idle(batch)
    created = 0
    for file in batch.files:
        try:
            created += scan_manifest.rebuild_for_file(db, file)
        except (splitter.PdfError, OSError):
            db.rollback()
            raise HTTPException(409, f"The stored scan '{file.original_name}' could not be read. "
                                "Nothing was changed.") from None
    if created:
        scan_manifest.record_event(db, batch.id, user.id, "manifest_built", new=f"{created} slots")
    db.commit()
    return scan_manifest.build_manifest(db, batch)


@router.get("/scan-slots/{slot_id}/image")
def slot_image(slot_id: int, db: Session = Depends(get_db),
               organization_id: int = Depends(current_organization_id)):
    slot, _ = _slot_or_404(db, slot_id, organization_id)
    image = storage.current().serve(storage.rotated_key(slot.crop_path, slot.rotation), "image/png")
    if image is None:
        raise HTTPException(404, "The stored image for this slot is missing.")
    return image


@router.get("/files/{file_id}/pages/{page_no}/image")
def page_image(file_id: int, page_no: int, db: Session = Depends(get_db),
               organization_id: int = Depends(current_organization_id)):
    """The whole original page, rendered on request from the stored upload."""
    file = db.scalar(select(UploadedFile).join(Batch, Batch.id == UploadedFile.batch_id).where(
        UploadedFile.id == file_id, Batch.organization_id == organization_id))
    if file is None:
        raise HTTPException(404, "File not found")
    try:
        data = storage.current().read(file.storage_path)
        if file.original_name.lower().endswith(".pdf"):
            image = splitter.render_pdf_page(data, page_no, max_page_pixels=settings.max_rendered_page_pixels)
        elif page_no == 1:
            image = splitter.load_image(data, max_pixels=settings.max_image_pixels)
        else:
            raise splitter.PdfError("That page is not available.")
    except (splitter.PdfError, OSError):
        raise HTTPException(404, "That page is not available.") from None
    image.thumbnail((1100, 1100))
    stream = io.BytesIO()
    image.convert("RGB").save(stream, format="JPEG", quality=80)
    return Response(stream.getvalue(), media_type="image/jpeg")


@router.post("/scan-slots/{slot_id}/mark-ticket")
def mark_ticket(slot_id: int, db: Session = Depends(get_db),
                user: User = Depends(require("scans.resolve"))):
    """A person says this slot is a real ticket: creates an unapproved record.

    Makes no AI call. In an AI batch the record is left unread and the existing
    Retry action reads it; neighbouring tickets are never reprocessed.
    """
    slot, batch = _slot_or_404(db, slot_id, user.organization_id)
    _require_idle(batch)
    if _ticket_at(db, slot) is not None:
        return scan_manifest.build_manifest(db, batch)  # already a record: repeatable
    previous = slot.decision
    # Atomic claim, so double-clicks and concurrent requests create one ticket.
    claimed = db.execute(update(ScanSlot).where(
        ScanSlot.id == slot.id,
        (ScanSlot.decision.is_(None)) | (ScanSlot.decision != "real_ticket"),
    ).values(decision="real_ticket", decided_by_user_id=user.id, decided_at=scan_manifest.utcnow()))
    if claimed.rowcount != 1:
        db.rollback()
        return scan_manifest.build_manifest(db, batch)
    ticket = Ticket(
        batch_id=slot.batch_id, file_id=slot.file_id, source_page=slot.page, position=slot.position,
        crop_box=slot.crop_box, crop_path=storage.reference_for(storage.rotated_key(slot.crop_path, slot.rotation)),
        crop_sha256=slot.crop_sha256,
    )
    pipeline.set_blank_fields(ticket)
    if batch.intake_mode != "manual":
        ticket.status = "failed"
        ticket.error = "Marked as a real ticket by a person; not read yet."
        ticket.flags = ["Not read yet - retry the batch or enter it by hand"]
        file = db.get(UploadedFile, slot.file_id)
        file.status, file.error = "failed", scan_manifest.UNREAD_MARKED_ERROR
    db.add(ticket)
    db.flush()
    db.add(AuditEvent(ticket_id=ticket.id, actor_user_id=user.id, field="created_from_scan_slot",
                      old_value=None, new_value=f"page {slot.page}, slot {slot.position + 1}"))
    scan_manifest.record_event(db, batch.id, user.id, "marked_ticket", slot_id=slot.id,
                               old=previous, new="real_ticket")
    db.flush()
    scan_manifest.settle_statuses(db, batch)
    db.commit()
    return scan_manifest.build_manifest(db, batch)


@router.post("/scan-slots/{slot_id}/confirm-blank")
def confirm_blank(slot_id: int, db: Session = Depends(get_db),
                  user: User = Depends(require("scans.confirm_blank"))):
    slot, batch = _slot_or_404(db, slot_id, user.organization_id)
    _require_idle(batch)
    if _ticket_at(db, slot) is not None:
        raise HTTPException(409, "This slot has a ticket record. Skip the ticket instead, so its history is kept.")
    if slot.detection == "content":
        raise HTTPException(409, "This slot has clear content and cannot be confirmed blank.")
    if slot.decision != "confirmed_blank":
        scan_manifest.record_event(db, batch.id, user.id, "confirmed_blank", slot_id=slot.id,
                                   old=slot.decision, new="confirmed_blank")
        slot.decision = "confirmed_blank"
        slot.decided_by_user_id = user.id
        slot.decided_at = scan_manifest.utcnow()
        db.flush()
        scan_manifest.settle_statuses(db, batch)
        db.commit()
    return scan_manifest.build_manifest(db, batch)


@router.post("/scan-slots/{slot_id}/rotate")
def rotate_slot(slot_id: int, body: Rotation, db: Session = Depends(get_db),
                user: User = Depends(require("scans.resolve"))):
    """Set orientation as a derived image; the original crop is always kept.

    Approved tickets are refused: what the approver saw must stay what is
    stored. Withdraw the approval first, which is audited. Values already on
    the ticket are kept as they are, since a person may have entered them.
    """
    if body.rotation not in (0, 90, 180, 270):
        raise HTTPException(422, "Rotation must be 0, 90, 180 or 270 degrees.")
    slot, batch = _slot_or_404(db, slot_id, user.organization_id)
    _require_idle(batch)
    ticket = _ticket_at(db, slot)
    if ticket is not None and ticket.status == "verified":
        raise HTTPException(409, "This ticket is approved. Withdraw the approval before changing its image.")
    if body.rotation == slot.rotation:
        return scan_manifest.build_manifest(db, batch)
    store = storage.current()
    if not store.exists(slot.crop_path):
        raise HTTPException(409, "The stored image for this slot is missing.")
    target = storage.reference_for(storage.rotated_key(slot.crop_path, body.rotation))
    if body.rotation:
        with Image.open(io.BytesIO(store.read(slot.crop_path))) as image:
            store.save_png(target, scan_manifest.rotated(image.convert("RGB"), body.rotation), replace=True)
    scan_manifest.record_event(db, batch.id, user.id, "rotated", slot_id=slot.id,
                               old=str(slot.rotation), new=str(body.rotation))
    if ticket is not None:
        db.add(AuditEvent(ticket_id=ticket.id, actor_user_id=user.id, field="scan_rotation",
                          old_value=str(slot.rotation), new_value=str(body.rotation)))
        ticket.crop_path = target
    slot.rotation = body.rotation
    db.commit()
    return scan_manifest.build_manifest(db, batch)
