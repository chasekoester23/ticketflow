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
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services import job_log  # noqa: E402
from app.services.extraction import get_extractor  # noqa: E402
from app.services.jobqueue import claim_next, execute_claimed, recover_interrupted  # noqa: E402
from app.services.system_health import heartbeat_path  # noqa: E402

logger = logging.getLogger("ticketflow.worker")
HEARTBEAT_SECONDS = 5


def _heartbeat(stop: threading.Event) -> None:
    """Touch a file so people can see this worker is alive, even during a long
    job. On its own thread, so a slow AI call can't make a live worker look dead."""
    path = heartbeat_path()
    while not stop.is_set():
        try:
            path.touch()
        except OSError:
            logger.warning("heartbeat_failed")
        stop.wait(HEARTBEAT_SECONDS)


def run(*, once: bool, poll_seconds: float) -> int:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    lock_path = settings.storage_dir / "worker.lock"
    # The OS automatically releases flock() when a worker dies, including
    # SIGKILL, so the next worker can exclusively reclaim running jobs.
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error("worker_lock_held")
            return 2

        with SessionLocal() as db:
            recovered = recover_interrupted(db)
            if recovered:
                logger.warning("jobs_recovered", extra={"jobs": recovered})

        logger.info("worker_started", extra={"poll_seconds": poll_seconds})
        stop = threading.Event()
        beat = threading.Thread(target=_heartbeat, args=(stop,), daemon=True)
        beat.start()
        try:
            while True:
                with SessionLocal() as db:
                    claimed = claim_next(db)
                if claimed is None:
                    if once:
                        return 0
                    time.sleep(poll_seconds)
                    continue
                job_id, token = claimed
                with SessionLocal() as db:
                    execute_claimed(db, job_id, token, extractor_factory=get_extractor)
                if once:
                    return 0
        finally:
            stop.set()
            beat.join(timeout=2)
            # A clean stop should read as stopped straight away.
            heartbeat_path().unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="TicketFlow's local durable extraction worker")
    parser.add_argument("--once", action="store_true", help="Process one pending job and exit")
    parser.add_argument("--poll-seconds", type=float, default=1.5)
    args = parser.parse_args()
    if not 0.25 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.25 and 60")
    job_log.configure()
    try:
        return run(once=args.once, poll_seconds=args.poll_seconds)
    except KeyboardInterrupt:
        logger.info("worker_stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
