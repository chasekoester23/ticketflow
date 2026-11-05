"""What's verified, what isn't, and what still needs a person.

Everything here is a count of records with a link to where to look. There is
deliberately no percent-complete figure: only people can say every paper ticket
was captured, and this reports whether they've said so.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Batch, InvoiceComparison, InvoiceTicketLink, InvoiceTicketRef, ReviewTiming, ScanSlot, Ticket, UploadedFile,
)


def build_report(db: Session, organization_id: int, project_id: int | None, comparison_statuses: Counter) -> dict:
    """A fixed number of queries, however many tickets exist."""
    batch_query = select(Batch).where(Batch.organization_id == organization_id)
    if project_id is not None:
        batch_query = batch_query.where(Batch.project_id == project_id)
    batches = list(db.scalars(batch_query.order_by(Batch.id)))
    ids = [b.id for b in batches]

    files = list(db.scalars(select(UploadedFile).where(UploadedFile.batch_id.in_(ids)))) if ids else []
    tickets = list(db.scalars(select(Ticket).where(Ticket.batch_id.in_(ids)))) if ids else []
    slots = list(db.execute(select(
        ScanSlot.batch_id, ScanSlot.file_id, ScanSlot.page, ScanSlot.position, ScanSlot.detection, ScanSlot.decision,
    ).where(ScanSlot.batch_id.in_(ids)))) if ids else []

    ticketed = {(t.file_id, t.source_page, t.position) for t in tickets}
    open_slots = [s for s in slots if (s.file_id, s.page, s.position) not in ticketed]
    faint = [s for s in open_slots if s.detection == "faint" and s.decision is None]
    empty = [s for s in open_slots if s.detection == "empty" and s.decision is None]
    confirmed_blank = [s for s in open_slots if s.decision == "confirmed_blank"]
    files_with_slots = {s.file_id for s in slots}
    files_with_tickets = {t.file_id for t in tickets}
    legacy = [f for f in files if f.id not in files_with_slots
              and (f.status == "ready_for_review" or f.id in files_with_tickets)]
    failed_files = [f for f in files if f.status == "failed"]

    records_by_batch = Counter(t.batch_id for t in tickets if t.status != "skipped")
    mismatched = [b for b in batches if b.expected_ticket_count is not None
                  and b.expected_ticket_count != records_by_batch[b.id]]

    reasons: dict[int, list[str]] = {}

    def note(batch_id: int, text: str) -> None:
        reasons.setdefault(batch_id, []).append(text)

    for batch_id, count in Counter(s.batch_id for s in faint).items():
        note(batch_id, f"{count} faint slot(s) not classified")
    for batch_id, count in Counter(f.batch_id for f in failed_files).items():
        note(batch_id, f"{count} failed file(s)")
    for batch_id, count in Counter(f.batch_id for f in legacy).items():
        note(batch_id, f"{count} older file(s) without a scan check")
    for batch in mismatched:
        note(batch.id, f"{records_by_batch[batch.id]} on record, {batch.expected_ticket_count} expected")

    status = Counter(t.status for t in tickets)
    verified = [t for t in tickets if t.status == "verified" and t.tonnage_confirmed]
    unpriced = [t for t in verified if t.price_snapshot_state != "priced" or t.applied_unit_price is None]
    unreadable = [t for t in tickets if t.status != "skipped" and any("unreadable" in f.lower() for f in t.flags or [])]

    comparisons_query = select(InvoiceComparison.id).where(InvoiceComparison.organization_id == organization_id)
    if project_id is not None:
        comparisons_query = comparisons_query.where(InvoiceComparison.project_id == project_id)
    comparison_ids = list(db.scalars(comparisons_query))
    itemized = set(db.scalars(select(InvoiceTicketRef.comparison_id).where(
        InvoiceTicketRef.comparison_id.in_(comparison_ids)))) if comparison_ids else set()
    # Every active link in the company, whichever line holds it: a ticket from
    # this project could have been linked from a line on another project.
    verified_ids = {t.id for t in verified}
    linked = set(db.scalars(select(InvoiceTicketLink.ticket_id).where(
        InvoiceTicketLink.ticket_id.in_(verified_ids), InvoiceTicketLink.released_at.is_(None)))) if verified_ids else set()
    tons = lambda rows: str(sum((t.tonnage or Decimal(0) for t in rows), Decimal(0)))  # noqa: E731

    timings = list(db.execute(select(ReviewTiming.intake_mode, ReviewTiming.seconds, ReviewTiming.fields_corrected).where(
        ReviewTiming.organization_id == organization_id, ReviewTiming.batch_id.in_(ids)))) if ids else []

    def timing(mode: str) -> dict:
        rows = [r for r in timings if r.intake_mode == mode]
        return {"samples": len(rows), "median_seconds": str(median(r.seconds for r in rows)) if rows else None,
                "with_corrections": sum(1 for r in rows if r.fields_corrected)}

    open_lines = sum(count for name, count in comparison_statuses.items() if name != "matches")
    unresolved = (len(faint) + len(failed_files) + len(legacy) + len(mismatched) + status["needs_review"]
                  + status["flagged"] + status["failed"] + len(unpriced) + open_lines)
    return {
        "project_id": project_id,
        "unresolved_items": unresolved,
        "intake": {
            "batches": len(batches), "files": len(files), "failed_files": len(failed_files),
            "files_without_scan_check": len(legacy),
            "pages": len({(s.file_id, s.page) for s in slots}), "slots": len(slots),
            "faint_unclassified": len(faint), "empty": len(empty), "confirmed_blank": len(confirmed_blank),
            "batches_with_paper_count": sum(1 for b in batches if b.expected_ticket_count is not None),
            "paper_count_mismatches": len(mismatched),
        },
        "review": {
            "records": sum(records_by_batch.values()), "needs_review": status["needs_review"],
            "flagged": status["flagged"], "failed": status["failed"], "verified": status["verified"],
            "skipped": status["skipped"], "with_unreadable_fields": len(unreadable),
            "verified_unpriced": len(unpriced), "verified_tons": tons(verified),
        },
        "reconciliation": {
            "invoice_lines": len(comparison_ids), "itemized_lines": len(itemized),
            "totals_only_lines": len(comparison_ids) - len(itemized),
            "lines_by_status": dict(comparison_statuses),
            "lines_needing_attention": open_lines,
            "verified_tickets_linked": len(linked), "verified_tickets_not_linked": len(verified) - len(linked),
            "tons_linked": tons([t for t in verified if t.id in linked]),
            "tons_not_linked": tons([t for t in verified if t.id not in linked]),
        },
        "batches_needing_attention": [{"batch_id": batch_id, "reasons": reasons[batch_id]} for batch_id in sorted(reasons)],
        "review_timing": {"enabled": settings.review_timing_enabled, "manual": timing("manual"), "extract": timing("extract")},
    }
