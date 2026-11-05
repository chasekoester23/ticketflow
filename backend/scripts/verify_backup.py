"""Prove a backup can be restored, optionally into a NEW location.

Usage (from backend/):
    python scripts/verify_backup.py BACKUP_FOLDER
    python scripts/verify_backup.py BACKUP_FOLDER --restore-to NEW_EMPTY_FOLDER

Never touches the live database. --restore-to refuses a folder that already has
anything in it, so it can't overwrite real data. Switching to a restored copy is
a separate, deliberate step: see docs/engineering/operations.md.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup import counts, sha256  # noqa: E402


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
    finally:
        connection.close()
    documents = manifest.get("documents")
    if documents:
        archive = folder / "documents.tar.gz"
        if not archive.is_file():
            problems.append("The documents archive is missing.")
        elif sha256(archive) != documents["sha256"]:
            problems.append("The documents archive has changed since the backup was taken.")
        else:
            try:
                with tarfile.open(archive, "r:gz") as tar:
                    tar.getmembers()
            except tarfile.TarError as exc:
                problems.append(f"The documents archive can't be read: {exc}")
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
