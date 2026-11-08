"""Prove a backup can be restored, optionally into a NEW location.

Usage (from backend/):
    python scripts/verify_backup.py BACKUP_FOLDER
    python scripts/verify_backup.py BACKUP_FOLDER --restore-to NEW_EMPTY_FOLDER

Never touches the live database. --restore-to refuses a folder that already has
anything in it, so it can't overwrite real data. Switching to a restored copy is
a separate, deliberate step: see docs/engineering/operations.md.

An incomplete backup is caught too: every document the database refers to has to be
in the archive, and match its recorded hash. For a full rehearsal that also starts
the application against the copy, use scripts/restore_drill.py.
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backup import counts, sha256, sqlite_columns  # noqa: E402

from app.services import document_audit  # noqa: E402

_NAMED = 10


def _named(refs: list[document_audit.DocumentRef]) -> str:
    shown = ", ".join(f"{ref.table} {ref.row_id}" for ref in refs[:_NAMED])
    return shown + (f" and {len(refs) - _NAMED} more" if len(refs) > _NAMED else "")


def _check_documents(archive: Path, wanted: dict[str, document_audit.DocumentRef]) -> list[str]:
    """Every referenced document is in the archive and matches its hash. Reading through
    the archive once does both."""
    remaining = dict(wanted)
    damaged: list[document_audit.DocumentRef] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                ref = remaining.pop(member.name, None) if member.isfile() else None
                if ref is None or not ref.sha256:
                    continue
                stream = tar.extractfile(member)
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != ref.sha256:
                    damaged.append(ref)
    except (tarfile.TarError, OSError, EOFError) as exc:
        return [f"The documents archive can't be read: {type(exc).__name__}"]
    problems = []
    if remaining:
        problems.append(f"Incomplete: {len(remaining)} document(s) the database refers to aren't in the archive: {_named(list(remaining.values()))}.")
    if damaged:
        problems.append(f"Damaged: {len(damaged)} document(s) in the archive don't match their recorded hash: {_named(damaged)}.")
    return problems


def verify(folder: Path) -> list[str]:
    """Problems found. Empty means this backup is sound."""
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        return ["No manifest.json in this folder."]
    manifest = json.loads(manifest_path.read_text())
    database = folder / "ticketflow.db"
    if not database.is_file():
        return ["The database copy is missing."]
    problems = []
    if sha256(database) != manifest["database_sha256"]:
        problems.append("The database copy has changed since the backup was taken.")
    wanted: dict[str, document_audit.DocumentRef] | None = None
    unknown: list[document_audit.DocumentRef] = []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        try:
            check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            check = str(exc)
        if check != "ok":
            problems.append(f"The database copy failed its integrity check: {check}")
        else:
            found = counts(connection)
            for name, expected in manifest["counts"].items():
                if found.get(name) != expected:
                    problems.append(f"{name}: manifest says {expected}, copy has {found.get(name)}.")
            wanted, unknown = document_audit.expected_keys(lambda sql: connection.execute(sql), sqlite_columns(connection))
    finally:
        connection.close()
    documents = manifest.get("documents")
    archive = folder / "documents.tar.gz"
    if documents:
        if not archive.is_file():
            problems.append("The documents archive is missing.")
        elif sha256(archive) != documents["sha256"]:
            problems.append("The documents archive has changed since the backup was taken.")
        elif wanted is not None:
            problems.extend(_check_documents(archive, wanted))
    elif wanted:
        problems.append(f"Incomplete: this backup has no documents, but its database refers to {len(wanted)}.")
    if unknown:
        problems.append(f"Incomplete: {len(unknown)} row(s) refer to a document at an unrecognisable location, so it can't be in this backup: {_named(unknown)}.")
    return problems


def restore(folder: Path, destination: Path) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f"Refusing to restore into {destination}: it isn't empty. Nothing was changed.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(folder / "ticketflow.db", destination / "ticketflow.db")
    (destination / "ticketflow.db").chmod(0o600)
    archive = folder / "documents.tar.gz"
    if archive.is_file():
        storage = destination / "storage"
        storage.mkdir(mode=0o700)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(storage, filter="data")  # Refuses paths outside the folder.
        # Whatever the modes were on the original disk, a restored copy is private.
        for parent, folders, files in os.walk(storage):
            for name in folders:
                os.chmod(os.path.join(parent, name), 0o700)
            for name in files:
                os.chmod(os.path.join(parent, name), 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--restore-to", type=Path)
    args = parser.parse_args(argv)
    problems = verify(args.folder)
    if problems:
        print("This backup has problems:", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    manifest = json.loads((args.folder / "manifest.json").read_text())
    print("Backup is sound: " + ", ".join(f"{count} {name}" for name, count in manifest["counts"].items()))
    if args.restore_to:
        restore(args.folder, args.restore_to)
        print(f"Restored a copy to {args.restore_to}. Your live data wasn't touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
