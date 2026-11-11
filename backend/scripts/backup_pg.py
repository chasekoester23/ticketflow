"""Encrypted, verified, off-service backups of a hosted PostgreSQL database and its documents.

Usage (from backend/):
    python scripts/backup_pg.py                  # database, then documents, then retention
    python scripts/backup_pg.py --no-documents   # the database only
    python scripts/backup_pg.py --list           # what the backup bucket holds

What it does, in order:
 1. Opens one read-only REPEATABLE READ transaction on DATABASE_URL and exports its
    snapshot. The counts and exact per-company totals in the manifest, and pg_dump -Fc,
    both read that same snapshot, so the manifest describes exactly what was dumped.
 2. Encrypts the dump with AES-256-GCM (BACKUP_ENCRYPTION_KEY; its name,
    BACKUP_ENCRYPTION_KEY_ID, goes in the header and the manifest).
 3. Uploads it to the BACKUP bucket (BACKUP_S3_*), a different bucket from the documents,
    then downloads it again, checks size and SHA-256, and decrypts it, which proves the
    authentication tag. Only then is meta.json written, which marks the backup complete.
 4. Copies documents into the backup bucket, encrypted, incrementally by key, and
    checks each copy by downloading, decrypting and hashing it.
 5. Applies retention (BACKUP_KEEP_DAILY newest days, BACKUP_KEEP_MONTHLY newest months)
    to complete database backups, only after this one was proved, never the newest.

Only reads the live database: one read-only transaction. Documents come from
BACKUP_SOURCE_S3_* (a read-only credential on the documents bucket), or STORAGE_DIR
if that isn't set. On any failure it exits 1 and, if ALERT_WEBHOOK_URL is set, says so
there. Prove a backup restores with scripts/restore_pg_drill.py.

The key never leaves this process and is never printed. Lose it and every backup made
with it is unreadable: keep a copy somewhere other than the platform running this.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import alerting  # noqa: E402
from backup import COUNT_TABLES, _FILES_NOT_BACKED_UP, _scaled  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import document_audit  # noqa: E402

TOOL = "ticketflow-backup-pg"
MAGIC = b"TFENC1\n\x00"
NONCE_BYTES, TAG_BYTES, CHUNK = 12, 16, 1024 * 1024
STAMP = "%Y%m%dT%H%M%SZ"
INCOMPLETE_AFTER = timedelta(days=2)


class BackupError(Exception):
    """Something a person must look at. The message never holds a secret or a value."""


# ------------------------------------------------------------------ encryption

def load_key(raw: str | None) -> bytes:
    """32 bytes, written as base64 (44 characters) or hex (64 characters)."""
    if not raw:
        raise BackupError("BACKUP_ENCRYPTION_KEY isn't set. Make one with: "
                          "python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\"")
    raw = raw.strip()
    for decode in (bytes.fromhex, lambda text: base64.b64decode(text, validate=True), base64.urlsafe_b64decode):
        try:
            key = decode(raw)
        except (ValueError, binascii.Error):
            continue
        if len(key) == 32:
            return key
    raise BackupError("BACKUP_ENCRYPTION_KEY must be 32 bytes, as base64 or hex.")


def _header(key_id: str, nonce: bytes) -> bytes:
    name = key_id.encode()
    if not 0 < len(name) < 256:
        raise BackupError("BACKUP_ENCRYPTION_KEY_ID must be 1 to 255 bytes.")
    return MAGIC + bytes([len(name)]) + name + nonce


def _read_header(stream) -> tuple[bytes, str, bytes]:
    magic = stream.read(len(MAGIC))
    if magic != MAGIC:
        raise BackupError("This isn't a TicketFlow encrypted backup, or its start is damaged.")
    length = stream.read(1)
    name = stream.read(length[0]) if length else b""
    nonce = stream.read(NONCE_BYTES)
    if not length or len(name) != length[0] or len(nonce) != NONCE_BYTES:
        raise BackupError("The encrypted backup's header is cut short.")
    return magic + length + name + nonce, name.decode(errors="replace"), nonce


def _cipher(key: bytes, nonce: bytes, tag: bytes | None = None):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(key), modes.GCM(nonce, tag))


def encrypt_file(source: Path, destination: Path, key: bytes, key_id: str) -> None:
    """Streamed AES-256-GCM: header (authenticated), ciphertext, 16-byte tag."""
    nonce = os.urandom(NONCE_BYTES)
    header = _header(key_id, nonce)
    encryptor = _cipher(key, nonce).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as plain, _private(destination) as out:
        out.write(header)
        for chunk in iter(lambda: plain.read(CHUNK), b""):
            out.write(encryptor.update(chunk))
        out.write(encryptor.finalize())
        out.write(encryptor.tag)


def decrypt_file(source: Path, destination: Path, key: bytes) -> str:
    """Decrypts, and keeps the result only if the tag proves nothing was changed. Returns the key id."""
    from cryptography.exceptions import InvalidTag
    size = source.stat().st_size
    with source.open("rb") as stream:
        header, key_id, nonce = _read_header(stream)
        if size < len(header) + TAG_BYTES:
            raise BackupError("The encrypted backup is cut short.")
        stream.seek(size - TAG_BYTES)
        tag = stream.read(TAG_BYTES)
        stream.seek(len(header))
        decryptor = _cipher(key, nonce, tag).decryptor()
        decryptor.authenticate_additional_data(header)
        remaining = size - len(header) - TAG_BYTES
        try:
            with _private(destination) as out:
                while remaining:
                    chunk = stream.read(min(CHUNK, remaining))
                    if not chunk:
                        raise BackupError("The encrypted backup is cut short.")
                    remaining -= len(chunk)
                    out.write(decryptor.update(chunk))
                decryptor.finalize()
        except InvalidTag:
            destination.unlink(missing_ok=True)
            raise BackupError(f"The backup failed its integrity check: it was changed or damaged, or the key isn't "
                              f"'{key_id}'. Nothing was restored from it.") from None
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return key_id


def encrypt_bytes(data: bytes, key: bytes, key_id: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(NONCE_BYTES)
    header = _header(key_id, nonce)
    return header + AESGCM(key).encrypt(nonce, data, header)


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    import io
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    header, key_id, nonce = _read_header(io.BytesIO(blob))
    try:
        return AESGCM(key).decrypt(nonce, blob[len(header):], header)
    except InvalidTag:
        raise BackupError(f"An encrypted object failed its integrity check (changed, damaged, or not key '{key_id}').") from None


class _private:
    """A new file only this user can read, created before anything is written to it."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.stream = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
        return self.stream

    def __exit__(self, *exc):
        self.stream.close()


def digest(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            sha.update(chunk)
    return sha.hexdigest(), path.stat().st_size


# ------------------------------------------------------------------ what the database holds

def snapshot_stats(connection) -> dict:
    """Counts, exact per-company approved totals, revision and referenced documents, from a
    SQLAlchemy connection. The same rules as scripts/backup.py, on SQLite or PostgreSQL."""
    from sqlalchemy import inspect, text
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    counts = {table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608  # nosec B608
              for table in COUNT_TABLES if table in tables}
    totals = {}
    if "tickets" in tables:
        counts["verified_tickets"] = connection.execute(text("SELECT COUNT(*) FROM tickets WHERE status = 'verified'")).scalar_one()
        rows = connection.execute(text(
            "SELECT organization_id, COUNT(*), COALESCE(SUM(CAST(ROUND(tonnage * 1000) AS BIGINT)), 0), "
            "COALESCE(SUM(CAST(ROUND(approved_cost * 100) AS BIGINT)), 0) FROM tickets "
            "WHERE status = 'verified' AND tonnage_confirmed = :yes GROUP BY organization_id ORDER BY organization_id"), {"yes": True})
        totals = {str(company): {"approved_tickets": int(tickets), "approved_tons": _scaled(int(tons), 3),
                                 "approved_cost": _scaled(int(cost), 2)} for company, tickets, tons, cost in rows}
    revision = (connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                if "alembic_version" in tables else None)
    columns = {}

    def columns_of(table: str) -> set[str]:
        if table not in columns:
            columns[table] = {column["name"] for column in inspector.get_columns(table)} if table in tables else set()
        return columns[table]
    wanted, _unknown = document_audit.expected_keys(lambda sql: connection.execute(text(sql)), columns_of)
    return {"revision": revision, "counts": counts, "totals": totals, "referenced_documents": sorted(wanted)}


# ------------------------------------------------------------------ connecting

def libpq_target(url: str) -> tuple[str, dict]:
    """A URL pg_dump/pg_restore accept, without the password, and the environment that
    carries it. A password on a command line can be read by anyone listing processes."""
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if not parsed.drivername.startswith(("postgresql", "postgres")):
        raise BackupError("This needs a PostgreSQL DATABASE_URL. For SQLite use scripts/backup.py.")
    env = {**os.environ}
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return parsed._replace(drivername="postgresql", password=None).render_as_string(hide_password=False), env


def same_database(one: str, other: str) -> bool:
    from sqlalchemy.engine import make_url
    try:
        a, b = make_url(one), make_url(other)
    except Exception:
        return False
    return ((a.host or "localhost").lower(), a.port or 5432, a.database) == ((b.host or "localhost").lower(), b.port or 5432, b.database)


def tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise BackupError(f"{name} isn't installed. Install the PostgreSQL client tools, the same major version as the server or newer.")
    return found


def _run(arguments: list[str], env: dict, timeout: int) -> None:
    try:
        result = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=timeout)  # noqa: S603 - fixed tools, no shell
    except subprocess.TimeoutExpired:
        raise BackupError(f"{Path(arguments[0]).name} took longer than {timeout} seconds and was stopped.") from None
    if result.returncode != 0:
        # pg tools' messages name objects and settings, not row values. The last line is enough.
        last = (result.stderr.strip().splitlines() or ["no message"])[-1][:300]
        raise BackupError(f"{Path(arguments[0]).name} failed: {last}")


# ------------------------------------------------------------------ the backup bucket

@dataclass
class Bucket:
    client: object
    name: str
    prefix: str

    def key(self, *parts: str) -> str:
        return "/".join([self.prefix.strip("/"), *parts]) if self.prefix.strip("/") else "/".join(parts)

    def keys(self, prefix: str) -> Iterator[dict]:
        token = None
        while True:
            page = self.client.list_objects_v2(Bucket=self.name, Prefix=prefix, **({"ContinuationToken": token} if token else {}))
            yield from page.get("Contents", [])
            if not page.get("IsTruncated"):
                return
            token = page["NextContinuationToken"]

    def put_bytes(self, key: str, data: bytes, **extra) -> None:
        self.client.put_object(Bucket=self.name, Key=key, Body=data, **extra)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.name, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.name, Key=key)


def s3_client(endpoint: str | None, region: str, access_key: str | None, secret: str | None):
    import boto3
    from botocore.config import Config
    # Checksums only when an operation requires them: S3-compatible services (R2, MinIO, B2)
    # differ in which of the newer default checksums they accept. SHA-256 is checked here anyway.
    config = Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}, s3={"addressing_style": "path"},
                    request_checksum_calculation="when_required", response_checksum_validation="when_required")
    return boto3.client("s3", endpoint_url=endpoint or None, region_name=region or None, aws_access_key_id=access_key,
                        aws_secret_access_key=secret, config=config)


def backup_bucket() -> Bucket:
    if not settings.backup_s3_bucket:
        raise BackupError("BACKUP_S3_BUCKET isn't set: backups go to their own bucket, separate from the documents.")
    if settings.backup_s3_bucket == _documents_bucket_name():
        raise BackupError("BACKUP_S3_BUCKET is the documents bucket. Backups must live somewhere else.")
    client = s3_client(settings.backup_s3_endpoint_url, settings.backup_s3_region, settings.backup_s3_access_key_id,
                       settings.backup_s3_secret_access_key)
    return Bucket(client, settings.backup_s3_bucket, settings.backup_s3_prefix)


def _documents_bucket_name() -> str | None:
    return settings.backup_source_s3_bucket


# ------------------------------------------------------------------ documents

class LocalDocuments:
    """STORAGE_DIR on this machine or a mounted volume."""

    def __init__(self, root: Path):
        self.root = root

    def objects(self) -> Iterator[tuple[str, int, str]]:
        if not self.root.is_dir():
            raise BackupError("The documents folder (STORAGE_DIR) doesn't exist.")
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.name not in _FILES_NOT_BACKED_UP and not path.name.startswith("."):
                stat = path.stat()
                yield str(path.relative_to(self.root).as_posix()), stat.st_size, f"{stat.st_mtime_ns}-{stat.st_size}"

    def read(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class S3Documents:
    """The documents bucket, read with a read-only credential (BACKUP_SOURCE_S3_*)."""

    def __init__(self, bucket: Bucket):
        self.bucket = bucket

    def objects(self) -> Iterator[tuple[str, int, str]]:
        base = self.bucket.key("") if self.bucket.prefix.strip("/") else ""
        for item in self.bucket.keys(base):
            key = item["Key"][len(base):]
            name = key.rsplit("/", 1)[-1]
            if key and name not in _FILES_NOT_BACKED_UP and not name.startswith("."):
                yield key, int(item["Size"]), str(item.get("ETag", "")).strip('"')

    def read(self, key: str) -> bytes:
        return self.bucket.get_bytes(self.bucket.key(key) if self.bucket.prefix.strip("/") else key)


def document_source():
    if settings.backup_source_s3_bucket:
        client = s3_client(settings.backup_source_s3_endpoint_url, settings.backup_source_s3_region,
                           settings.backup_source_s3_access_key_id, settings.backup_source_s3_secret_access_key)
        return S3Documents(Bucket(client, settings.backup_source_s3_bucket, settings.backup_source_s3_prefix))
    return LocalDocuments(settings.storage_dir)


def backup_documents(source, bucket: Bucket, key: bytes, key_id: str) -> dict:
    """Copies what's new or changed, encrypted, each copy proved by reading it back. The index
    remembers what each source object was, so an unchanged one is never read again."""
    index_key = bucket.key("documents-index.json")
    try:
        index = json.loads(bucket.get_bytes(index_key))
    except Exception:  # first run, or an unreadable index: copy everything again
        index = {}
    present = {item["Key"] for item in bucket.keys(bucket.key("documents") + "/")}
    copied = unchanged = copied_bytes = 0
    seen = set()
    try:
        for name, size, etag in source.objects():
            seen.add(name)
            target = bucket.key("documents", name)
            known = index.get(name)
            if known and known.get("etag") == etag and known.get("bytes") == size and target in present:
                unchanged += 1
                continue
            data = source.read(name)
            sha = hashlib.sha256(data).hexdigest()
            bucket.put_bytes(target, encrypt_bytes(data, key, key_id), Metadata={"sha256": sha, "key-id": key_id})
            if hashlib.sha256(decrypt_bytes(bucket.get_bytes(target), key)).hexdigest() != sha:
                raise BackupError(f"The copy of a document didn't match after upload ({len(seen)} checked so far).")
            index[name] = {"etag": etag, "bytes": len(data), "sha256": sha, "key_id": key_id}
            copied += 1
            copied_bytes += len(data)
    finally:
        # Saved even after a failure, so the next run doesn't redo what was proved.
        bucket.put_bytes(index_key, json.dumps(index, sort_keys=True).encode(), ContentType="application/json")
    return {"objects": len(seen), "copied": copied, "unchanged": unchanged, "bytes_copied": copied_bytes,
            "kept_from_earlier": len(set(index) - seen), "_present": set(index)}


# ------------------------------------------------------------------ retention

def kept_stamps(stamps: list[datetime], daily: int, monthly: int) -> set[datetime]:
    """The newest backup of each of the newest ``daily`` days and ``monthly`` months. The
    newest backup of all is always kept."""
    kept, days, months = set(), set(), set()
    ordered = sorted(stamps, reverse=True)
    for stamp in ordered:
        if stamp.date() not in days and len(days) < daily:
            days.add(stamp.date())
            kept.add(stamp)
        if (stamp.year, stamp.month) not in months and len(months) < monthly:
            months.add((stamp.year, stamp.month))
            kept.add(stamp)
    if ordered:
        kept.add(ordered[0])
    return kept


def database_backups(bucket: Bucket) -> dict[str, dict]:
    """{stamp: {"complete": bool, "keys": [...]}} for everything under postgres/."""
    found: dict[str, dict] = {}
    base = bucket.key("postgres") + "/"
    for item in bucket.keys(base):
        stamp, _, name = item["Key"][len(base):].partition("/")
        entry = found.setdefault(stamp, {"complete": False, "keys": []})
        entry["keys"].append(item["Key"])
        entry["complete"] |= name == "meta.json"
    return found


def _parse_stamp(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def prune(bucket: Bucket, daily: int, monthly: int, now: datetime | None = None) -> list[str]:
    """Removes complete backups outside retention, and unfinished uploads over two days old.
    Anything not named like ours is left alone."""
    now = now or datetime.now(timezone.utc)
    backups = {stamp: entry for stamp, entry in database_backups(bucket).items() if _parse_stamp(stamp)}
    complete = {_parse_stamp(stamp): stamp for stamp, entry in backups.items() if entry["complete"]}
    keep = kept_stamps(list(complete), daily, monthly)
    removed = []
    for stamp, entry in sorted(backups.items()):
        when = _parse_stamp(stamp)
        if entry["complete"] and when in keep:
            continue
        if not entry["complete"] and now - when < INCOMPLETE_AFTER:
            continue
        # meta.json first, so a half-removed backup never looks complete.
        for key in sorted(entry["keys"], key=lambda k: not k.endswith("/meta.json")):
            bucket.delete(key)
        removed.append(stamp)
    return removed


# ------------------------------------------------------------------ recording runs

def record_run(ok: bool, *, finished: datetime, size: int | None = None, detail: str = "") -> None:
    """For System Health: the backup_runs table if it exists, else a status file beside the
    local backups. Best effort: a failure here never fails or hides the backup itself."""
    try:
        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.pool import NullPool

        from app.db import normalized_url
        engine = create_engine(normalized_url(settings.database_url), poolclass=NullPool, hide_parameters=True)
        try:
            with engine.begin() as connection:
                if inspect(connection).has_table("backup_runs"):
                    connection.execute(text("INSERT INTO backup_runs (kind, finished_at, ok, bytes, detail) "
                                            "VALUES ('postgres', :at, :ok, :size, :detail)"),
                                       {"at": finished, "ok": ok, "size": size, "detail": detail[:300]})
                    return
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001
        print(f"Couldn't record this run in the database ({type(exc).__name__}).", file=sys.stderr)
    if ok:
        try:
            settings.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            status = settings.backup_dir / "pg-backup-latest.json"
            with _private(status) as out:
                out.write(json.dumps({"finished_at": finished.isoformat(), "bytes": size}).encode())
        except OSError:
            pass


# ------------------------------------------------------------------ the backup

def make_backup(bucket: Bucket, key: bytes, key_id: str, *, documents=None, work: Path, timeout: int = 7200) -> dict:
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    from app.db import normalized_url
    target, env = libpq_target(settings.database_url)
    pg_dump = tool("pg_dump")
    taken = datetime.now(timezone.utc)
    stamp = taken.strftime(STAMP)
    dump, sealed = work / "database.dump", work / "database.dump.enc"
    engine = create_engine(normalized_url(settings.database_url), poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                snapshot = connection.execute(text("SELECT pg_export_snapshot()")).scalar_one()
                stats = snapshot_stats(connection)
                server_version = connection.execute(text("SHOW server_version")).scalar_one()
                # The transaction stays open until pg_dump has read the same snapshot.
                _run([pg_dump, "--format=custom", f"--snapshot={snapshot}", f"--file={dump}", f"--dbname={target}"], env, timeout)
    finally:
        engine.dispose()
    os.chmod(dump, 0o600)
    _run([tool("pg_restore"), "--list", str(dump)], env, 600)  # a readable archive, before it's sealed
    plain_sha, plain_bytes = digest(dump)
    encrypt_file(dump, sealed, key, key_id)
    dump.unlink()
    sealed_sha, sealed_bytes = digest(sealed)

    version = subprocess.run([pg_dump, "--version"], capture_output=True, text=True).stdout.strip()  # noqa: S603
    manifest = {"tool": TOOL, "format": 1, "taken_at": taken.isoformat(), "revision": stats["revision"], "counts": stats["counts"],
                "totals": stats["totals"], "referenced_documents": len(stats["referenced_documents"]),
                "database": {"format": "pg_dump custom", "sha256": plain_sha, "bytes": plain_bytes,
                             "server_version": server_version, "pg_dump": version},
                "encryption": {"algorithm": "AES-256-GCM", "key_id": key_id}, "documents": None}

    folder = bucket.key("postgres", stamp)
    bucket.client.upload_file(str(sealed), bucket.name, f"{folder}/dump.enc")
    check = work / "check.enc"
    bucket.client.download_file(bucket.name, f"{folder}/dump.enc", str(check))
    if digest(check) != (sealed_sha, sealed_bytes):
        raise BackupError("The uploaded dump didn't match what was sent. The backup isn't marked complete.")
    restored = work / "check.dump"
    decrypt_file(check, restored, key)
    if digest(restored) != (plain_sha, plain_bytes):
        raise BackupError("The uploaded dump decrypted to something else. The backup isn't marked complete.")
    restored.unlink()
    check.unlink()
    sealed.unlink()

    problems = []
    if documents is not None:
        copied = backup_documents(documents, bucket, key, key_id)
        present = copied.pop("_present")
        missing = [name for name in stats["referenced_documents"] if name not in present]
        copied["referenced_missing"] = len(missing)
        manifest["documents"] = copied
        if missing:
            problems.append(f"{len(missing)} document(s) the database refers to aren't in the documents store, so they aren't "
                            "in the backup either. Run python scripts/check_documents.py.")

    sealed_manifest = encrypt_bytes(json.dumps(manifest, sort_keys=True).encode(), key, key_id)
    bucket.put_bytes(f"{folder}/manifest.enc", sealed_manifest)
    if decrypt_bytes(bucket.get_bytes(f"{folder}/manifest.enc"), key) != json.dumps(manifest, sort_keys=True).encode():
        raise BackupError("The uploaded manifest didn't read back. The backup isn't marked complete.")
    # Written last: a backup with meta.json is one that was proved. It holds nothing about any company.
    meta = {"tool": TOOL, "format": 1, "taken_at": taken.isoformat(), "key_id": key_id, "revision": stats["revision"],
            "objects": {"dump.enc": {"sha256": sealed_sha, "bytes": sealed_bytes},
                        "manifest.enc": {"sha256": hashlib.sha256(sealed_manifest).hexdigest(), "bytes": len(sealed_manifest)}},
            "plaintext": {"sha256": plain_sha, "bytes": plain_bytes}, "problems": len(problems)}
    bucket.put_bytes(f"{folder}/meta.json", json.dumps(meta, indent=2).encode(), ContentType="application/json")
    return {"stamp": stamp, "manifest": manifest, "meta": meta, "problems": problems}


def list_backups(bucket: Bucket) -> list[dict]:
    found = []
    for stamp, entry in sorted(database_backups(bucket).items(), reverse=True):
        meta = json.loads(bucket.get_bytes(bucket.key("postgres", stamp, "meta.json"))) if entry["complete"] else {}
        found.append({"stamp": stamp, "complete": entry["complete"], "bytes": meta.get("objects", {}).get("dump.enc", {}).get("bytes"),
                      "key_id": meta.get("key_id"), "problems": meta.get("problems")})
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-documents", action="store_true", help="Back up the database only")
    parser.add_argument("--no-prune", action="store_true", help="Don't apply retention this time")
    parser.add_argument("--list", action="store_true", help="List the backups in the bucket, then stop")
    parser.add_argument("--timeout", type=int, default=7200, help="Seconds pg_dump may take")
    args = parser.parse_args(argv)
    try:
        bucket = backup_bucket()
        if args.list:
            for item in list_backups(bucket):
                print(json.dumps(item))
            return 0
        key = load_key(settings.backup_encryption_key)
        with tempfile.TemporaryDirectory(prefix="tf-pg-backup-") as work:
            os.chmod(work, 0o700)
            result = make_backup(bucket, key, settings.backup_encryption_key_id, work=Path(work), timeout=args.timeout,
                                 documents=None if args.no_documents else document_source())
    except Exception as exc:  # noqa: BLE001 - every failure is reported the same way
        reason = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__} (see the log above)"
        print(f"Backup FAILED: {reason}", file=sys.stderr)
        record_run(False, finished=datetime.now(timezone.utc), detail=reason)
        alerting.send(settings.alert_webhook_url, "backup_failed", "The PostgreSQL backup failed.", details={"reason": reason[:300]})
        return 1
    manifest, meta = result["manifest"], result["meta"]
    print(f"Backup {result['stamp']} written and verified: {meta['objects']['dump.enc']['bytes']} encrypted bytes, "
          f"key '{meta['key_id']}'.")
    print("Contains: " + ", ".join(f"{count} {name}" for name, count in manifest["counts"].items()))
    if manifest["documents"]:
        documents = manifest["documents"]
        print(f"Documents: {documents['copied']} copied and verified, {documents['unchanged']} unchanged, of {documents['objects']}.")
    if result["problems"]:
        for problem in result["problems"]:
            print("Problem: " + problem, file=sys.stderr)
        print("Because of that, no older backup was removed.", file=sys.stderr)
        record_run(False, finished=datetime.now(timezone.utc), size=meta["objects"]["dump.enc"]["bytes"], detail=result["problems"][0])
        alerting.send(settings.alert_webhook_url, "backup_incomplete", "The PostgreSQL backup finished with problems.",
                      details={"problems": len(result["problems"])})
        return 1
    record_run(True, finished=datetime.now(timezone.utc), size=meta["objects"]["dump.enc"]["bytes"])
    if not args.no_prune:
        try:
            for stamp in prune(bucket, settings.backup_keep_daily, settings.backup_keep_monthly):
                print(f"Removed backup {stamp} (outside retention).")
        except Exception as exc:  # noqa: BLE001
            print(f"Retention couldn't be applied ({type(exc).__name__}); every backup was kept.", file=sys.stderr)
            alerting.send(settings.alert_webhook_url, "backup_retention_failed", "Backup retention couldn't be applied.")
    print("Prove it restores: python scripts/restore_pg_drill.py --latest --target-url <an empty database>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
