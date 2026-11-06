"""What's verified, what isn't, and what still needs a person.

Everything here is a count of records with a link to where to look. There is
deliberately no percent-complete figure: only people can say every paper ticket
was captured, and this reports whether they've said so.
"""
from __future__ import annotations

from collections import Counter
from statistics import median

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from .aggregates import as_decimal, exact_sum
from ..models import (
    Batch, InvoiceComparison, InvoiceTicketLink, InvoiceTicketRef, ReviewTiming, ScanSlot, Ticket, UploadedFile,
)


def build_report(db: Session, organization_id: int, project_id: int | None, comparison_statuses: Counter) -> dict:
    """A fixed number of queries, however many tickets exist. The database does the
    adding up, using a subquery for "these batches", so there's no limit on how many."""
    batch_query = select(Batch).where(Batch.organization_id == organization_id)
    if project_id is not None:
        batch_query = batch_query.where(Batch.project_id == project_id)
    batches = list(db.scalars(batch_query.order_by(Batch.id)))
    in_scope = select(Batch.id).where(Batch.organization_id == organization_id)
    if project_id is not None:
        in_scope = in_scope.where(Batch.project_id == project_id)

    file_counts = list(db.execute(select(UploadedFile.batch_id, UploadedFile.status, func.count()).where(
        UploadedFile.batch_id.in_(in_scope)).group_by(UploadedFile.batch_id, UploadedFile.status)))
    ticket_counts = list(db.execute(select(Ticket.batch_id, Ticket.status, func.count()).where(
        Ticket.batch_id.in_(in_scope)).group_by(Ticket.batch_id, Ticket.status)))
    has_ticket = exists().where(Ticket.file_id == ScanSlot.file_id, Ticket.source_page == ScanSlot.page, Ticket.position == ScanSlot.position)
    open_slots = list(db.execute(select(ScanSlot.batch_id, ScanSlot.detection, ScanSlot.decision, func.count()).where(
        ScanSlot.batch_id.in_(in_scope), ~has_ticket).group_by(ScanSlot.batch_id, ScanSlot.detection, ScanSlot.decision)))
    slot_total = db.scalar(select(func.count()).select_from(ScanSlot).where(ScanSlot.batch_id.in_(in_scope))) or 0
    pages = db.scalar(select(func.count()).select_from(
        select(ScanSlot.file_id, ScanSlot.page).where(ScanSlot.batch_id.in_(in_scope)).distinct().subquery())) or 0

    faint = Counter()
    empty = confirmed_blank = 0
    for batch_id, detection, decision, count in open_slots:
        if detection == "faint" and decision is None:
            faint[batch_id] += count
        if detection == "empty" and decision is None:
            empty += count
        if decision == "confirmed_blank":
            confirmed_blank += count
    has_slot = exists().where(ScanSlot.file_id == UploadedFile.id)
    has_record = exists().where(Ticket.file_id == UploadedFile.id)
    legacy = list(db.execute(select(UploadedFile.batch_id, func.count()).where(
        UploadedFile.batch_id.in_(in_scope), ~has_slot, or_(UploadedFile.status == "ready_for_review", has_record),
    ).group_by(UploadedFile.batch_id)))
    legacy_total = sum(count for _, count in legacy)
    failed_files = Counter()
    file_total = 0
    for batch_id, file_status, count in file_counts:
        file_total += count
        if file_status == "failed":
            failed_files[batch_id] += count

    records_by_batch = Counter()
    status = Counter()
    for batch_id, ticket_status, count in ticket_counts:
        status[ticket_status] += count
        if ticket_status != "skipped":
            records_by_batch[batch_id] += count
    mismatched = [b for b in batches if b.expected_ticket_count is not None
                  and b.expected_ticket_count != records_by_batch[b.id]]

    reasons: dict[int, list[str]] = {}

    def note(batch_id: int, text: str) -> None:
        reasons.setdefault(batch_id, []).append(text)

    for batch_id, count in faint.items():
        note(batch_id, f"{count} faint slot(s) not classified")
    for batch_id, count in failed_files.items():
        note(batch_id, f"{count} failed file(s)")
    for batch_id, count in legacy:
        note(batch_id, f"{count} older file(s) without a scan check")
    for batch in mismatched:
        note(batch.id, f"{records_by_batch[batch.id]} on record, {batch.expected_ticket_count} expected")

    counted = and_(Ticket.batch_id.in_(in_scope), Ticket.status == "verified", Ticket.tonnage_confirmed.is_(True))
    linked = exists().where(InvoiceTicketLink.ticket_id == Ticket.id, InvoiceTicketLink.released_at.is_(None))
    tons_all = exact_sum(Ticket.tonnage, 3)
    verified_row = db.execute(select(func.count().label("n"), tons_all.label("tons")).where(counted)).one()
    linked_row = db.execute(select(func.count().label("n"), tons_all.label("tons")).where(counted, linked)).one()
    unpriced_count = db.scalar(select(func.count()).select_from(Ticket).where(
        counted, or_(Ticket.price_snapshot_state != "priced", Ticket.price_snapshot_state.is_(None), Ticket.applied_unit_price.is_(None)))) or 0
    unreadable = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.batch_id.in_(in_scope), Ticket.status != "skipped", cast(Ticket.flags, String).ilike("%unreadable%"))) or 0
    verified_tons = as_decimal(verified_row.tons, 3)
    linked_tons = as_decimal(linked_row.tons, 3)

    comparisons_query = select(InvoiceComparison.id).where(InvoiceComparison.organization_id == organization_id)
    if project_id is not None:
        comparisons_query = comparisons_query.where(InvoiceComparison.project_id == project_id)
    comparison_ids = list(db.scalars(comparisons_query))
    itemized = set(db.scalars(select(InvoiceTicketRef.comparison_id).where(
        InvoiceTicketRef.comparison_id.in_(comparison_ids)))) if comparison_ids else set()

    timings = list(db.execute(select(ReviewTiming.intake_mode, ReviewTiming.seconds, ReviewTiming.fields_corrected).where(
        ReviewTiming.organization_id == organization_id, ReviewTiming.batch_id.in_(in_scope))))

    def timing(mode: str) -> dict:
        rows = [r for r in timings if r.intake_mode == mode]
        return {"samples": len(rows), "median_seconds": str(median(r.seconds for r in rows)) if rows else None,
                "with_corrections": sum(1 for r in rows if r.fields_corrected)}

    open_lines = sum(count for name, count in comparison_statuses.items() if name != "matches")
    faint_total = sum(faint.values())
    failed_total = sum(failed_files.values())
    unresolved = (faint_total + failed_total + legacy_total + len(mismatched) + status["needs_review"]
                  + status["flagged"] + status["failed"] + unpriced_count + open_lines)
    return {
        "project_id": project_id,
        "unresolved_items": unresolved,
        "intake": {
            "batches": len(batches), "files": file_total, "failed_files": failed_total,
            "files_without_scan_check": legacy_total,
            "pages": pages, "slots": slot_total,
            "faint_unclassified": faint_total, "empty": empty, "confirmed_blank": confirmed_blank,
            "batches_with_paper_count": sum(1 for b in batches if b.expected_ticket_count is not None),
            "paper_count_mismatches": len(mismatched),
        },
        "review": {
            "records": sum(records_by_batch.values()), "needs_review": status["needs_review"],
            "flagged": status["flagged"], "failed": status["failed"], "verified": status["verified"],
            "skipped": status["skipped"], "with_unreadable_fields": unreadable,
            "verified_unpriced": unpriced_count, "verified_tons": str(verified_tons),
        },
        "reconciliation": {
            "invoice_lines": len(comparison_ids), "itemized_lines": len(itemized),
            "totals_only_lines": len(comparison_ids) - len(itemized),
            "lines_by_status": dict(comparison_statuses),
            "lines_needing_attention": open_lines,
            "verified_tickets_linked": linked_row.n, "verified_tickets_not_linked": verified_row.n - linked_row.n,
            "tons_linked": str(linked_tons),
            "tons_not_linked": str(verified_tons - linked_tons),
        },
        "batches_needing_attention": [{"batch_id": batch_id, "reasons": reasons[batch_id]} for batch_id in sorted(reasons)],
        "review_timing": {"enabled": settings.review_timing_enabled, "manual": timing("manual"), "extract": timing("extract")},
    }
