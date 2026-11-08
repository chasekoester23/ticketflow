"""Make a verified copy of the database and stored documents.

Usage (from backend/):
    python scripts/backup.py [--output-dir DIR] [--no-documents]
    python scripts/backup.py --keep 14 --copy-to /Volumes/BackupDrive/ticketflow
    python scripts/backup.py --print-schedule launchd

Safe while TicketFlow is running: SQLite's online backup copies a consistent
snapshot. Only reads the live database. Each backup goes in a new folder and
nothing is ever overwritten. Backups hold real documents, so keep them private
and off the machine they came from.

--keep only ever removes older folders it recognises as its own, and never the
newest. --print-schedule only prints a template: installing it is your choice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402
from app.services import document_audit  # noqa: E402

COUNT_TABLES = ("organizations", "users", "projects", "batches", "tickets", "audit_events", "invoice_comparisons")
# What makes a folder ours to remove: this name, and a manifest only this script writes.
OWN_FOLDER = re.compile(r"^ticketflow-\d{8}-\d{6}(-\d+)?$")
_FILES_NOT_BACKED_UP = ("worker.lock", "worker.heartbeat")
_BACKEND = Path(__file__).resolve().parent.parent


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


def company_totals(connection: sqlite3.Connection) -> dict[str, dict]:
    """Each company's approved tons and cost. SQLite keeps decimals as floats, so each
    is rounded to a whole number of thousandths or cents before adding: exact, and the
    same arithmetic the app's own totals use."""
    try:
        rows = connection.execute(
            "SELECT organization_id, COUNT(*), COALESCE(SUM(CAST(ROUND(tonnage * 1000) AS INTEGER)), 0), "
            "COALESCE(SUM(CAST(ROUND(approved_cost * 100) AS INTEGER)), 0) FROM tickets "
            "WHERE status = 'verified' AND tonnage_confirmed = 1 GROUP BY organization_id ORDER BY organization_id").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(company): {"approved_tickets": tickets, "approved_tons": _scaled(tons, 3), "approved_cost": _scaled(cost, 2)}
            for company, tickets, tons, cost in rows}


def _scaled(units: int, scale: int) -> str:
    sign = "-" if units < 0 else ""
    digits = str(abs(units)).rjust(scale + 1, "0")
    return f"{sign}{digits[:-scale]}.{digits[-scale:]}"


def _revision(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def sqlite_columns(connection: sqlite3.Connection):
    def columns_of(table: str) -> set[str]:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}  # noqa: S608
    return columns_of


def _private_copy_of_documents(storage: Path, archive: Path) -> int:
    files = 0
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(storage.rglob("*")):
            # A leading dot is a write in progress, never a finished document.
            if path.is_file() and path.name not in _FILES_NOT_BACKED_UP and not path.name.startswith("."):
                tar.add(path, arcname=str(path.relative_to(storage)))
                files += 1
    os.chmod(archive, 0o600)
    return files


def _write_private_json(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)


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
        found, revision, totals = counts(target), _revision(target), company_totals(target)
        # Decided from the snapshot, because a document uploaded after it doesn't matter
        # here, while one the snapshot refers to must be in this backup.
        wanted, _unknown = document_audit.expected_keys(lambda sql: target.execute(sql), sqlite_columns(target))
    finally:
        target.close()
        source.close()
    os.chmod(copy, 0o600)

    manifest = {
        "tool": "ticketflow-backup", "taken_at": stamp.isoformat(), "revision": revision, "counts": found, "totals": totals,
        "database_sha256": sha256(copy), "database_bytes": copy.stat().st_size, "documents": None,
    }
    if storage is not None and storage.is_dir():
        archive = folder / "documents.tar.gz"
        files = _private_copy_of_documents(storage, archive)
        manifest["documents"] = {"sha256": sha256(archive), "bytes": archive.stat().st_size, "files": files, "referenced": len(wanted)}
    # Written last, so a folder with a manifest is a backup that finished.
    _write_private_json(folder / "manifest.json", manifest)
    return folder


# ------------------------------------------------------------------ keeping and removing

def _manifest_of(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / "manifest.json").read_text())
        datetime.fromisoformat(data["taken_at"])
        data["database_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def own_backups(directory: Path) -> list[tuple[str, Path]]:
    """Folders that are certainly backups this script made, newest first. Anything else
    in the folder, whatever it's called, is none of its business."""
    found = []
    if not directory.is_dir():
        return found
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_dir() or not OWN_FOLDER.match(path.name):
            continue
        manifest = _manifest_of(path)
        if manifest is not None:
            found.append((datetime.fromisoformat(manifest["taken_at"]).isoformat(), path))
    return sorted(found, key=lambda item: (item[0], item[1].name), reverse=True)


def newest_backup(directory: Path) -> Path | None:
    backups = own_backups(directory)
    return backups[0][1] if backups else None


def _looks_complete(folder: Path) -> bool:
    """The files a manifest promises are there at the size it recorded. Cheap enough to
    run over every backup kept. A complete check of the newest happens before this."""
    manifest = _manifest_of(folder)
    if manifest is None:
        return False
    database = folder / "ticketflow.db"
    if not database.is_file() or database.stat().st_size != manifest.get("database_bytes", database.stat().st_size):
        return False
    documents = manifest.get("documents")
    if documents:
        archive = folder / "documents.tar.gz"
        if not archive.is_file() or archive.stat().st_size != documents.get("bytes", archive.stat().st_size):
            return False
    return True


def prune(directory: Path, keep: int) -> list[Path]:
    """Remove this script's older backups beyond the newest ``keep`` sound ones. A damaged
    backup doesn't count toward ``keep``, so it can't push out the last good one, and it
    isn't removed either: a person should see it. The newest is never removed."""
    if keep < 1:
        raise ValueError("--keep must be at least 1, so the newest backup is always kept.")
    backups = [path for _taken, path in own_backups(directory)]
    sound = [path for path in backups if _looks_complete(path)]
    removed = []
    for path in sound[keep:]:
        if backups and path == backups[0]:
            continue
        shutil.rmtree(path)
        removed.append(path)
    return removed


# ------------------------------------------------------------------ off-device copies

class CopyFailed(Exception):
    """The copy didn't arrive intact. Nothing was left at the destination."""


class BackupTarget(Protocol):
    """Somewhere else to keep a backup. A cloud target would implement these two."""

    def put(self, folder: Path) -> str:
        """Copy a finished backup there and prove it arrived intact, or raise CopyFailed."""

    def prune(self, keep: int) -> list[str]:
        """Apply the same retention there."""


class LocalFolderTarget:
    """Another disk, a mounted drive or a network share."""

    def __init__(self, root: Path):
        self.root = root

    def on_the_same_disk_as(self, folder: Path) -> bool:
        return self.root.stat().st_dev == folder.stat().st_dev

    def put(self, folder: Path) -> str:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        final = self.root / folder.name
        if final.exists():
            raise CopyFailed(f"{final} already exists. Nothing was overwritten.")
        # A folder of this name doesn't count as a backup until it has been proved.
        partial = self.root / f".{folder.name}.{uuid.uuid4().hex}.partial"
        partial.mkdir(mode=0o700)
        try:
            for path in folder.iterdir():
                if path.is_file():
                    shutil.copyfile(path, partial / path.name)
                    os.chmod(partial / path.name, 0o600)
            problems = _verify(partial)
            if problems:
                raise CopyFailed("The copy didn't match the backup: " + "; ".join(problems))
            os.rename(partial, final)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return str(final)

    def prune(self, keep: int) -> list[str]:
        return [path.name for path in prune(self.root, keep)]


def _verify(folder: Path) -> list[str]:
    # Imported here because verify_backup uses this module's helpers.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verify_backup import verify
    return verify(folder)


# ------------------------------------------------------------------ schedule templates

def schedule_template(kind: str, *, keep: int, copy_to: Path | None, output_dir: Path) -> str:
    """Text for a person to read and install if they choose. Nothing here installs it."""
    arguments = [sys.executable, str(_BACKEND / "scripts" / "backup.py"), "--output-dir", str(output_dir), "--keep", str(keep)]
    if copy_to is not None:
        arguments += ["--copy-to", str(copy_to)]
    log = output_dir / "backup.log"
    if kind == "cron":
        command = " ".join(f"'{argument}'" if " " in argument else argument for argument in arguments)
        return (f"# TicketFlow nightly backup at 02:30, keeping the newest {keep}. Nothing has been installed.\n"
                "# To install it: run 'crontab -e' and paste the line below. Check it the next morning with:\n"
                f"#   python scripts/verify_backup.py {output_dir}/<newest folder>\n"
                f"30 2 * * * cd '{_BACKEND}' && {command} >> '{log}' 2>&1\n")
    lines = "\n".join(f"        <string>{escape(argument)}</string>" for argument in arguments)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- TicketFlow nightly backup at 02:30, keeping the newest {keep}. Nothing has been installed.
     To install it: save this as ~/Library/LaunchAgents/com.ticketflow.backup.plist, then run
       launchctl load ~/Library/LaunchAgents/com.ticketflow.backup.plist
     A sleeping Mac runs it when it next wakes. To remove it, 'launchctl unload' that file and delete it. -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ticketflow.backup</string>
    <key>WorkingDirectory</key>
    <string>{escape(str(_BACKEND))}</string>
    <key>ProgramArguments</key>
    <array>
{lines}
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{escape(str(log))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(log))}</string>
</dict>
</plist>
"""


# ------------------------------------------------------------------ running it

def _bytes_in(storage: Path | None) -> int:
    if storage is None or not storage.is_dir():
        return 0
    return sum(path.stat().st_size for path in storage.rglob("*") if path.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=settings.backup_dir)
    parser.add_argument("--no-documents", action="store_true", help="Copy the database only. Verifying this backup will report it as incomplete")
    parser.add_argument("--keep", type=int, help="After a sound backup, remove this script's older backups beyond the newest N")
    parser.add_argument("--copy-to", type=Path, help="Also copy the backup to this folder, on another disk, and verify it there")
    parser.add_argument("--print-schedule", choices=("launchd", "cron"), help="Print a nightly schedule to install yourself, then stop. Installs nothing")
    args = parser.parse_args(argv)
    if args.keep is not None and args.keep < 1:
        parser.error("--keep must be at least 1, so the newest backup is always kept.")
    if args.print_schedule:
        print(schedule_template(args.print_schedule, keep=args.keep or 14, copy_to=args.copy_to, output_dir=args.output_dir.resolve()), end="")
        return 0
    database = sqlite_path(settings.database_url)
    if database is None:
        print("This script copies SQLite databases. For PostgreSQL use pg_dump and your storage provider's versioning.",
              file=sys.stderr)
        return 2
    if not database.is_file():
        print(f"Database not found: {database}", file=sys.stderr)
        return 1
    storage = None if args.no_documents else settings.storage_dir
    existing = args.output_dir if args.output_dir.exists() else args.output_dir.parent
    if shutil.disk_usage(existing).free < database.stat().st_size * 2 + _bytes_in(storage):
        print("Not enough free space for a backup. Nothing was written.", file=sys.stderr)
        return 1
    folder = make_backup(database=database, storage=storage, output_dir=args.output_dir)
    manifest = json.loads((folder / "manifest.json").read_text())
    print(f"Backup written to {folder}")
    print("Contains: " + ", ".join(f"{count} {name}" for name, count in manifest["counts"].items()))
    problems = _verify(folder)
    if problems and not args.no_documents:
        print("This backup has problems, so nothing older was removed:", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    if problems:
        print("Without documents, this backup can't restore your scans: " + "; ".join(problems), file=sys.stderr)
    target = LocalFolderTarget(args.copy_to) if args.copy_to else None
    if target is not None:
        try:
            print(f"Copied and verified at {target.put(folder)}")
        except (CopyFailed, OSError) as exc:
            print(f"The off-device copy failed, so nothing older was removed: {exc}", file=sys.stderr)
            return 1
        if target.on_the_same_disk_as(folder):
            print("Warning: that folder is on the same disk as this backup, so it won't survive losing the disk.", file=sys.stderr)
    if args.keep is not None and not problems:
        for path in prune(args.output_dir, args.keep):
            print(f"Removed older backup {path.name}")
        if target is not None:
            for name in target.prune(args.keep):
                print(f"Removed older copy {name}")
    if target is None:
        print("Reminder: a backup on the same disk doesn't protect against losing that disk. Use --copy-to.")
    print("Prove it can be recovered from: python scripts/restore_drill.py " + str(folder))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
