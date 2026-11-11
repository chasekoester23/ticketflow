"""Persistent extraction queue, shared safely by one or many worker processes.

The FastAPI server records jobs transactionally and never executes extraction. Workers
claim jobs from the database:

- **Claiming** is atomic. On PostgreSQL, claims take turns on an advisory lock and
  pass over rows another transaction holds (FOR UPDATE SKIP LOCKED). On SQLite, the
  claim is one guarded UPDATE, and SQLite has one writer.
- **Leases.** A claimed job belongs to its worker until `lease_expires_at`. The worker
  renews it from its heartbeat thread. Once a lease lapses, any worker sets the job
  back to pending (`recover_expired`), or sets it aside after settings.max_job_attempts.
- **Fencing.** Every commit a worker makes for a job first checks, holding the job's
  row, that its claim token is still current (db.LeaseLost otherwise). A worker that
  lost its lease therefore can't write over the next holder's work.
- **Fairness.** The next job goes to the company with the fewest jobs running, then
  the one served longest ago, then the oldest job. MAX_CONCURRENT_JOBS_PER_ORGANIZATION
  caps how many of one company's jobs run at once.

Local mode (run_worker.py on SQLite) still holds the single-machine file lock and may
recover every running job at start (`recover_interrupted`), since no other worker can
be alive. AI calls are at-least-once. Logs here hold ids only. See job_log.py.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session, aliased

from ..config import settings
from ..db import LeaseLost, scope_to_organization
from ..models import Batch, ProcessingJob
from . import pipeline
from .extraction import ExtractionError

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
INLINE_OWNER = "inline"
# Any constant: claims across every worker take turns on it, so per-company limits hold exactly.
CLAIM_LOCK_KEY = 7_311_204_651
# How far back "the company served longest ago" looks, so the claim never scans all history.
FAIRNESS_WINDOW = timedelta(hours=6)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(db: Session, batch_id: int, *, only_failed: bool = False) -> ProcessingJob:
    """Add a durable job in the caller's transaction, never commit here."""
    job = ProcessingJob(
        batch_id=batch_id, status=PENDING,
        only_failed=only_failed, attempts=0,
    )
    db.add(job)
    return job


def _company_of(job) -> object:
    # Correlated explicitly: it sits two subqueries deep, beyond automatic correlation.
    return select(Batch.organization_id).where(Batch.id == job.batch_id).correlate_except(Batch).scalar_subquery()


def _running_for(company) -> object:
    other, other_batch = aliased(ProcessingJob), aliased(Batch)
    return (select(func.count()).select_from(other).join(other_batch, other_batch.id == other.batch_id)
            .where(other.status == RUNNING, other_batch.organization_id == company).scalar_subquery())


def _served_at(company, since: datetime) -> object:
    other, other_batch = aliased(ProcessingJob), aliased(Batch)
    return (select(func.max(other.claimed_at)).select_from(other).join(other_batch, other_batch.id == other.batch_id)
            .where(other.claimed_at >= since, other_batch.organization_id == company).scalar_subquery())


def _under_limit(job) -> list:
    limit = settings.max_concurrent_jobs_per_organization
    return [_running_for(_company_of(job)) < limit] if limit > 0 else []


def _next_pending(now: datetime | None = None):
    """The next job to run, fairly across companies. On PostgreSQL a row another worker
    has locked is passed over rather than waited for. Subqueries lock nothing, so a
    person's open batch never holds up a claim. SQLite renders no FOR UPDATE: it has one
    writer, and the guarded update in claim_next settles any tie."""
    company = _company_of(ProcessingJob)
    since = (now or _now()) - FAIRNESS_WINDOW
    return (select(ProcessingJob.id).where(ProcessingJob.status == PENDING, *_under_limit(ProcessingJob))
            .order_by(_running_for(company), _served_at(company, since).asc().nulls_first(), ProcessingJob.id)
            .limit(1).with_for_update(skip_locked=True))


def _lease_values(now: datetime) -> dict:
    return {"lease_expires_at": now + timedelta(seconds=settings.worker_lease_seconds), "heartbeat_at": now}


def claim_next(db: Session, worker_id: str | None = None) -> tuple[int, str] | None:
    """Atomically transition one persisted pending job to running, under a lease."""
    now = _now()
    if db.get_bind().dialect.name == "postgresql":
        # Held until the commit below, so two workers can't both count a company at its limit.
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLAIM_LOCK_KEY})
    candidate = db.scalar(_next_pending(now))
    if candidate is None:
        db.commit()
        return None
    token = secrets.token_hex(24)
    result = db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == candidate, ProcessingJob.status == PENDING, *_under_limit(ProcessingJob))
        .values(
            status=RUNNING, claim_token=token, attempts=ProcessingJob.attempts + 1, error=None,
            lease_owner=(worker_id or INLINE_OWNER)[:64], claimed_at=now, **_lease_values(now),
        )
    )
    db.commit()
    return (candidate, token) if result.rowcount == 1 else None


def renew_lease(db: Session, job_id: int, token: str) -> bool:
    """Extend the lease on a job this worker still holds. False means it was taken away."""
    result = db.execute(update(ProcessingJob).where(
        ProcessingJob.id == job_id, ProcessingJob.claim_token == token, ProcessingJob.status == RUNNING,
    ).values(**_lease_values(_now())))
    db.commit()
    return result.rowcount == 1


def _released() -> dict:
    return {"claim_token": None, "lease_owner": None, "lease_expires_at": None}


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """Set back to pending every running job whose lease has lapsed, or set it aside once
    it has used up its attempts. Safe for any number of workers at once: each change is
    guarded by the token it saw, and a lease renewed meanwhile is left alone."""
    now = now or _now()
    lapsed = or_(ProcessingJob.lease_expires_at.is_(None), ProcessingJob.lease_expires_at < now)
    rows = db.execute(select(ProcessingJob.id, ProcessingJob.claim_token, ProcessingJob.attempts)
                      .where(ProcessingJob.status == RUNNING, lapsed).order_by(ProcessingJob.id)
                      .with_for_update(skip_locked=True)).all()
    recovered = 0
    for job_id, token, attempts in rows:
        held_by = ProcessingJob.claim_token.is_(None) if token is None else ProcessingJob.claim_token == token
        guard = (ProcessingJob.id == job_id, ProcessingJob.status == RUNNING, held_by, lapsed)
        if (attempts or 0) >= settings.max_job_attempts:
            if db.execute(update(ProcessingJob).where(*guard).values(status=FAILED, **_released())).rowcount:
                _give_up(db, db.get(ProcessingJob, job_id))
                recovered += 1
        elif db.execute(update(ProcessingJob).where(*guard).values(status=PENDING, **_released())).rowcount:
            logger.warning("job_lease_expired", extra={"job_id": job_id, "attempt": attempts})
            recovered += 1
    db.commit()
    return recovered


def recover_interrupted(db: Session) -> int:
    """Pick up what a dead worker left running. A job that has already taken the
    worker down too many times is set aside instead of being tried again, otherwise
    one bad scan stops all processing forever.

    Call ONLY after acquiring the exclusive local worker file lock (local mode), when no
    other worker can be alive. Database-coordinated workers use recover_expired.
    Returns how many jobs were found interrupted.
    """
    interrupted = list(db.scalars(select(ProcessingJob).where(ProcessingJob.status == RUNNING).order_by(ProcessingJob.id)))
    for job in interrupted:
        if (job.attempts or 0) >= settings.max_job_attempts:
            _give_up(db, job)
        else:
            job.status, job.claim_token, job.lease_owner, job.lease_expires_at = PENDING, None, None, None
            logger.warning("job_requeued", extra={"job_id": job.id, "batch_id": job.batch_id, "attempt": job.attempts})
    db.commit()
    return len(interrupted)


def _give_up(db: Session, job: ProcessingJob) -> None:
    """Set one job aside for a person. Only files that never finished are touched:
    finished files, tickets and approvals stay exactly as they are."""
    message = (f"Processing this upload stopped the worker {job.attempts} times, so it was set aside to let other uploads continue. "
               "Finished files and reviewed tickets weren't changed. Retry it, upload a re-scanned copy, or enter these tickets by hand.")
    job.status, job.claim_token, job.error = FAILED, None, message
    job.lease_owner, job.lease_expires_at = None, None
    logger.error("job_gave_up", extra={"job_id": job.id, "batch_id": job.batch_id, "attempt": job.attempts,
                                       "max_attempts": settings.max_job_attempts})
    batch = db.get(Batch, job.batch_id)
    if batch is None:
        return
    for file in batch.files:
        if file.status in ("uploaded", "processing"):
            file.status, file.error = FAILED, message
    # If everything had already finished, there's nothing to retry, so don't say failed.
    batch.status = FAILED if any(file.status == FAILED for file in batch.files) else "ready_for_review"


def _mark_unfinished_files_failed(batch: Batch, message: str) -> None:
    for file in batch.files:
        if file.status in ("uploaded", "processing"):
            file.status = FAILED
            file.error = message
    batch.status = FAILED


def _finish(db: Session, job_id: int, token: str, **values) -> None:
    """Close the job, only if this worker still holds it."""
    result = db.execute(update(ProcessingJob).where(
        ProcessingJob.id == job_id, ProcessingJob.claim_token == token, ProcessingJob.status == RUNNING,
    ).values(**_released(), **values))
    if result.rowcount != 1:
        db.rollback()
        raise LeaseLost(job_id)
    db.commit()


def execute_claimed(
    db: Session, job_id: int, token: str, *, extractor_factory,
) -> None:
    """Execute one claimed job. Never erase reviewed/approved ticket data.

    A recovered job uses only_failed mode, so completed files are untouched.
    Controlled extractor failures create visible, manually retryable batches.
    Raises db.LeaseLost, having written nothing more, if the job was taken away.
    """
    job = db.get(ProcessingJob, job_id)
    if job is None or job.status != RUNNING or job.claim_token != token:
        raise RuntimeError("Job not held by this worker")
    ids = {"job_id": job_id, "batch_id": job.batch_id, "attempt": job.attempts}
    started = time.perf_counter()
    logger.info("job_started", extra=ids)
    # The job's company, read before scoping, as the claiming role. From here on this
    # session can only see and write that company's rows when POSTGRES_RLS is on.
    company = db.scalar(select(Batch.organization_id).where(Batch.id == job.batch_id))
    previous_scope = db.info.get("organization_id")
    if company is not None:
        scope_to_organization(db, company)
    db.info["fence"] = (job_id, token)
    try:
        batch = db.get(Batch, job.batch_id)
        if batch is None:
            raise RuntimeError("Queued batch no longer exists")
        try:
            # Explicit manual batches intentionally never touch the vision API
            # (or its key), even when the worker is configured for AI jobs.
            extractor = None if batch.intake_mode == "manual" else extractor_factory()
        except ExtractionError as exc:
            # The model is not configured or is unavailable. No invented OCR.
            _mark_unfinished_files_failed(batch, str(exc))
            for file in batch.files:
                if job.only_failed and file.status not in ("uploaded", "processing", FAILED):
                    continue
                if file.status == "ready_for_review":
                    continue
                file.status = FAILED
                file.error = str(exc)
            batch.status = FAILED
            db.commit()
        else:
            # On interrupted job restart, protect completed files and already
            # corrected/verified tickets; rerun only unfinished/failed crops.
            pipeline.process_batch(
                db, batch.id, extractor,
                only_failed=job.only_failed or job.attempts > 1,
            )
        db.info.pop("fence", None)
        _finish(db, job_id, token, status=FINISHED, error=None)
        logger.info("job_finished", extra={**ids, "batch_status": db.get(Batch, job.batch_id).status,
                                           "ms": round((time.perf_counter() - started) * 1000)})
    except LeaseLost:
        db.rollback()
        logger.warning("job_lease_lost", extra=ids)
        raise
    except Exception:
        logger.exception("job_failed", extra=ids)
        db.rollback()
        db.info.pop("fence", None)
        # Held until the commit, so a worker that has since taken the job over isn't overwritten.
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update())
        if job is None or job.claim_token != token or job.status != RUNNING:
            raise
        batch = db.get(Batch, job.batch_id)
        if batch is not None:
            _mark_unfinished_files_failed(
                batch, "Processing interrupted unexpectedly. Please retry.",
            )
        job.status = FAILED
        job.claim_token, job.lease_owner, job.lease_expires_at = None, None, None
        job.error = "Unexpected processing error; retry the failed batch."
        db.commit()
    finally:
        db.info.pop("fence", None)
        db.info.pop("lease_lost", None)
        if previous_scope is None:
            db.info.pop("organization_id", None)
        else:
            db.info["organization_id"] = previous_scope
