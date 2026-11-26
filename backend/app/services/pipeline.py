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
import io
import logging
from decimal import Decimal
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Batch, Material, ScanEvent, ScanSlot, Supplier, Ticket, UploadedFile, normal_name
from . import scan_manifest, segmentation, splitter, storage, ticket_text, units, usage
from .extraction import ExtractionError, Extractor
from .validation import (compute_flags, parse_amount, parse_date, parse_tonnage, same_number_in_batch_flag,
                         unit_not_stated_flag)

logger = logging.getLogger(__name__)

READER_SOURCE = "reader"  # read by the configured AI reader: one call, counted against the monthly limit
TEXT_SOURCE = ticket_text.SOURCE  # read from a digital PDF's own text: no call, not counted

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
    return [splitter.load_image(data, max_pixels=settings.max_image_pixels,
                                max_source_pixels=settings.max_source_image_pixels)]


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


def catalog_spellings(db: Session, organization_id: int | None) -> dict[str, dict[str, str]]:
    """{kind: {normalized name: catalog name}} for one company. A normalized form two
    catalog names share is left out, so it never picks between them."""
    spellings: dict[str, dict[str, str]] = {}
    for kind, model in (("supplier", Supplier), ("material", Material)):
        found: dict[str, set[str]] = {}
        for name in db.scalars(select(model.name).where(model.organization_id == organization_id)):
            found.setdefault(normal_name(name), set()).add(name)
        spellings[kind] = {key: next(iter(names)) for key, names in found.items() if len(names) == 1}
    return spellings


def _catalog_spelling(value: str | None, known: dict[str, str]) -> str | None:
    """The catalog's spelling of a name that differs only by case, spacing or punctuation."""
    if not value:
        return value
    return known.get(normal_name(value), value)


def _quantity(extracted) -> tuple[Decimal | None, str, Decimal | None, str | None, str | None]:
    """(quantity, unit, source quantity, source unit, flag) for a reader's answer.

    The reader gives the quantity in the unit printed. Pounds are saved as tons by the
    rule a person's entry follows (units.pounds_to_tons, keeping the pounds); other
    units are kept as they are. A quantity with no unit the reader could name is not
    assumed to be tons: it stays unentered, with a flag. A reader that never answers for
    the unit (written before units) keeps the old contract that its figure is tons.
    """
    raw = extracted.tonnage
    amount, printed = parse_tonnage(raw), None
    if amount is None:
        amount, printed = parse_amount(raw)  # "49,000 lb": the unit written with the figure
    unit = extracted.unit or printed
    if printed and extracted.unit and printed != extracted.unit:
        amount = None  # the figure and the unit field disagree: never pick one
    if not getattr(extracted, "reports_unit", False):
        return amount, units.DEFAULT, None, None, None
    if unit is None:
        flag = unit_not_stated_flag(raw) if raw not in (None, "") else None
        return None, units.DEFAULT, None, None, flag
    if unit == "lb" and amount is not None:
        return units.pounds_to_tons(amount), "ton", amount, "lb", None
    return amount, ("ton" if unit == "lb" else unit), None, None, None


def _weights(extracted) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Gross and tare, only with a unit for them (lb or ton). A weight with no unit can't
    be checked against the quantity, so it stays only in what was read."""
    gross, gross_unit = parse_amount(extracted.gross_weight)
    tare, tare_unit = parse_amount(extracted.tare_weight)
    stated = {u for u in (extracted.weight_unit, gross_unit, tare_unit) if u}
    if len(stated) != 1 or not stated <= set(units.WEIGHT_UNITS) or (gross is None and tare is None):
        return None, None, None
    return gross, tare, stated.pop()


def apply_extraction(ticket: Ticket, extracted, raw: dict, catalog: dict[str, dict[str, str]] | None = None,
                     *, source: str = READER_SOURCE) -> None:
    """``catalog`` (from catalog_spellings) adopts the company's own spelling of a name the
    reader wrote differently, e.g. "WABASH RIDGE AGGREGATES". ``raw`` keeps what was read,
    with ``source`` saying how: "reader" (the AI reader) or "pdf_text" (the PDF's own text)."""
    catalog = catalog or {}
    ticket.raw_extraction = {**raw, "source": source} if isinstance(raw, dict) else raw
    ticket.ticket_number = extracted.ticket_number
    ticket.delivery_date = parse_date(extracted.delivery_date)
    ticket.material_type = _catalog_spelling(extracted.material_type, catalog.get("material", {}))
    quantity, unit, source_quantity, source_unit, unit_flag = _quantity(extracted)
    ticket.tonnage, ticket.unit = quantity, unit
    ticket.source_quantity, ticket.source_unit = source_quantity, source_unit
    ticket.gross_weight, ticket.tare_weight, ticket.weight_unit = _weights(extracted)
    ticket.truck_number = (getattr(extracted, "truck_number", None) or None) and extracted.truck_number[:50]
    ticket.supplier = _catalog_spelling(extracted.supplier, catalog.get("supplier", {}))
    ticket.job_location = extracted.job_location
    ticket.flags = compute_flags(
        ticket_number=ticket.ticket_number, delivery_date=ticket.delivery_date,
        material_type=ticket.material_type, tonnage=ticket.tonnage, supplier=ticket.supplier,
        job_location=ticket.job_location, raw_date=extracted.delivery_date,
        raw_tonnage=None if unit_flag else extracted.tonnage, unit=ticket.unit,
        gross_weight=ticket.gross_weight, tare_weight=ticket.tare_weight, weight_unit=ticket.weight_unit,
    )
    if unit_flag:  # says why there is no quantity, in place of "Tonnage missing"
        ticket.flags = [*(f for f in ticket.flags if f != "Tonnage missing"), unit_flag]
    hours = raw.get("hours") if isinstance(raw, dict) else None
    if hours not in (None, "") and ticket.tonnage is None:
        # Hourly trucking isn't priced yet (docs/engineering/billing-units.md). Say so, rather than "tonnage missing".
        ticket.flags = [*ticket.flags, f"Hourly ticket ({hours} h): Quarvera prices by the ton for now, so there are no tons to approve. Flag it or skip it."]
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
    # Auto layout: (page, position) -> why a person should check that region's crop.
    notes: dict = field(default_factory=dict)


@dataclass
class _Job:
    state: _FileState
    key: tuple[int, int]
    ticket: Ticket
    is_new: bool
    snapshot: tuple | None
    future: Future
    source: str = READER_SOURCE


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
        # Catalog spellings per company, read once per batch rather than once per ticket.
        self.catalogs: dict[int | None, dict[str, dict[str, str]]] = {}
        # The text layer of the PDF being read, one file at a time: (file id, pages or None).
        self.text_layer: tuple[int | None, list | None] = (None, None)

    def _catalog(self, organization_id: int | None) -> dict[str, dict[str, str]]:
        if organization_id not in self.catalogs:
            self.catalogs[organization_id] = catalog_spellings(self.db, organization_id)
        return self.catalogs[organization_id]

    def _company(self, batch_id: int) -> int | None:
        return self.db.scalar(select(Batch.organization_id).where(Batch.id == batch_id))

    def _pages_text(self, file_id: int) -> list | None:
        """The text layer of each page of a stored PDF, or None for an image or a PDF that
        can't be opened again. Read once per file and dropped when the next file starts."""
        if self.text_layer[0] == file_id:
            return self.text_layer[1]
        pages = None
        file = self.db.get(UploadedFile, file_id)
        if file is not None and file.original_name.lower().endswith(".pdf"):
            try:
                pages = ticket_text.read_pages(
                    storage.current().read(file.storage_path), dpi=settings.render_dpi,
                    max_page_pixels=settings.max_rendered_page_pixels, max_pages=settings.max_pdf_pages)
            except Exception:  # the reader then reads it, exactly as before
                logger.warning("pdf_text_unavailable", extra={"file_id": file_id})
        self.text_layer = (file_id, pages)
        return pages

    def _read_text(self, state: _FileState, ticket: Ticket):
        """(ExtractedTicket, raw) from the PDF's own text in this crop, or None when the
        crop has no usable text or the text leaves any field missing or in doubt."""
        pages = self._pages_text(state.file_id)
        if not pages or not ticket.source_page or ticket.source_page > len(pages) or not ticket.crop_box:
            return None
        company = self.organization_id if self.organization_id is not None else self._company(state.batch_id)
        try:
            return ticket_text.read_crop(pages[ticket.source_page - 1], ticket.crop_box,
                                         supplier_names=tuple(self._catalog(company).get("supplier", {}).values()))
        except Exception:
            logger.exception("pdf_text_read_failed", extra={"file_id": state.file_id})
            return None

    def submit(self, state: _FileState, key, ticket: Ticket, is_new: bool, image) -> None:
        if self.extractor is None:
            raise RuntimeError("AI extraction requires a configured extractor")
        source = READER_SOURCE
        from_text = self._read_text(state, ticket)
        if from_text is not None:
            # A digital PDF's text gave every field without doubt: no reader call, nothing counted.
            future, source = Future(), TEXT_SOURCE
            future.set_result(from_text)
        elif self.allowance is not None and self.allowance <= 0:
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
        if self.allowance is not None and source == READER_SOURCE:
            self.allowance -= 1
        self.queue.append(_Job(state, key, ticket, is_new, None if is_new else _values(ticket), future, source))
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
        """What the read cost, saved with its ticket. A read nobody kept still cost money.
        A read from the PDF's own text made no call: it costs nothing and isn't counted."""
        if job.source != READER_SOURCE:
            return
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
            organization_id = self._company(job.state.batch_id)
            apply_extraction(ticket, extracted, raw, self._catalog(organization_id), source=job.source)
            twin = self._same_number(job, ticket, organization_id)
            if twin is not None:
                ticket.flags = [*ticket.flags, same_number_in_batch_flag(*twin)]
        self.db.add(ticket)
        self.db.flush()
        self._record_usage(job, result, ticket.id)
        self.db.commit()


    def _same_number(self, job: _Job, ticket: Ticket, organization_id: int | None) -> tuple[int, int] | None:
        """(page, position) of another ticket in this batch with this supplier and ticket
        number, so a ticket scanned twice is flagged when it's read, not only at approval.
        The number is compared ignoring case and outer spaces, the supplier as matching does."""
        number = (ticket.ticket_number or "").strip().upper()
        if not number or not ticket.supplier:
            return None
        query = select(Ticket.source_page, Ticket.position).where(
            Ticket.organization_id == organization_id, Ticket.batch_id == job.state.batch_id,
            Ticket.number_key == number, Ticket.supplier_key == normal_name(ticket.supplier),
            Ticket.status != "skipped")
        if ticket.id is not None:
            query = query.where(Ticket.id != ticket.id)
        found = self.db.execute(query.order_by(Ticket.file_id, Ticket.source_page, Ticket.position).limit(1)).first()
        return (found[0], found[1]) if found else None


def finish_file(db: Session, state: _FileState) -> None:
    """A file's status, once everything it holds has been read."""
    if not state.prepared or state.pending or state.aborted:
        return
    flag_layout_notes(db, state)
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


LAYOUT_REVIEW_EVENT = "auto_layout_review"


def flag_layout_notes(db: Session, state: _FileState) -> None:
    """Put each auto-layout review note on its ticket once it has been read (reading
    resets a ticket's flags). An approved ticket is never changed. A note is a prompt to
    look, never a value, so failing to add one is logged rather than failing the file."""
    if not state.notes:
        return
    try:
        for (page, position), note in state.notes.items():
            ticket = db.scalar(select(Ticket).where(
                Ticket.file_id == state.file_id, Ticket.source_page == page, Ticket.position == position))
            if ticket is not None and ticket.status != "verified" and note not in (ticket.flags or []):
                ticket.flags = [*(ticket.flags or []), note]
        db.commit()
    except Exception:
        logger.exception("layout_note_failed", extra={"batch_id": state.batch_id, "file_id": state.file_id})
        db.rollback()


def layout_notes(db: Session, file: UploadedFile) -> dict[int, str]:
    """Review notes recorded when a file's auto-layout regions were first found, by slot id."""
    rows = db.execute(select(ScanEvent.slot_id, ScanEvent.new_value).join(ScanSlot, ScanSlot.id == ScanEvent.slot_id)
                      .where(ScanSlot.file_id == file.id, ScanEvent.action == LAYOUT_REVIEW_EVENT))
    return {slot_id: segmentation.REVIEW_NOTES[code] for slot_id, code in rows if code in segmentation.REVIEW_NOTES}


def page_crops(page_img, page_no: int, layout: str, slots: dict, notes: dict[int, str]) -> list[splitter.Crop]:
    """The crops of one page. With the auto layout, a page already on record keeps the
    regions found the first time and its stored crops are read again, so a retry (or a
    later change to detection) never renumbers or re-cuts tickets people have seen.
    That includes any region a person added by hand."""
    if layout == "auto":
        known = sorted((position, slot) for (page, position), slot in slots.items() if page == page_no)
        if known:
            store = storage.current()
            crops = []
            for position, slot in known:
                box = tuple(slot.crop_box)
                try:
                    with Image.open(io.BytesIO(store.read(slot.crop_path))) as stored:
                        image = stored.convert("RGB")
                except OSError:  # the stored crop is gone: cut it again from the page
                    image = page_img.crop(box)
                crops.append(splitter.Crop(position, box, image, notes.get(slot.id)))
            return crops
    return splitter.split_page(page_img, layout)


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
    notes = layout_notes(db, file) if layout == "auto" else {}
    to_read = []

    for page_no, page_img in enumerate(pages, start=1):
        for crop in page_crops(page_img, page_no, layout, slots, notes):
            fresh = (page_no, crop.position) not in slots
            # Every slot goes on record, including blank-looking ones.
            slot = scan_manifest.record_slot(db, file, slots, page_no, crop, sha256_bytes)
            if fresh and crop.review:
                # Kept with the slot, so the note survives a retry. No actor: the system.
                db.flush()
                scan_manifest.record_event(db, file.batch_id, None, LAYOUT_REVIEW_EVENT, slot_id=slot.id,
                                           new=segmentation.REVIEW_CODES.get(crop.review))
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
            if crop.review:
                state.notes[key] = crop.review

            if manual:
                # Manual intake creates real, unapproved records with actual
                # stored image crops. No model call and no invented values.
                # Never replace an existing reviewed/approved ticket.
                ticket.error = None
                set_blank_fields(ticket)
                if crop.review:
                    ticket.flags = [*ticket.flags, crop.review]
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
