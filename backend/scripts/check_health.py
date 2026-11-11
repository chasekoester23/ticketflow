"""A health check to run on a schedule (cron, a platform cron service), alerting if anything is wrong.

Usage (from backend/):
    python scripts/check_health.py --url https://app.example.com
    python scripts/check_health.py --url https://app.example.com --max-queue-minutes 30 --max-backup-hours 26

Checks, for the whole installation (every company together, counts only):
  - the API answers /health/ready (the database is reachable from it);
  - the queue: uploads waiting, and how long the oldest has waited;
  - jobs that failed in the last 24 hours;
  - the worker's latest heartbeat, if the worker_heartbeats table exists;
  - how old the newest complete encrypted PostgreSQL backup is (the backup bucket if
    BACKUP_S3_* is set, otherwise the backup_runs table or status file).
Prints one JSON line. Exit code 0 when all is well, 1 when anything needs a person, and
then, if ALERT_WEBHOOK_URL is set, posts the problems there. Never sends record contents.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import alerting  # noqa: E402

from app.config import settings  # noqa: E402


def check_ready(url: str | None, opener=None) -> tuple[bool | None, str]:
    if not url:
        return None, "not checked (no --url)"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False, "--url must be http(s)"
    target = url.rstrip("/") + "/health/ready"
    try:
        with (opener or urllib.request.urlopen)(urllib.request.Request(target), timeout=10) as response:  # nosec B310
            return response.status == 200, f"HTTP {response.status}"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def database_numbers(database_url: str) -> dict:
    """Queue, failures and heartbeat for every company together."""
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import NullPool

    from app.db import normalized_url
    engine = create_engine(normalized_url(database_url), poolclass=NullPool, hide_parameters=True)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    try:
        with engine.connect() as connection:
            pending, oldest = connection.execute(text(
                "SELECT COUNT(*), MIN(created_at) FROM processing_jobs WHERE status = 'pending'")).one()
            failed = connection.execute(text("SELECT COUNT(*) FROM processing_jobs WHERE status = 'failed' AND created_at >= :since"),
                                        {"since": since}).scalar_one()
            inspector = inspect(connection)
            heartbeat = None
            if inspector.has_table("worker_heartbeats"):
                columns = {column["name"] for column in inspector.get_columns("worker_heartbeats")}
                chosen = next((name for name in ("last_seen_at", "seen_at", "heartbeat_at", "updated_at") if name in columns), None)
                if chosen:
                    heartbeat = _as_utc(connection.execute(text(f"SELECT MAX({chosen}) FROM worker_heartbeats")).scalar())  # noqa: S608  # nosec B608
            backup = None
            if inspector.has_table("backup_runs"):
                backup = _as_utc(connection.execute(text(
                    "SELECT MAX(finished_at) FROM backup_runs WHERE ok = :ok AND kind = 'postgres'"), {"ok": True}).scalar())
    finally:
        engine.dispose()
    now = datetime.now(timezone.utc)
    oldest = _as_utc(oldest)
    return {"pending": pending, "oldest_pending_seconds": int((now - oldest).total_seconds()) if oldest else None,
            "failed_24h": failed, "worker_seen_seconds": int((now - heartbeat).total_seconds()) if heartbeat else None,
            "backup_finished_at": backup}


def newest_backup_in_bucket() -> datetime | None:
    from backup_pg import backup_bucket, database_backups
    bucket = backup_bucket()
    complete = sorted(stamp for stamp, entry in database_backups(bucket).items() if entry["complete"])
    if not complete:
        return None
    return datetime.fromisoformat(json.loads(bucket.get_bytes(bucket.key("postgres", complete[-1], "meta.json")))["taken_at"])


def evaluate(*, ready: tuple[bool | None, str], numbers: dict | None, backup_at: datetime | None,
             max_queue_minutes: int, max_backup_hours: int, max_failed: int, worker_stale_seconds: int) -> list[str]:
    problems = []
    if ready[0] is False:
        problems.append(f"The API isn't ready ({ready[1]}).")
    if numbers is None:
        problems.append("The database couldn't be read.")
    else:
        oldest = numbers["oldest_pending_seconds"]
        if oldest is not None and oldest > max_queue_minutes * 60:
            problems.append(f"{numbers['pending']} upload(s) waiting; the oldest for {oldest // 60} minutes.")
        if numbers["failed_24h"] > max_failed:
            problems.append(f"{numbers['failed_24h']} processing job(s) failed in the last 24 hours.")
        seen = numbers["worker_seen_seconds"]
        if seen is not None and seen > worker_stale_seconds and numbers["pending"]:
            problems.append(f"The worker was last seen {seen} seconds ago, with uploads waiting.")
    if backup_at is None:
        problems.append("No complete PostgreSQL backup was found.")
    elif datetime.now(timezone.utc) - backup_at > timedelta(hours=max_backup_hours):
        hours = int((datetime.now(timezone.utc) - backup_at).total_seconds() // 3600)
        problems.append(f"The newest PostgreSQL backup is {hours} hours old.")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="The public address of the app (its /health/ready is checked)")
    parser.add_argument("--max-queue-minutes", type=int, default=30)
    parser.add_argument("--max-backup-hours", type=int, default=26)
    parser.add_argument("--max-failed-jobs", type=int, default=0, help="Failed jobs in 24 h allowed before alerting")
    parser.add_argument("--worker-stale-seconds", type=int, default=120)
    parser.add_argument("--no-backup-check", action="store_true", help="Skip the backup age check (e.g. local SQLite installs)")
    args = parser.parse_args(argv)

    ready = check_ready(args.url)
    try:
        numbers = database_numbers(settings.database_url)
    except Exception as exc:  # noqa: BLE001
        print(f"Database check failed ({type(exc).__name__}).", file=sys.stderr)
        numbers = None
    backup_at = None
    if not args.no_backup_check:
        try:
            backup_at = newest_backup_in_bucket() if settings.backup_s3_bucket else (numbers or {}).get("backup_finished_at")
        except Exception as exc:  # noqa: BLE001
            print(f"Backup check failed ({type(exc).__name__}).", file=sys.stderr)
        if backup_at is None and not settings.backup_s3_bucket:
            try:
                data = json.loads((settings.backup_dir / "pg-backup-latest.json").read_text())
                backup_at = _as_utc(data["finished_at"])
            except (OSError, ValueError, KeyError):
                pass
    problems = evaluate(ready=ready, numbers=numbers, backup_at=backup_at if not args.no_backup_check else datetime.now(timezone.utc),
                        max_queue_minutes=args.max_queue_minutes, max_backup_hours=args.max_backup_hours,
                        max_failed=args.max_failed_jobs, worker_stale_seconds=args.worker_stale_seconds)
    summary = {"ok": not problems, "ready": ready[0], "problems": problems,
               **({key: value for key, value in numbers.items() if key != "backup_finished_at"} if numbers else {}),
               "backup_age_hours": round((datetime.now(timezone.utc) - backup_at).total_seconds() / 3600, 1) if backup_at else None}
    print(json.dumps(summary))
    if problems:
        alerting.send(settings.alert_webhook_url, "health_check_failed", f"{len(problems)} problem(s) need attention.",
                      details={"problems": problems})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
