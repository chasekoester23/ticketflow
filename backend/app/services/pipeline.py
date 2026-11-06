"""Batch processing: stored file -> pages -> crops -> extraction -> validated Ticket rows.

Each file is processed independently, so one bad PDF never fails the whole batch.
"""
import hashlib
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Batch, Ticket, UploadedFile
from . import scan_manifest, splitter
from .extraction import ExtractionError, Extractor
from .validation import compute_flags, parse_date, parse_tonnage

logger = logging.getLogger(__name__)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pages_for(file: UploadedFile, data: bytes):
    if file.original_name.lower().endswith(".pdf"):
        return splitter.render_pdf_pages(
            data, dpi=settings.render_dpi,
            max_pages=settings.max_pdf_pages,
            max_page_pixels=settings.max_rendered_page_pixels,
            max_total_pixels=settings.max_total_pdf_pixels,
        )
    return [splitter.load_image(data, max_pixels=settings.max_image_pixels)]


def set_blank_fields(ticket: Ticket) -> None:
    """A real, unapproved record with no values: nothing is invented."""
    ticket.ticket_number = None
    ticket.delivery_date = None
    ticket.material_type = None
    ticket.tonnage = None
    ticket.supplier = None
    ticket.job_location = None
    ticket.raw_extraction = None
    ticket.tonnage_confirmed = False
    ticket.price_snapshot_state = "pending"
    ticket.flags = compute_flags(
        ticket_number=None, delivery_date=None, material_type=None,
        tonnage=None, supplier=None, job_location=None,
    )
    ticket.status = "needs_review"


def process_file(db: Session, file: UploadedFile, layout: str, extractor: Extractor | None, *, manual: bool = False) -> None:
    file.status, file.error = "processing", None
    db.commit()
    try:
        data = Path(file.storage_path).read_bytes()
        pages = pages_for(file, data)
    except (splitter.PdfError, OSError) as exc:
        file.status, file.error = "failed", str(exc)
        db.commit()
        return

    # Keep successfully extracted, manually corrected, verified and skipped tickets.
    # Only a failed crop is eligible for re-extraction. In particular, deleting all
    # tickets on retry would also delete a user's edits and verification history.
    existing = {(t.source_page, t.position): t for t in file.tickets}
    visited: set[tuple[int, int]] = set()

    slots = scan_manifest.slots_by_position(db, file.id)
    any_failure = False
    detected_crops = 0

    for page_no, page_img in enumerate(pages, start=1):
        for crop in splitter.split_page(page_img, layout):
            # Every slot goes on record, including blank-looking ones.
            slot = scan_manifest.record_slot(db, file, slots, page_no, crop, sha256_bytes)
            key = (page_no, crop.position)
            ticket = existing.get(key)
            if slot.detection != "content" and slot.decision != "real_ticket" and ticket is None:
                continue  # kept in the manifest for a person; no ticket record
            detected_crops += 1
            visited.add(key)
            if ticket is not None and ticket.status != "failed":
                continue  # never overwrite review work on an existing ticket

            image = scan_manifest.rotated(crop.image, slot.rotation)
            path = Path(scan_manifest.rotated_path(slot.crop_path, slot.rotation))
            if slot.rotation:
                scan_manifest.write_private_png(path, image)
            if ticket is None:
                ticket = Ticket(
                    batch_id=file.batch_id, file_id=file.id,
                    source_page=page_no, position=crop.position,
                )
            ticket.crop_box = list(crop.box)
            ticket.crop_path = str(path)
            # The unrotated crop's hash: rotating must not defeat the check
            # that an identical scan cannot be approved twice.
            ticket.crop_sha256 = slot.crop_sha256
            ticket.error = None

            if manual:
                # Manual intake creates real, unapproved records with actual
                # stored image crops. No model call and no invented values.
                # Never replace an existing reviewed/approved ticket.
                set_blank_fields(ticket)
                db.add(ticket)
                continue

            if extractor is None:
                raise RuntimeError("AI extraction requires a configured extractor")
            try:
                extracted, raw = extractor.extract(image)
            except ExtractionError as exc:
                ticket.status = "failed"
                ticket.error = str(exc)
                ticket.flags = ["Extraction failed - enter manually or retry"]
                any_failure = True
                db.add(ticket)
                continue

            ticket.raw_extraction = raw
            ticket.ticket_number = extracted.ticket_number
            ticket.delivery_date = parse_date(extracted.delivery_date)
            ticket.material_type = extracted.material_type
            ticket.tonnage = parse_tonnage(extracted.tonnage)
            ticket.supplier = extracted.supplier
            ticket.job_location = extracted.job_location
            ticket.flags = compute_flags(
                ticket_number=ticket.ticket_number, delivery_date=ticket.delivery_date,
                material_type=ticket.material_type, tonnage=ticket.tonnage, supplier=ticket.supplier,
                job_location=ticket.job_location, raw_date=extracted.delivery_date,
                raw_tonnage=extracted.tonnage,
            )
            ticket.status = "needs_review"  # tonnage must be human-confirmed
            ticket.tonnage_confirmed = False
            db.add(ticket)

    # Never report an all-white (or effectively empty) upload as completed.
    # A contractor must see an explicit failure instead of an empty "success"
    # queue that appears to account for every original delivery.
    if detected_crops == 0:
        file.status = "failed"
        file.error = scan_manifest.NO_CONTENT_ERROR
        db.commit()
        return

    # A failed crop that was not encountered again must not silently disappear.
    any_failure = any_failure or any(
        t.status == "failed" and key not in visited
        for key, t in existing.items()
    )
    file.status = "failed" if any_failure else "ready_for_review"
    file.error = scan_manifest.SOME_FAILED_ERROR if any_failure else None
    db.commit()


def process_batch(db: Session, batch_id: int, extractor: Extractor | None, only_failed: bool = False) -> None:
    batch = db.get(Batch, batch_id)
    if batch is None:
        return
    batch.status = "processing"
    db.commit()
    for file in batch.files:
        if only_failed and file.status not in ("failed", "uploaded", "processing"):
            continue
        file_id = file.id
        try:
            if batch.intake_mode == "manual":
                process_file(db, file, batch.layout, None, manual=True)
            else:
                process_file(db, file, batch.layout, extractor)
        except Exception:
            logger.exception("file_failed", extra={"batch_id": batch_id, "file_id": file_id})
            db.rollback()
            # An unexpected crop, storage or API error should not prevent
            # processing the other files or erase already reviewed tickets.
            recovered = db.get(UploadedFile, file_id)
            recovered.status = "failed"
            recovered.error = "Unexpected file-processing error. Please retry."
            db.commit()
    batch = db.get(Batch, batch_id)
    batch.status = "failed" if any(f.status == "failed" for f in batch.files) else "ready_for_review"
    db.commit()
