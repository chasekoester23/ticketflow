"""Is this installation healthy? Answers the questions an owner actually has.

Reads only. Never returns a secret, a path, or anything from a company's records
beyond counts. Each worker records itself in worker_heartbeats, which any process
can read. A local worker also touches a heartbeat file, as before.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from ..config import BASE_DIR, settings
from ..models import Batch, InvoiceDocument, ProcessingJob, ScanSlot, Ticket, UploadedFile, WorkerHeartbeat, as_utc
from . import storage

HEARTBEAT_NAME = "worker.heartbeat"
# The worker touches this every few seconds, even during a long job.
WORKER_STALE_SECONDS = 30
LOW_DISK_BYTES = 2 * 1024 ** 3
BACKUP_OLD_DAYS = 7
# The newest of each kind of document are looked for on every visit. scripts/check_documents.py checks all of them.
DOCUMENT_SAMPLE = 50
# scripts/backup_pg.py records each run here when the table exists (see docs/operations/backups.md).
BACKUP_RUNS_TABLE = "backup_runs"
PG_BACKUP_STATUS_FILE = "pg-backup-latest.json"
BACKUP_STALE_HOURS = 26  # a daily backup, plus a little slack


# ------------------------------------------------------------------ request counts, this process

_started = time.time()
_counts_lock = threading.Lock()
_request_counts = {"requests": 0, "server_errors": 0}


def count_request(status: int) -> None:
    """Called once per request by the request log. Counts only, nothing about the request."""
    with _counts_lock:
        _request_counts["requests"] += 1
        if status >= 500:
            _request_counts["server_errors"] += 1


def request_counts() -> dict:
    with _counts_lock:
        requests, errors = _request_counts["requests"], _request_counts["server_errors"]
    return {"since_seconds": int(time.time() - _started), "requests": requests, "server_errors": errors,
            "error_rate": round(errors / requests, 4) if requests else 0.0}


def heartbeat_path() -> Path:
    return settings.storage_dir / HEARTBEAT_NAME


def worker_last_seen_seconds() -> float | None:
    try:
        return max(0.0, time.time() - heartbeat_path().stat().st_mtime)
    except OSError:
        return None


def _stale_seconds() -> float:
    return max(WORKER_STALE_SECONDS, 3 * settings.worker_heartbeat_seconds)


def live_workers(db: Session) -> list[WorkerHeartbeat]:
    """Workers that have beaten recently, from any machine. Never says which company's job
    each holds: this is shown to every company."""
    since = datetime.now(timezone.utc) - timedelta(seconds=_stale_seconds())
    return list(db.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at >= since)
                           .order_by(WorkerHeartbeat.last_seen_at.desc())))


def _table_last_seen_seconds(db: Session | None) -> float | None:
    try:
        if db is not None:
            newest = db.scalar(select(func.max(WorkerHeartbeat.last_seen_at)))
        else:
            from ..db import engine  # noqa: PLC0415 - the app's own database, for callers without a session
            with engine.connect() as connection:
                newest = connection.scalar(select(func.max(WorkerHeartbeat.last_seen_at)))
    except Exception:  # No table yet, or no database: the file alone answers.
        return None
    return None if newest is None else max(0.0, (datetime.now(timezone.utc) - as_utc(newest)).total_seconds())


def worker_running(db: Session | None = None) -> bool:
    seen = worker_last_seen_seconds()
    if seen is not None and seen <= WORKER_STALE_SECONDS:
        return True
    table = _table_last_seen_seconds(db)
    return table is not None and table <= _stale_seconds()


def expected_revision() -> str | None:
    try:
        return ScriptDirectory.from_config(Config(str(BASE_DIR / "alembic.ini"))).get_current_head()
    except Exception:
        return None


def schema_revisions(engine) -> tuple[str | None, str | None]:
    """(what the database is at, what this code needs)."""
    with engine.connect() as connection:
        current = (connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                   if inspect(connection).has_table("alembic_version") else None)
    return current, expected_revision()


def current_revision(db: Session) -> str | None:
    if not inspect(db.get_bind()).has_table("alembic_version"):
        return None
    return db.execute(text("SELECT version_num FROM alembic_version")).scalar()


def latest_backup() -> dict | None:
    """The newest backup described by a manifest. Its folder isn't reported."""
    directory = settings.backup_dir
    if not directory.is_dir():
        return None
    newest = None
    for manifest in directory.glob("*/manifest.json"):
        try:
            data = json.loads(manifest.read_text())
            taken = datetime.fromisoformat(data["taken_at"])
        except (OSError, ValueError, KeyError):
            continue
        if newest is None or taken > newest["taken_at"]:
            try:
                copied = json.loads((manifest.parent / "copied.json").read_text())
                off_device = not copied.get("same_disk", True)
            except (OSError, ValueError, AttributeError):
                off_device = False
            newest = {"taken_at": taken, "tickets": data.get("counts", {}).get("tickets"), "off_device": off_device}
    return newest


# Rules migration 20261106_18 leaves out if existing rows already break them.
SAFEGUARD_INDEXES = {"tickets": ("uq_tickets_slot", "uq_tickets_approved_scan"), "supplier_rates": ("uq_supplier_rate",),
                     "invoice_documents": ("uq_invoice_document_file",)}


def missing_safeguards(db: Session) -> list[str]:
    inspector = inspect(db.get_bind())
    return [name for table, names in SAFEGUARD_INDEXES.items()
            for name in names if name not in {index["name"] for index in inspector.get_indexes(table)}]


def _check(name: str, state: str, message: str, action: str | None = None) -> dict:
    return {"name": name, "state": state, "message": message, "action": action}


def _stored_documents(db: Session, organization_id: int) -> dict:
    """Whether this company's newest documents are where the database says. A sample,
    so the page stays fast. Says how many, never which file or where."""
    company_batches = select(Batch.id).where(Batch.organization_id == organization_id)
    references = [
        *db.scalars(select(UploadedFile.storage_path).where(UploadedFile.batch_id.in_(company_batches))
                    .order_by(UploadedFile.id.desc()).limit(DOCUMENT_SAMPLE)),
        *db.scalars(select(ScanSlot.crop_path).where(ScanSlot.batch_id.in_(company_batches)).order_by(ScanSlot.id.desc()).limit(DOCUMENT_SAMPLE)),
        *db.scalars(select(Ticket.crop_path).where(Ticket.organization_id == organization_id).order_by(Ticket.id.desc()).limit(DOCUMENT_SAMPLE)),
        *db.scalars(select(InvoiceDocument.storage_path).where(InvoiceDocument.organization_id == organization_id)
                    .order_by(InvoiceDocument.id.desc()).limit(DOCUMENT_SAMPLE)),
    ]
    action = "From the backend folder, run: python scripts/check_documents.py. Then see 'Missing documents' in docs/engineering/operations.md."
    if not settings.storage_dir.is_dir():
        return _check("Stored documents", "problem", "The storage folder is missing, so no scan or invoice can open.",
                      "Check STORAGE_DIR, and that its drive is connected. " + action)
    if not references:
        return _check("Stored documents", "ok", "No documents stored yet.")
    store = storage.current()
    missing = sum(1 for reference in references if not store.exists(reference))
    if missing:
        return _check("Stored documents", "problem", f"{missing} of the {len(references)} newest documents checked are missing.", action)
    return _check("Stored documents", "ok", f"The {len(references)} newest documents checked are all there.")


def _failed_jobs(db: Session, organization_id: int) -> dict:
    """Uploads the worker gave up on, or failed unexpectedly, that nobody has retried successfully."""
    count = db.scalar(select(func.count()).select_from(ProcessingJob).join(Batch, Batch.id == ProcessingJob.batch_id).where(
        Batch.organization_id == organization_id, ProcessingJob.status == "failed", Batch.status == "failed")) or 0
    if not count:
        return _check("Failed jobs", "ok", "No uploads are waiting on a person.")
    return _check("Failed jobs", "attention", f"{count} upload(s) couldn't be processed and were set aside.",
                  "Open Ticket intake, retry them once, or enter those tickets by hand. The worker's log names each "
                  "job: look for job_gave_up or job_failed.")


def _timed(probe) -> tuple[bool, float]:
    started = time.perf_counter()
    try:
        probe()
        ok = True
    except Exception:  # Any failure at all means unreachable; its text could hold a path or address.
        ok = False
    return ok, round((time.perf_counter() - started) * 1000, 1)


def storage_reachable() -> tuple[bool, float]:
    """Asks the document store about a key that never exists: proves it answers, for any driver."""
    return _timed(lambda: storage.current().exists("health-probe/never-written"))


def database_reachable(db: Session) -> tuple[bool, float]:
    return _timed(lambda: db.execute(text("SELECT 1")))


def latest_pg_backup(db: Session) -> dict | None:
    """The newest successful PostgreSQL backup: from the backup_runs table when it exists
    (the backup runs as its own service), else a status file beside local backups."""
    try:
        if inspect(db.get_bind()).has_table(BACKUP_RUNS_TABLE):
            row = db.execute(text(f"SELECT finished_at FROM {BACKUP_RUNS_TABLE} WHERE ok = :ok AND kind = 'postgres' "  # noqa: S608  # nosec B608
                                  "ORDER BY finished_at DESC LIMIT 1"), {"ok": True}).first()
            if row is not None:
                return {"taken_at": as_utc(row[0]) if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0])),
                        "source": "table"}
    except Exception:
        db.rollback()
    try:
        data = json.loads((settings.backup_dir / PG_BACKUP_STATUS_FILE).read_text())
        return {"taken_at": datetime.fromisoformat(data["finished_at"]), "source": "file"}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _age_hours(when: datetime | None) -> float | None:
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - when).total_seconds() / 3600, 1)


def metrics(db: Session, organization_id: int) -> dict:
    """Numbers for the people running TicketFlow. Job and reading numbers are this company's
    only; the rest describe the installation (this API process, the database, storage and
    backups) and hold nothing about any company."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    company_batches = select(Batch.id).where(Batch.organization_id == organization_id)
    jobs = dict(db.execute(select(ProcessingJob.status, func.count()).where(
        ProcessingJob.batch_id.in_(company_batches), ProcessingJob.created_at >= since).group_by(ProcessingJob.status)).all())
    queued = db.execute(select(func.count(), func.min(ProcessingJob.created_at)).where(
        ProcessingJob.batch_id.in_(company_batches), ProcessingJob.status == "pending")).one()
    reader_failures = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.organization_id == organization_id, Ticket.status == "failed", Ticket.created_at >= since)) or 0
    database_ok, database_ms = database_reachable(db)
    storage_ok, storage_ms = storage_reachable()
    local = latest_backup()
    postgres = latest_pg_backup(db)
    return {
        "api": request_counts(),
        "database": {"reachable": database_ok, "ms": database_ms},
        "storage": {"reachable": storage_ok, "ms": storage_ms},
        "queue": {"pending": queued[0] or 0,
                  "oldest_pending_seconds": int((datetime.now(timezone.utc) - as_utc(queued[1])).total_seconds()) if queued[1] else None},
        "jobs_24h": {"finished": jobs.get("finished", 0), "failed": jobs.get("failed", 0),
                     "pending": jobs.get("pending", 0), "running": jobs.get("running", 0)},
        # Tickets the AI reader couldn't read. Retries of temporary failures aren't recorded
        # in a table yet; the worker's log has them.
        "reader_24h": {"failed_tickets": reader_failures, "retries": None},
        "backups": {
            "local_age_hours": _age_hours(local["taken_at"]) if local else None,
            "postgres_age_hours": _age_hours(postgres["taken_at"]) if postgres else None,
            "stale_after_hours": BACKUP_STALE_HOURS,
        },
    }


def uses_pg_backups(db: Session) -> bool:
    """PostgreSQL is backed up by scripts/backup_pg.py; backup.py copies SQLite only."""
    return db.get_bind().dialect.name == "postgresql"


def _postgres_backup_check(db: Session) -> dict:
    action = ("Run python scripts/backup_pg.py on a daily schedule, and python scripts/restore_pg_drill.py to prove it "
              "restores. See docs/operations/backups.md.")
    backup = latest_pg_backup(db)
    if backup is None:
        return _check("Backups", "attention", "No record of an encrypted PostgreSQL backup is visible from here.", action)
    age = _age_hours(backup["taken_at"]) or 0.0
    if age > BACKUP_STALE_HOURS:
        return _check("Backups", "problem", f"The latest encrypted PostgreSQL backup is {age:.0f} hours old.", action)
    return _check("Backups", "ok", f"The latest encrypted PostgreSQL backup finished {age:.0f} hour(s) ago, off this service.")


def build(db: Session, organization_id: int) -> dict:
    checks = []

    seen = worker_last_seen_seconds()
    table_seen = _table_last_seen_seconds(db)
    if table_seen is not None and (seen is None or table_seen < seen):
        seen = table_seen
    workers = live_workers(db) if table_seen is not None else []
    company_batches = select(Batch.id).where(Batch.organization_id == organization_id)
    job_counts = dict(db.execute(select(ProcessingJob.status, func.count()).where(
        ProcessingJob.batch_id.in_(company_batches)).group_by(ProcessingJob.status)).all())
    pending = job_counts.get("pending", 0)
    running = job_counts.get("running", 0)
    if worker_running(db):
        count = f"{len(workers)} workers running" if len(workers) > 1 else "Running"
        checks.append(_check("Processing worker", "ok", f"{count}, last seen {int(seen or 0)} seconds ago."))
    elif pending or running:
        checks.append(_check("Processing worker", "problem",
                             f"Not running, and {pending + running} upload(s) are waiting on it.",
                             "From the backend folder, run: python scripts/run_worker.py"))
    else:
        checks.append(_check("Processing worker", "attention", "Not running. New uploads will wait until it starts.",
                             "From the backend folder, run: python scripts/run_worker.py"))

    failed_files = db.scalar(select(func.count()).select_from(UploadedFile).join(Batch, Batch.id == UploadedFile.batch_id).where(
        Batch.organization_id == organization_id, UploadedFile.status == "failed")) or 0
    if failed_files:
        checks.append(_check("Uploads", "attention", f"{failed_files} uploaded file(s) failed to process.",
                             "Open Ticket intake and retry them, or enter those tickets by hand."))
    else:
        checks.append(_check("Uploads", "ok", "No failed files."))
    checks.append(_failed_jobs(db, organization_id))
    checks.append(_stored_documents(db, organization_id))

    oldest = db.scalar(select(func.min(ProcessingJob.created_at)).where(
        ProcessingJob.status == "pending", ProcessingJob.batch_id.in_(company_batches)))
    oldest_seconds = (datetime.now(timezone.utc) - as_utc(oldest)).total_seconds() if oldest else None

    revision, expected = current_revision(db), expected_revision()
    if revision is None:
        checks.append(_check("Database version", "attention", "This database isn't tracked by migrations.",
                             "Back up, then see docs/engineering/operations.md before upgrading."))
    elif expected and revision != expected:
        checks.append(_check("Database version", "problem", f"At {revision}, but this code expects {expected}.",
                             "Back up, then run: python -m alembic upgrade head"))
    else:
        checks.append(_check("Database version", "ok", f"Up to date ({revision})."))

    missing = missing_safeguards(db)
    if missing:
        checks.append(_check("Duplicate safeguards", "problem",
                             f"Existing records break {len(missing)} rule(s) that stop double counting, so the database can't enforce them: "
                             + ", ".join(missing) + ".",
                             "See 'Duplicates found during an upgrade' in docs/engineering/operations.md."))
    else:
        checks.append(_check("Duplicate safeguards", "ok", "The database enforces the rules that stop double counting."))

    backup = latest_backup()
    if uses_pg_backups(db):
        checks.append(_postgres_backup_check(db))
        backup = False  # backup.py copies SQLite only; its check doesn't apply here
    copy_somewhere = ("Keep one off this computer too: python scripts/backup.py --copy-to /Volumes/YourDrive/ticketflow "
                      "(an external drive, or a folder your cloud storage syncs).")
    if backup is False:
        pass
    elif backup is None:
        checks.append(_check("Backups", "problem", "No backup yet. A lost or broken disk would lose every approved ticket and scan.",
                             "From the backend folder, run: python scripts/backup.py, then python scripts/restore_drill.py --latest "
                             "to prove it restores. " + copy_somewhere + " For a daily backup: python scripts/backup.py "
                             "--print-schedule launchd prints a schedule to install."))
    else:
        age_days = (datetime.now(timezone.utc) - backup["taken_at"]).days
        if age_days >= BACKUP_OLD_DAYS:
            checks.append(_check("Backups", "attention", f"The latest backup is {age_days} days old.",
                                 "From the backend folder, run: python scripts/backup.py. To make it daily: "
                                 "python scripts/backup.py --print-schedule launchd"))
        elif not backup["off_device"]:
            checks.append(_check("Backups", "attention", f"Latest backup is {age_days} day(s) old, but it's only on this computer.",
                                 copy_somewhere))
        else:
            checks.append(_check("Backups", "ok", f"Latest backup is {age_days} day(s) old, with a verified copy off this computer."))

    try:
        free = shutil.disk_usage(settings.storage_dir).free
    except OSError:
        free = None
    if free is None:
        checks.append(_check("Disk space", "attention", "Could not read free space for document storage."))
    elif free < LOW_DISK_BYTES:
        checks.append(_check("Disk space", "problem", f"Only {free // 1024 ** 2} MB free for document storage.",
                             "Free up space before uploading more scans."))
    else:
        checks.append(_check("Disk space", "ok", f"{free // 1024 ** 3} GB free for document storage."))

    provider_key = {"gemini": settings.gemini_api_key, "anthropic": settings.anthropic_api_key}.get(settings.extraction_provider)
    # Off is a choice, not a fault: manual entry is a complete way to work.
    checks.append(_check("AI ticket reading", "ok" if provider_key else "off",
                         f"Configured ({settings.extraction_provider})." if provider_key
                         else "Not configured. Manual entry works without it."))
    return {
        "healthy": all(check["state"] in ("ok", "off") for check in checks),
        "checks": checks,
        "queue": {"pending": pending, "running": running, "failed_jobs": job_counts.get("failed", 0),
                  "oldest_pending_seconds": int(oldest_seconds) if oldest_seconds is not None else None},
        "worker": {"running": worker_running(db), "last_seen_seconds": int(seen) if seen is not None else None,
                   "live_workers": len(workers),
                   "coordination": settings.worker_coordination,
                   "max_concurrent_jobs_per_organization": settings.max_concurrent_jobs_per_organization or None},
        "database": {"kind": db.get_bind().dialect.name, "revision": revision, "expected_revision": expected},
        "invoice_reader": settings.invoice_extractor,
        "metrics": metrics(db, organization_id),
    }
