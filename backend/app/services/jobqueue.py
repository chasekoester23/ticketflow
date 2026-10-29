"""Persistent local extraction queue.

The FastAPI server records jobs transactionally and never executes extraction.
A separate single-machine worker holds an exclusive OS file lock and drains
pending jobs. A worker restart can safely reclaim interrupted jobs *after*
the previous worker's OS lock is released.

This is not a distributed production queue, and AI calls are at-least-once.
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Batch, ProcessingJob
from . import pipeline
from .extraction import ExtractionError

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"


def enqueue(db: Session, batch_id: int, *, only_failed: bool = False) -> ProcessingJob:
    """Add a durable job in the caller's transaction, never commit here."""
    job = ProcessingJob(
        batch_id=batch_id, status=PENDING,
        only_failed=only_failed, attempts=0,
    )
    db.add(job)
    return job


def claim_next(db: Session) -> tuple[int, str] | None:
    """Atomically transition one persisted pending job to running."""
    candidate = db.scalar(
        select(ProcessingJob.id).where(ProcessingJob.status == PENDING)
        .order_by(ProcessingJob.id).limit(1)
    )
    if candidate is None:
        return None
    token = secrets.token_hex(24)
    result = db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == candidate, ProcessingJob.status == PENDING)
        .values(
            status=RUNNING, claim_token=token,
            attempts=ProcessingJob.attempts + 1, error=None,
        )
    )
    db.commit()
    return (candidate, token) if result.rowcount == 1 else None


def recover_interrupted(db: Session) -> int:
    """Call ONLY after acquiring the exclusive local worker file lock."""
    result = db.execute(
        update(ProcessingJob).where(ProcessingJob.status == RUNNING)
        .values(status=PENDING, claim_token=None)
    )
    db.commit()
    return result.rowcount


def _mark_unfinished_files_failed(batch: Batch, message: str) -> None:
    for file in batch.files:
        if file.status in ("uploaded", "processing"):
            file.status = FAILED
            file.error = message
    batch.status = FAILED


def execute_claimed(
    db: Session, job_id: int, token: str, *, extractor_factory,
) -> None:
    """Execute one claimed job. Never erase reviewed/approved ticket data.

    A recovered job uses only_failed mode, so completed files are untouched.
    Controlled extractor failures create visible, manually retryable batches.
    """
    job = db.get(ProcessingJob, job_id)
    if job is None or job.status != RUNNING or job.claim_token != token:
        raise RuntimeError("Job not held by this worker")
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
        job = db.get(ProcessingJob, job_id)
        job.status = FINISHED
        job.claim_token = None
        job.error = None
        db.commit()
    except Exception:
        logger.exception("Job %s failed unexpectedly", job_id)
        db.rollback()
        job = db.get(ProcessingJob, job_id)
        if job is None or job.claim_token != token:
            raise
        batch = db.get(Batch, job.batch_id)
        if batch is not None:
            _mark_unfinished_files_failed(
                batch, "Processing interrupted unexpectedly. Please retry.",
            )
        job.status = FAILED
        job.claim_token = None
        job.error = "Unexpected processing error; retry the failed batch."
        db.commit()
