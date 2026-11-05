"""Is this installation healthy? Answers the questions an owner actually has.

Reads only. Never returns a secret, a path, or anything from a company's records
beyond counts. The heartbeat is a file because the worker is a single-machine
process; a hosted queue would report through its own service.
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from ..config import BASE_DIR, settings
from ..models import Batch, ProcessingJob, UploadedFile, as_utc

HEARTBEAT_NAME = "worker.heartbeat"
# The worker touches this every few seconds, even during a long job.
WORKER_STALE_SECONDS = 30
LOW_DISK_BYTES = 2 * 1024 ** 3
BACKUP_OLD_DAYS = 7


def heartbeat_path() -> Path:
    return settings.storage_dir / HEARTBEAT_NAME


def worker_last_seen_seconds() -> float | None:
    try:
        return max(0.0, time.time() - heartbeat_path().stat().st_mtime)
    except OSError:
        return None


def worker_running() -> bool:
    seen = worker_last_seen_seconds()
    return seen is not None and seen <= WORKER_STALE_SECONDS


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
            newest = {"taken_at": taken, "tickets": data.get("counts", {}).get("tickets")}
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


def build(db: Session, organization_id: int) -> dict:
    checks = []

    seen = worker_last_seen_seconds()
    company_batches = select(Batch.id).where(Batch.organization_id == organization_id)
    job_counts = dict(db.execute(select(ProcessingJob.status, func.count()).where(
        ProcessingJob.batch_id.in_(company_batches)).group_by(ProcessingJob.status)).all())
    pending = job_counts.get("pending", 0)
    running = job_counts.get("running", 0)
    if worker_running():
        checks.append(_check("Processing worker", "ok", f"Running, last seen {int(seen or 0)} seconds ago."))
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
    if backup is None:
        checks.append(_check("Backups", "problem", "No backup found. A lost disk would lose every approved ticket.",
                             "From the backend folder, run: python scripts/backup.py"))
    else:
        age_days = (datetime.now(timezone.utc) - backup["taken_at"]).days
        if age_days >= BACKUP_OLD_DAYS:
            checks.append(_check("Backups", "attention", f"The latest backup is {age_days} days old.",
                                 "From the backend folder, run: python scripts/backup.py"))
        else:
            checks.append(_check("Backups", "ok", f"Latest backup is {age_days} day(s) old."))

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
        "worker": {"running": worker_running(), "last_seen_seconds": int(seen) if seen is not None else None},
        "database": {"kind": db.get_bind().dialect.name, "revision": revision, "expected_revision": expected},
        "invoice_reader": settings.invoice_extractor,
    }
