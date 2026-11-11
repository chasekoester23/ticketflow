"""Many workers sharing one database: leases, fencing, fairness, limits and usage.

Runs on a real file database with SQLite, and on PostgreSQL in CI. Scans and companies
are fictional, and readers are stand-ins: nothing here calls an AI provider.
"""
from __future__ import annotations

import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.auth import current_user
from app.config import settings
from app.db import Base, LeaseLost, enforce_references, engine_options
from app.main import app
from app.models import Batch, ExtractionUsage, Organization, ProcessingJob, Ticket, UploadedFile, User, WorkerHeartbeat
from app.routers import api as api_module
from app.services import jobqueue, system_health, usage
from app.services.validation import ExtractedTicket

from .conftest import TEST_DATABASE_URL, FixtureExtractor, _reset_postgres
from .fixtures_stack import Stack, build_stack, scan_batch, wait_until
from .sample_data import TICKETS, make_scan_pdf

COMPANIES = (1, 2, 3)


class MeteredReader(FixtureExtractor):
    """Answers like FixtureExtractor, with the usage record the Gemini reader attaches."""

    model = "stand-in-model"

    def extract(self, image):
        ticket, raw = super().extract(image)
        return ticket, {**raw, "_ticketflow": {"model": self.model, "attempts": 1, "seconds": 0.012,
                                               "prompt_tokens": 1000, "output_tokens": 100, "thinking_tokens": 50}}


def _user(org: int, role: str = "owner") -> User:
    return User(id=org, organization_id=org, username=f"owner-{org}", display_name=f"Owner {org}",
                password_hash="x", active=True, role=role, must_change_password=False)


@pytest.fixture()
def shared(tmp_path, monkeypatch, postgres_engine):
    """Three companies on a database every thread reaches over its own connection."""
    if postgres_engine is not None:
        engine = postgres_engine
        _reset_postgres(engine)
    else:
        url = f"sqlite:///{tmp_path / 'workers.db'}"
        engine = create_engine(url, **engine_options(url))
        enforce_references(engine)
        Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as db:
        for org in COMPANIES:
            db.add(Organization(id=org, name=f"Fictional Company {org}"))
            db.flush()
            db.add(_user(org))
        db.commit()
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr("app.main.engine", engine)
    monkeypatch.setattr(api_module, "SessionLocal", Session)
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)

    def override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[db_module.get_db] = override
    with TestClient(app, headers={"X-TicketFlow-Request": "1"}) as client:
        client.Session = Session

        def act_as(org: int, role: str = "owner"):
            app.dependency_overrides[current_user] = lambda: _user(org, role)
            return client

        client.act_as = act_as
        act_as(1)
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)
    if postgres_engine is None:
        engine.dispose()


def _upload(client, org: int, intake: str = "extract") -> int:
    client.act_as(org)
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": intake},
                           files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _job_for(Session, batch_id: int) -> ProcessingJob:
    with Session() as db:
        return db.scalar(select(ProcessingJob).where(ProcessingJob.batch_id == batch_id).order_by(ProcessingJob.id.desc()))


def _expire(Session, job_id: int) -> None:
    with Session() as db:
        db.execute(update(ProcessingJob).where(ProcessingJob.id == job_id)
                   .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        db.commit()


def _tickets(Session, batch_id: int) -> list[Ticket]:
    with Session() as db:
        return list(db.scalars(select(Ticket).where(Ticket.batch_id == batch_id)))


# ------------------------------------------------------------------ leases

def test_a_claim_takes_a_lease_and_the_heartbeat_extends_it(shared):
    batch_id = _upload(shared, 1)
    with shared.Session() as db:
        job_id, token = jobqueue.claim_next(db, "worker-a")
        job = db.get(ProcessingJob, job_id)
        assert (job.batch_id, job.lease_owner, job.attempts) == (batch_id, "worker-a", 1)
        first = job.lease_expires_at
        assert first is not None and job.claimed_at is not None
    time.sleep(0.02)
    with shared.Session() as db:
        assert jobqueue.renew_lease(db, job_id, token) is True
        assert db.get(ProcessingJob, job_id).lease_expires_at > first
        assert jobqueue.renew_lease(db, job_id, "not-the-token") is False


def test_a_lapsed_lease_is_taken_back_and_a_live_one_is_left_alone(shared):
    lapsed, live = _upload(shared, 1), _upload(shared, 2)
    with shared.Session() as db:
        first = jobqueue.claim_next(db, "dead-worker")
        second = jobqueue.claim_next(db, "live-worker")
    assert {_job_for(shared.Session, lapsed).id, _job_for(shared.Session, live).id} == {first[0], second[0]}
    _expire(shared.Session, _job_for(shared.Session, lapsed).id)
    with shared.Session() as db:
        assert jobqueue.recover_expired(db) == 1
    gone, kept = _job_for(shared.Session, lapsed), _job_for(shared.Session, live)
    assert (gone.status, gone.claim_token, gone.lease_owner) == ("pending", None, None)
    assert (kept.status, kept.lease_owner) == ("running", "live-worker")


def test_a_job_left_running_by_an_older_worker_without_a_lease_is_taken_back(shared):
    batch_id = _upload(shared, 1)
    with shared.Session() as db:
        db.execute(update(ProcessingJob).where(ProcessingJob.batch_id == batch_id).values(
            status="running", claim_token="old", attempts=1))
        db.commit()
        assert jobqueue.recover_expired(db) == 1
    assert _job_for(shared.Session, batch_id).status == "pending"


def test_a_job_whose_lease_keeps_lapsing_is_set_aside_after_the_attempt_cap(shared, monkeypatch):
    monkeypatch.setattr(settings, "max_job_attempts", 2)
    batch_id = _upload(shared, 1)
    for _ in range(2):
        with shared.Session() as db:
            job_id, _token = jobqueue.claim_next(db, "crashing")
        _expire(shared.Session, job_id)
        with shared.Session() as db:
            assert jobqueue.recover_expired(db) == 1
    job = _job_for(shared.Session, batch_id)
    assert job.status == "failed" and "stopped the worker 2 times" in job.error
    with shared.Session() as db:
        assert {f.status for f in db.scalars(select(UploadedFile).where(UploadedFile.batch_id == batch_id))} == {"failed"}
        assert jobqueue.claim_next(db) is None


# ------------------------------------------------------------------ fencing

def test_a_worker_that_lost_its_lease_before_starting_writes_nothing(shared):
    batch_id = _upload(shared, 1)
    with shared.Session() as db:
        old = jobqueue.claim_next(db, "slow-worker")
    _expire(shared.Session, old[0])
    with shared.Session() as db:
        jobqueue.recover_expired(db)
        new = jobqueue.claim_next(db, "new-worker")
    assert new[0] == old[0] and new[1] != old[1]
    with shared.Session() as db, pytest.raises(RuntimeError, match="not held"):
        jobqueue.execute_claimed(db, *old, extractor_factory=FixtureExtractor)
    assert _tickets(shared.Session, batch_id) == []
    with shared.Session() as db:
        assert db.get(UploadedFile, db.scalar(select(UploadedFile.id).where(UploadedFile.batch_id == batch_id))).status == "uploaded"
        jobqueue.execute_claimed(db, *new, extractor_factory=FixtureExtractor)
    assert len(_tickets(shared.Session, batch_id)) == 4
    assert _job_for(shared.Session, batch_id).status == "finished"


def test_a_lease_lost_mid_job_stops_the_old_worker_writing_and_nothing_is_doubled(shared):
    """The old worker reads its second ticket while another takes the job over. From then
    on every commit it tries is refused, and the new holder finishes the batch alone."""
    batch_id = _upload(shared, 1)
    with shared.Session() as db:
        old = jobqueue.claim_next(db, "stalled-worker")
    taken = {}

    class Stalls(FixtureExtractor):
        def extract(self, image):
            if self.calls == 1:  # The first ticket is saved; then the lease lapses and is taken.
                _expire(shared.Session, old[0])
                with shared.Session() as other:
                    jobqueue.recover_expired(other)
                    taken["claim"] = jobqueue.claim_next(other, "new-worker")
            return super().extract(image)

    with shared.Session() as db, pytest.raises(LeaseLost):
        jobqueue.execute_claimed(db, *old, extractor_factory=Stalls)
    assert taken["claim"][0] == old[0]
    assert len(_tickets(shared.Session, batch_id)) == 1, "nothing after the takeover was saved by the old worker"
    job = _job_for(shared.Session, batch_id)
    assert (job.status, job.lease_owner) == ("running", "new-worker"), "the old worker didn't close or fail the job"
    with shared.Session() as db:
        jobqueue.execute_claimed(db, *taken["claim"], extractor_factory=FixtureExtractor)
    tickets = _tickets(shared.Session, batch_id)
    assert len(tickets) == 4 and len({(t.source_page, t.position) for t in tickets}) == 4
    assert _job_for(shared.Session, batch_id).status == "finished"


# ------------------------------------------------------------------ fairness and limits

def test_one_company_with_a_backlog_cant_starve_another(shared):
    """A single worker alternates: the company served longest ago goes next."""
    backlog = [_upload(shared, 1) for _ in range(3)]
    other = _upload(shared, 2)
    order = []
    while True:
        with shared.Session() as db:
            claimed = jobqueue.claim_next(db, "only-worker")
            if claimed is None:
                break
            order.append(db.get(ProcessingJob, claimed[0]).batch_id)
            jobqueue.execute_claimed(db, *claimed, extractor_factory=FixtureExtractor)
    assert order[:2] == [backlog[0], other], "the second company waited behind the whole backlog"
    assert sorted(order) == sorted([*backlog, other])


def test_the_company_with_fewest_jobs_running_goes_first(shared):
    first = [_upload(shared, 1) for _ in range(2)]
    second = _upload(shared, 2)
    with shared.Session() as db:
        a = jobqueue.claim_next(db, "a")
        b = jobqueue.claim_next(db, "b")
        assert [db.get(ProcessingJob, c[0]).batch_id for c in (a, b)] == [first[0], second]


def test_a_company_at_its_limit_waits_while_others_proceed(shared, monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_organization", 1)
    first = [_upload(shared, 1) for _ in range(3)]
    with shared.Session() as db:
        assert db.get(ProcessingJob, jobqueue.claim_next(db, "a")[0]).batch_id == first[0]
        assert jobqueue.claim_next(db, "b") is None, "a second job of a company at its limit was claimed"
    other = _upload(shared, 2)
    with shared.Session() as db:
        assert db.get(ProcessingJob, jobqueue.claim_next(db, "b")[0]).batch_id == other


def test_many_workers_at_once_keep_every_company_whole_and_separate(shared, monkeypatch):
    """Three workers drain nine batches from three companies at once, one job per company
    at a time. Every ticket is read once, saved once, and stays with its own company."""
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_organization", 1)
    batches = {org: [_upload(shared, org) for _ in range(3)] for org in COMPANIES}
    running, peak, guard = Counter(), Counter(), threading.Lock()
    real_process = jobqueue.pipeline.process_batch

    def watched(db, batch_id, *args, **kwargs):
        org = db.get(Batch, batch_id).organization_id
        with guard:
            running[org] += 1
            peak[org] = max(peak[org], running[org])
        try:
            time.sleep(0.05)  # Long enough that the others try to claim meanwhile.
            return real_process(db, batch_id, *args, **kwargs)
        finally:
            with guard:
                running[org] -= 1

    monkeypatch.setattr(jobqueue.pipeline, "process_batch", watched)
    errors, start = [], threading.Barrier(3)

    def worker(name):
        try:
            start.wait(timeout=10)
            idle = 0
            while idle < 20:
                with shared.Session() as db:
                    claimed = jobqueue.claim_next(db, name)
                if claimed is None:
                    idle += 1
                    time.sleep(0.05)
                    continue
                idle = 0
                with shared.Session() as db:
                    jobqueue.execute_claimed(db, *claimed, extractor_factory=MeteredReader)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(f"worker-{n}",)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors and not any(t.is_alive() for t in threads)
    assert max(peak.values()) == 1, f"a company ran more jobs at once than its limit: {dict(peak)}"
    with shared.Session() as db:
        assert {j.status for j in db.scalars(select(ProcessingJob))} == {"finished"}
        for org, ids in batches.items():
            for batch_id in ids:
                tickets = list(db.scalars(select(Ticket).where(Ticket.batch_id == batch_id)))
                assert len(tickets) == 4 and len({(t.file_id, t.source_page, t.position) for t in tickets}) == 4
                assert {t.organization_id for t in tickets} == {org}
            assert db.scalar(select(func.count()).select_from(ExtractionUsage).where(ExtractionUsage.organization_id == org)) == 12
        assert db.scalar(select(func.count()).select_from(Ticket)) == 36


# ------------------------------------------------------------------ usage and the monthly limit

def test_each_read_is_recorded_with_its_tokens_and_no_values(shared):
    batch_id = _upload(shared, 2)
    with shared.Session() as db:
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db, "w"), extractor_factory=MeteredReader)
        rows = list(db.scalars(select(ExtractionUsage)))
    assert len(rows) == 4
    assert {(r.organization_id, r.batch_id, r.model, r.prompt_tokens, r.output_tokens, r.thinking_tokens, r.outcome)
            for r in rows} == {(2, batch_id, "stand-in-model", 1000, 100, 50, "ok")}
    assert all(r.ticket_id is not None and r.seconds == Decimal("0.012") for r in rows)
    stored = " ".join(str(getattr(r, c.name)) for r in rows for c in ExtractionUsage.__table__.columns)
    for truth in TICKETS:
        for value in truth.values():
            if isinstance(value, str) and len(value) > 3:
                assert value not in stored


def test_a_failed_read_is_recorded_and_its_ticket_stays_reviewable(shared):
    from app.services.extraction import ExtractionError

    class Refuses:
        model = "stand-in-model"

        def extract(self, _image):
            raise ExtractionError("Gemini request failed: a temporary problem.", kind="temporary", status=503, attempts=3)

    batch_id = _upload(shared, 1)
    with shared.Session() as db:
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db, "w"), extractor_factory=Refuses)
        rows = list(db.scalars(select(ExtractionUsage)))
    assert [(r.outcome, r.attempts, r.model) for r in rows] == [("temporary", 3, "stand-in-model")] * 4
    tickets = _tickets(shared.Session, batch_id)
    assert len(tickets) == 4 and {t.status for t in tickets} == {"failed"}


def test_a_company_over_its_monthly_limit_gets_no_more_reads_and_says_why(shared, monkeypatch):
    monkeypatch.setattr(settings, "monthly_ticket_reads_per_organization", 6)
    first, second = _upload(shared, 1), _upload(shared, 1)
    reader = MeteredReader()
    for _ in (first, second):
        with shared.Session() as db:
            jobqueue.execute_claimed(db, *jobqueue.claim_next(db, "w"), extractor_factory=lambda: reader)
    assert reader.calls == 6
    over = [t for t in _tickets(shared.Session, second) if t.status == "failed"]
    assert len(over) == 2 and all("6 ticket reads for this month" in t.error for t in over)
    other = _upload(shared, 2)  # Another company's allowance is its own.
    with shared.Session() as db:
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db, "w"), extractor_factory=MeteredReader)
    assert {t.status for t in _tickets(shared.Session, other)} == {"needs_review"}


def test_the_usage_report_is_for_owners_and_their_own_company_only(shared, monkeypatch):
    _upload(shared, 1)
    with shared.Session() as db:
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db, "w"), extractor_factory=MeteredReader)
    report = shared.act_as(1).get("/api/system/usage").json()
    assert (report["tickets_read"], report["prompt_tokens"], report["output_tokens"], report["thinking_tokens"]) == (4, 4000, 400, 200)
    assert report["estimated_cost_usd"] is None and "MODEL_PRICES" in report["cost_note"]
    assert shared.act_as(2).get("/api/system/usage").json()["tickets_read"] == 0
    assert shared.act_as(1, "manager").get("/api/system/usage").status_code == 403
    monkeypatch.setattr(settings, "model_prices", {"stand-in-model": {"input": Decimal("1.25"), "output": Decimal("10")}})
    priced = shared.act_as(1).get("/api/system/usage").json()
    # 4,000 input tokens at 1.25 and 600 output and thinking tokens at 10, per million.
    assert priced["estimated_cost_usd"] == "0.0110" and priced["models"][0]["estimated_cost_usd"] == "0.0110"


def test_the_month_starts_on_the_first_in_utc():
    assert usage.month_start(datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)) == datetime(2026, 11, 1, tzinfo=timezone.utc)


# ------------------------------------------------------------------ live workers

def test_live_workers_come_from_the_table_and_old_ones_dont_count(shared):
    now = datetime.now(timezone.utc)
    with shared.Session() as db:
        for name, seen in (("a", now), ("b", now - timedelta(seconds=2)), ("gone", now - timedelta(hours=1))):
            db.add(WorkerHeartbeat(id=name, host="fictional-host", pid=1, mode="database", started_at=now, last_seen_at=seen))
        db.commit()
    status = shared.act_as(1).get("/api/system/status").json()
    assert status["worker"]["running"] is True and status["worker"]["live_workers"] == 2
    check = next(c for c in status["checks"] if c["name"] == "Processing worker")
    assert check["state"] == "ok" and "2 workers running" in check["message"]
    assert "fictional-host" not in str(status)


def test_a_worker_registers_itself_and_leaves_no_row_behind(shared, monkeypatch):
    from scripts import run_worker
    _upload(shared, 1, intake="manual")
    seen = []
    monkeypatch.setattr(run_worker, "SessionLocal", shared.Session)
    real = run_worker.execute_claimed

    def spy(db, *args, **kwargs):
        with shared.Session() as other:
            seen.extend((w.mode, w.current_job_id is not None) for w in other.scalars(select(WorkerHeartbeat)))
        return real(db, *args, **kwargs)

    monkeypatch.setattr(run_worker, "execute_claimed", spy)
    monkeypatch.setattr(settings, "worker_coordination", "database")
    assert run_worker.run(once=True, poll_seconds=0.25) == 0
    assert seen == [("database", True)]
    with shared.Session() as db:
        assert db.scalar(select(func.count()).select_from(WorkerHeartbeat)) == 0
        assert {j.status for j in db.scalars(select(ProcessingJob))} == {"finished"}


def test_two_database_workers_can_run_side_by_side(shared, monkeypatch):
    """No file lock in database mode: a second worker starts, and both drain the queue."""
    from scripts import run_worker
    monkeypatch.setattr(run_worker, "SessionLocal", shared.Session)
    monkeypatch.setattr(run_worker, "get_extractor", MeteredReader)
    monkeypatch.setattr(settings, "worker_coordination", "database")
    ids = [_upload(shared, org) for org in (1, 2, 3, 1)]
    stop = threading.Event()
    results = []
    threads = [threading.Thread(target=lambda: results.append(run_worker.run(once=False, poll_seconds=0.25, stopping=stop)))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        wait_until(lambda: all(_job_for(shared.Session, b).status == "finished" for b in ids), seconds=60, what="all jobs")
    finally:
        stop.set()
        for thread in threads:
            thread.join(30)
    assert results == [0, 0]
    assert sum(len(_tickets(shared.Session, b)) for b in ids) == 16


# ------------------------------------------------------------------ real processes, killed for real

@pytest.fixture()
def stack(tmp_path, postgres_engine):
    running = build_stack(tmp_path, postgres_engine, TEST_DATABASE_URL, _reset_postgres)
    running.start_server()
    try:
        yield running
    finally:
        running.stop_everything()
        if postgres_engine is None:
            running.engine.dispose()


def _stack_job(stack: Stack, batch_id: int) -> ProcessingJob:
    with stack.Session() as db:
        return db.scalar(select(ProcessingJob).where(ProcessingJob.batch_id == batch_id).order_by(ProcessingJob.id.desc()))


def test_database_workers_renew_leases_and_take_over_from_one_killed(stack):
    """Worker A claims a batch and stalls; B runs beside it and does other work, but leaves
    A's job alone for longer than a lease, because A's heartbeat keeps renewing it. A is
    killed; once its lease lapses, B takes the job over and finishes it exactly once."""
    lease = {"WORKER_COORDINATION": "database", "WORKER_LEASE_SECONDS": "15", "WORKER_HEARTBEAT_SECONDS": "1"}
    client = stack.signed_in()
    held = scan_batch(client, 2)
    stalled = stack.start_worker(pause_at="after_claim", **lease)
    wait_until(stack.paused, seconds=60, what="worker A to claim")
    stack.clear_pause()
    beside = stack.start_worker(**lease)
    other = scan_batch(client, 1)
    stack.wait_for_batch(client, other, "ready_for_review", seconds=60)
    time.sleep(17)  # Longer than one lease.
    job = _stack_job(stack, held)
    assert job.status == "running" and job.attempts == 1, "B took a job whose holder was alive"
    os.kill(stalled.pid, 9)
    stalled.wait(10)
    wait_until(lambda: _stack_job(stack, held).status == "finished", seconds=60, what="B to take over")
    tickets = stack.batch_tickets(client, held)
    assert len(tickets) == 8 and len({(t["file_id"], t["source_page"], t["position"]) for t in tickets}) == 8
    assert _stack_job(stack, held).attempts == 2
    assert beside.poll() is None, "B kept running"
    with stack.Session() as db:
        rows = {w.pid: w for w in db.scalars(select(WorkerHeartbeat))}
    assert set(rows) == {stalled.pid, beside.pid}, "each worker recorded itself; the killed one couldn't remove its row"
    assert system_health.as_utc(rows[beside.pid].last_seen_at) > system_health.as_utc(rows[stalled.pid].last_seen_at)
