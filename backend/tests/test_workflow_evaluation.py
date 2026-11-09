"""The whole-workflow evaluation, on a small synthetic batch whose every answer is known.

Synthetic on purpose: these check the arithmetic and the guarantees (read-only,
independent labels required, company scoping), not TicketFlow's real accuracy.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    Batch, InvoiceComparison, InvoiceTicketLink, Organization, ProcessingJob, Project, ReviewTiming, ScanSlot, Ticket,
    UploadedFile,
)
from app.services.evaluation import DatasetError
from app.services.workflow_evaluation import evaluate_workflow, load_truth

START = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
TRUE = {"ticket_number": "1001", "delivery_date": "2026-10-06", "material_type": "Sand", "tonnage": "20.000",
        "supplier": "Ridgeline Aggregates", "job_location": "Main St"}


def _ticket(db, position, *, raw, final, status="verified", file_id=1, batch_id=1):
    db.add(ScanSlot(batch_id=batch_id, file_id=file_id, page=1, position=position, crop_box=[0, 0, 1, 1], crop_path="x",
                    crop_sha256=f"{batch_id}{position}".ljust(64, "0"), ink_fraction=0.1, detection="content"))
    values = {**final, "delivery_date": date.fromisoformat(final["delivery_date"]) if final.get("delivery_date") else None,
              "tonnage": Decimal(final["tonnage"]) if final.get("tonnage") else None}
    db.add(Ticket(batch_id=batch_id, organization_id=1, file_id=file_id, source_page=1, position=position, crop_box=[0, 0, 1, 1],
                  crop_path="x", crop_sha256=f"{batch_id}{position}".ljust(64, "0"), raw_extraction=raw, status=status,
                  tonnage_confirmed=status == "verified", flags=[],
                  verified_at=START + timedelta(minutes=30 + position) if status == "verified" else None, **values))


@pytest.fixture()
def world(tmp_path):
    path = tmp_path / "pilot.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Organization(id=1, name="Fictional"), Organization(id=2, name="Other Fictional")])
        db.flush()
        db.add(Project(id=1, organization_id=1, code="P1", name="Fictional"))
        db.flush()
        db.add(Batch(id=1, organization_id=1, project_id=1, layout="grid_2x2", status="ready_for_review",
                     intake_mode="extract", created_at=START))
        db.add(Batch(id=2, organization_id=2, layout="grid_2x2", status="ready_for_review", intake_mode="extract"))
        db.flush()
        db.add(UploadedFile(id=1, batch_id=1, original_name="scan-01.pdf", storage_path="x", sha256="a" * 64, status="ready_for_review"))
        db.flush()
        # 0: read perfectly and approved as read.
        _ticket(db, 0, raw=TRUE, final=TRUE)
        # 1: model misread the tonnage; a person fixed it.
        _ticket(db, 1, raw={**TRUE, "tonnage": "28.000"}, final=TRUE)
        # 2: model misread the ticket number and nobody caught it: the costly case.
        _ticket(db, 2, raw={**TRUE, "ticket_number": "1007"}, final={**TRUE, "ticket_number": "1007"})
        # 3 is a ticket on paper with no record at all (missed). A blank position 3 slot exists.
        db.add(ScanSlot(batch_id=1, file_id=1, page=1, position=3, crop_box=[0, 0, 1, 1], crop_path="x",
                        crop_sha256="f" * 64, ink_fraction=0.0, detection="empty"))
        db.add(ReviewTiming(organization_id=1, batch_id=1, intake_mode="extract", seconds=Decimal("12.5"), fields_corrected=0))
        db.add(ReviewTiming(organization_id=1, batch_id=1, intake_mode="extract", seconds=Decimal("40.0"), fields_corrected=1))
        db.add(ProcessingJob(batch_id=1, status="done", attempts=2))
        db.flush()
        db.add(InvoiceComparison(id=1, organization_id=1, project_id=1, invoice_number="INV-9", supplier_name="Ridgeline Aggregates",
                                 supplier_key="ridgeline aggregates", material_name="Sand", material_key="sand",
                                 date_from=date(2026, 10, 1), date_to=date(2026, 10, 31), billed_tons=Decimal("60.000"),
                                 billed_unit_price=Decimal("15.0000"), billed_subtotal=Decimal("900.00")))
        db.flush()
        db.add(InvoiceTicketLink(comparison_id=1, ticket_id=1, listed_number="1001", verified_tons=Decimal("20.000")))
        db.commit()
    truth = {"labelled_by": "AB", "labelled_independently": True, "synthetic": True, "api_cost_total": "0.30",
             "slots": [{"file": "scan-01.pdf", "page": 1, "position": p, "ticket": True, "crop_complete": p != 1,
                        "expected": TRUE} for p in range(4)],
             "invoice_checks": [{"invoice_number": "INV-9", "material": "sand", "person_found_discrepancy": True}]}
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(truth))
    yield path, engine, truth_path
    engine.dispose()


def test_extraction_and_final_accuracy_are_scored_separately_against_the_paper(world):
    _, engine, truth_path = world
    with Session(engine) as db:
        report = evaluate_workflow(db, 1, [1], load_truth(truth_path))
    assert report["evidence"].startswith("SYNTHETIC")
    assert report["detection"] | {} == {**report["detection"], "labelled_tickets": 4, "with_a_record": 3, "missed": 1,
                                        "missed_with_slot_marked_empty": 1, "crops_labelled": 4, "crops_complete": 3}
    assert report["extraction"]["tickets_scored"] == 3 and report["extraction"]["all_six_correct"] == 1
    assert report["extraction"]["fields"]["tonnage"]["mismatch"] == 1
    assert report["final"]["tickets_approved_and_scored"] == 3 and report["final"]["all_six_correct"] == 2
    assert report["critical_errors"] == {"approved_ticket_number_wrong": 1, "approved_tonnage_wrong": 0,
                                         "extraction_ticket_number_missing": 0, "extraction_ticket_number_wrong": 1,
                                         "extraction_tonnage_missing": 0, "extraction_tonnage_wrong": 1}
    assert report["corrections"]["tickets_corrected"] == 1 and report["corrections"]["by_field"]["tonnage"] == 1
    assert report["corrections"]["effect"] == {"fixed": 1, "broke": 0, "still_wrong": 0, "cosmetic": 0}


def test_time_retries_cost_and_reconciliation_are_reported_with_their_limits(world):
    _, engine, truth_path = world
    with Session(engine) as db:
        report = evaluate_workflow(db, 1, [1], load_truth(truth_path))
    assert report["review_time"] == {"samples": 2, "total_seconds": "52.5", "median_seconds": "26.25", "samples_with_corrections": 1}
    assert report["batch_completion_seconds"] == {"1": 32 * 60}
    assert report["processing"] | {} == {**report["processing"], "jobs": 1, "attempts": 2, "jobs_retried": 1}
    assert report["cost"] == {"api_cost_total_entered": "0.30", "approved_tickets": 3, "per_approved_ticket": "0.1000"}
    assert report["invoice_reconciliation"]["lines_linked_to_these_batches"] == 1
    # The linked tickets have no locked rate, so TicketFlow can't decide; that's not a catch.
    assert report["invoice_reconciliation"]["by_status"] == {"incomplete": 1}
    assert report["invoice_reconciliation"]["person_checks"]["ticketflow_undecided"] == 1
    assert report["invoice_reconciliation"]["person_checks"]["both_found"] == 0
    assert any("4 labelled tickets" in note for note in report["limitations"])


def test_labels_must_be_independent_complete_and_valid(world, tmp_path):
    _, _, truth_path = world
    good = json.loads(truth_path.read_text())
    for broken in ({**good, "labelled_independently": False}, {**good, "labelled_by": ""},
                   {**good, "slots": [{**good["slots"][0], "expected": {**TRUE, "tonnage": "twenty"}}]},
                   {**good, "slots": [{**good["slots"][0], "expected": {"ticket_number": "1"}}]},
                   {**good, "slots": good["slots"][:1] * 2}, {**good, "api_cost_total": "-1"}):
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(broken))
        with pytest.raises(DatasetError):
            load_truth(path)


def test_another_companys_batch_is_refused(world):
    _, engine, truth_path = world
    with Session(engine) as db, pytest.raises(DatasetError):
        evaluate_workflow(db, 1, [1, 2], load_truth(truth_path))


def test_the_script_opens_the_database_read_only_and_leaves_it_unchanged(world, tmp_path, capsys):
    import importlib.util
    path, engine, truth_path = world
    engine.dispose()
    before = path.read_bytes()
    spec = importlib.util.spec_from_file_location("evaluate_workflow", __file__.replace("tests/test_workflow_evaluation.py", "scripts/evaluate_workflow.py"))
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    out = tmp_path / "report.json"
    assert script.main(["--database", str(path), "--organization", "1", "--batch", "1", "--truth", str(truth_path), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["final"]["all_six_correct"] == 2
    assert path.read_bytes() == before
    read_only = script.read_only_engine(str(path))
    with read_only.connect() as connection, pytest.raises(Exception, match="readonly|read-only"):
        connection.exec_driver_sql("DELETE FROM tickets")
    read_only.dispose()
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM tickets").fetchone() == (3,)
