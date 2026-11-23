"""Prove an encrypted PostgreSQL backup restores, into a separate empty database.

Usage (from backend/):
    python scripts/restore_pg_drill.py --latest --target-url postgresql://drill@host/ticketflow_drill
    python scripts/restore_pg_drill.py --backup 20261110T023000Z --target-url ... --cleanup --report drill.json

Downloads the backup from the backup bucket, checks its size and SHA-256, decrypts it
(which proves it wasn't changed), restores it with pg_restore into --target-url, then
compares every count and each company's exact approved totals with the manifest taken
at backup time, and checks the documents the restored database refers to are in the
backup bucket, decrypting a sample of them. Exit code 0 means pass, 1 means fail.

Never touches the source: it refuses a target that is DATABASE_URL, and refuses a
target that already holds tables, so it can't overwrite anything. --cleanup empties the
target's public schema afterwards; without it the restored copy is left for you to
inspect. A drill restores data, so run it only somewhere as private as production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alerting  # noqa: E402
from backup_pg import (BackupError, backup_bucket, database_backups, decrypt_bytes, decrypt_file, digest, libpq_target,  # noqa: E402
                       load_key, same_database, snapshot_stats, tool, _run)

from app.config import settings  # noqa: E402


class Drill:
    def __init__(self, target_url: str):
        self.target_url = target_url
        self.checks: list[dict] = []

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})
        print(f"{'pass' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
        return passed

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check["passed"] for check in self.checks)


def _engine(url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from app.db import normalized_url
    return create_engine(normalized_url(url), poolclass=NullPool, hide_parameters=True)


def target_is_empty(url: str) -> bool:
    from sqlalchemy import text
    engine = _engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
            )).scalar_one() == 0
    finally:
        engine.dispose()


def empty_target(url: str) -> None:
    from sqlalchemy import text
    engine = _engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def choose(bucket, stamp: str | None) -> str:
    complete = sorted(name for name, entry in database_backups(bucket).items() if entry["complete"])
    if not complete:
        raise BackupError("The backup bucket holds no complete database backup.")
    if stamp is None:
        return complete[-1]
    if stamp not in complete:
        raise BackupError(f"No complete backup named {stamp}.")
    return stamp


def run(target_url: str, *, stamp: str | None, cleanup: bool, sample: int, work: Path) -> Drill:
    drill = Drill(target_url)
    if settings.database_url and same_database(target_url, settings.database_url):
        raise BackupError("The target is the live database (DATABASE_URL). A drill only ever restores somewhere else.")
    target, env = libpq_target(target_url)
    if not target_is_empty(target_url):
        raise BackupError("The target database already holds tables. Give an empty database, so nothing is overwritten.")
    key = load_key(settings.backup_encryption_key)
    bucket = backup_bucket()
    stamp = choose(bucket, stamp)
    folder = bucket.key("postgres", stamp)
    meta = json.loads(bucket.get_bytes(f"{folder}/meta.json"))
    print(f"Drill of backup {stamp}, taken {meta['taken_at']}, key '{meta['key_id']}'.")

    sealed = work / "dump.enc"
    bucket.client.download_file(bucket.name, f"{folder}/dump.enc", str(sealed))
    expected = meta["objects"]["dump.enc"]
    if not drill.record("Downloaded intact", digest(sealed) == (expected["sha256"], expected["bytes"]),
                        f"{expected['bytes']} bytes, SHA-256 as recorded"):
        return drill
    dump = work / "dump"
    try:
        decrypt_file(sealed, dump, key)
    except BackupError as exc:
        drill.record("Decrypted and authenticated", False, str(exc))
        return drill
    plain = meta["plaintext"]
    if not drill.record("Decrypted and authenticated", digest(dump) == (plain["sha256"], plain["bytes"]), "AES-256-GCM tag verified"):
        return drill
    manifest = json.loads(decrypt_bytes(bucket.get_bytes(f"{folder}/manifest.enc"), key))
    sealed.unlink()

    try:
        _run([tool("pg_restore"), "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
              f"--dbname={target}", str(dump)], env, 7200)
        drill.record("Restored with pg_restore", True)
    except BackupError as exc:
        drill.record("Restored with pg_restore", False, str(exc))
        return drill
    finally:
        dump.unlink(missing_ok=True)

    engine = _engine(target_url)
    try:
        with engine.connect() as connection:
            found = snapshot_stats(connection)
    finally:
        engine.dispose()
    drill.record("Database version", found["revision"] == manifest["revision"], f"{found['revision']}")
    drill.record("Counts match the backup", found["counts"] == manifest["counts"],
                 ", ".join(f"{count} {name}" for name, count in found["counts"].items()))
    drill.record("Each company's approved totals match exactly", found["totals"] == manifest["totals"],
                 f"{len(found['totals'])} compan{'y' if len(found['totals']) == 1 else 'ies'}")

    referenced = found["referenced_documents"]
    if manifest.get("documents") is None:
        drill.record("Documents", not referenced, "this backup was made with --no-documents" if referenced else "none referenced")
    else:
        stored = {item["Key"] for item in bucket.keys(bucket.key("documents") + "/")}
        missing = [name for name in referenced if bucket.key("documents", name) not in stored]
        drill.record("Every referenced document is in the backup bucket", not missing,
                     f"{len(referenced) - len(missing)} of {len(referenced)}")
        index = json.loads(bucket.get_bytes(bucket.key("documents-index.json")))
        chosen = random.SystemRandom().sample([n for n in referenced if n not in missing], min(sample, len(referenced) - len(missing)))
        bad = 0
        for name in chosen:
            data = decrypt_bytes(bucket.get_bytes(bucket.key("documents", name)), key)
            bad += hashlib.sha256(data).hexdigest() != index.get(name, {}).get("sha256")
        drill.record("A sample of documents decrypts to what was copied", bad == 0, f"{len(chosen) - bad} of {len(chosen)} checked")

    if cleanup:
        empty_target(target_url)
        print("The restored copy was removed from the target (--cleanup).")
    else:
        print("The restored copy was left in the target database. Remove it when you're done: it holds real records.")
    return drill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target-url", required=True, help="An EMPTY PostgreSQL database to restore into")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--latest", action="store_true")
    which.add_argument("--backup", help="A backup's name, as --list in backup_pg.py shows it")
    parser.add_argument("--cleanup", action="store_true", help="Empty the target afterwards")
    parser.add_argument("--sample", type=int, default=20, help="How many documents to decrypt and check")
    parser.add_argument("--report", type=Path, help="Also write the result as JSON here")
    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)
    try:
        with tempfile.TemporaryDirectory(prefix="tf-pg-drill-") as work:
            drill = run(args.target_url, stamp=args.backup, cleanup=args.cleanup, sample=args.sample, work=Path(work))
    except Exception as exc:  # noqa: BLE001
        reason = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}"
        print(f"Drill FAILED before restoring anything: {reason}", file=sys.stderr)
        alerting.send(settings.alert_webhook_url, "restore_drill_failed", "The restore drill failed.", details={"reason": reason[:300]})
        return 1
    seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    print(f"{'PASSED' if drill.passed else 'FAILED'} in {seconds} seconds.")
    if args.report:
        args.report.write_text(json.dumps({"passed": drill.passed, "seconds": seconds, "checks": drill.checks}, indent=2))
    if not drill.passed:
        alerting.send(settings.alert_webhook_url, "restore_drill_failed", "The restore drill failed a check.")
    return 0 if drill.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
