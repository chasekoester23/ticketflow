"""Durable extraction queue regression tests on synthetic scans only."""
from sqlalchemy import select

from app.db import get_db
from app.main import app
from app.config import settings
from app.routers import api as api_module
from app.models import Batch, ProcessingJob
from app.services import jobqueue, pipeline
from app.services.extraction import ExtractionError

from .conftest import FixtureExtractor
from .sample_data import make_scan_pdf


def _pending_upload(client, monkeypatch, files=None):
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    scans = files or [("scan.pdf", make_scan_pdf(), "application/pdf")]
    result = client.post(
        "/api/batches", data={"layout": "grid_2x2"},
        files=[("files", item) for item in scans],
    )
    assert result.status_code == 200, result.text
    return result.json()["id"]


def _db():
    return api_module.SessionLocal()


def test_upload_survives_web_request_and_waits_for_durable_worker(client, monkeypatch):
    batch_id = _pending_upload(client, monkeypatch)
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "uploaded"
    assert client.get(f"/api/batches/{batch_id}/tickets").json() == []

    # New session simulates the worker process opening persisted work.
    with _db() as db:
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.batch_id == batch_id))
        assert job and job.status == "pending" and job.attempts == 0
        claimed = jobqueue.claim_next(db)
        assert claimed and claimed[0] == job.id
        jobqueue.execute_claimed(db, *claimed, extractor_factory=FixtureExtractor)

    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "ready_for_review"
    assert len(client.get(f"/api/batches/{batch_id}/tickets").json()) == 4
    with _db() as db:
        assert db.get(ProcessingJob, claimed[0]).status == "finished"
        assert db.get(ProcessingJob, claimed[0]).attempts == 1


def test_restarted_worker_replays_only_unfinished_files_and_preserves_verification(client, monkeypatch):
    one = ("scan-one.pdf", make_scan_pdf(), "application/pdf")
    two = ("scan-two.pdf", make_scan_pdf(), "application/pdf")
    batch_id = _pending_upload(client, monkeypatch, files=[one, two])

    # Worker claims job and completes the first file. It then dies before
    # recording job completion. This models SIGKILL/power loss.
    with _db() as db:
        job_id, old_token = jobqueue.claim_next(db)
        batch = db.get(Batch, batch_id)
        batch.status = "processing"
        db.commit()
        pipeline.process_file(db, batch.files[0], batch.layout, FixtureExtractor())
        assert batch.files[0].status == "ready_for_review"
        assert batch.files[1].status == "uploaded"

    first = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert len(first) == 4
    first_id = first[0]["id"]
    corrected = client.patch(f"/api/tickets/{first_id}", json={"tonnage": "26.250"})
    assert corrected.status_code == 200
    verified = client.post(f"/api/tickets/{first_id}/verify")
    assert verified.status_code == 200
    audit_before = client.get(f"/api/tickets/{first_id}/audit").json()

    with _db() as db:
        assert jobqueue.recover_interrupted(db) == 1
        resumed = jobqueue.claim_next(db)
        assert resumed and resumed[0] == job_id and resumed[1] != old_token
        with __import__("pytest").raises(RuntimeError, match="not held"):
            jobqueue.execute_claimed(db, job_id, old_token, extractor_factory=FixtureExtractor)
        jobqueue.execute_claimed(db, *resumed, extractor_factory=FixtureExtractor)

    after = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert len(after) == 8
    assert first_id == after[0]["id"]
    assert after[0]["status"] == "verified"
    assert after[0]["tonnage"] == "26.250"
    assert after[0]["tonnage_confirmed"] is True
    assert after[0]["price_snapshot_state"] == "unpriced"
    assert client.get(f"/api/tickets/{first_id}/audit").json() == audit_before
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "ready_for_review"

    with _db() as db:
        assert db.get(ProcessingJob, job_id).attempts == 2
        assert db.get(ProcessingJob, job_id).status == "finished"


def test_missing_api_key_fails_visibly_and_manual_retry_only_enqueues_once(client, monkeypatch):
    batch_id = _pending_upload(client, monkeypatch)
    with _db() as db:
        claimed = jobqueue.claim_next(db)
        def not_configured():
            raise ExtractionError("ANTHROPIC_API_KEY is not set")
        jobqueue.execute_claimed(db, *claimed, extractor_factory=not_configured)

    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "failed"
    assert batch["files"][0]["status"] == "failed"
    assert "ANTHROPIC_API_KEY" in batch["files"][0]["error"]
    assert client.get(f"/api/batches/{batch_id}/tickets").json() == []

    first = client.post(f"/api/batches/{batch_id}/retry")
    assert first.status_code == 200
    assert first.json()["status"] == "processing"
    assert client.post(f"/api/batches/{batch_id}/retry").status_code == 409
    with _db() as db:
        jobs = db.scalars(
            select(ProcessingJob).where(ProcessingJob.batch_id == batch_id)
            .order_by(ProcessingJob.id)
        ).all()
        assert len(jobs) == 2
        assert [j.status for j in jobs] == ["finished", "pending"]
        assert jobs[1].only_failed is True
        claimed = jobqueue.claim_next(db)
        jobqueue.execute_claimed(db, *claimed, extractor_factory=FixtureExtractor)

    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "ready_for_review"
    assert len(client.get(f"/api/batches/{batch_id}/tickets").json()) == 4


def test_upload_validation_never_creates_phantom_queue_jobs(client, monkeypatch):
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    invalid = client.post(
        "/api/batches", data={"layout": "grid_2x2"},
        files=[("files", ("fake.pdf", b"invalid document", "application/pdf"))],
    )
    assert invalid.status_code == 400
    with _db() as db:
        assert db.scalars(select(ProcessingJob)).all() == []


def test_worker_exception_marks_batch_failed_without_losing_completed_files(client, monkeypatch):
    batch_id = _pending_upload(client, monkeypatch)
    with _db() as db:
        claimed = jobqueue.claim_next(db)
        def crash():
            raise RuntimeError("simulated unexpected provider initialization crash")
        jobqueue.execute_claimed(db, *claimed, extractor_factory=crash)
    with _db() as db:
        job = db.get(ProcessingJob, claimed[0])
        assert job.status == "failed"
        assert "retry" in job.error.lower()
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "failed"
    assert client.post(f"/api/batches/{batch_id}/retry").status_code == 200
