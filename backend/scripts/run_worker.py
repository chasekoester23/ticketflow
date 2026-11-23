"""TicketFlow's durable extraction worker.

Run separately from FastAPI:
    cd backend
    python scripts/run_worker.py

Use --once to process at most one queued batch and exit (for local tests).
This service must be running for the API to process uploaded tickets.

Two ways to coordinate (WORKER_COORDINATION, default auto):
- local, the default on SQLite: one worker per machine. It holds an OS file lock, so a
  second refuses to start, and on start it takes back whatever a dead worker left running.
- database, the default on PostgreSQL: any number of workers, on any number of machines.
  Each claims jobs under a lease it renews from its heartbeat, and takes over only jobs
  whose lease has lapsed. See app/services/jobqueue.py.

Either way, each worker records itself in worker_heartbeats, which System Health reads.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.pool import SingletonThreadPool, StaticPool  # noqa: E402

from app.config import configuration_problems, settings  # noqa: E402
from app.db import LeaseLost, SessionLocal  # noqa: E402
from app.models import WorkerHeartbeat  # noqa: E402
from app.services import job_log  # noqa: E402
from app.services.extraction import get_extractor  # noqa: E402
from app.services.jobqueue import claim_next, execute_claimed, recover_expired, recover_interrupted, renew_lease  # noqa: E402
from app.services.system_health import heartbeat_path  # noqa: E402

logger = logging.getLogger("ticketflow.worker")
# Rows of workers not seen for this long are removed by the next worker to start.
FORGET_AFTER = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def coordination(session_factory=None) -> str:
    """local or database, from WORKER_COORDINATION and the database in use."""
    if settings.worker_coordination != "auto":
        return settings.worker_coordination
    with (session_factory or SessionLocal)() as db:
        return "database" if db.get_bind().dialect.name == "postgresql" else "local"


def _version() -> str | None:
    value = os.environ.get("TICKETFLOW_VERSION") or os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:12]
    return value[:64] or None


class Presence:
    """This worker's row in worker_heartbeats, its lease on the current job, and, in local
    mode, the heartbeat file. Beats on its own thread, so a slow AI call can't make a live
    worker look dead or let its lease lapse."""

    def __init__(self, mode: str):
        self.mode = mode
        self.id = f"{socket.gethostname()[:40]}-{os.getpid()}-{secrets.token_hex(3)}"
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.job: tuple[int, str] | None = None
        self.thread = threading.Thread(target=self._loop, name="ticketflow-heartbeat", daemon=True)
        # One connection shared by every thread (tests' in-memory SQLite) can't take a
        # second thread's writes in the middle of the first's transaction.
        bind = SessionLocal.kw.get("bind") if hasattr(SessionLocal, "kw") else None
        self.database_beats = not isinstance(getattr(bind, "pool", None), (StaticPool, SingletonThreadPool))

    def start(self) -> None:
        with SessionLocal() as db:
            db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < _now() - FORGET_AFTER))
            now = _now()
            db.add(WorkerHeartbeat(id=self.id, host=socket.gethostname()[:255], pid=os.getpid(), mode=self.mode,
                                   version=_version(), started_at=now, last_seen_at=now))
            db.commit()
        self._touch_file()
        self.thread.start()

    def holding(self, job: tuple[int, str] | None) -> None:
        with self.lock:
            self.job = job
        if not self.database_beats:
            return
        with SessionLocal() as db:
            row = db.get(WorkerHeartbeat, self.id)
            if row is not None:
                row.current_job_id, row.last_seen_at = (job[0] if job else None), _now()
                db.commit()

    def _touch_file(self) -> None:
        if self.mode != "local":
            return
        try:
            heartbeat_path().touch()
        except OSError:
            logger.warning("heartbeat_failed")

    def beat(self) -> None:
        self._touch_file()
        if not self.database_beats:
            return
        with self.lock:
            job = self.job
        try:
            with SessionLocal() as db:
                if job is not None and not renew_lease(db, *job):
                    logger.warning("job_lease_lost", extra={"job_id": job[0]})
                row = db.get(WorkerHeartbeat, self.id)
                if row is not None:
                    row.last_seen_at, row.current_job_id = _now(), job[0] if job else None
                    db.commit()
        except Exception:  # A database blip must not end the heartbeat; the next beat tries again.
            logger.warning("heartbeat_failed", exc_info=True)

    def _loop(self) -> None:
        while not self.stop.wait(settings.worker_heartbeat_seconds):
            self.beat()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=5)
        try:
            with SessionLocal() as db:
                db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.id == self.id))
                db.commit()
        except Exception:
            logger.warning("heartbeat_cleanup_failed", exc_info=True)
        if self.mode == "local":
            # A clean stop should read as stopped straight away.
            heartbeat_path().unlink(missing_ok=True)


def run(*, once: bool, poll_seconds: float, stopping: threading.Event | None = None) -> int:
    problems = configuration_problems(settings)
    if problems:
        # The same settings the API refuses to start with: the worker shares its database and documents.
        print("Refusing to start: " + " ".join(problems), file=sys.stderr)
        return 2
    stopping = stopping or threading.Event()
    mode = coordination()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as held:
        if mode == "local":
            lock = held.enter_context((settings.storage_dir / "worker.lock").open("a+b"))
            # The OS automatically releases flock() when a worker dies, including
            # SIGKILL, so the next worker can exclusively reclaim running jobs.
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.error("worker_lock_held")
                return 2
            with SessionLocal() as db:
                recovered = recover_interrupted(db)
            if recovered:
                logger.warning("jobs_recovered", extra={"jobs": recovered})

        presence = Presence(mode)
        presence.start()
        held.callback(presence.close)
        logger.info("worker_started", extra={"poll_seconds": poll_seconds, "mode": mode, "worker": presence.id})
        last_recovery = 0.0
        while not stopping.is_set():
            if mode == "database" and time.monotonic() - last_recovery >= min(30.0, settings.worker_lease_seconds / 3):
                try:
                    with SessionLocal() as db:
                        recovered = recover_expired(db)
                except Exception:  # A lock conflict or blip: the next round tries again, or another worker does.
                    logger.warning("recovery_failed", exc_info=True)
                    recovered = 0
                if recovered:
                    logger.warning("jobs_recovered", extra={"jobs": recovered})
                last_recovery = time.monotonic()
            with SessionLocal() as db:
                claimed = claim_next(db, presence.id)
            if claimed is None:
                if once:
                    return 0
                stopping.wait(poll_seconds)
                continue
            presence.holding(claimed)
            try:
                with SessionLocal() as db:
                    execute_claimed(db, *claimed, extractor_factory=get_extractor)
            except LeaseLost:
                pass  # Logged by execute_claimed. Another worker has the job now; nothing more was written.
            finally:
                presence.holding(None)
            if once:
                return 0
        logger.info("worker_stopped")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TicketFlow's durable extraction worker")
    parser.add_argument("--once", action="store_true", help="Process one pending job and exit")
    parser.add_argument("--poll-seconds", type=float, default=1.5)
    args = parser.parse_args()
    if not 0.25 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.25 and 60")
    job_log.configure()
    stopping = threading.Event()
    # A deploy sends SIGTERM: take no new job, finish this one if there's time. If the
    # process is killed first, its lease lapses and another worker takes the job over.
    signal.signal(signal.SIGTERM, lambda *_args: stopping.set())
    try:
        return run(once=args.once, poll_seconds=args.poll_seconds, stopping=stopping)
    except KeyboardInterrupt:
        logger.info("worker_stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
