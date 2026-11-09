"""Score a finished pilot batch against independent ground truth.

The crop benchmark in evaluation.py asks "how well does the model read a crop".
This asks the commercial question: "after scanning, extraction, human review and
invoice reconciliation, what did TicketFlow get right, what did people have to
fix, how long did it take, and what did it cost". It reads a database (opened
read-only by scripts/evaluate_workflow.py) and a ground-truth file a person wrote
by reading the paper tickets, never by looking at TicketFlow's answers.

Two accuracies are kept apart on purpose:
  * extraction: the model's untouched output (Ticket.raw_extraction), before anyone
    corrected it;
  * final: what a person approved. A wrong approved figure is the costly failure.

Nothing here writes to the database. The report holds counts and verdicts, never
ticket contents, so it can be shared without sharing the documents.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Batch, InvoiceComparison, InvoiceTicketLink, ProcessingJob, ReviewTiming, ScanSlot, Ticket, UploadedFile, as_utc,
)
from .evaluation import FIELDS, DatasetError, _field_verdict, _normalize, _reference_is_valid

CRITICAL = ("ticket_number", "tonnage")
WRONG = ("mismatch", "unexpected")  # confidently wrong, as opposed to left blank


@dataclass(frozen=True)
class Truth:
    file: str
    page: int
    position: int
    is_ticket: bool
    crop_complete: bool | None
    expected: dict[str, Any] | None


def load_truth(path: Path) -> dict[str, Any]:
    """Refuses anything that doesn't say it was labelled independently, so a file
    copied from TicketFlow's own answers can't pass for ground truth."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"Can't read the ground-truth file: {exc}") from None
    if not isinstance(data, dict) or data.get("labelled_independently") is not True:
        raise DatasetError("The ground-truth file must say \"labelled_independently\": true, meaning a person "
                           "read each paper ticket without looking at TicketFlow's values.")
    if not str(data.get("labelled_by") or "").strip():
        raise DatasetError("Say who labelled the tickets (\"labelled_by\"), so the labels can be checked.")
    synthetic = data.get("synthetic") is True
    slots, seen = [], set()
    for n, row in enumerate(data.get("slots") or [], start=1):
        if isinstance(row, dict) and "synthetic" in row and (row["synthetic"] is True) != synthetic:
            raise DatasetError(f"Slot {n} is marked {'synthetic' if row['synthetic'] is True else 'real'}, but the "
                               "file is not: synthetic and real labels can't be scored together.")
        try:
            key = (str(row["file"]), int(row["page"]), int(row["position"]))
            is_ticket = bool(row["ticket"])
        except (KeyError, TypeError, ValueError):
            raise DatasetError(f"Slot {n} needs file, page, position and ticket (true or false).") from None
        if key in seen:
            raise DatasetError(f"Slot {n} repeats {key}.")
        seen.add(key)
        expected = row.get("expected") if is_ticket else None
        if is_ticket:
            if not isinstance(expected, dict) or set(expected) != set(FIELDS):
                raise DatasetError(f"Slot {n} is a ticket, so it needs all six expected fields (null when unreadable).")
            bad = [f for f in FIELDS if not _reference_is_valid(f, expected[f])]
            if bad:
                raise DatasetError(f"Slot {n} has invalid expected values for {', '.join(bad)}. Dates are YYYY-MM-DD, "
                                   "tons a positive number with at most three decimals, null when unreadable.")
        crop = row.get("crop_complete")
        if crop is not None and not isinstance(crop, bool):
            raise DatasetError(f"Slot {n}: crop_complete is true, false or left out.")
        slots.append(Truth(*key, is_ticket, crop, expected))
    if not slots:
        raise DatasetError("The ground-truth file has no slots.")
    invoices = data.get("invoice_checks") or []
    for n, row in enumerate(invoices, start=1):
        if not isinstance(row, dict) or not row.get("invoice_number") or not isinstance(row.get("person_found_discrepancy"), bool):
            raise DatasetError(f"Invoice check {n} needs invoice_number and person_found_discrepancy (true or false).")
    cost = data.get("api_cost_total")
    if cost is not None:
        try:
            cost = Decimal(str(cost))
        except InvalidOperation:
            raise DatasetError("api_cost_total must be a number, as shown on the provider's bill.") from None
        if not cost.is_finite() or cost < 0:
            raise DatasetError("api_cost_total must be zero or more.")
    return {"slots": slots, "invoices": invoices, "api_cost_total": cost, "synthetic": synthetic,
            "labelled_by": str(data["labelled_by"]).strip(), "notes": str(data.get("notes") or "")[:500]}


def combine_truths(truths: list[dict[str, Any]]) -> dict[str, Any]:
    """Several ground-truth files as one. Refuses to mix synthetic and real labels:
    a report must be wholly one kind of evidence, or its numbers mean nothing."""
    if not truths:
        raise DatasetError("No ground-truth files given.")
    kinds = {t["synthetic"] for t in truths}
    if len(kinds) > 1:
        raise DatasetError("These ground-truth files mix synthetic and real labels. Score them in separate "
                           "reports: synthetic results are an engineering check, not real-world accuracy.")
    if len(truths) == 1:
        return truths[0]
    slots, seen = [], set()
    for t in truths:
        for slot in t["slots"]:
            key = (slot.file, slot.page, slot.position)
            if key in seen:
                raise DatasetError(f"{key} is labelled in more than one ground-truth file.")
            seen.add(key)
            slots.append(slot)
    costs = [t["api_cost_total"] for t in truths]
    return {"slots": slots, "invoices": [i for t in truths for i in t["invoices"]],
            "api_cost_total": None if any(c is None for c in costs) else sum(costs, Decimal(0)),
            "synthetic": kinds.pop(), "labelled_by": "; ".join(sorted({t["labelled_by"] for t in truths})),
            "notes": " | ".join(t["notes"] for t in truths if t["notes"])[:500]}


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _field_table(verdicts: dict[str, Counter], total: int) -> dict[str, Any]:
    return {f: {"scored": total, **{k: verdicts[f][k] for k in ("correct", "mismatch", "missing", "unexpected")},
                "accuracy": _rate(verdicts[f]["correct"], total)} for f in FIELDS}


def _current(ticket: Ticket) -> dict[str, Any]:
    return {f: getattr(ticket, f) for f in FIELDS}


def evaluate_workflow(db: Session, organization_id: int, batch_ids: list[int], truth: dict[str, Any]) -> dict[str, Any]:
    batches = list(db.scalars(select(Batch).where(Batch.organization_id == organization_id, Batch.id.in_(batch_ids))))
    if sorted(b.id for b in batches) != sorted(set(batch_ids)):
        raise DatasetError("Some of those batches don't exist in this company.")
    files = list(db.scalars(select(UploadedFile).where(UploadedFile.batch_id.in_(batch_ids))))
    by_name: dict[str, list[UploadedFile]] = {}
    for f in files:
        by_name.setdefault(f.original_name, []).append(f)
    ambiguous = sorted(name for name, found in by_name.items() if len(found) > 1)
    if ambiguous:
        raise DatasetError(f"These file names appear more than once in the batches, so labels can't be tied to one: {ambiguous}")
    tickets = {(t.file_id, t.source_page, t.position): t for t in db.scalars(
        select(Ticket).where(Ticket.batch_id.in_(batch_ids)))}  # batches checked against the company above
    slots = {(s.file_id, s.page, s.position): s for s in db.scalars(select(ScanSlot).where(ScanSlot.batch_id.in_(batch_ids)))}

    detection = Counter()
    raw_verdicts = {f: Counter() for f in FIELDS}
    final_verdicts = {f: Counter() for f in FIELDS}
    corrections = Counter()
    correction_effect = Counter()
    critical = Counter()
    raw_scored = final_scored = tickets_corrected = all_six_raw = all_six_final = 0
    unknown_files = set()
    labelled_positions = set()
    for label in truth["slots"]:
        found = by_name.get(label.file)
        if not found:
            unknown_files.add(label.file)
            continue
        key = (found[0].id, label.page, label.position)
        labelled_positions.add(key)
        ticket, slot = tickets.get(key), slots.get(key)
        has_record = ticket is not None and ticket.status != "skipped"
        if not label.is_ticket:
            detection["labelled_blank"] += 1
            detection["record_for_blank"] += has_record
            continue
        detection["labelled_tickets"] += 1
        detection["with_record"] += has_record
        detection["missed"] += not has_record
        detection["slot_said_empty"] += slot is not None and slot.detection == "empty"
        if label.crop_complete is not None:
            detection["crop_labelled"] += 1
            detection["crop_complete"] += label.crop_complete
        if not has_record:
            continue
        raw = ticket.raw_extraction if isinstance(ticket.raw_extraction, dict) else None
        if raw is not None:
            raw_scored += 1
            verdicts = {f: _field_verdict(f, label.expected[f], raw.get(f)) for f in FIELDS}
            for f, v in verdicts.items():
                raw_verdicts[f][v] += 1
            all_six_raw += all(v == "correct" for v in verdicts.values())
            for f in CRITICAL:
                critical[f"extraction_{f}_wrong"] += verdicts[f] in WRONG
                critical[f"extraction_{f}_missing"] += verdicts[f] == "missing"
        if ticket.status != "verified":
            detection["not_yet_approved"] += 1
            continue
        final_scored += 1
        current = _current(ticket)
        verdicts = {f: _field_verdict(f, label.expected[f], current[f]) for f in FIELDS}
        for f, v in verdicts.items():
            final_verdicts[f][v] += 1
        all_six_final += all(v == "correct" for v in verdicts.values())
        for f in CRITICAL:
            critical[f"approved_{f}_wrong"] += verdicts[f] in WRONG
        if raw is not None:
            changed = [f for f in FIELDS if _normalize(f, raw.get(f)) != _normalize(f, current[f])]
            tickets_corrected += bool(changed)
            for f in changed:
                corrections[f] += 1
                before = _field_verdict(f, label.expected[f], raw.get(f)) == "correct"
                after = verdicts[f] == "correct"
                correction_effect["fixed" if after and not before else "broke" if before and not after else
                                  "still_wrong" if not after else "cosmetic"] += 1

    records_unlabelled = sum(1 for k, t in tickets.items() if t.status != "skipped" and k not in labelled_positions)
    timings = list(db.execute(select(ReviewTiming.seconds, ReviewTiming.fields_corrected).where(
        ReviewTiming.organization_id == organization_id, ReviewTiming.batch_id.in_(batch_ids))))
    jobs = list(db.scalars(select(ProcessingJob).where(ProcessingJob.batch_id.in_(batch_ids))))
    approved = [t for t in tickets.values() if t.status == "verified"]
    elapsed = {}
    for b in batches:
        open_records = [t for t in tickets.values() if t.batch_id == b.id and t.status in ("needs_review", "flagged", "failed")]
        done = [t.verified_at for t in tickets.values() if t.batch_id == b.id and t.verified_at]
        if open_records or not done:
            elapsed[b.id] = None
        else:
            elapsed[b.id] = round((as_utc(max(done)) - as_utc(b.created_at)).total_seconds())

    cost = truth["api_cost_total"]
    report = {
        "evidence": "SYNTHETIC: an engineering check only, not real-world performance" if truth["synthetic"]
                    else "Real documents labelled by a person",
        "labelled_by": truth["labelled_by"],
        "sample": {"batches": len(batches), "files": len(files), "labelled_slots": len(truth["slots"]),
                   "labelled_tickets": detection["labelled_tickets"], "records_without_a_label": records_unlabelled,
                   "label_files_not_found": sorted(unknown_files)},
        "detection": {
            "labelled_tickets": detection["labelled_tickets"], "with_a_record": detection["with_record"],
            "missed": detection["missed"], "missed_with_slot_marked_empty": detection["slot_said_empty"],
            "detection_rate": _rate(detection["with_record"], detection["labelled_tickets"]),
            "labelled_blank": detection["labelled_blank"], "records_for_blank_positions": detection["record_for_blank"],
            "crops_labelled": detection["crop_labelled"], "crops_complete": detection["crop_complete"],
            "crop_completeness": _rate(detection["crop_complete"], detection["crop_labelled"]),
        },
        "extraction": {"tickets_scored": raw_scored, "all_six_correct": all_six_raw,
                       "all_six_rate": _rate(all_six_raw, raw_scored), "fields": _field_table(raw_verdicts, raw_scored)},
        "final": {"tickets_approved_and_scored": final_scored, "not_yet_approved": detection["not_yet_approved"],
                  "all_six_correct": all_six_final, "all_six_rate": _rate(all_six_final, final_scored),
                  "fields": _field_table(final_verdicts, final_scored)},
        "critical_errors": dict(sorted(critical.items())),
        "corrections": {"approved_tickets_with_extraction": sum(1 for t in approved if isinstance(t.raw_extraction, dict)),
                        "tickets_corrected": tickets_corrected, "by_field": {f: corrections[f] for f in FIELDS},
                        "effect": {k: correction_effect[k] for k in ("fixed", "broke", "still_wrong", "cosmetic")}},
        "review_time": {"samples": len(timings), "total_seconds": str(sum((t.seconds for t in timings), Decimal(0))),
                        "median_seconds": str(median(t.seconds for t in timings)) if timings else None,
                        "samples_with_corrections": sum(1 for t in timings if t.fields_corrected)},
        "batch_completion_seconds": {str(k): v for k, v in sorted(elapsed.items())},
        "processing": {"jobs": len(jobs), "attempts": sum(j.attempts or 0 for j in jobs),
                       "jobs_retried": sum(1 for j in jobs if (j.attempts or 0) > 1),
                       "jobs_failed": sum(1 for j in jobs if j.status == "failed"),
                       "files_failed": sum(1 for f in files if f.status == "failed"),
                       "tickets_failed_extraction": sum(1 for t in tickets.values() if t.status == "failed")},
        "cost": {"api_cost_total_entered": str(cost) if cost is not None else None,
                 "approved_tickets": len(approved),
                 "per_approved_ticket": str((cost / len(approved)).quantize(Decimal("0.0001"))) if cost is not None and approved else None},
        "invoice_reconciliation": _invoice_checks(db, organization_id, batch_ids, truth["invoices"]),
        "limitations": _limitations(truth, detection, timings, cost),
    }
    return report


def _invoice_checks(db: Session, organization_id: int, batch_ids: list[int], checks: list[dict]) -> dict[str, Any]:
    """Compares TicketFlow's verdict on each invoice line with the discrepancy a person
    found checking the same invoice by hand."""
    from ..routers.invoices import _line_out
    from .invoice_context import LineContext
    linked = select(InvoiceTicketLink.comparison_id).join(Ticket, Ticket.id == InvoiceTicketLink.ticket_id).where(
        Ticket.batch_id.in_(batch_ids), InvoiceTicketLink.released_at.is_(None))
    items = list(db.scalars(select(InvoiceComparison).where(
        InvoiceComparison.organization_id == organization_id, InvoiceComparison.id.in_(linked))))
    context = LineContext(db, organization_id, items)
    status = {item.id: _line_out(db, item, organization_id, context).status for item in items}
    out: dict[str, Any] = {"lines_linked_to_these_batches": len(items), "by_status": dict(Counter(status.values()))}
    if not checks:
        out["person_checks"] = None
        return out
    verdict = Counter()
    for check in checks:
        lines = [i for i in items if i.invoice_number == str(check["invoice_number"])
                 and (not check.get("material") or _normalize("material_type", i.material_name) == _normalize("material_type", check["material"]))]
        if not lines:
            verdict["not_entered_in_ticketflow"] += 1
            continue
        found = check["person_found_discrepancy"]
        if any(status[i.id] in ("incomplete", "no_verified_tickets") for i in lines):
            # TicketFlow couldn't decide (unapproved or unpriced tickets): neither a catch nor a miss.
            verdict["ticketflow_undecided"] += 1
            continue
        flagged = any(status[i.id] == "discrepancy" for i in lines)
        verdict["both_found" if flagged and found else "missed_by_ticketflow" if found else
                "flagged_only_by_ticketflow" if flagged else "both_clean"] += 1
    out["person_checks"] = {k: verdict[k] for k in ("both_found", "missed_by_ticketflow", "flagged_only_by_ticketflow",
                                                   "both_clean", "ticketflow_undecided", "not_entered_in_ticketflow")}
    return out


def _limitations(truth: dict, detection: Counter, timings: list, cost) -> list[str]:
    notes = []
    if truth["synthetic"]:
        notes.append("Synthetic documents: says the measurement works, nothing about real suppliers, handwriting or scanners.")
    n = detection["labelled_tickets"]
    if n < 200:
        notes.append(f"{n} labelled tickets. Below about 200, one or two errors move a rate by a percent or more; "
                     "read the counts, not the percentages.")
    notes.append("Ground truth is one person's reading of the paper. Have a second person label a sample to check it.")
    if not timings:
        notes.append("No review timings were recorded, so human review time is unknown.")
    else:
        notes.append("Review timings are opt-in and per ticket screen; they leave out sorting paper, scanning and setup.")
    notes.append("Batch completion runs from upload to the last approval, idle time included.")
    if cost is None:
        notes.append("No API cost was entered. TicketFlow doesn't record model calls or tokens, so cost must come from the provider's bill.")
    else:
        notes.append("API cost is the figure a person entered from the bill, divided by approved tickets; it isn't measured.")
    notes.append("Retries are counted per processing job, not per model call.")
    return notes
