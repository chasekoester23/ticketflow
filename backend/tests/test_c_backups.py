"""Encrypted off-service backups, retention, the restore drill's refusals, health checks and
alerts, without PostgreSQL or a real bucket. A small in-memory stand-in for S3 plays the
bucket. The real thing, pg_dump to MinIO and back into a second PostgreSQL database, runs in
CI: tests/test_c_backup_pg_live.py and .github/workflows/backup-restore.yml.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import alerting  # noqa: E402  (the same module objects the scripts themselves import)
import backup_pg  # noqa: E402
import check_health  # noqa: E402
import restore_pg_drill  # noqa: E402

from app.config import settings  # noqa: E402
from scripts.backup import company_totals, counts  # noqa: E402

from .test_b_backup_recovery import business  # noqa: F401  (a fixture: a real fictional business on SQLite)

KEY = os.urandom(32)
KEY_TEXT = base64.b64encode(KEY).decode()


class FakeS3:
    """The calls backup_pg makes, kept in memory. Lists two keys a page, so paging is used."""

    def __init__(self, corrupt_puts: bool = False):
        self.objects: dict[tuple[str, str], tuple[bytes, dict]] = {}
        self.corrupt_puts = corrupt_puts

    def put_object(self, Bucket, Key, Body, Metadata=None, **_extra):
        data = Body if isinstance(Body, bytes) else Body.read()
        if self.corrupt_puts:
            data = data[:-1] + bytes([data[-1] ^ 1])
        self.objects[(Bucket, Key)] = (data, Metadata or {})

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0]), "Metadata": self.objects[(Bucket, Key)][1]}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def upload_file(self, Filename, Bucket, Key):
        self.objects[(Bucket, Key)] = (Path(Filename).read_bytes(), {})

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[(Bucket, Key)][0])

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 2]
        more = start + 2 < len(keys)
        return {"Contents": [{"Key": key, "Size": len(self.objects[(Bucket, key)][0]), "ETag": '"x"'} for key in page],
                "IsTruncated": more, **({"NextContinuationToken": str(start + 2)} if more else {})}

    def keys(self, bucket="backups"):
        return sorted(key for b, key in self.objects if b == bucket)


@pytest.fixture()
def bucket():
    return backup_pg.Bucket(FakeS3(), "backups", "tf")


# ------------------------------------------------------------------ encryption

def test_the_key_is_32_bytes_as_base64_or_hex():
    assert backup_pg.load_key(KEY_TEXT) == KEY
    assert backup_pg.load_key(KEY.hex()) == KEY
    for bad in ("", None, base64.b64encode(os.urandom(16)).decode(), "not a key at all"):
        with pytest.raises(backup_pg.BackupError, match="BACKUP_ENCRYPTION_KEY"):
            backup_pg.load_key(bad)


def test_a_file_round_trips_and_any_change_is_refused(tmp_path):
    plain = tmp_path / "dump"
    plain.write_bytes(os.urandom(3 * backup_pg.CHUNK + 17))
    sealed, back = tmp_path / "dump.enc", tmp_path / "back"
    backup_pg.encrypt_file(plain, sealed, KEY, "primary")
    assert oct(sealed.stat().st_mode & 0o777) == "0o600"
    assert plain.read_bytes()[:4096] not in sealed.read_bytes()
    assert backup_pg.decrypt_file(sealed, back, KEY) == "primary"
    assert back.read_bytes() == plain.read_bytes()

    for position in (len(backup_pg.MAGIC) + 1, sealed.stat().st_size // 2, sealed.stat().st_size - 1):
        damaged = bytearray(sealed.read_bytes())
        damaged[position] ^= 1
        (tmp_path / "damaged").write_bytes(bytes(damaged))
        with pytest.raises(backup_pg.BackupError):
            backup_pg.decrypt_file(tmp_path / "damaged", tmp_path / "out", KEY)
        assert not (tmp_path / "out").exists(), "unauthenticated output must not be left behind"
    with pytest.raises(backup_pg.BackupError, match="integrity"):
        backup_pg.decrypt_file(sealed, tmp_path / "out", os.urandom(32))
    (tmp_path / "short").write_bytes(sealed.read_bytes()[:-30])
    with pytest.raises(backup_pg.BackupError):
        backup_pg.decrypt_file(tmp_path / "short", tmp_path / "out", KEY)


def test_the_key_name_is_authenticated_too():
    blob = backup_pg.encrypt_bytes(b"manifest", KEY, "primary")
    renamed = blob.replace(b"primary", b"primarz", 1)
    assert backup_pg.decrypt_bytes(blob, KEY) == b"manifest"
    with pytest.raises(backup_pg.BackupError):
        backup_pg.decrypt_bytes(renamed, KEY)


# ------------------------------------------------------------------ the manifest's numbers

def test_counts_and_exact_totals_match_the_sqlite_backup_script(business):
    """The same rules as scripts/backup.py, through SQLAlchemy, so they run on PostgreSQL too."""
    database, _documents = business
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        stats = backup_pg.snapshot_stats(connection)
    engine.dispose()
    raw = sqlite3.connect(database)
    try:
        assert stats["counts"] == counts(raw)
        assert stats["totals"] == company_totals(raw) and len(stats["totals"]) == 2
    finally:
        raw.close()
    assert stats["revision"] and len(stats["referenced_documents"]) > 5


# ------------------------------------------------------------------ documents

def test_documents_are_copied_encrypted_incrementally_and_proved(business, bucket, tmp_path):
    import shutil
    _database, documents = business
    store = tmp_path / "storage"
    shutil.copytree(documents, store)
    (store / "worker.heartbeat").write_text("not a document")
    source = backup_pg.LocalDocuments(store)
    first = backup_pg.backup_documents(source, bucket, KEY, "primary")
    files = [path for path in store.rglob("*") if path.is_file() and path.name != "worker.heartbeat"]
    assert first["copied"] == len(files) > 5 and first["unchanged"] == 0
    copy = bucket.client.objects[("backups", "tf/documents/" + str(files[0].relative_to(store)))]
    assert files[0].read_bytes()[:64] not in copy[0], "documents must be encrypted in the backup bucket"
    assert copy[1]["sha256"] and not any("heartbeat" in key for key in bucket.client.keys())

    second = backup_pg.backup_documents(source, bucket, KEY, "primary")
    assert second["copied"] == 0 and second["unchanged"] == len(files)

    files[0].write_bytes(b"replaced" + files[0].read_bytes())
    os.utime(files[0], ns=(1, 1))
    third = backup_pg.backup_documents(source, bucket, KEY, "primary")
    assert third["copied"] == 1
    # A copy deleted from the bucket is noticed and made again.
    bucket.client.delete_object("backups", "tf/documents/" + str(files[1].relative_to(store)))
    assert backup_pg.backup_documents(source, bucket, KEY, "primary")["copied"] == 1


def test_a_copy_that_doesnt_read_back_fails_the_backup(tmp_path):
    (tmp_path / "a.png").write_bytes(b"fictional image")
    damaged = backup_pg.Bucket(FakeS3(corrupt_puts=True), "backups", "tf")
    with pytest.raises(backup_pg.BackupError):
        backup_pg.backup_documents(backup_pg.LocalDocuments(tmp_path), damaged, KEY, "primary")


def test_documents_can_come_from_a_bucket_with_a_prefix(tmp_path):
    client = FakeS3()
    client.put_object(Bucket="docs", Key="live/batches/1/a.png", Body=b"one")
    client.put_object(Bucket="docs", Key="live/.partial", Body=b"skip")
    source = backup_pg.S3Documents(backup_pg.Bucket(client, "docs", "live"))
    assert [name for name, _size, _etag in source.objects()] == ["batches/1/a.png"]
    assert source.read("batches/1/a.png") == b"one"


def test_backups_never_go_to_the_documents_bucket(monkeypatch):
    monkeypatch.setattr(settings, "backup_s3_bucket", "shared")
    monkeypatch.setattr(settings, "backup_source_s3_bucket", "shared")
    with pytest.raises(backup_pg.BackupError, match="documents bucket"):
        backup_pg.backup_bucket()
    monkeypatch.setattr(settings, "backup_s3_bucket", None)
    with pytest.raises(backup_pg.BackupError, match="BACKUP_S3_BUCKET"):
        backup_pg.backup_bucket()


# ------------------------------------------------------------------ retention

def _stamp(when: datetime) -> str:
    return when.strftime(backup_pg.STAMP)


def test_retention_keeps_newest_days_and_months():
    now = datetime(2026, 11, 10, 2, 30, tzinfo=timezone.utc)
    stamps = [now - timedelta(days=day, hours=hours) for day in range(120) for hours in (0, 12)]
    kept = backup_pg.kept_stamps(stamps, daily=7, monthly=4)
    days = sorted({stamp.date() for stamp in kept})
    assert max(kept) == now
    assert len([d for d in days if d > (now - timedelta(days=7)).date()]) == 7
    assert {(s.year, s.month) for s in kept} == {(2026, 11), (2026, 10), (2026, 9), (2026, 8)}
    assert len(kept) == 7 + 3  # each kept month's newest is in November's days, or is one more
    assert backup_pg.kept_stamps([now], daily=1, monthly=0) == {now}


def test_prune_removes_only_our_old_complete_and_stale_unfinished_backups(bucket):
    now = datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
    for day in range(10):
        folder = bucket.key("postgres", _stamp(now - timedelta(days=day)))
        bucket.put_bytes(f"{folder}/dump.enc", b"x")
        bucket.put_bytes(f"{folder}/meta.json", b"{}")
    stale, fresh = _stamp(now - timedelta(days=5, hours=1)), _stamp(now - timedelta(hours=1))
    bucket.put_bytes(bucket.key("postgres", stale, "dump.enc"), b"partial")
    bucket.put_bytes(bucket.key("postgres", fresh, "dump.enc"), b"uploading now")
    bucket.put_bytes(bucket.key("postgres", "not-ours", "file"), b"leave me")
    removed = backup_pg.prune(bucket, daily=3, monthly=1, now=now)
    remaining = backup_pg.database_backups(bucket)
    complete = sorted(stamp for stamp, entry in remaining.items() if entry["complete"])
    assert complete == sorted(_stamp(now - timedelta(days=day)) for day in range(3)) or len(complete) == 3
    assert stale in removed and fresh in remaining and "not-ours" in remaining
    assert all(not bucket.client.keys() or not key.startswith(bucket.key("postgres", stamp)) for stamp in removed
               for key in bucket.client.keys())


# ------------------------------------------------------------------ the drill's refusals

def test_the_drill_never_restores_into_the_live_database(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_url", "postgresql://tf:secret@db.internal:5432/ticketflow")
    with pytest.raises(backup_pg.BackupError, match="live database"):
        restore_pg_drill.run("postgres://other:pw@DB.internal/ticketflow", stamp=None, cleanup=True, sample=1, work=tmp_path)
    assert backup_pg.same_database("postgresql://a@h:5432/x", "postgresql+psycopg://b:c@H/x")
    assert not backup_pg.same_database("postgresql://a@h/x", "postgresql://a@h/x_drill")


def test_passwords_never_go_on_a_command_line():
    target, env = backup_pg.libpq_target("postgresql+psycopg://tf:S3cret-Pass@db.internal:5432/ticketflow?sslmode=require")
    assert "S3cret-Pass" not in target and target.startswith("postgresql://tf@db.internal:5432/ticketflow")
    assert "sslmode=require" in target and env["PGPASSWORD"] == "S3cret-Pass"
    with pytest.raises(backup_pg.BackupError, match="PostgreSQL"):
        backup_pg.libpq_target("sqlite:///ticketflow.db")


# ------------------------------------------------------------------ failures are loud, secrets stay quiet

def test_a_failed_backup_alerts_and_never_prints_the_key(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(alerting, "send", lambda url, event, summary, details=None: sent.append((url, event, summary, details)))
    monkeypatch.setattr(settings, "backup_encryption_key", KEY_TEXT)
    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.example/fictional")
    monkeypatch.setattr(settings, "backup_s3_bucket", None)
    monkeypatch.setattr(settings, "database_url", "sqlite://")
    monkeypatch.setattr(backup_pg, "record_run", lambda *a, **k: None)
    assert backup_pg.main([]) == 1
    output = capsys.readouterr()
    assert "FAILED" in output.err and KEY_TEXT not in output.out + output.err
    assert sent and sent[0][1] == "backup_failed" and KEY_TEXT not in json.dumps(sent)


def test_the_webhook_gets_only_a_small_json_message():
    posted = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        posted.append((request.full_url, json.loads(request.data), request.get_header("Content-type"), timeout))
        return Response()

    assert alerting.send(None, "x", "nothing configured", opener=opener) is False and posted == []
    assert alerting.send("file:///etc/passwd", "x", "bad scheme", opener=opener) is False and posted == []
    assert alerting.send("https://hooks.example/abc", "backup_failed", "The backup failed.", details={"reason": "r"}, opener=opener)
    url, body, kind, timeout = posted[0]
    assert kind == "application/json" and timeout == 10
    assert set(body) == {"text", "event", "summary", "source", "at", "details"} and body["event"] == "backup_failed"

    def broken(request, timeout):
        raise OSError("connection refused to https://hooks.example/abc")
    assert alerting.send("https://hooks.example/abc", "x", "y", opener=broken) is False


# ------------------------------------------------------------------ the scheduled health check

def _fresh_database(tmp_path) -> str:
    from app.db import Base
    url = f"sqlite:///{tmp_path / 'health.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO organizations (id, name) VALUES (1, 'Fictional Co')"))
        connection.execute(text("INSERT INTO batches (id, organization_id, status, layout, intake_mode, created_at) "
                                "VALUES (1, 1, 'processing', 'single', 'ai', CURRENT_TIMESTAMP)"))
    engine.dispose()
    return url


def test_the_health_check_passes_quietly_and_alerts_on_a_stuck_queue(tmp_path, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(check_health.alerting, "send", lambda url, event, summary, details=None: sent.append((event, details)))
    url = _fresh_database(tmp_path)
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "backup_s3_bucket", None)
    monkeypatch.setattr(settings, "backup_dir", tmp_path)
    (tmp_path / "pg-backup-latest.json").write_text(json.dumps({"finished_at": datetime.now(timezone.utc).isoformat()}))
    assert check_health.main([]) == 0 and sent == []
    assert json.loads(capsys.readouterr().out)["ok"] is True

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO processing_jobs (batch_id, status, only_failed, attempts, created_at) "
                                "VALUES (1, 'pending', 0, 0, :at)"), {"at": datetime.now(timezone.utc) - timedelta(hours=2)})
    engine.dispose()
    assert check_health.main([]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["pending"] == 1 and summary["oldest_pending_seconds"] >= 7000
    assert sent and sent[0][0] == "health_check_failed" and "waiting" in sent[0][1]["problems"][0]


def test_the_health_check_alerts_on_an_old_or_missing_backup_and_an_unready_api():
    now = datetime.now(timezone.utc)
    quiet = {"pending": 0, "oldest_pending_seconds": None, "failed_24h": 0, "worker_seen_seconds": None}
    kwargs = dict(max_queue_minutes=30, max_backup_hours=26, max_failed=0, worker_stale_seconds=120)
    assert check_health.evaluate(ready=(True, "HTTP 200"), numbers=quiet, backup_at=now, **kwargs) == []
    assert "backup" in check_health.evaluate(ready=(True, ""), numbers=quiet, backup_at=None, **kwargs)[0]
    assert "30 hours" in check_health.evaluate(ready=(True, ""), numbers=quiet, backup_at=now - timedelta(hours=30), **kwargs)[0]
    assert "isn't ready" in check_health.evaluate(ready=(False, "URLError"), numbers=quiet, backup_at=now, **kwargs)[0]
    failing = {**quiet, "failed_24h": 3}
    assert "failed" in check_health.evaluate(ready=(None, ""), numbers=failing, backup_at=now, **kwargs)[0]
