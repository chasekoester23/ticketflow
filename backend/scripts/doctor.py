"""Check that this installation is set up correctly, and say what to do if it isn't.

Usage (from backend/):
    python scripts/doctor.py
    python scripts/doctor.py --json

Validates configuration, the database, storage permissions and the migration state.
Reads only, apart from creating and removing one small file to prove the storage
folder is writable. Never prints a password, a key or anything from a record.
Exit code 0 means no problems, 1 means at least one.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import configuration_problems, settings  # noqa: E402
from app.db import engine as configured_engine  # noqa: E402
from app.services import system_health  # noqa: E402

_ACTION_UPGRADE = "Back up, then run: python -m alembic upgrade head"


def _check(name: str, state: str, message: str, action: str | None = None) -> dict:
    return {"name": name, "state": state, "message": message, "action": action}


def _private(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def _configuration() -> list[dict]:
    problems = configuration_problems(settings)
    if problems:
        return [_check("Configuration", "problem", problem, "Fix this in backend/.env or the environment.") for problem in problems]
    mode = "production" if settings.ticketflow_env == "production" else "development"
    return [_check("Configuration", "ok", f"Settings are consistent for {mode}.")]


def _database(engine) -> list[dict]:
    checks = []
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            if engine.dialect.name == "sqlite":
                result = connection.execute(text("PRAGMA quick_check")).scalar()
                if result != "ok":
                    return [_check("Database", "problem", "SQLite's integrity check failed.", "See 'Database corruption' in docs/engineering/operations.md.")]
                orphaned = len(connection.execute(text("PRAGMA foreign_key_check")).fetchall())
                if orphaned:
                    checks.append(_check("Database references", "attention", f"{orphaned} row(s) point at something that doesn't exist.",
                                         "Run scripts/migrate_sqlite_to_postgres.py --source <database> --preflight-only to list them."))
    except SQLAlchemyError as exc:
        cause = getattr(exc, "orig", exc)
        return [_check("Database", "problem", f"Can't use the database ({type(cause).__name__}).", "Check DATABASE_URL, and that the database is running.")]
    checks.insert(0, _check("Database", "ok", f"Answering ({engine.dialect.name})."))
    if engine.dialect.name == "sqlite":
        database = engine.url.database
        if database and Path(database).is_file() and not _private(Path(database)):
            checks.append(_check("Database file", "attention", "Other accounts on this machine can read the database file.", "Run: chmod 600 on the database file."))
    return checks


def _migrations(engine) -> list[dict]:
    try:
        current, expected = system_health.schema_revisions(engine)
    except SQLAlchemyError:
        return []  # Already reported: the database can't be used.
    checks = []
    if current is None:
        checks.append(_check("Migrations", "problem", "This database isn't tracked by migrations.", _ACTION_UPGRADE))
    elif current != expected:
        checks.append(_check("Migrations", "problem", f"At {current}, but this code needs {expected}.", _ACTION_UPGRADE))
    else:
        checks.append(_check("Migrations", "ok", f"Up to date ({current})."))
    if inspect(engine).has_table("tickets"):
        with Session(engine) as db:
            missing = system_health.missing_safeguards(db)
        if missing:
            checks.append(_check("Duplicate safeguards", "problem", "Existing records break rules the database should enforce: " + ", ".join(missing) + ".",
                                 "See 'Duplicates found during an upgrade' in docs/engineering/operations.md."))
        else:
            checks.append(_check("Duplicate safeguards", "ok", "The database enforces the rules that stop double counting."))
    return checks


def _storage() -> list[dict]:
    root = settings.storage_dir
    if not root.exists():
        return [_check("Storage folder", "attention", "It doesn't exist yet. It's created when the API first starts.")]
    if not root.is_dir():
        return [_check("Storage folder", "problem", "STORAGE_DIR isn't a folder.", "Point STORAGE_DIR at a folder.")]
    probe = root / f".doctor-{uuid.uuid4().hex}.partial"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        probe.unlink()
    except OSError:
        return [_check("Storage folder", "problem", "This account can't write to it.", "Fix the owner or permissions of STORAGE_DIR.")]
    checks = [_check("Storage folder", "ok", "Writable.") if _private(root) else
              _check("Storage folder", "attention", "Writable, but other accounts on this machine can look inside it.", "Run: chmod 700 on STORAGE_DIR.")]
    free = shutil.disk_usage(root).free
    if free < system_health.LOW_DISK_BYTES:
        checks.append(_check("Disk space", "problem", f"Only {free // 1024 ** 2} MB free for documents.", "Free up space before uploading more scans."))
    else:
        checks.append(_check("Disk space", "ok", f"{free // 1024 ** 3} GB free."))
    return checks


def _backups() -> list[dict]:
    folder = settings.backup_dir
    if not folder.is_dir():
        return [_check("Backups", "problem", "No backups yet.", "Run: python scripts/backup.py")]
    wide = [path.name for path in folder.iterdir() if path.is_dir() and not _private(path)]
    if wide:
        return [_check("Backups", "attention", f"{len(wide)} backup folder(s) can be read by other accounts on this machine.", "Run: chmod -R go-rwx on the backup folder.")]
    return [_check("Backups", "ok", "Private.")]


def run_checks(engine=None) -> list[dict]:
    engine = engine if engine is not None else configured_engine
    return _configuration() + _database(engine) + _migrations(engine) + _storage() + _backups()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    checks = run_checks()
    if args.json:
        print(json.dumps(checks, indent=2))
    else:
        for check in checks:
            print(f"{check['state']:<10} {check['name']}: {check['message']}")
            if check["action"]:
                print(f"           -> {check['action']}")
    return 1 if any(check["state"] == "problem" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
