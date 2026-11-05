"""Make a verified copy of the database and stored documents.

Usage (from backend/): python scripts/backup.py [--output-dir DIR] [--no-documents]

Safe while TicketFlow is running: SQLite's online backup copies a consistent
snapshot. Only reads the live database. Each backup goes in a new folder and
nothing is ever overwritten. Backups hold real documents, so keep them private
and off the machine they came from.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

COUNT_TABLES = ("organizations", "users", "projects", "batches", "tickets", "audit_events", "invoice_comparisons")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    return Path(unquote(url[len("sqlite:///"):]))


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    found: dict[str, int] = {}
    for table in COUNT_TABLES:
        try:
            found[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        except sqlite3.OperationalError:
            continue
    try:
        found["verified_tickets"] = connection.execute("SELECT COUNT(*) FROM tickets WHERE status = 'verified'").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    return found


def _revision(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def make_backup(*, database: Path, storage: Path | None, output_dir: Path) -> Path:
    if not database.is_file():
        raise SystemExit(f"Database not found: {database}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc)
    folder = output_dir / ("ticketflow-" + stamp.strftime("%Y%m%d-%H%M%S"))
    suffix = 1
    while folder.exists():
        suffix += 1
        folder = output_dir / f"ticketflow-{stamp.strftime('%Y%m%d-%H%M%S')}-{suffix}"
    folder.mkdir(mode=0o700)

    copy = folder / "ticketflow.db"
    # Read-only on the source: this can't change the live database.
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise SystemExit(f"Backup failed its integrity check: {check}. The live database wasn't changed.")
        found, revision = counts(target), _revision(target)
    finally:
        target.close()
        source.close()
    os.chmod(copy, 0o600)

    manifest = {
        "taken_at": stamp.isoformat(), "revision": revision, "counts": found,
        "database_sha256": sha256(copy), "documents": None,
    }
    if storage is not None and storage.is_dir():
        archive = folder / "documents.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(storage.rglob("*")):
                if path.is_file() and path.name not in ("worker.lock", "worker.heartbeat"):
                    tar.add(path, arcname=str(path.relative_to(storage)))
        os.chmod(archive, 0o600)
        manifest["documents"] = {"sha256": sha256(archive), "bytes": archive.stat().st_size}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=settings.backup_dir)
    parser.add_argument("--no-documents", action="store_true", help="Copy the database only")
    args = parser.parse_args(argv)
    database = sqlite_path(settings.database_url)
    if database is None:
        print("This script copies SQLite databases. For PostgreSQL use pg_dump and your storage provider's versioning.",
              file=sys.stderr)
        return 2
    if not database.is_file():
        print(f"Database not found: {database}", file=sys.stderr)
        return 1
    existing = args.output_dir if args.output_dir.exists() else args.output_dir.parent
    if shutil.disk_usage(existing).free < database.stat().st_size * 2:
        print("Not enough free space for a backup. Nothing was written.", file=sys.stderr)
        return 1
    folder = make_backup(database=database, storage=None if args.no_documents else settings.storage_dir, output_dir=args.output_dir)
    manifest = json.loads((folder / "manifest.json").read_text())
    print(f"Backup written to {folder}")
    print("Contains: " + ", ".join(f"{count} {name}" for name, count in manifest["counts"].items()))
    print("Check it with: python scripts/verify_backup.py " + str(folder))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
