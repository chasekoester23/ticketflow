"""Scan completeness: every physical slot on every scanned page is on record.

A ticket that is never recorded is worse than one misread, because totals look
complete without it. So each slot is kept, including blank-looking ones, with
its image, and people can reconcile pages and slots against what they scanned.

This records evidence only. It never approves a ticket or assigns a value, and
a slot's classification is either deterministic (``detection``) or a named
person's decision (``decision``).
"""
from __future__ import annotations

from datetime import datetime, timezone

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Batch, ScanEvent, ScanSlot, Ticket, UploadedFile
from . import splitter, storage

NO_CONTENT_ERROR = ("No ticket content detected in this file. Inspect the original "
                    "scan and layout, then upload a corrected document.")
SOME_FAILED_ERROR = "Some tickets could not be extracted"
UNREAD_MARKED_ERROR = "A slot marked as a real ticket has not been read yet. Retry, or enter it by hand."
# Only these file failures can be cleared by people resolving individual slots.
# An unreadable file or unexpected crash always needs an explicit retry.
RESOLVABLE_ERRORS = {NO_CONTENT_ERROR, SOME_FAILED_ERROR, UNREAD_MARKED_ERROR}

_ROTATIONS = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
              270: Image.Transpose.ROTATE_90}


def rotated(image: Image.Image, degrees: int) -> Image.Image:
    return image.transpose(_ROTATIONS[degrees]) if degrees else image


def record_slot(db: Session, file: UploadedFile, existing: dict, page_no: int,
                crop: splitter.Crop, sha256) -> ScanSlot:
    """Create or refresh the slot for one physical position. Idempotent, and
    never changes a person's decision or rotation."""
    assessment = splitter.assess_slot(crop.image)
    slot = existing.get((page_no, crop.position))
    if slot is None:
        slot = ScanSlot(batch_id=file.batch_id, file_id=file.id, page=page_no, position=crop.position,
                        crop_path=f"crops/{file.batch_id}/f{file.id}_p{page_no}_c{crop.position}.png", rotation=0)
        db.add(slot)
        existing[(page_no, crop.position)] = slot
    store = storage.current()
    if not store.exists(slot.crop_path):
        store.save_png(storage.reference_for(slot.crop_path), crop.image)
    slot.crop_box = list(crop.box)
    slot.crop_sha256 = sha256(store.read(slot.crop_path))
    slot.ink_fraction = assessment.ink
    slot.detection = assessment.detection
    return slot


def slots_by_position(db: Session, file_id: int) -> dict[tuple[int, int], ScanSlot]:
    return {(s.page, s.position): s for s in db.scalars(
        select(ScanSlot).where(ScanSlot.file_id == file_id))}


def record_event(db: Session, batch_id: int, actor_id: int | None, action: str, *,
                 slot_id: int | None = None, old: str | None = None, new: str | None = None) -> None:
    db.add(ScanEvent(batch_id=batch_id, slot_id=slot_id, actor_user_id=actor_id,
                     action=action, old_value=old, new_value=new))


def settle_statuses(db: Session, batch: Batch) -> None:
    """Re-derive file and batch status after people resolve individual slots."""
    if batch.status in ("uploaded", "processing"):
        return  # the worker owns these states
    for file in batch.files:
        if file.status != "failed" or file.error not in RESOLVABLE_ERRORS:
            continue
        tickets = list(db.scalars(select(Ticket).where(Ticket.file_id == file.id)))
        slots = list(db.scalars(select(ScanSlot).where(ScanSlot.file_id == file.id)))
        if any(t.status == "failed" for t in tickets):
            continue
        if tickets or (slots and all(s.decision == "confirmed_blank" for s in slots)):
            file.status, file.error = "ready_for_review", None
    batch.status = "failed" if any(f.status == "failed" for f in batch.files) else "ready_for_review"


def rebuild_for_file(db: Session, file: UploadedFile) -> int:
    """Create slots for a file processed before the manifest existed.

    Reads the stored scan and adds records only: existing tickets, crops and
    approvals are not changed. Raises PdfError/OSError if the scan is unusable.
    """
    from .pipeline import pages_for, sha256_bytes

    pages = pages_for(file, storage.current().read(file.storage_path))
    batch = db.get(Batch, file.batch_id)
    existing = slots_by_position(db, file.id)
    created = 0
    for page_no, page_img in enumerate(pages, start=1):
        for crop in splitter.split_page(page_img, batch.layout):
            if (page_no, crop.position) not in existing:
                created += 1
            record_slot(db, file, existing, page_no, crop, sha256_bytes)
    return created


def build_manifest(db: Session, batch: Batch) -> dict:
    """Deterministic counts a person can check against the paper in their hand."""
    tickets = list(db.scalars(select(Ticket).where(Ticket.batch_id == batch.id)))
    by_position = {(t.file_id, t.source_page, t.position): t for t in tickets}
    slots = list(db.scalars(select(ScanSlot).where(ScanSlot.batch_id == batch.id)
                            .order_by(ScanSlot.file_id, ScanSlot.page, ScanSlot.position)))
    slots_of: dict[int, list[ScanSlot]] = {}
    for slot in slots:
        slots_of.setdefault(slot.file_id, []).append(slot)

    def state(slot: ScanSlot, ticket: Ticket | None) -> str:
        if ticket is not None:
            return "ticket"
        if slot.decision == "confirmed_blank":
            return "confirmed_blank"
        return "suspected_blank" if slot.detection == "faint" else "empty"

    files_out, legacy_files, states = [], 0, []
    for file in batch.files:
        pages: dict[int, list[dict]] = {}
        for slot in slots_of.get(file.id, []):
            ticket = by_position.get((file.id, slot.page, slot.position))
            slot_state = state(slot, ticket)
            states.append(slot_state)
            pages.setdefault(slot.page, []).append({
                "id": slot.id, "position": slot.position, "state": slot_state,
                "detection": slot.detection, "decision": slot.decision,
                "rotation": slot.rotation, "crop_box": slot.crop_box,
                "crop_sha256": slot.crop_sha256,
                "ticket_id": ticket.id if ticket else None,
                "ticket_number": ticket.ticket_number if ticket else None,
                "ticket_status": ticket.status if ticket else None,
            })
        # Finished, or has tickets, but predates the manifest. A file that could
        # not be opened has no slots either, and is reported as failed instead.
        is_legacy = not slots_of.get(file.id) and (
            file.status == "ready_for_review" or any(t.file_id == file.id for t in tickets))
        legacy_files += is_legacy
        files_out.append({
            "id": file.id, "original_name": file.original_name, "sha256": file.sha256,
            "status": file.status, "error": file.error, "legacy": is_legacy,
            "pages": [{"page": n, "slots": s} for n, s in sorted(pages.items())],
        })

    records = [t for t in tickets if t.status != "skipped"]
    by_status = lambda name: sum(1 for t in tickets if t.status == name)  # noqa: E731
    failed_files = sum(1 for f in batch.files if f.status == "failed")
    processing = batch.status in ("uploaded", "processing")
    counts = {
        "files": len(batch.files),
        "failed_files": failed_files,
        "legacy_files": legacy_files,
        "pages": sum(len(f["pages"]) for f in files_out),
        "slots": len(slots),
        "records": len(records),
        "suspected_blank": states.count("suspected_blank"),
        "empty": states.count("empty"),
        "confirmed_blank": states.count("confirmed_blank"),
        "needs_review": by_status("needs_review"),
        "flagged": by_status("flagged"),
        "verified": by_status("verified"),
        "failed_tickets": by_status("failed"),
        "skipped": by_status("skipped"),
    }
    expected = batch.expected_ticket_count
    blockers = []
    if failed_files:
        blockers.append(f"{failed_files} file(s) did not finish processing.")
    if legacy_files:
        blockers.append(f"{legacy_files} older file(s) have no slot record yet, so blank-looking "
                        "slots cannot be checked. Build the scan check to review them.")
    if counts["suspected_blank"]:
        blockers.append(f"{counts['suspected_blank']} slot(s) look faint. Mark each as a real "
                        "ticket or confirm it is blank.")
    if counts["failed_tickets"]:
        blockers.append(f"{counts['failed_tickets']} ticket(s) could not be read. Retry or enter by hand.")
    if expected is not None and expected != len(records):
        blockers.append(f"You expected {expected} ticket(s), but {len(records)} are on record.")

    if processing:
        status = "processing"
    elif blockers:
        status = "needs_attention"
    elif expected is None:
        status = "count_not_confirmed"  # never implies every paper ticket was captured
    else:
        status = "count_matches"
    return {"batch_id": batch.id, "layout": batch.layout, "status": status,
            "expected_ticket_count": expected, "counts": counts, "blockers": blockers,
            "files": files_out}


def project_intake_issues(db: Session, organization_id: int, project_id: int) -> list[str]:
    """Reasons a project's delivery records may be missing tickets entirely.

    Any of these could hide a delivery from an invoice comparison.
    """
    # A fixed number of queries, because this runs for every invoice line shown.
    batches = db.execute(select(Batch.id, Batch.expected_ticket_count).where(
        Batch.organization_id == organization_id, Batch.project_id == project_id)).all()
    if not batches:
        return []
    ids = [row[0] for row in batches]
    open_slots = set(db.execute(
        select(ScanSlot.file_id, ScanSlot.page, ScanSlot.position).where(
            ScanSlot.batch_id.in_(ids), ScanSlot.detection == "faint", ScanSlot.decision.is_(None))
    ).all())
    if open_slots:
        ticketed = set(db.execute(
            select(Ticket.file_id, Ticket.source_page, Ticket.position).where(Ticket.batch_id.in_(ids))
        ).all())
        open_slots -= ticketed
    failed = db.scalar(select(func.count()).select_from(UploadedFile).where(
        UploadedFile.batch_id.in_(ids), UploadedFile.status == "failed"))
    expected = {batch_id: count for batch_id, count in batches if count is not None}
    short: list[int] = []
    if expected:
        records = dict(db.execute(
            select(Ticket.batch_id, func.count()).where(Ticket.batch_id.in_(expected), Ticket.status != "skipped")
            .group_by(Ticket.batch_id)
        ).all())
        short = [batch_id for batch_id, count in expected.items() if records.get(batch_id, 0) != count]
    issues = []
    if open_slots:
        issues.append(f"{len(open_slots)} faint scan slot(s) in this project have not been classified; "
                      "one could be a delivery on this invoice.")
    if failed:
        issues.append(f"{failed} uploaded file(s) in this project did not finish processing; "
                      "their deliveries may be missing.")
    if short:
        issues.append(f"{len(short)} batch(es) in this project do not match the ticket count people "
                      "expected: " + ", ".join(f"#{i}" for i in short) + ".")
    return issues


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
