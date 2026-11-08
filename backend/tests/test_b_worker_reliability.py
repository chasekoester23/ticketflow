"""A worker that dies, a job that can't finish, and running work twice.

Scans are fictional, and the extractor is a stand-in. None of this says anything
about how a real AI provider behaves.
"""
import json
import logging
import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite

from app.config import settings
from app.models import AuditEvent, Batch, ProcessingJob, ScanSlot, Ticket, UploadedFile
from app.routers import api as api_module
from app.services import job_log, jobqueue, pipeline

from .conftest import TEST_DATABASE_URL, FixtureExtractor
from .sample_data import make_scan_pdf

needs_postgres = pytest.mark.skipif(not TEST_DATABASE_URL, reason="Row locks need PostgreSQL (CI).")
SECRET = "SECRET-TICKET-VALUE"


def _db():
    return api_module.SessionLocal()


def _upload(client, monkeypatch, *names) -> int:
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    files = [("files", (name, make_scan_pdf(), "application/pdf")) for name in names or ("scan.pdf",)]
    response = client.post("/api/batches", data={"layout": "grid_2x2"}, files=files)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _crash_repeatedly(times: int) -> int:
    """What a worker that dies mid-job leaves behind, each time: a claimed job, then a
    new worker starting up. Returns the job id."""
    job_id = None
    for _ in range(times):
        with _db() as db:
            jobqueue.recover_interrupted(db)
            claimed = jobqueue.claim_next(db)
            assert claimed is not None
            job_id = claimed[0]
    return job_id


# ------------------------------------------------------------------ the retry cap

def test_a_job_that_keeps_crashing_the_worker_stops_being_retried(client, monkeypatch):
    batch_id = _upload(client, monkeypatch)
    job_id = _crash_repeatedly(settings.max_job_attempts)
    with _db() as db:
        assert jobqueue.recover_interrupted(db) == 1
        job = db.get(ProcessingJob, job_id)
        assert job.status == "failed" and job.claim_token is None and job.attempts == settings.max_job_attempts
        assert jobqueue.claim_next(db) is None  # Before the cap existed, this reclaimed it forever.
    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "failed"
    message = batch["files"][0]["error"]
    assert batch["files"][0]["status"] == "failed"
    # Says what happened and what a person can do about it.
    assert f"{settings.max_job_attempts} times" in message and "Retry" in message and "by hand" in message


def test_a_job_under_the_cap_is_still_retried(client, monkeypatch):
    _upload(client, monkeypatch)
    job_id = _crash_repeatedly(settings.max_job_attempts - 1)
    with _db() as db:
        assert jobqueue.recover_interrupted(db) == 1
        job = db.get(ProcessingJob, job_id)
        assert job.status == "pending" and job.claim_token is None
        assert jobqueue.claim_next(db)[0] == job_id


def test_the_cap_is_configurable(client, monkeypatch):
    monkeypatch.setattr(settings, "max_job_attempts", 1)
    _upload(client, monkeypatch)
    job_id = _crash_repeatedly(1)
    with _db() as db:
        jobqueue.recover_interrupted(db)
        assert db.get(ProcessingJob, job_id).status == "failed"


def test_one_poison_upload_doesnt_stop_the_uploads_behind_it(client, monkeypatch):
    from scripts import run_worker

    poison = _upload(client, monkeypatch, "poison.pdf")
    _crash_repeatedly(settings.max_job_attempts)
    fine = _upload(client, monkeypatch, "fine.pdf")
    monkeypatch.setattr(run_worker, "SessionLocal", api_module.SessionLocal)
    monkeypatch.setattr(run_worker, "get_extractor", FixtureExtractor)
    assert run_worker.run(once=True, poll_seconds=0.25) == 0
    assert client.get(f"/api/batches/{poison}").json()["status"] == "failed"
    assert client.get(f"/api/batches/{fine}").json()["status"] == "ready_for_review"
    assert len(client.get(f"/api/batches/{fine}/tickets").json()) == 4


def test_giving_up_touches_nothing_already_finished_or_verified(client, monkeypatch):
    batch_id = _upload(client, monkeypatch, "done.pdf", "poison.pdf")
    # The worker finishes one file, then dies on the next.
    with _db() as db:
        job_id, _token = jobqueue.claim_next(db)
        batch = db.get(Batch, batch_id)
        batch.status = "processing"
        db.commit()
        pipeline.process_file(db, batch.files[0], batch.layout, FixtureExtractor())
    tickets = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert len(tickets) == 4
    first = tickets[0]["id"]
    assert client.patch(f"/api/tickets/{first}", json={"tonnage": "26.250"}).status_code == 200
    verified = client.post(f"/api/tickets/{first}/verify")
    assert verified.status_code == 200
    audit = client.get(f"/api/tickets/{first}/audit").json()
    with _db() as db:
        db.get(ProcessingJob, job_id).attempts = settings.max_job_attempts
        db.get(Batch, batch_id).files[1].status = "processing"
        db.commit()
        assert jobqueue.recover_interrupted(db) == 1
    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "failed"
    assert [f["status"] for f in batch["files"]] == ["ready_for_review", "failed"]
    assert batch["files"][0]["error"] is None
    after = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert after[0]["id"] == first and after[0]["status"] == "verified" and after[0]["tonnage"] == "26.250"
    assert after[0]["revision"] == verified.json()["revision"]
    assert client.get(f"/api/tickets/{first}/audit").json() == audit
    assert len(after) == 4


def test_a_person_can_still_retry_after_the_worker_gave_up(client, monkeypatch):
    batch_id = _upload(client, monkeypatch)
    _crash_repeatedly(settings.max_job_attempts)
    with _db() as db:
        jobqueue.recover_interrupted(db)
    response = client.post(f"/api/batches/{batch_id}/retry")
    assert response.status_code == 200, response.text
    with _db() as db:
        new_job = db.scalar(select(ProcessingJob).where(ProcessingJob.status == "pending"))
        assert new_job.attempts == 0 and new_job.only_failed is True  # A person's decision gets a fresh count.
        claimed = jobqueue.claim_next(db)
        jobqueue.execute_claimed(db, *claimed, extractor_factory=FixtureExtractor)
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "ready_for_review"


def test_giving_up_after_everything_finished_doesnt_strand_the_batch(client, monkeypatch):
    """The worker can die after the last file but before recording the job as done.
    Marking that batch failed would leave nothing to retry and no way out."""
    batch_id = _upload(client, monkeypatch)
    with _db() as db:
        job_id, _token = jobqueue.claim_next(db)
        batch = db.get(Batch, batch_id)
        pipeline.process_batch(db, batch.id, FixtureExtractor())
        db.get(ProcessingJob, job_id).attempts = settings.max_job_attempts
        db.commit()
        assert jobqueue.recover_interrupted(db) == 1
        assert db.get(ProcessingJob, job_id).status == "failed"
    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "ready_for_review"
    assert batch["files"][0]["status"] == "ready_for_review" and batch["files"][0]["error"] is None


def test_a_job_whose_batch_is_gone_still_gives_up_cleanly(client):
    job = ProcessingJob(id=999, batch_id=99999, status="running", attempts=settings.max_job_attempts)
    with _db() as db:
        jobqueue._give_up(db, job)
    assert job.status == "failed" and "stopped the worker" in job.error


# ------------------------------------------------------------------ claiming

def test_claiming_skips_locked_rows_on_postgresql_only():
    statement = jobqueue._next_pending()
    assert "FOR UPDATE SKIP LOCKED" in str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" not in str(statement.compile(dialect=sqlite.dialect()))


@needs_postgres
def test_a_second_worker_passes_over_a_job_another_is_claiming(client, monkeypatch):
    first_batch = _upload(client, monkeypatch, "a.pdf")
    second_batch = _upload(client, monkeypatch, "b.pdf")
    holder = _db()
    try:
        held = holder.scalar(jobqueue._next_pending())  # Holds the row lock, as a claim in progress does.
        assert held is not None
        with _db() as db:
            claimed = jobqueue.claim_next(db)  # Without SKIP LOCKED this waits, then fails on the lock timeout.
            assert claimed is not None and claimed[0] != held
            job = db.get(ProcessingJob, claimed[0])
            assert job.batch_id == second_batch and job.status == "running"
    finally:
        holder.rollback()
        holder.close()
    with _db() as db:
        assert db.scalar(select(ProcessingJob).where(ProcessingJob.batch_id == first_batch)).status == "pending"


@needs_postgres
def test_many_workers_never_claim_one_job_twice(client, monkeypatch):
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    with _db() as db:
        batch = Batch(organization_id=1, layout="single", status="uploaded", intake_mode="manual")
        db.add(batch)
        db.flush()
        db.add_all(ProcessingJob(batch_id=batch.id, status="pending") for _ in range(30))
        db.commit()
    claims: list[int] = []
    guard = threading.Lock()
    start = threading.Barrier(6)

    def worker():
        start.wait(timeout=10)
        while True:
            with _db() as db:
                claimed = jobqueue.claim_next(db)
            if claimed is None:
                return
            with guard:
                claims.append(claimed[0])

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads)
    assert len(claims) == 30 and len(set(claims)) == 30


# ------------------------------------------------------------------ running work twice

class CountingExtractor(FixtureExtractor):
    pass


def _everything(db, batch_id):
    tickets = [(t.id, t.source_page, t.position, t.status, t.ticket_number, t.tonnage, t.supplier, t.lock_version, t.approved_cost,
                t.applied_unit_price, t.verified_by_user_id, t.crop_path, t.crop_sha256)
               for t in db.scalars(select(Ticket).where(Ticket.batch_id == batch_id).order_by(Ticket.id))]
    slots = [(s.id, s.page, s.position, s.crop_path, s.crop_sha256, s.rotation, s.decision)
             for s in db.scalars(select(ScanSlot).where(ScanSlot.batch_id == batch_id).order_by(ScanSlot.id))]
    audit = db.scalar(select(func.count()).select_from(AuditEvent)
                      .join(Ticket, Ticket.id == AuditEvent.ticket_id).where(Ticket.batch_id == batch_id))
    return tickets, slots, audit


def _review_some(client, batch_id):
    """One corrected and approved, one corrected, one flagged, one skipped."""
    rows = client.get(f"/api/batches/{batch_id}/tickets").json()
    client.patch(f"/api/tickets/{rows[0]['id']}", json={"tonnage": "26.250", "supplier": "Corrected Supplier"})
    assert client.post(f"/api/tickets/{rows[0]['id']}/verify").status_code == 200
    client.patch(f"/api/tickets/{rows[1]['id']}", json={"ticket_number": "CORRECTED-1"})
    client.post(f"/api/tickets/{rows[2]['id']}/flag", json={"note": "Check this"})
    client.post(f"/api/tickets/{rows[3]['id']}/skip")


@pytest.mark.parametrize("only_failed", [False, True])
def test_rerunning_a_whole_batch_changes_nothing_and_calls_nothing(client, monkeypatch, only_failed):
    batch_id = _upload(client, monkeypatch)
    with _db() as db:
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=FixtureExtractor)
    _review_some(client, batch_id)
    with _db() as db:
        before = _everything(db, batch_id)
        extractor = CountingExtractor()
        jobqueue.enqueue(db, batch_id, only_failed=only_failed)
        db.commit()
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=lambda: extractor)
    with _db() as db:
        assert _everything(db, batch_id) == before
    assert extractor.calls == 0  # A repeat costs nothing and overwrites nothing.
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "ready_for_review"


def test_running_a_batch_many_times_never_duplicates_a_ticket_or_slot(client, monkeypatch):
    batch_id = _upload(client, monkeypatch)
    with _db() as db:
        for _ in range(4):
            pipeline.process_batch(db, batch_id, FixtureExtractor())
        assert db.scalar(select(func.count()).select_from(Ticket).where(Ticket.batch_id == batch_id)) == 4
        assert db.scalar(select(func.count()).select_from(ScanSlot).where(ScanSlot.batch_id == batch_id)) == 4
        assert db.scalar(select(func.count()).select_from(UploadedFile).where(UploadedFile.batch_id == batch_id)) == 1


def test_a_crash_partway_through_a_file_saves_nothing_from_that_file(client, monkeypatch):
    """Each file commits once, at its end. So a crash can't leave half a file's
    tickets behind for a rerun to trip over."""
    batch_id = _upload(client, monkeypatch)

    class DiesOnTheThirdCrop(FixtureExtractor):
        def extract(self, image):
            if self.calls == 2:
                raise KeyboardInterrupt  # Stands in for the process dying: nothing in pipeline catches it.
            return super().extract(image)

    with _db() as db:
        job_id, _token = jobqueue.claim_next(db)
        batch = db.get(Batch, batch_id)
        with pytest.raises(KeyboardInterrupt):
            pipeline.process_batch(db, batch.id, DiesOnTheThirdCrop())
    with _db() as db:
        assert db.scalar(select(func.count()).select_from(Ticket).where(Ticket.batch_id == batch_id)) == 0
        assert jobqueue.recover_interrupted(db) == 1
        claimed = jobqueue.claim_next(db)
        assert claimed[0] == job_id
        jobqueue.execute_claimed(db, *claimed, extractor_factory=FixtureExtractor)
    rows = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert len(rows) == 4 and len({(r["source_page"], r["position"]) for r in rows}) == 4


# ------------------------------------------------------------------ logs

def _lines(caplog) -> list[dict]:
    formatter = job_log.JsonLines()
    return [json.loads(formatter.format(record)) for record in caplog.records]


def test_worker_logs_are_json_with_job_and_batch_ids(client, monkeypatch, caplog):
    batch_id = _upload(client, monkeypatch)
    with caplog.at_level(logging.INFO, logger="app.services"):
        with _db() as db:
            claimed = jobqueue.claim_next(db)
            jobqueue.execute_claimed(db, *claimed, extractor_factory=FixtureExtractor)
    lines = _lines(caplog)
    events = {line["event"]: line for line in lines}
    assert {"job_started", "job_finished"} <= set(events)
    for name in ("job_started", "job_finished"):
        assert events[name]["job_id"] == claimed[0] and events[name]["batch_id"] == batch_id and events[name]["attempt"] == 1
    assert events["job_finished"]["batch_status"] == "ready_for_review"
    assert all("at" in line and "level" in line for line in lines)


def test_recovery_says_what_it_did_to_which_job(client, monkeypatch, caplog):
    batch_id = _upload(client, monkeypatch)
    job_id = _crash_repeatedly(settings.max_job_attempts)
    with caplog.at_level(logging.INFO, logger="app.services"):
        with _db() as db:
            jobqueue.recover_interrupted(db)
    gave_up = next(line for line in _lines(caplog) if line["event"] == "job_gave_up")
    assert gave_up["job_id"] == job_id and gave_up["batch_id"] == batch_id and gave_up["attempt"] == settings.max_job_attempts
    assert gave_up["level"] == "ERROR"


def test_a_failure_logs_its_kind_and_where_but_never_what_it_says(client, monkeypatch, caplog):
    """An exception's text can quote a ticket: pydantic errors quote their input."""
    batch_id = _upload(client, monkeypatch)

    class Blows(FixtureExtractor):
        def extract(self, image):
            raise ValueError(f"could not parse ticket number {SECRET}")

    with caplog.at_level(logging.INFO, logger="app.services"):
        with _db() as db:
            jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=Blows)
    lines = _lines(caplog)
    assert SECRET not in json.dumps(lines)
    failed = next(line for line in lines if line["event"] == "file_failed")
    assert failed["batch_id"] == batch_id and failed["error"] == "ValueError"
    assert any("extract" in frame for frame in failed["trace"])


def test_a_crashing_factory_logs_no_message_either(client, monkeypatch, caplog):
    _upload(client, monkeypatch)

    def crash():
        raise RuntimeError(SECRET)

    with caplog.at_level(logging.INFO, logger="app.services"):
        with _db() as db:
            jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=crash)
    lines = _lines(caplog)
    assert SECRET not in json.dumps(lines)
    assert next(line for line in lines if line["event"] == "job_failed")["error"] == "RuntimeError"


def test_another_librarys_log_text_is_left_out():
    """A library logging at the wrong moment could quote a request or a document."""
    record = logging.LogRecord("httpx", logging.WARNING, __file__, 1, "POST https://example.invalid?key=%s", (SECRET,), None)
    line = json.loads(job_log.JsonLines().format(record))
    assert SECRET not in json.dumps(line)
    assert line["logger"] == "httpx" and line["level"] == "WARNING"


def test_configuring_worker_logs_replaces_the_handlers_and_is_repeatable():
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        job_log.configure()
        job_log.configure()
        ours = [h for h in root.handlers if isinstance(h.formatter, job_log.JsonLines)]
        assert len(ours) == 1
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("app").getEffectiveLevel() == logging.INFO
    finally:
        root.handlers[:] = saved
        root.setLevel(level)
        logging.getLogger("app").setLevel(logging.NOTSET)
        logging.getLogger("ticketflow").setLevel(logging.NOTSET)
