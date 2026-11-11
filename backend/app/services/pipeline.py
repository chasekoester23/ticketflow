"""Batch processing: stored file -> pages -> crops -> extraction -> validated Ticket rows.

Each file is processed independently, so one bad PDF never fails the whole batch.

Reading is the slow part, so several crops are read at once (EXTRACTION_CONCURRENCY).
Only the reader runs on other threads. Every database write happens here, on the
worker's own thread, so SQLite sees one writer, as before. Results are saved in scan
order, as soon as each is ready, so a person can start reviewing while the rest are
read, and the list of tickets only ever grows at the end.
"""
from __future__ import annotations

import hashlib
import logging
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Batch, Ticket, UploadedFile
from . import scan_manifest, splitter, storage, usage
from .extraction import ExtractionError, Extractor
from .validation import compute_flags, parse_date, parse_tonnage

logger = logging.getLogger(__name__)

FAILED_FLAG = "Extraction failed - enter manually or retry"
READING_AGAIN = "Reading this ticket again."
FIELDS = ("ticket_number", "delivery_date", "material_type", "tonnage", "supplier", "job_location")


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


def _values(ticket: Ticket) -> tuple:
    return tuple(getattr(ticket, name) for name in FIELDS)


def apply_extraction(ticket: Ticket, extracted, raw: dict) -> None:
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
    hours = raw.get("hours") if isinstance(raw, dict) else None
    if hours not in (None, "") and ticket.tonnage is None:
        # Hourly trucking isn't priced yet (docs/engineering/billing-units.md). Say so, rather than "tonnage missing".
        ticket.flags = [*ticket.flags, f"Hourly ticket ({hours} h): TicketFlow prices by the ton for now, so there are no tons to approve. Flag it or skip it."]
    ticket.status = "needs_review"  # tonnage must be human-confirmed
    ticket.tonnage_confirmed = False
    ticket.error = None


@dataclass
class _FileState:
    file_id: int
    batch_id: int
    existing: dict
    visited: set = field(default_factory=set)
    detected: int = 0
    pending: int = 0
    any_failure: bool = False
    prepared: bool = False
    aborted: bool = False


@dataclass
class _Job:
    state: _FileState
    key: tuple[int, int]
    ticket: Ticket
    is_new: bool
    snapshot: tuple | None
    future: Future


class _Reader:
    """Reads crops on a few threads and hands each result back here, in scan order."""

    def __init__(self, db: Session, extractor: Extractor | None, workers: int,
                 organization_id: int | None = None, allowance: int | None = None):
        self.db = db
        self.extractor = extractor
        self.organization_id = organization_id
        # Reads this company may still make this month. None: no limit.
        self.allowance = allowance
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ticketflow-read") if extractor and workers > 1 else None
        # Crops waiting or in flight. Bounded, so a long PDF doesn't hold every crop in memory.
        # Wide enough that one slow ticket at the front doesn't leave the other threads idle.
        self.window = max(1, workers * 4)
        self.queue: deque[_Job] = deque()

    def submit(self, state: _FileState, key, ticket: Ticket, is_new: bool, image) -> None:
        if self.extractor is None:
            raise RuntimeError("AI extraction requires a configured extractor")
        if self.allowance is not None and self.allowance <= 0:
            # Over the company's monthly limit: no call is made, and the ticket says why.
            future = Future()
            future.set_exception(usage.limit_reached())
        elif self.pool is not None:
            future = self.pool.submit(self.extractor.extract, image)
        else:
            future = Future()
            try:
                future.set_result(self.extractor.extract(image))
            except BaseException as exc:  # handed over exactly as a thread would
                future.set_exception(exc)
        if self.allowance is not None:
            self.allowance -= 1
        self.queue.append(_Job(state, key, ticket, is_new, None if is_new else _values(ticket), future))
        state.pending += 1
        while len(self.queue) >= self.window:
            self._apply_next()
        self.apply_ready()

    def apply_ready(self) -> None:
        """Save whatever has finished, in order, without waiting. A crash then costs only
        the reads still in flight, and a person sees each ticket as soon as it's read."""
        while self.queue and self.queue[0].future.done():
            self._apply_next()

    def drain(self) -> None:
        while self.queue:
            self._apply_next()

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)

    def _apply_next(self) -> None:
        job = self.queue.popleft()
        try:
            result = job.future.result()
        except ExtractionError as exc:
            result = exc
        except Exception:
            logger.exception("ticket_read_failed", extra={"batch_id": job.state.batch_id, "file_id": job.state.file_id})
            result = ExtractionError("Unexpected error while reading this ticket. Retry the batch.", kind="unexpected")
        try:
            self._apply(job, result)
        except Exception:
            logger.exception("ticket_save_failed", extra={"batch_id": job.state.batch_id, "file_id": job.state.file_id})
            self.db.rollback()
            job.state.aborted = True
            recovered = self.db.get(UploadedFile, job.state.file_id)
            if recovered is not None:
                recovered.status, recovered.error = "failed", "Unexpected file-processing error. Please retry."
                self.db.commit()
        finally:
            job.state.pending -= 1
            finish_file(self.db, job.state)

    def _record_usage(self, job: _Job, result, ticket_id: int | None) -> None:
        """What the read cost, saved with its ticket. A read nobody kept still cost money."""
        row = usage.row_for(result, organization_id=self.organization_id, batch_id=job.state.batch_id,
                            ticket_id=ticket_id, model=getattr(self.extractor, "model", None))
        if row is not None:
            self.db.add(row)

    def _apply(self, job: _Job, result) -> None:
        ticket = job.ticket
        if job.is_new:
            taken = self.db.scalar(select(Ticket.id).where(
                Ticket.file_id == job.state.file_id, Ticket.source_page == job.key[0], Ticket.position == job.key[1]))
            if taken is not None:
                self._record_usage(job, result, taken)
                self.db.commit()
                return  # someone already recorded this slot; never a second ticket for one crop
        else:
            self.db.refresh(ticket)
            if ticket.status != "failed" or _values(ticket) != job.snapshot:
                # A person entered or changed this ticket while it was being read. Their work wins.
                logger.info("kept_person_edit", extra={"ticket_id": ticket.id})
                self._record_usage(job, result, ticket.id)
                self.db.commit()
                return
        if isinstance(result, ExtractionError):
            ticket.status = "failed"
            ticket.error = str(result)
            ticket.flags = [FAILED_FLAG]
            job.state.any_failure = True
        else:
            extracted, raw = result
            apply_extraction(ticket, extracted, raw)
        self.db.add(ticket)
        self.db.flush()
        self._record_usage(job, result, ticket.id)
        self.db.commit()


def finish_file(db: Session, state: _FileState) -> None:
    """A file's status, once everything it holds has been read."""
    if not state.prepared or state.pending or state.aborted:
        return
    file = db.get(UploadedFile, state.file_id)
    # Never report an all-white (or effectively empty) upload as completed.
    # A contractor must see an explicit failure instead of an empty "success"
    # queue that appears to account for every original delivery.
    if state.detected == 0:
        file.status, file.error = "failed", scan_manifest.NO_CONTENT_ERROR
        db.commit()
        return
    # A failed crop that was not encountered again must not silently disappear.
    failed = state.any_failure or any(
        t.status == "failed" and key not in state.visited for key, t in state.existing.items())
    file.status = "failed" if failed else "ready_for_review"
    file.error = scan_manifest.SOME_FAILED_ERROR if failed else None
    db.commit()


def prepare_file(db: Session, reader: _Reader, file: UploadedFile, layout: str, *, manual: bool) -> _FileState | None:
    store = storage.current()
    file.status, file.error = "processing", None
    db.commit()
    try:
        data = store.read(file.storage_path)
        pages = pages_for(file, data)
    except (splitter.PdfError, storage.DocumentMissing) as exc:
        file.status, file.error = "failed", str(exc)
        db.commit()
        return None
    except OSError:
        # The operating system's message names the path, and this goes on the record.
        file.status, file.error = "failed", "The stored file couldn't be read. Check the storage folder, then retry."
        db.commit()
        return None

    # Keep successfully extracted, manually corrected, verified and skipped tickets.
    # Only a failed crop is eligible for re-extraction. In particular, deleting all
    # tickets on retry would also delete a user's edits and verification history.
    state = _FileState(file.id, file.batch_id, {(t.source_page, t.position): t for t in file.tickets})
    slots = scan_manifest.slots_by_position(db, file.id)
    to_read = []

    for page_no, page_img in enumerate(pages, start=1):
        for crop in splitter.split_page(page_img, layout):
            # Every slot goes on record, including blank-looking ones.
            slot = scan_manifest.record_slot(db, file, slots, page_no, crop, sha256_bytes)
            key = (page_no, crop.position)
            ticket = state.existing.get(key)
            if slot.detection != "content" and slot.decision != "real_ticket" and ticket is None:
                continue  # kept in the manifest for a person; no ticket record
            state.detected += 1
            state.visited.add(key)
            if ticket is not None and ticket.status != "failed":
                continue  # never overwrite review work on an existing ticket

            image = scan_manifest.rotated(crop.image, slot.rotation)
            crop_reference = storage.reference_for(storage.rotated_key(slot.crop_path, slot.rotation))
            if slot.rotation:
                store.save_png(crop_reference, image, replace=True)
            is_new = ticket is None
            if is_new:
                ticket = Ticket(
                    batch_id=file.batch_id, file_id=file.id,
                    source_page=page_no, position=crop.position,
                )
            ticket.crop_box = list(crop.box)
            ticket.crop_path = crop_reference
            # The unrotated crop's hash: rotating must not defeat the check
            # that an identical scan cannot be approved twice.
            ticket.crop_sha256 = slot.crop_sha256

            if manual:
                # Manual intake creates real, unapproved records with actual
                # stored image crops. No model call and no invented values.
                # Never replace an existing reviewed/approved ticket.
                ticket.error = None
                set_blank_fields(ticket)
                db.add(ticket)
                continue
            # A new ticket is saved only once it has been read. One being read
            # again stays on record, saying so.
            ticket.error = None if is_new else READING_AGAIN
            to_read.append((key, ticket, is_new, image))

    # Every slot in the file, and any ticket being read again, goes on record before
    # reading starts, so the page's "3 of 12 ready" counts the whole file from the start.
    db.commit()
    for key, ticket, is_new, image in to_read:
        reader.submit(state, key, ticket, is_new, image)
    state.prepared = True
    finish_file(db, state)
    return state


def process_file(db: Session, file: UploadedFile, layout: str, extractor: Extractor | None, *, manual: bool = False) -> None:
    company = file.batch.organization_id
    reader = _Reader(db, None if manual else extractor, 1 if manual else settings.extraction_concurrency,
                     organization_id=company, allowance=None if manual else usage.reads_left_this_month(db, company))
    try:
        prepare_file(db, reader, file, layout, manual=manual)
        reader.drain()
    finally:
        reader.close()


def process_batch(db: Session, batch_id: int, extractor: Extractor | None, only_failed: bool = False) -> None:
    batch = db.get(Batch, batch_id)
    if batch is None:
        return
    batch.status = "processing"
    db.commit()
    manual = batch.intake_mode == "manual"
    allowance = None if manual else usage.reads_left_this_month(db, batch.organization_id)
    # One pool for the whole batch, so twenty single-ticket photos are read as fast as one four-up PDF.
    reader = _Reader(db, None if manual else extractor, 1 if manual else settings.extraction_concurrency,
                     organization_id=batch.organization_id, allowance=allowance)
    states: list[_FileState] = []
    try:
        for file in batch.files:
            if only_failed and file.status not in ("failed", "uploaded", "processing"):
                continue
            file_id = file.id
            reader.apply_ready()
            try:
                state = prepare_file(db, reader, file, batch.layout, manual=manual)
                if state is not None:
                    states.append(state)
            except Exception:
                logger.exception("file_failed", extra={"batch_id": batch_id, "file_id": file_id})
                db.rollback()
                # An unexpected crop, storage or API error should not prevent
                # processing the other files or erase already reviewed tickets.
                for state in states:
                    if state.file_id == file_id:
                        state.aborted = True
                recovered = db.get(UploadedFile, file_id)
                recovered.status = "failed"
                recovered.error = "Unexpected file-processing error. Please retry."
                db.commit()
        reader.drain()
    finally:
        reader.close()
    batch = db.get(Batch, batch_id)
    batch.status = "failed" if any(f.status == "failed" for f in batch.files) else "ready_for_review"
    db.commit()
