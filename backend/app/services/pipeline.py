"""Batch processing: stored file -> pages -> crops -> extraction -> validated Ticket rows.

Each file is processed independently, so one bad PDF never fails the whole batch.
"""
import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Batch, Ticket, UploadedFile
from . import splitter
from .extraction import ExtractionError, Extractor
from .validation import compute_flags, parse_date, parse_tonnage


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pages_for(file: UploadedFile, data: bytes):
    if file.original_name.lower().endswith(".pdf"):
        return splitter.render_pdf_pages(data, dpi=settings.render_dpi)
    return [splitter.load_image(data)]


def process_file(db: Session, file: UploadedFile, layout: str, extractor: Extractor) -> None:
    file.status, file.error = "processing", None
    db.commit()
    try:
        data = Path(file.storage_path).read_bytes()
        pages = _pages_for(file, data)
    except (splitter.PdfError, OSError) as exc:
        file.status, file.error = "failed", str(exc)
        db.commit()
        return

    # Retry support: drop tickets from a previous attempt of this file.
    for old in list(file.tickets):
        db.delete(old)
    db.flush()

    crops_dir = settings.storage_dir / "crops" / str(file.batch_id)
    crops_dir.mkdir(parents=True, exist_ok=True)
    any_failure = False

    for page_no, page_img in enumerate(pages, start=1):
        for crop in splitter.split_page(page_img, layout):
            if layout != "single" and splitter.is_blank(crop.image):
                continue  # empty quadrant (fewer than 4 tickets on the scan)
            path = crops_dir / f"f{file.id}_p{page_no}_c{crop.position}.png"
            crop.image.save(path, format="PNG")
            ticket = Ticket(
                batch_id=file.batch_id, file_id=file.id, source_page=page_no, position=crop.position,
                crop_box=list(crop.box), crop_path=str(path), crop_sha256=sha256_bytes(path.read_bytes()),
            )
            try:
                extracted, raw = extractor.extract(crop.image)
            except ExtractionError as exc:
                ticket.status, ticket.error, ticket.flags = "failed", str(exc), ["Extraction failed - enter manually or retry"]
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
                job_location=ticket.job_location, raw_date=extracted.delivery_date, raw_tonnage=extracted.tonnage,
            )
            ticket.status = "needs_review"  # always: tonnage must be human-confirmed in the MVP
            db.add(ticket)

    file.status = "failed" if any_failure else "ready_for_review"
    if any_failure:
        file.error = "Some tickets could not be extracted"
    db.commit()


def process_batch(db: Session, batch_id: int, extractor: Extractor, only_failed: bool = False) -> None:
    batch = db.get(Batch, batch_id)
    if batch is None:
        return
    batch.status = "processing"
    db.commit()
    for file in batch.files:
        if only_failed and file.status != "failed":
            continue
        process_file(db, file, batch.layout, extractor)
    batch.status = "failed" if any(f.status == "failed" for f in batch.files) else "ready_for_review"
    db.commit()
