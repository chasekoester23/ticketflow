"""The real thing, in CI only: pg_dump of a real PostgreSQL business, encrypted, to MinIO,
then restored with pg_restore into a SECOND database and checked, while the source is
proved untouched. Damage, a wrong key, a non-empty target and the live database as a
target must all be refused. See .github/workflows/backup-restore.yml.

Needs BACKUP_TEST_SOURCE_URL and BACKUP_TEST_TARGET_URL (two disposable databases with
"test" in their names), and BACKUP_TEST_S3_ENDPOINT, _ACCESS_KEY and _SECRET (MinIO).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

SOURCE = os.environ.get("BACKUP_TEST_SOURCE_URL")
TARGET = os.environ.get("BACKUP_TEST_TARGET_URL")
ENDPOINT = os.environ.get("BACKUP_TEST_S3_ENDPOINT")
pytestmark = pytest.mark.skipif(not (SOURCE and TARGET and ENDPOINT), reason="needs PostgreSQL and MinIO: runs in backup-restore.yml")

BACKEND = Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"ci-only-fictional-backup-key-32b").decode()
BACKUPS, DOCUMENTS = "tf-backups-ci", "tf-documents-ci"


def _engine(url: str):
    from app.db import normalized_url
    return create_engine(normalized_url(url), poolclass=NullPool)


def _wipe(url: str) -> None:
    assert "test" in url.rsplit("/", 1)[-1], "only disposable databases"
    engine = _engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _tables(url: str) -> int:
    engine = _engine(url)
    with engine.connect() as connection:
        found = connection.execute(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")).scalar_one()
    engine.dispose()
    return found


def _fingerprint(url: str) -> dict:
    """Every row of every table, hashed: proves the source wasn't changed at all."""
    engine = _engine(url)
    with engine.connect() as connection:
        tables = [row[0] for row in connection.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"))
                  if row[0] != "backup_runs"]  # the backup records its own run, when that table exists
        found = {table: connection.execute(text(f"SELECT md5(COALESCE(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {table} t")).scalar_one()  # noqa: S608
                 for table in tables}
    engine.dispose()
    return found


def _s3():
    sys.path.insert(0, str(BACKEND / "scripts"))
    import backup_pg
    return backup_pg.s3_client(ENDPOINT, "us-east-1", os.environ["BACKUP_TEST_S3_ACCESS_KEY"], os.environ["BACKUP_TEST_S3_SECRET"])


def _env(storage: Path, backups: Path, **extra) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("BACKUP_", "TEST_DATABASE_URL", "GEMINI", "ANTHROPIC"))}
    env.update(DATABASE_URL=SOURCE, STORAGE_DIR=str(storage), BACKUP_DIR=str(backups), BACKUP_ENCRYPTION_KEY=KEY,
               BACKUP_ENCRYPTION_KEY_ID="ci-2026-11", BACKUP_S3_ENDPOINT_URL=ENDPOINT, BACKUP_S3_REGION="us-east-1",
               BACKUP_S3_BUCKET=BACKUPS, BACKUP_S3_ACCESS_KEY_ID=os.environ["BACKUP_TEST_S3_ACCESS_KEY"],
               BACKUP_S3_SECRET_ACCESS_KEY=os.environ["BACKUP_TEST_S3_SECRET"], BACKUP_S3_PREFIX="drill",
               BACKUP_SOURCE_S3_ENDPOINT_URL=ENDPOINT, BACKUP_SOURCE_S3_REGION="us-east-1", BACKUP_SOURCE_S3_BUCKET=DOCUMENTS,
               BACKUP_SOURCE_S3_ACCESS_KEY_ID=os.environ["BACKUP_TEST_S3_ACCESS_KEY"],
               BACKUP_SOURCE_S3_SECRET_ACCESS_KEY=os.environ["BACKUP_TEST_S3_SECRET"], BACKUP_SOURCE_S3_PREFIX="live",
               ALERT_WEBHOOK_URL="", PYTHONPATH=str(BACKEND), **extra)
    return env


def _script(name: str, *arguments: str, env: dict) -> subprocess.CompletedProcess:
    result = subprocess.run([sys.executable, str(BACKEND / "scripts" / name), *arguments], cwd=BACKEND, env=env,
                            capture_output=True, text=True, timeout=600)
    print(f"--- {name} {' '.join(arguments)} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


@pytest.fixture(scope="module")
def business(tmp_path_factory):
    from alembic import command

    from .migration_helpers import alembic_config
    from .test_b_migrate_sqlite_to_postgres import build_fictional_business
    root = tmp_path_factory.mktemp("pg-business")
    storage = root / "storage"
    _wipe(SOURCE)
    _wipe(TARGET)
    command.upgrade(alembic_config(SOURCE), "head")
    patch = pytest.MonkeyPatch()
    try:
        build_fictional_business(SOURCE, storage, patch)
    finally:
        patch.undo()
    s3 = _s3()
    for bucket in (BACKUPS, DOCUMENTS):
        try:
            s3.create_bucket(Bucket=bucket)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass
    documents = [path for path in storage.rglob("*") if path.is_file()]
    assert len(documents) > 5
    for path in documents:
        s3.put_object(Bucket=DOCUMENTS, Key="live/" + path.relative_to(storage).as_posix(), Body=path.read_bytes())
    return root, storage


def test_backup_restore_into_a_second_database_and_the_source_is_untouched(business):
    root, storage = business
    env = _env(storage, root / "local-backups")
    before = _fingerprint(SOURCE)

    first = _script("backup_pg.py", env=env)
    assert first.returncode == 0, first.stderr
    assert "written and verified" in first.stdout and KEY not in first.stdout + first.stderr
    again = _script("backup_pg.py", env=env)
    assert again.returncode == 0 and " 0 copied and verified" in again.stdout, "documents are copied incrementally"
    listed = [json.loads(line) for line in _script("backup_pg.py", "--list", env=env).stdout.splitlines()]
    assert len(listed) == 2 and all(item["complete"] and item["key_id"] == "ci-2026-11" for item in listed)
    assert _fingerprint(SOURCE) == before, "a backup must change nothing in the source"

    # Nothing readable in the bucket: the dump, manifest and documents are all ciphertext.
    s3 = _s3()
    names = [item["Key"] for item in s3.list_objects_v2(Bucket=BACKUPS, Prefix="drill/")["Contents"]]
    dump_key = next(name for name in names if name.endswith("/dump.enc"))
    assert s3.get_object(Bucket=BACKUPS, Key=dump_key)["Body"].read(8) == b"TFENC1\n\x00"
    assert not any(b"Fictional" in s3.get_object(Bucket=BACKUPS, Key=name)["Body"].read() for name in names if not name.endswith(".json"))

    report = root / "drill.json"
    drill = _script("restore_pg_drill.py", "--latest", "--target-url", TARGET, "--report", str(report), env=env)
    assert drill.returncode == 0, drill.stdout + drill.stderr
    checks = {check["name"]: check["passed"] for check in json.loads(report.read_text())["checks"]}
    assert all(checks.values()) and "Each company's approved totals match exactly" in checks
    assert "Every referenced document is in the backup bucket" in checks
    assert _fingerprint(TARGET) == before, "the restored copy is the source, row for row"
    assert _fingerprint(SOURCE) == before

    # A second drill into the now non-empty target is refused before anything happens.
    refused = _script("restore_pg_drill.py", "--latest", "--target-url", TARGET, env=env)
    assert refused.returncode == 1 and "already holds tables" in refused.stderr
    assert _fingerprint(TARGET) == before
    # The live database is never a target.
    _wipe(TARGET)
    live = _script("restore_pg_drill.py", "--latest", "--target-url", SOURCE, env=env)
    assert live.returncode == 1 and "live database" in live.stderr
    assert _fingerprint(SOURCE) == before

    # One flipped byte in the stored dump: refused, and nothing restored.
    newest = sorted(name for name in names if name.endswith("/dump.enc"))[-1]
    original = s3.get_object(Bucket=BACKUPS, Key=newest)["Body"].read()
    damaged = bytearray(original)
    damaged[len(damaged) // 2] ^= 1
    s3.put_object(Bucket=BACKUPS, Key=newest, Body=bytes(damaged))
    tampered = _script("restore_pg_drill.py", "--latest", "--target-url", TARGET, env=env)
    assert tampered.returncode == 1 and "FAIL" in tampered.stdout and _tables(TARGET) == 0
    s3.put_object(Bucket=BACKUPS, Key=newest, Body=original)

    # The wrong key can't read it.
    wrong = _script("restore_pg_drill.py", "--latest", "--target-url", TARGET,
                    env={**env, "BACKUP_ENCRYPTION_KEY": base64.b64encode(b"another-fictional-key-of-32-byte").decode()})
    assert wrong.returncode == 1 and "integrity" in wrong.stdout and _tables(TARGET) == 0

    # --cleanup proves it, then leaves the target empty.
    cleaned = _script("restore_pg_drill.py", "--latest", "--target-url", TARGET, "--cleanup", env=env)
    assert cleaned.returncode == 0 and _tables(TARGET) == 0
    assert _fingerprint(SOURCE) == before


def test_retention_applies_to_the_real_bucket(business):
    root, storage = business
    s3 = _s3()
    env = _env(storage, root / "local-backups", BACKUP_KEEP_DAILY="1", BACKUP_KEEP_MONTHLY="0", BACKUP_S3_PREFIX="retention")
    for _ in range(2):
        assert _script("backup_pg.py", "--no-documents", env=env).returncode == 0
    # Two backups within the same second can share a name: count distinct complete ones.
    listed = [json.loads(line) for line in _script("backup_pg.py", "--list", env=env).stdout.splitlines()]
    assert len(listed) == 1 and listed[0]["complete"]
    assert s3.list_objects_v2(Bucket=BACKUPS, Prefix="retention/postgres/")["KeyCount"] == 3
