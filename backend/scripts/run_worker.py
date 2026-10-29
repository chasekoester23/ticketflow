"""TicketFlow's single-machine durable extraction worker.

Run separately from FastAPI:
    cd backend
    python scripts/run_worker.py

Use --once to process at most one queued batch and exit (for local tests).
This service must be running for the API to process uploaded tickets.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services.extraction import get_extractor  # noqa: E402
from app.services.jobqueue import claim_next, execute_claimed, recover_interrupted  # noqa: E402

logger = logging.getLogger("ticketflow.worker")


def run(*, once: bool, poll_seconds: float) -> int:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    lock_path = settings.storage_dir / "worker.lock"
    # The OS automatically releases flock() when a worker dies, including
    # SIGKILL, so the next worker can exclusively reclaim running jobs.
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error("Another TicketFlow worker already holds the local lock")
            return 2

        with SessionLocal() as db:
            recovered = recover_interrupted(db)
            if recovered:
                logger.warning("Recovered %s processing job(s) after worker restart", recovered)

        logger.info("Worker started; waiting for queued batches")
        while True:
            with SessionLocal() as db:
                claimed = claim_next(db)
            if claimed is None:
                if once:
                    return 0
                time.sleep(poll_seconds)
                continue
            job_id, token = claimed
            logger.info("Processing durable job %s", job_id)
            with SessionLocal() as db:
                execute_claimed(db, job_id, token, extractor_factory=get_extractor)
            logger.info("Finished attempt for job %s", job_id)
            if once:
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TicketFlow's local durable extraction worker")
    parser.add_argument("--once", action="store_true", help="Process one pending job and exit")
    parser.add_argument("--poll-seconds", type=float, default=1.5)
    args = parser.parse_args()
    if not 0.25 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.25 and 60")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return run(once=args.once, poll_seconds=args.poll_seconds)
    except KeyboardInterrupt:
        logger.info("Worker stopped; running job will resume after next worker start")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
