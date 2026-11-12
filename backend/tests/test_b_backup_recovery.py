"""Backups a person can rely on: retention, off-device copies, completeness, and a real
restore rehearsal. All fictional. Nothing here touches a real database or real documents.
"""
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app.config import configuration_problems, settings
from app.services import document_audit, storage
from scripts import backup as backup_script
from scripts import check_documents, doctor, restore_drill
from scripts import verify_backup as verify_script

from .migration_helpers import alembic_config, head_revision
from .test_b_migrate_sqlite_to_postgres import build_fictional_business

OPERATIONS = Path(__file__).resolve().parents[2] / "docs" / "engineering" / "operations.md"


def _manifest(folder: Path) -> dict:
    return json.loads((folder / "manifest.json").read_text())


@pytest.fixture(scope="session")
def business(tmp_path_factory) -> tuple[Path, Path]:
    """A real database built by the migrations and the API, with real stored documents."""
    pytest_monkeypatch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("business")
    database, documents = root / "ticketflow.db", root / "storage"
    url = f"sqlite:///{database}"
    command.upgrade(alembic_config(url), "head")
    try:
        build_fictional_business(url, documents, pytest_monkeypatch)
    finally:
        pytest_monkeypatch.undo()
    return database, documents


@pytest.fixture()
def live(business, tmp_path) -> tuple[Path, Path, Path]:
    """This test's own copy of the business, and an empty place for backups."""
    database, documents = business
    shutil.copyfile(database, tmp_path / "ticketflow.db")
    shutil.copytree(documents, tmp_path / "storage")
    return tmp_path / "ticketflow.db", tmp_path / "storage", tmp_path / "backups"


def _backup(live) -> Path:
    database, documents, output = live
    return backup_script.make_backup(database=database, storage=documents, output_dir=output)


def _rewrite_archive(folder: Path, *, drop: str | None = None, replace: tuple[str, bytes] | None = None) -> None:
    """The archive as if it had been made incomplete or damaged, with its checksum updated
    so that only the document-level check can catch it."""
    archive = folder / "documents.tar.gz"
    temporary = folder / "new.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(temporary, "w:gz") as target:
        for member in source:
            if member.name == drop:
                continue
            data = source.extractfile(member) if member.isfile() else None
            if replace and member.name == replace[0]:
                import io
                member.size = len(replace[1])
                data = io.BytesIO(replace[1])
            target.addfile(member, data)
    temporary.replace(archive)
    manifest = _manifest(folder)
    manifest["documents"].update(sha256=backup_script.sha256(archive), bytes=archive.stat().st_size)
    (folder / "manifest.json").write_text(json.dumps(manifest))


def _first_crop(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        return connection.execute("SELECT crop_path FROM tickets ORDER BY id LIMIT 1").fetchone()[0]
    finally:
        connection.close()


# ------------------------------------------------------------------ completeness

def test_a_complete_backup_of_a_real_business_is_sound(live):
    folder = _backup(live)
    assert verify_script.verify(folder) == []
    manifest = _manifest(folder)
    assert manifest["tool"] == "ticketflow-backup"
    assert manifest["documents"]["files"] >= manifest["documents"]["referenced"] > 10
    assert manifest["totals"]  # Each company's exact approved figures.


def test_a_document_missing_from_the_archive_is_caught(live):
    folder = _backup(live)
    crop = _first_crop(live[0])
    _rewrite_archive(folder, drop=crop)
    problems = verify_script.verify(folder)
    # The slot and its ticket share one image: one document, missing.
    assert any(problem.startswith("Incomplete: 1 document") and "scan_slots" in problem for problem in problems)
    assert crop not in " ".join(problems)  # Row ids, never paths.


def test_a_document_changed_inside_the_archive_is_caught(live):
    folder = _backup(live)
    connection = sqlite3.connect(live[0])
    upload = connection.execute("SELECT storage_path FROM uploaded_files LIMIT 1").fetchone()[0]
    connection.close()
    _rewrite_archive(folder, replace=(upload, b"%PDF not the same bytes"))
    assert any("Damaged: 1 document" in problem for problem in verify_script.verify(folder))


def test_a_backup_with_no_documents_is_called_incomplete(live):
    database, _documents, output = live
    folder = backup_script.make_backup(database=database, storage=None, output_dir=output)
    assert any("no documents" in problem for problem in verify_script.verify(folder))


def test_a_legacy_absolute_path_counts_as_being_in_the_archive(live):
    """Backups of older databases: their paths name a location that's long gone."""
    database, _documents, _output = live
    connection = sqlite3.connect(database)
    for table, column in (("tickets", "crop_path"), ("scan_slots", "crop_path"), ("uploaded_files", "storage_path"), ("invoice_documents", "storage_path")):
        connection.execute(f"UPDATE {table} SET {column} = '/old/machine/storage/' || {column}")
    connection.commit()
    connection.close()
    assert verify_script.verify(_backup(live)) == []


def test_a_rotated_image_must_be_in_the_archive_too(live):
    database, _documents, _output = live
    connection = sqlite3.connect(database)
    crop = connection.execute("SELECT crop_path FROM scan_slots WHERE rotation <> 0 LIMIT 1").fetchone()
    connection.close()
    assert crop is not None
    folder = _backup(live)
    assert verify_script.verify(folder) == []
    _rewrite_archive(folder, drop=storage.rotated_key(crop[0], 90))
    assert any(problem.startswith("Incomplete") for problem in verify_script.verify(folder))


# ------------------------------------------------------------------ privacy

def test_backups_are_private_whatever_the_umask(live):
    previous = os.umask(0)
    try:
        folder = _backup(live)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for name in ("ticketflow.db", "documents.tar.gz", "manifest.json"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600, name


def test_a_restored_copy_is_private_even_if_the_originals_werent(live, tmp_path):
    _database, documents, _output = live
    for path in documents.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    folder = _backup(live)
    target = tmp_path / "restored"
    verify_script.restore(folder, target)
    for path in target.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path


# ------------------------------------------------------------------ retention

def _fake_backup(directory: Path, name: str, taken_at: str, **manifest) -> Path:
    folder = directory / name
    folder.mkdir(parents=True)
    (folder / "ticketflow.db").write_bytes(b"x")
    (folder / "manifest.json").write_text(json.dumps({
        "taken_at": taken_at, "database_sha256": "0" * 64, "counts": {}, "documents": None, **manifest}))
    return folder


def _dated(directory: Path, count: int) -> list[Path]:
    return [_fake_backup(directory, f"ticketflow-202601{day:02d}-120000", f"2026-01-{day:02d}T12:00:00+00:00") for day in range(1, count + 1)]


def test_keep_removes_only_the_oldest(tmp_path):
    backups = _dated(tmp_path, 5)
    removed = backup_script.prune(tmp_path, 2)
    assert sorted(path.name for path in removed) == [backups[0].name, backups[1].name, backups[2].name]
    assert backups[3].exists() and backups[4].exists()
    assert not backups[0].exists()


def test_keep_goes_by_the_manifest_date_not_the_folder_name(tmp_path):
    older_by_date = _fake_backup(tmp_path, "ticketflow-20991231-120000", "2020-01-01T00:00:00+00:00")
    newer_by_date = _fake_backup(tmp_path, "ticketflow-20200101-120000", "2026-01-01T00:00:00+00:00")
    assert backup_script.prune(tmp_path, 1) == [older_by_date]
    assert newer_by_date.exists()


def test_keep_leaves_everything_it_doesnt_recognise(tmp_path):
    _dated(tmp_path, 2)
    notes = tmp_path / "ticketflow-notes"
    notes.mkdir()
    (notes / "manifest.json").write_text(json.dumps({"taken_at": "2000-01-01T00:00:00+00:00", "database_sha256": "x"}))
    no_manifest = tmp_path / "ticketflow-20000101-000000"
    no_manifest.mkdir()
    not_ours = _fake_backup(tmp_path, "my-photos", "2000-01-01T00:00:00+00:00")
    bad_manifest = tmp_path / "ticketflow-20000102-000000"
    bad_manifest.mkdir()
    (bad_manifest / "manifest.json").write_text("{not json")
    no_digest = tmp_path / "ticketflow-20000103-000000"
    no_digest.mkdir()
    (no_digest / "manifest.json").write_text(json.dumps({"taken_at": "2000-01-01T00:00:00+00:00"}))
    a_file = tmp_path / "ticketflow-20000104-000000"
    a_file.write_text("a file")
    assert len(backup_script.prune(tmp_path, 1)) == 1
    for survivor in (notes, no_manifest, not_ours, bad_manifest, no_digest, a_file):
        assert survivor.exists(), survivor


def test_keep_doesnt_follow_a_symlink(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    target = _fake_backup(elsewhere, "ticketflow-20000101-000000", "2000-01-01T00:00:00+00:00")
    backups = tmp_path / "backups"
    _dated(backups, 2)
    (backups / "ticketflow-20000101-000000").symlink_to(target)
    backup_script.prune(backups, 1)
    assert target.exists()


def test_keep_never_removes_the_newest_even_if_its_damaged(tmp_path):
    backups = _dated(tmp_path, 3)
    (backups[2] / "ticketflow.db").unlink()  # The newest fails its check.
    removed = backup_script.prune(tmp_path, 1)
    assert backups[2].exists()
    assert backups[1].exists(), "A damaged backup mustn't push out the last good one."
    assert removed == [backups[0]]


def test_keep_leaves_damaged_backups_for_a_person(tmp_path):
    backups = _dated(tmp_path, 3)
    (backups[0] / "ticketflow.db").unlink()
    assert backup_script.prune(tmp_path, 1) == [backups[1]]
    assert backups[0].exists()


@pytest.mark.parametrize("keep", [0, -1])
def test_keep_refuses_a_number_that_could_remove_the_newest(tmp_path, keep):
    with pytest.raises(ValueError):
        backup_script.prune(tmp_path, keep)
    with pytest.raises(SystemExit):
        backup_script.main(["--keep", str(keep)])


def _as_configured(monkeypatch, database: Path, documents: Path, output: Path) -> None:
    monkeypatch.setattr(backup_script.settings, "database_url", f"sqlite:///{database}")
    monkeypatch.setattr(backup_script.settings, "storage_dir", documents)
    monkeypatch.setattr(backup_script.settings, "backup_dir", output)


def test_the_command_verifies_before_it_removes_anything(live, monkeypatch, capsys):
    database, documents, output = live
    _as_configured(monkeypatch, database, documents, output)
    old = _dated(output, 3)
    assert backup_script.main(["--output-dir", str(output), "--keep", "2"]) == 0
    assert not old[0].exists() and not old[1].exists() and old[2].exists()
    assert "Removed older backup" in capsys.readouterr().out


def test_a_backup_that_fails_verification_removes_nothing(live, monkeypatch):
    database, documents, output = live
    _as_configured(monkeypatch, database, documents, output)
    old = _dated(output, 3)
    shutil.rmtree(documents / "crops")  # The database still refers to these.
    assert backup_script.main(["--output-dir", str(output), "--keep", "1"]) == 1
    assert all(path.exists() for path in old)


# ------------------------------------------------------------------ off-device copies

def test_a_copy_arrives_verified_and_private(live, tmp_path):
    folder = _backup(live)
    target = backup_script.LocalFolderTarget(tmp_path / "drive" / "ticketflow")
    placed = Path(target.put(folder))
    assert placed.name == folder.name
    assert verify_script.verify(placed) == []
    assert stat.S_IMODE(placed.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in placed.iterdir())
    assert not list(placed.parent.glob(".*"))


def test_a_damaged_copy_isnt_kept_and_doesnt_look_like_a_backup(live, tmp_path, monkeypatch):
    folder = _backup(live)
    real_copy = shutil.copyfile

    def flips_a_byte(source, destination, **kwargs):
        real_copy(source, destination, **kwargs)
        if Path(source).name == "ticketflow.db":
            with open(destination, "r+b") as stream:
                stream.seek(100)
                stream.write(b"\xff\xff")

    monkeypatch.setattr(backup_script.shutil, "copyfile", flips_a_byte)
    root = tmp_path / "drive"
    with pytest.raises(backup_script.CopyFailed):
        backup_script.LocalFolderTarget(root).put(folder)
    assert list(root.iterdir()) == []


def test_a_copy_never_overwrites(live, tmp_path):
    folder = _backup(live)
    target = backup_script.LocalFolderTarget(tmp_path / "drive")
    target.put(folder)
    with pytest.raises(backup_script.CopyFailed):
        target.put(folder)


def test_the_command_copies_and_warns_about_the_same_disk(live, tmp_path, monkeypatch, capsys):
    database, documents, output = live
    _as_configured(monkeypatch, database, documents, output)
    drive = tmp_path / "drive"
    assert backup_script.main(["--output-dir", str(output), "--copy-to", str(drive), "--keep", "1"]) == 0
    captured = capsys.readouterr()
    assert "Copied and verified" in captured.out and "same disk" in captured.err
    assert len(list(drive.iterdir())) == 1
    # Recorded beside the backup, so System Health knows it wouldn't survive losing this disk.
    newest = backup_script.newest_backup(output)
    assert json.loads((newest / "copied.json").read_text())["same_disk"] is True


def test_retention_applies_at_the_destination_too(live, tmp_path, monkeypatch):
    database, documents, output = live
    _as_configured(monkeypatch, database, documents, output)
    drive = tmp_path / "drive"
    older = _dated(drive, 2)
    assert backup_script.main(["--output-dir", str(output), "--copy-to", str(drive), "--keep", "2"]) == 0
    assert not older[0].exists() and older[1].exists()


def test_a_failed_copy_removes_nothing(live, tmp_path, monkeypatch):
    database, documents, output = live
    _as_configured(monkeypatch, database, documents, output)
    old = _dated(output, 2)

    def refuses(_self, _folder):
        raise backup_script.CopyFailed("the drive went away")

    monkeypatch.setattr(backup_script.LocalFolderTarget, "put", refuses)
    assert backup_script.main(["--output-dir", str(output), "--copy-to", str(tmp_path / "drive"), "--keep", "1"]) == 1
    assert all(path.exists() for path in old)


# ------------------------------------------------------------------ schedule templates

@pytest.mark.parametrize("kind, expected", [("cron", "30 2 * * *"), ("launchd", "com.ticketflow.backup")])
def test_a_schedule_is_only_printed(kind, expected, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(backup_script.settings, "database_url", "postgresql://not-used.example/none")  # Proves it doesn't run a backup.
    assert backup_script.main(["--print-schedule", kind, "--output-dir", str(tmp_path / "b"), "--keep", "7", "--copy-to", str(tmp_path / "off")]) == 0
    printed = capsys.readouterr().out
    assert expected in printed and "--keep" in printed and "7" in printed and str(tmp_path / "off") in printed
    assert "Nothing has been installed" in printed
    assert not (tmp_path / "b").exists()


def test_the_launchd_template_is_valid_xml(tmp_path, capsys):
    import plistlib
    backup_script.main(["--print-schedule", "launchd", "--output-dir", str(tmp_path / "with space & amp")])
    plist = plistlib.loads(capsys.readouterr().out.encode())
    assert plist["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert str(tmp_path / "with space & amp") in plist["ProgramArguments"]


# ------------------------------------------------------------------ check_documents

def _count(result_rows, table):
    return [row for row in result_rows if row["table"] == table]


def test_check_documents_on_a_sound_installation(live, capsys):
    database, documents, _output = live
    code = check_documents.main(["--database", f"sqlite:///{database}", "--storage", str(documents)])
    assert code == 0 and "Every document is sound." in capsys.readouterr().out
    result = check_documents.check_database(f"sqlite:///{database}", documents)
    assert result.checked > 10 and result.hash_checked > 5 and result.ok


def test_check_documents_finds_a_missing_file(live, capsys):
    database, documents, _output = live
    (documents / _first_crop(database)).unlink()
    assert check_documents.main(["--database", f"sqlite:///{database}", "--storage", str(documents)]) == 1
    printed = capsys.readouterr().out
    assert "Missing" in printed and "tickets" in printed
    assert str(documents) not in printed  # Row ids, never paths.


def test_check_documents_finds_a_changed_file(live, capsys):
    database, documents, _output = live
    connection = sqlite3.connect(database)
    upload = connection.execute("SELECT storage_path FROM uploaded_files LIMIT 1").fetchone()[0]
    connection.close()
    (documents / upload).write_bytes(b"%PDF changed since it was stored")
    result = check_documents.check_database(f"sqlite:///{database}", documents)
    assert result.mismatched_count == 1 and _count(result.mismatched, "uploaded_files")
    assert check_documents.main(["--database", f"sqlite:///{database}", "--storage", str(documents), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["mismatched_count"] == 1


def test_a_rotated_crop_isnt_compared_with_the_original_crops_hash(live):
    database, documents, _output = live
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT COUNT(*) FROM tickets WHERE crop_path LIKE '%\\_r90.png' ESCAPE '\\'").fetchone()[0] >= 1
    connection.close()
    assert check_documents.check_database(f"sqlite:///{database}", documents).mismatched_count == 0


def test_inside_only_isnt_fooled_by_the_original_location(live, tmp_path):
    """A restored copy can look complete when the old folder is still on this machine."""
    database, documents, _output = live
    connection = sqlite3.connect(database)
    connection.execute("UPDATE tickets SET crop_path = ? || '/' || crop_path", (str(documents),))
    connection.commit()
    crop = _first_crop(database)
    connection.close()
    empty = tmp_path / "restored-without-documents"
    empty.mkdir()
    relaxed = check_documents.check_database(f"sqlite:///{database}", empty)
    assert relaxed.outside_storage_count >= 1 and Path(crop).is_file()
    strict = check_documents.check_database(f"sqlite:///{database}", empty, inside_only=True)
    assert strict.outside_storage_count == 0 and not strict.ok


def test_references_follow_what_exists_in_an_older_database():
    tables = {"tickets": {"id", "crop_path"}}
    rows = list(document_audit.references(lambda sql: [(1, "crops/1/a.png")], lambda table: tables.get(table, set())))
    assert rows == [document_audit.DocumentRef("tickets", 1, "crops/1/a.png", None)]


# ------------------------------------------------------------------ the restore drill

def test_the_drill_passes_on_a_good_backup_and_cleans_up(live, tmp_path, capsys):
    folder = _backup(live)
    work = tmp_path / "work"
    work.mkdir()
    report = tmp_path / "drill.json"
    before = backup_script.sha256(live[0])
    code = restore_drill.main([str(folder), "--work-dir", str(work), "--report", str(report)])
    output = capsys.readouterr().out
    assert code == 0, output
    assert "RESTORE DRILL PASSED" in output
    found = json.loads(report.read_text())
    assert found["passed"] is True
    names = [check["name"] for check in found["checks"]]
    assert "The application starts against the restored copy" in names
    for kind in ("/api/tickets/", "/api/files/", "/api/invoice-documents/"):
        assert any(kind in name and "opens through the API" in name for name in names), kind
    assert any("the app's totals equal the backup's" in name for name in names)
    assert list(work.iterdir()) == []  # The copy holds real documents, so it's gone.
    assert backup_script.sha256(live[0]) == before  # The live database wasn't touched.


def test_the_drill_fails_on_an_incomplete_backup(live, tmp_path, capsys):
    folder = _backup(live)
    _rewrite_archive(folder, drop=_first_crop(live[0]))
    assert restore_drill.main([str(folder), "--work-dir", str(tmp_path)]) == 1
    assert "RESTORE DRILL FAILED" in capsys.readouterr().out


def test_the_drill_fails_when_totals_dont_match_the_backup(live, tmp_path, capsys):
    folder = _backup(live)
    manifest = _manifest(folder)
    company = next(iter(manifest["totals"]))
    manifest["totals"][company]["approved_cost"] = "0.01"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    assert restore_drill.main([str(folder), "--work-dir", str(tmp_path)]) == 1
    assert "Exact totals per company" in capsys.readouterr().out


def test_the_drill_isnt_fooled_by_the_original_documents_still_being_here(live, tmp_path, capsys):
    """The dangerous false pass: old full paths in the database, and the live folder still
    on this machine, could make a restore look fine with documents missing from it."""
    database, documents, _output = live
    connection = sqlite3.connect(database)
    connection.execute("UPDATE tickets SET crop_path = ? || '/' || crop_path", (str(documents),))
    connection.commit()
    connection.close()
    folder = _backup(live)
    crop = _first_crop(database)
    key = storage.key_for(crop)
    # Only the restored copy loses it. The original stays where the old path says.
    _rewrite_archive(folder, drop=key)
    assert Path(crop).is_file()
    real_verify = restore_drill.verify
    restore_drill.verify = lambda _folder: []  # Get past the archive check, to the restored copy's own check.
    try:
        assert restore_drill.main([str(folder), "--work-dir", str(tmp_path)]) == 1
    finally:
        restore_drill.verify = real_verify
    assert "Every document is in the restored copy" in capsys.readouterr().out


def test_the_drill_upgrades_an_older_copy_as_a_real_recovery_would(live, tmp_path, capsys):
    database, _documents, _output = live
    connection = sqlite3.connect(database)
    connection.execute("UPDATE alembic_version SET version_num = '20261106_18'")
    connection.commit()
    connection.close()
    folder = _backup(live)
    assert restore_drill.main([str(folder), "--work-dir", str(tmp_path)]) == 0, capsys.readouterr().out
    assert f"to {head_revision()}" in capsys.readouterr().out


def test_the_drill_refuses_a_folder_that_isnt_a_backup(tmp_path, capsys):
    assert restore_drill.main([str(tmp_path / "absent")]) == 1
    (tmp_path / "empty").mkdir()
    assert restore_drill.main([str(tmp_path / "empty"), "--work-dir", str(tmp_path)]) == 1
    assert "No manifest" in capsys.readouterr().out


def test_the_latest_backup_is_chosen_by_date(tmp_path):
    backups = _dated(tmp_path, 3)
    assert backup_script.newest_backup(tmp_path) == backups[2]
    assert backup_script.newest_backup(tmp_path / "absent") is None


# ------------------------------------------------------------------ doctor

@pytest.fixture()
def installation(tmp_path, monkeypatch):
    database = tmp_path / "ticketflow.db"
    url = f"sqlite:///{database}"
    command.upgrade(alembic_config(url), "head")
    documents = tmp_path / "storage"
    documents.mkdir(mode=0o700)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    database.chmod(0o600)
    monkeypatch.setattr(settings, "storage_dir", documents)
    monkeypatch.setattr(settings, "backup_dir", backups)
    engine = create_engine(url, poolclass=NullPool)
    yield engine, database, documents, backups
    engine.dispose()


def _states(engine) -> dict[str, str]:
    return {check["name"]: check["state"] for check in doctor.run_checks(engine)}


def test_doctor_on_a_healthy_installation(installation, capsys, monkeypatch):
    engine, *_ = installation
    states = _states(engine)
    assert states["Configuration"] == "ok" and states["Database"] == "ok" and states["Migrations"] == "ok"
    assert states["Duplicate safeguards"] == "ok" and states["Storage folder"] == "ok" and states["Backups"] == "ok"
    monkeypatch.setattr(doctor, "configured_engine", engine)
    assert doctor.main([]) == 0
    assert "Up to date" in capsys.readouterr().out


def test_doctor_finds_an_old_migration(installation):
    engine, database, *_ = installation
    connection = sqlite3.connect(database)
    connection.execute("UPDATE alembic_version SET version_num = '20261105_17'")
    connection.commit()
    connection.close()
    report = next(check for check in doctor.run_checks(engine) if check["name"] == "Migrations")
    assert report["state"] == "problem" and "20261105_17" in report["message"] and "alembic upgrade head" in report["action"]


def test_doctor_finds_a_missing_safeguard(installation):
    engine, database, *_ = installation
    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX uq_tickets_slot")
    connection.commit()
    connection.close()
    report = next(check for check in doctor.run_checks(engine) if check["name"] == "Duplicate safeguards")
    assert report["state"] == "problem" and "uq_tickets_slot" in report["message"] and "operations.md" in report["action"]


def test_doctor_finds_a_folder_others_can_read(installation):
    engine, _database, documents, backups = installation
    documents.chmod(0o755)
    (backups / "ticketflow-20260101-000000").mkdir(mode=0o755)
    states = _states(engine)
    assert states["Storage folder"] == "attention" and states["Backups"] == "attention"


def test_doctor_finds_storage_it_cant_write_to(installation, monkeypatch):
    engine, _database, documents, _backups = installation

    def refused(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(doctor.os, "open", refused)
    assert _states(engine)["Storage folder"] == "problem"
    assert not list(documents.iterdir())


def test_doctor_leaves_nothing_in_storage(installation):
    engine, _database, documents, _backups = installation
    doctor.run_checks(engine)
    assert list(documents.iterdir()) == []


def test_doctor_finds_a_database_it_cant_open(installation, tmp_path):
    bad = tmp_path / "not-a-database.db"
    bad.write_bytes(b"this isn't a database file" * 100)
    engine = create_engine(f"sqlite:///{bad}", poolclass=NullPool)
    states = _states(engine)
    assert states["Database"] == "problem"
    engine.dispose()


def test_doctor_finds_a_bad_setting_and_never_prints_secrets(installation, monkeypatch, capsys):
    engine, *_ = installation
    monkeypatch.setattr(settings, "trusted_hosts", ["*"])
    monkeypatch.setattr(settings, "gemini_api_key", "fictional-key-value")
    monkeypatch.setattr(doctor, "configured_engine", engine)
    assert doctor.main([]) == 1
    printed = capsys.readouterr().out
    assert "TRUSTED_HOSTS" in printed and "fictional-key-value" not in printed and str(engine.url) not in printed


def test_configuration_problems_match_what_startup_refuses(monkeypatch):
    assert configuration_problems(settings) == []
    monkeypatch.setattr(settings, "cors_origins", ["https://app.example"])
    assert "SESSION_SECURE_COOKIE" in configuration_problems(settings)[0]


# ------------------------------------------------------------------ the written steps work

def test_the_documented_safeguard_statements_really_create_each_one(tmp_path):
    """Written instructions for a bad day are worth testing."""
    path = tmp_path / "x.db"
    command.upgrade(alembic_config(f"sqlite:///{path}"), "head")
    statements = re.findall(r"^\s*(CREATE UNIQUE INDEX [^;]+;)", OPERATIONS.read_text(), flags=re.MULTILINE)
    assert len(statements) == 4
    from app.services import system_health
    connection = sqlite3.connect(path)
    for names in system_health.SAFEGUARD_INDEXES.values():
        for name in names:
            connection.execute(f"DROP INDEX {name}")
    for statement in statements:
        connection.execute(statement)
    documented = {row[0]: row[1] for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name LIKE 'uq_%'")}
    connection.commit()
    connection.close()
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        assert system_health.missing_safeguards(db) == []
    engine.dispose()
    assert "WHERE status = 'verified'" in documented["uq_tickets_approved_scan"]
    # And they're the same columns the models declare.
    from .test_schema import _differences
    assert _differences(f"sqlite:///{path}") == []


def test_every_section_system_health_points_to_exists():
    written = OPERATIONS.read_text()
    for heading in ("Duplicates found during an upgrade", "Database corruption", "Application crash", "Interrupted processing", "Missing documents"):
        assert f"### {heading}" in written
    assert "(#missing-documents)" in written

