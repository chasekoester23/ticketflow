"""Logs, health, backups, restore, first-run progress and rate sheets."""
import json
import logging
import os
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import pytest

from app.auth import current_user
from app.config import settings
from app.main import app
from app.models import ProcessingJob, Ticket, User
from app.services import rate_sheet, system_health
from scripts import backup as backup_script
from scripts import verify_backup as verify_script

from .test_query_budgets import _seed, _session


def _as(role):
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="t", password_hash="x", active=True, role=role, must_change_password=False)


# ------------------------------------------------------------ request logs

def test_every_response_has_a_reference_and_a_log_line_with_no_document_text(client, caplog):
    with caplog.at_level(logging.INFO, logger="ticketflow.request"):
        response = client.get("/api/records/search", params={"q": "SECRET-SUPPLIER-NAME"})
    assert response.status_code == 200
    reference = response.headers["X-Request-ID"]
    line = next(json.loads(r.getMessage()) for r in caplog.records if reference in r.getMessage())
    assert line["route"] == "/api/records/search" and line["status"] == 200 and line["method"] == "GET"
    assert "SECRET-SUPPLIER-NAME" not in caplog.text


def test_an_unexpected_error_gives_a_reference_not_a_stack_trace(client, monkeypatch, caplog):
    _seed(1)

    def boom(*_args, **_kwargs):
        raise RuntimeError("internal detail: /private/path")

    monkeypatch.setattr("app.routers.api.pricing_svc.rate_table", boom)
    with caplog.at_level(logging.ERROR, logger="ticketflow.request"):
        response = client.get("/api/batches/1/tickets")
    assert response.status_code == 500
    body = response.json()
    assert body["request_id"] in body["detail"]
    assert "/private/path" not in response.text
    assert response.headers["X-Request-ID"] == body["request_id"]
    assert "RuntimeError" in caplog.text


def test_security_headers_survive_the_log_middleware(client):
    response = client.get("/api/status")
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Frame-Options"] == "DENY"


# ------------------------------------------------------------ system status

def _check(status, name):
    return next(c for c in status["checks"] if c["name"] == name)


def test_a_stopped_worker_is_reported_with_how_to_start_it(client):
    _seed(1)
    with _session() as db:
        db.add(ProcessingJob(batch_id=1, status="pending"))
        db.commit()
    status = client.get("/api/system/status").json()
    worker = _check(status, "Processing worker")
    assert worker["state"] == "problem" and "1 upload(s) are waiting" in worker["message"]
    assert "run_worker.py" in worker["action"]
    assert status["queue"]["pending"] == 1 and status["worker"]["running"] is False
    assert status["healthy"] is False


def test_a_recent_heartbeat_means_running_and_an_old_one_doesnt(client):
    path = system_health.heartbeat_path()
    path.touch()
    assert client.get("/api/status").json()["worker_running"] is True
    assert _check(client.get("/api/system/status").json(), "Processing worker")["state"] == "ok"
    old = time.time() - system_health.WORKER_STALE_SECONDS - 5
    os.utime(path, (old, old))
    assert client.get("/api/status").json()["worker_running"] is False


def test_worker_run_beats_and_clears_its_heartbeat(client, monkeypatch):
    from app.routers import api as api_module
    from scripts import run_worker
    from .conftest import FixtureExtractor
    seen = []
    monkeypatch.setattr(run_worker, "SessionLocal", api_module.SessionLocal)
    monkeypatch.setattr(run_worker, "get_extractor", FixtureExtractor)
    real_claim = run_worker.claim_next

    def claim(db):
        time.sleep(0.05)
        seen.append(system_health.heartbeat_path().exists())
        return real_claim(db)

    monkeypatch.setattr(run_worker, "claim_next", claim)
    assert run_worker.run(once=True, poll_seconds=0.25) == 0
    assert seen == [True]
    assert not system_health.heartbeat_path().exists()


def test_missing_and_old_backups_are_called_out(client, monkeypatch, tmp_path):
    monkeypatch.setattr(system_health, "uses_pg_backups", lambda _db: False)  # backup.py's check; PostgreSQL's is in test_c_internet_facing.py
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "none")
    assert _check(client.get("/api/system/status").json(), "Backups")["state"] == "problem"
    old = tmp_path / "b" / "ticketflow-old"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text(json.dumps({"taken_at": "2020-01-01T00:00:00+00:00", "counts": {}}))
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "b")
    assert _check(client.get("/api/system/status").json(), "Backups")["state"] == "attention"


def test_status_reveals_no_keys_or_paths(client, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "fictional-key-value")
    text = client.get("/api/system/status").text
    assert "fictional-key-value" not in text and str(settings.storage_dir) not in text


@pytest.mark.parametrize("role, allowed", [("owner", 200), ("manager", 200), ("clerk", 403), ("viewer", 403)])
def test_only_managers_and_owners_see_system_status(client, role, allowed):
    _as(role)
    assert client.get("/api/system/status").status_code == allowed


def test_queue_counts_are_company_scoped(client):
    from app.models import Organization
    with _session() as db:
        db.add(Organization(id=2, name="Another Company"))
        db.commit()
    _seed(1, organization_id=2, batch_id=2)
    with _session() as db:
        db.add(ProcessingJob(batch_id=2, status="pending"))
        db.commit()
    assert client.get("/api/system/status").json()["queue"]["pending"] == 0


# ------------------------------------------------------------ backups

def _database(path: Path, tickets=3) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT)")
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    connection.execute("INSERT INTO alembic_version VALUES ('20261105_17')")
    connection.executemany("INSERT INTO tickets (status) VALUES (?)", [("verified",)] + [("needs_review",)] * (tickets - 1))
    connection.commit()
    connection.close()
    return path


@pytest.fixture()
def source(tmp_path):
    storage = tmp_path / "storage"
    (storage / "uploads" / "1").mkdir(parents=True)
    (storage / "uploads" / "1" / "fictional.pdf").write_bytes(b"%PDF fictional")
    (storage / "worker.lock").write_bytes(b"")
    return _database(tmp_path / "live.db"), storage, tmp_path / "backups"


def test_backup_is_verified_and_leaves_the_live_database_alone(source):
    database, storage, output = source
    before = backup_script.sha256(database)
    folder = backup_script.make_backup(database=database, storage=storage, output_dir=output)
    assert backup_script.sha256(database) == before
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["counts"]["tickets"] == 3 and manifest["counts"]["verified_tickets"] == 1
    assert manifest["revision"] == "20261105_17"
    assert verify_script.verify(folder) == []
    assert oct((folder / "ticketflow.db").stat().st_mode)[-3:] == "600"


def test_two_backups_never_overwrite_each_other(source):
    database, storage, output = source
    first = backup_script.make_backup(database=database, storage=None, output_dir=output)
    second = backup_script.make_backup(database=database, storage=None, output_dir=output)
    assert first != second and first.exists() and second.exists()


def test_a_backup_sees_a_consistent_copy_while_someone_is_writing(source):
    database, storage, output = source
    writer = sqlite3.connect(database)
    writer.execute("BEGIN")
    writer.execute("INSERT INTO tickets (status) VALUES ('needs_review')")
    folder = backup_script.make_backup(database=database, storage=None, output_dir=output)
    writer.rollback()
    writer.close()
    assert json.loads((folder / "manifest.json").read_text())["counts"]["tickets"] == 3


@pytest.mark.parametrize("damage, expected", [
    (lambda f: (f / "ticketflow.db").write_bytes(b"not a database"), "changed"),
    (lambda f: (f / "documents.tar.gz").write_bytes(b"junk"), "documents archive has changed"),
    (lambda f: (f / "documents.tar.gz").unlink(), "archive is missing"),
    (lambda f: (f / "ticketflow.db").unlink(), "database copy is missing"),
    (lambda f: (f / "manifest.json").unlink(), "No manifest"),
])
def test_verify_catches_damage(source, damage, expected):
    database, storage, output = source
    folder = backup_script.make_backup(database=database, storage=storage, output_dir=output)
    damage(folder)
    assert any(expected in problem for problem in verify_script.verify(folder))


def test_restore_goes_to_a_new_place_and_works(source, tmp_path):
    database, storage, output = source
    folder = backup_script.make_backup(database=database, storage=storage, output_dir=output)
    target = tmp_path / "restored"
    assert verify_script.main([str(folder), "--restore-to", str(target)]) == 0
    connection = sqlite3.connect(target / "ticketflow.db")
    assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 3
    connection.close()
    assert (target / "storage" / "uploads" / "1" / "fictional.pdf").read_bytes() == b"%PDF fictional"
    assert not (target / "storage" / "worker.lock").exists()


def test_restore_refuses_to_overwrite_anything(source, tmp_path):
    database, storage, output = source
    folder = backup_script.make_backup(database=database, storage=storage, output_dir=output)
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "ticketflow.db").write_bytes(b"precious")
    with pytest.raises(SystemExit):
        verify_script.main([str(folder), "--restore-to", str(target)])
    assert (target / "ticketflow.db").read_bytes() == b"precious"


def test_restore_refuses_a_damaged_backup(source, tmp_path):
    database, storage, output = source
    folder = backup_script.make_backup(database=database, storage=storage, output_dir=output)
    (folder / "ticketflow.db").write_bytes(b"not a database")
    assert verify_script.main([str(folder), "--restore-to", str(tmp_path / "new")]) == 1
    assert not (tmp_path / "new").exists()


# ------------------------------------------------------------ first run

def test_onboarding_follows_what_actually_exists(client):
    steps = lambda: {s["key"]: s["done"] for s in client.get("/api/onboarding").json()["steps"]}  # noqa: E731
    assert not any(steps().values())
    _seed(1)
    assert steps()["project"] and steps()["upload"] and not steps()["approve"]
    client.post("/api/tickets/1/verify")
    assert steps()["approve"]
    assert client.get("/api/onboarding").json()["complete"] is False


# ------------------------------------------------------------ rate sheets

SHEET = "Supplier\tMaterial\tPrice\nExample Aggregates\tNo. 57 Stone\t28.50\nExample Aggregates\tBase Rock\t$18.00/ton\nSample Stone Co\tNo. 57 Stone\t29\n"


def test_a_rate_sheet_previews_without_saving(client):
    plan = client.post("/api/rates/import", json={"text": SHEET}).json()
    assert plan["applied"] is False and plan["adds"] == 3
    assert plan["new_suppliers"] == ["Example Aggregates", "Sample Stone Co"]
    assert client.get("/api/suppliers").json() == [] and client.get("/api/rates").json() == []


def test_applying_a_rate_sheet_records_every_change(client):
    assert client.post("/api/rates/import", json={"text": SHEET, "apply": True}).json()["applied"] is True
    assert len(client.get("/api/rates").json()) == 3
    assert len(client.get("/api/suppliers").json()) == 2 and len(client.get("/api/materials").json()) == 2
    actions = [e["action"] for e in client.get("/api/pricing/history").json()]
    assert actions.count("rate_set") == 3 and actions.count("supplier_added") == 2 and actions.count("material_added") == 2


def test_a_second_sheet_reports_changes_and_matches_existing_spellings(client):
    client.post("/api/rates/import", json={"text": SHEET, "apply": True})
    plan = client.post("/api/rates/import", json={"text": "EXAMPLE AGGREGATES.\tno. 57 stone\t30\nSample Stone Co\tNo. 57 Stone\t29"}).json()
    assert plan["new_suppliers"] == [] and plan["new_materials"] == []
    assert [(p["action"], p["old_price"]) for p in plan["plan"]] == [("change", "28.5000"), ("unchanged", "29.0000")]
    assert plan["plan"][0]["supplier"] == "Example Aggregates"
    client.post("/api/rates/import", json={"text": "EXAMPLE AGGREGATES.\tno. 57 stone\t30", "apply": True})
    assert len(client.get("/api/suppliers").json()) == 2
    latest = client.get("/api/pricing/history").json()[0]
    assert latest["action"] == "rate_changed" and latest["old"] == "28.5000" and latest["new"] == "30.0000"


def test_a_new_rate_never_changes_a_locked_price(client):
    _seed(1)
    client.post("/api/rates/import", json={"text": "Example Aggregates\tSand\t10", "apply": True})
    locked = client.post("/api/tickets/1/verify").json()
    assert Decimal(locked["unit_price"]) == 10
    client.post("/api/rates/import", json={"text": "Example Aggregates\tSand\t99", "apply": True})
    assert Decimal(client.get("/api/batches/1/tickets").json()[0]["unit_price"]) == 10
    with _session() as db:
        assert db.get(Ticket, 1).applied_unit_price == 10


@pytest.mark.parametrize("text, expected", [
    ("Example\tSand", "three columns"),
    ("Example\tSand\tabc", "not a price"),
    ("Example\tSand\t-5", "positive"),
    ("Example\tSand\t1.23456", "more than 4 decimal"),
    ("Example\tSand\t1\nExample\tSand\t2", "repeats line 2"),
    ("\tSand\t1", "required"),
])
def test_any_bad_row_stops_the_whole_sheet(client, text, expected):
    response = client.post("/api/rates/import", json={"text": "Good Supplier\tGood Material\t5\n" + text, "apply": True})
    assert response.status_code == 422 and expected in response.json()["detail"] and "Nothing was saved" in response.json()["detail"]
    assert client.get("/api/suppliers").json() == []


def test_two_spellings_of_one_pair_are_refused(client):
    response = client.post("/api/rates/import", json={"text": "Example Aggregates\tSand\t1\nEXAMPLE AGGREGATES.\tsand\t2", "apply": True})
    assert response.status_code == 422 and "same supplier and material" in response.json()["detail"]


def test_a_saved_other_spelling_cant_become_a_new_supplier(client):
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/name-aliases", json={"kind": "supplier", "alias": "EA Inc", "target_id": client.get("/api/suppliers").json()[0]["id"]})
    response = client.post("/api/rates/import", json={"text": "EA Inc\tSand\t1", "apply": True})
    assert response.status_code == 422 and "another spelling" in response.json()["detail"]


def test_clerks_cant_import_rates(client):
    _as("clerk")
    assert client.post("/api/rates/import", json={"text": SHEET}).status_code == 403


def test_header_row_is_skipped_only_when_first():
    assert [r.line for r in rate_sheet.parse("Supplier\tMaterial\tRate\nA\tB\t1")] == [2]


def test_a_backup_only_on_this_computer_says_so_and_a_verified_copy_elsewhere_is_ok(client, monkeypatch, tmp_path):
    monkeypatch.setattr(system_health, "uses_pg_backups", lambda _db: False)  # backup.py's check; PostgreSQL's is in test_c_internet_facing.py
    from datetime import datetime, timezone
    folder = tmp_path / "b" / "ticketflow-20261110-120000"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"taken_at": datetime.now(timezone.utc).isoformat(), "counts": {}}))
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "b")
    check = _check(client.get("/api/system/status").json(), "Backups")
    assert check["state"] == "attention" and "only on this computer" in check["message"] and "--copy-to" in check["action"]
    (folder / "copied.json").write_text(json.dumps({"verified_at": "2026-11-10T12:00:00+00:00", "same_disk": True}))
    assert _check(client.get("/api/system/status").json(), "Backups")["state"] == "attention"  # same disk doesn't count
    (folder / "copied.json").write_text(json.dumps({"verified_at": "2026-11-10T12:00:00+00:00", "same_disk": False}))
    check = _check(client.get("/api/system/status").json(), "Backups")
    assert check["state"] == "ok" and "verified copy off this computer" in check["message"]
