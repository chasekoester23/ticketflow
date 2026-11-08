"""The SQLite-to-PostgreSQL copy: refuses bad data, and proves what it copied.

Preflight and the copy's own logic run on SQLite everywhere, through a stand-in
target. The real copy runs where TEST_DATABASE_URL names a PostgreSQL server, which
is CI. All data is fictional. Nothing here touches a real database.
"""
import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import db as db_module
from app import main as main_module
from app.auth import current_user
from app.config import settings
from app.db import engine_options, enforce_references, normalized_url
from app.main import app
from app.models import Batch, LoginSession, Organization, Project, ReviewTiming, Ticket, UploadedFile, User
from app.routers import api as api_module
from scripts import migrate_sqlite_to_postgres as migrate

from .conftest import TEST_DATABASE_URL, FixtureExtractor
from .migration_helpers import alembic_config, head_revision
from .sample_data import make_invoice_pdf, make_scan_pdf

needs_postgres = pytest.mark.skipif(not TEST_DATABASE_URL, reason="The copy itself runs where TEST_DATABASE_URL is set (CI).")
SECRET = "SECRET-TICKET-VALUE"


# ------------------------------------------------------------------ the API against any one database

@contextmanager
def api_against(url: str, storage: Path, monkeypatch):
    """The real application, its startup checks included, on exactly this database."""
    engine = create_engine(normalized_url(url), **engine_options(url))
    enforce_references(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    saved = dict(app.dependency_overrides)
    with monkeypatch.context() as patch:
        patch.setattr(main_module, "engine", engine)
        patch.setattr(settings, "storage_dir", storage)
        patch.setattr(api_module, "SessionLocal", Session)
        patch.setattr(api_module, "get_extractor", FixtureExtractor)
        patch.setattr(api_module, "_notify_worker", lambda background, _job: background.add_task(api_module._drain_job_for_tests))
        app.dependency_overrides[db_module.get_db] = override
        try:
            with TestClient(app, headers={"X-TicketFlow-Request": "1"}) as client:
                yield client, Session
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(saved)
    engine.dispose()


def act_as(organization_id: int, user_id: int) -> None:
    app.dependency_overrides[current_user] = lambda: User(
        id=user_id, organization_id=organization_id, username="fictional", display_name="Fictional", password_hash="x",
        active=True, role="owner", must_change_password=False)


# ------------------------------------------------------------------ a small source, for checks

def _rows_for(db, organization_id: int, number: str) -> None:
    project = Project(organization_id=organization_id, code=f"P{number}", name="Fictional Job")
    db.add(project)
    db.flush()
    batch = Batch(organization_id=organization_id, project_id=project.id, layout="single", status="ready_for_review", intake_mode="manual")
    db.add(batch)
    db.flush()
    file = UploadedFile(batch_id=batch.id, original_name="f.pdf", storage_path=f"uploads/{batch.id}/f.pdf", sha256="0" * 64, status="ready_for_review")
    db.add(file)
    db.flush()
    db.add(Ticket(batch_id=batch.id, file_id=file.id, source_page=1, position=0, crop_box=[0, 0, 1, 1], crop_path=f"crops/{batch.id}/c.png",
                  crop_sha256=f"{batch.id:064d}", ticket_number=number, delivery_date=date(2026, 9, 1), material_type="Sand",
                  supplier="Example Aggregates", job_location="Lot", tonnage=Decimal("20.500"), flags=[], status="needs_review"))


@pytest.fixture(scope="session")
def small_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("migrate") / "template.db"
    command.upgrade(alembic_config(f"sqlite:///{path}"), "head")
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as db:
        first = Organization(name="First Fictional Co")
        second = Organization(name="Second Fictional Co")
        db.add_all([first, second])
        db.flush()
        db.add_all([User(organization_id=first.id, username="first-owner", password_hash="x", role="owner"),
                    User(organization_id=second.id, username="second-owner", password_hash="x", role="owner")])
        _rows_for(db, first.id, "1001")
        _rows_for(db, second.id, "1002")
        db.commit()
    engine.dispose()
    return path


@pytest.fixture()
def small_source(small_template, tmp_path) -> Path:
    path = tmp_path / "source.db"
    shutil.copyfile(small_template, path)
    return path


def _change(path: Path, *statements) -> None:
    connection = sqlite3.connect(path)  # Foreign keys off, which is how old rows got in.
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    connection.close()


def _preflight(path: Path, tmp_path: Path) -> tuple[int, dict]:
    report = tmp_path / "report.json"
    code = migrate.main(["--source", str(path), "--preflight-only", "--report", str(report)])
    return code, json.loads(report.read_text())


def _kinds(report: dict) -> set[str]:
    return {problem["kind"] for problem in report["preflight"]["problems"]}


# ------------------------------------------------------------------ preflight

def test_a_clean_source_passes_and_is_not_changed(small_source, tmp_path, capsys):
    before = migrate.file_digest(small_source)
    code, report = _preflight(small_source, tmp_path)
    assert code == 0 and report["outcome"] == "preflight_clean"
    assert report["source"]["sha256_before"] == report["source"]["sha256_after"] == before
    assert report["source"]["unchanged"] is True
    assert migrate.file_digest(small_source) == before
    assert report["preflight"]["tables"]["tickets"]["rows"] == 2
    assert not (tmp_path / "source.db-journal").exists() and not (tmp_path / "source.db-wal").exists()
    assert "Preflight clean" in capsys.readouterr().out
    assert oct((tmp_path / "report.json").stat().st_mode)[-3:] == "600"


def test_preflight_needs_no_target_and_a_copy_needs_one(small_source):
    for arguments in (["--source", str(small_source)],
                      ["--source", str(small_source), "--preflight-only", "--target", "postgresql://x/y"]):
        with pytest.raises(SystemExit) as stopped:
            migrate.main(arguments)
        assert stopped.value.code == 2


def test_the_source_connection_cannot_write(small_source):
    source = migrate.Source(small_source)
    try:
        with pytest.raises(sqlite3.OperationalError):
            source.meta.execute("DELETE FROM organizations")
        with pytest.raises(sqlite3.OperationalError):
            source.raw.execute("UPDATE tickets SET supplier = 'x'")
    finally:
        source.close()


def test_a_read_only_file_can_be_checked(small_source, tmp_path):
    small_source.chmod(0o400)
    code, _report = _preflight(small_source, tmp_path)
    assert code == 0


@pytest.mark.parametrize("statements, kind, table", [
    # What SQLite let in before it enforced foreign keys.
    (["UPDATE tickets SET batch_id = 999 WHERE id = 1"], "missing_reference", "tickets"),
    (["UPDATE batches SET project_id = 999 WHERE id = 1"], "missing_reference", "batches"),
    # Longer than the column. SQLite never checked.
    ([f"UPDATE tickets SET supplier = '{SECRET * 20}' WHERE id = 1"], "value_too_long", "tickets"),
    ([f"UPDATE projects SET code = '{SECRET * 5}' WHERE id = 1"], "value_too_long", "projects"),
    # NUL bytes, in text and inside JSON.
    ([f"UPDATE tickets SET supplier = '{SECRET}' || char(0) || 'x' WHERE id = 1"], "nul_byte", "tickets"),
    ([f"""UPDATE tickets SET flags = '["{SECRET}\\u0000"]' WHERE id = 1"""], "nul_byte", "tickets"),
    # Values PostgreSQL would refuse, or silently change.
    (["UPDATE tickets SET tonnage = 1.23456 WHERE id = 1"], "too_many_decimals", "tickets"),
    ([f"UPDATE tickets SET tonnage = '{SECRET}' WHERE id = 1"], "not_a_number", "tickets"),
    (["UPDATE tickets SET tonnage = 123456789012.5 WHERE id = 1"], "number_too_large", "tickets"),
    (["UPDATE tickets SET source_page = 1099511627776 WHERE id = 1"], "integer_out_of_range", "tickets"),
    (["UPDATE tickets SET tonnage_confirmed = 7 WHERE id = 1"], "not_a_boolean", "tickets"),
    ([f"UPDATE tickets SET delivery_date = '{SECRET}' WHERE id = 1"], "bad_date", "tickets"),
    ([f"UPDATE tickets SET created_at = '{SECRET}' WHERE id = 1"], "bad_timestamp", "tickets"),
    ([f"UPDATE tickets SET crop_box = '{SECRET}' WHERE id = 1"], "bad_json", "tickets"),
    # Records two companies disagree about.
    (["UPDATE tickets SET organization_id = 999 WHERE id = 1"], "company_mismatch", "tickets"),
    (["UPDATE tickets SET organization_id = NULL WHERE id = 1"], "company_mismatch", "tickets"),
    # The schema isn't what this code expects.
    (["ALTER TABLE tickets ADD COLUMN leftover TEXT"], "unexpected_column", "tickets"),
    (["CREATE TABLE leftovers (id INTEGER PRIMARY KEY)"], "unexpected_table", "leftovers"),
])
def test_preflight_reports_each_problem_without_quoting_or_fixing_it(small_source, tmp_path, capsys, statements, kind, table):
    _change(small_source, *statements)
    before = migrate.file_digest(small_source)
    code, report = _preflight(small_source, tmp_path)
    assert code == 1 and report["outcome"] == "preflight_found_problems"
    assert kind in _kinds(report)
    assert any(p["kind"] == kind and p["table"] == table for p in report["preflight"]["problems"])
    assert migrate.file_digest(small_source) == before
    output = capsys.readouterr()
    assert kind in output.err
    for text_seen in (output.out, output.err, (tmp_path / "report.json").read_text()):
        assert SECRET not in text_seen


def test_preflight_names_the_rows_involved(small_source, tmp_path):
    _change(small_source, "UPDATE tickets SET batch_id = 999 WHERE id = 2")
    _code, report = _preflight(small_source, tmp_path)
    [problem] = [p for p in report["preflight"]["problems"] if p["kind"] == "missing_reference"]
    assert problem["sample_ids"] == [2] and problem["column"] == "batch_id" and problem["references"] == "batches"


def test_duplicates_are_found_even_when_the_source_has_no_index_for_them(small_source, tmp_path):
    """Migration 18 leaves a unique index out rather than touch existing rows."""
    _change(small_source,
            "DROP INDEX uq_tickets_slot",
            "INSERT INTO tickets (batch_id, organization_id, file_id, source_page, position, crop_box, crop_path, crop_sha256, flags, status, "
            "tonnage_confirmed, created_at, lock_version) SELECT batch_id, organization_id, file_id, source_page, position, crop_box, crop_path, "
            "'another-scan', flags, status, tonnage_confirmed, created_at, lock_version FROM tickets WHERE id = 1")
    code, report = _preflight(small_source, tmp_path)
    assert code == 1
    [problem] = [p for p in report["preflight"]["problems"] if p["kind"] == "duplicate_rows"]
    assert problem["index"] == "uq_tickets_slot" and problem["table"] == "tickets" and problem["rows"] == 1


def test_a_partial_unique_rule_only_counts_the_rows_it_applies_to(small_source, tmp_path):
    same_scan = ("UPDATE tickets SET crop_sha256 = 'same', organization_id = (SELECT organization_id FROM batches WHERE id = 1), "
                 "batch_id = 1, file_id = 1, position = id")
    _change(small_source, "DROP INDEX uq_tickets_approved_scan", same_scan)
    code, report = _preflight(small_source, tmp_path)
    assert "duplicate_rows" not in _kinds(report) and code == 0  # Neither is approved, so PostgreSQL allows both.
    _change(small_source, "UPDATE tickets SET status = 'verified'")
    code, report = _preflight(small_source, tmp_path)
    assert code == 1 and "duplicate_rows" in _kinds(report)
    assert next(p for p in report["preflight"]["problems"] if p["kind"] == "duplicate_rows")["index"] == "uq_tickets_approved_scan"


def test_refuses_a_source_that_is_not_at_the_current_migration(small_source, tmp_path):
    _change(small_source, "UPDATE alembic_version SET version_num = '20261105_17'")
    code, report = _preflight(small_source, tmp_path)
    assert code == 1 and "source_not_at_current_migration" in _kinds(report)
    problem = next(p for p in report["preflight"]["problems"] if p["kind"] == "source_not_at_current_migration")
    assert "20261105_17" in problem["detail"] and head_revision() in problem["detail"]


def test_refuses_a_source_with_no_migration_history(small_source, tmp_path):
    _change(small_source, "DROP TABLE alembic_version")
    code, report = _preflight(small_source, tmp_path)
    assert code == 1 and "source_not_at_current_migration" in _kinds(report)


def test_refuses_a_source_someone_is_writing_to(small_source, tmp_path, capsys):
    (tmp_path / "source.db-journal").write_bytes(b"in progress")
    report = tmp_path / "r.json"
    assert migrate.main(["--source", str(small_source), "--preflight-only", "--report", str(report)]) == 1
    assert "Stop the API and worker" in capsys.readouterr().err
    assert json.loads(report.read_text())["outcome"] == "refused"


def test_refuses_a_missing_source(tmp_path):
    report = tmp_path / "r.json"
    assert migrate.main(["--source", str(tmp_path / "absent.db"), "--preflight-only", "--report", str(report)]) == 1
    assert not (tmp_path / "absent.db").exists()


def test_refuses_a_target_that_is_not_postgresql_and_never_prints_a_password(small_source, tmp_path, capsys):
    report = tmp_path / "r.json"
    other = tmp_path / "other.db"
    assert migrate.main(["--source", str(small_source), "--target", f"sqlite:///{other}", "--report", str(report)]) == 1
    assert json.loads(report.read_text())["outcome"] == "refused"
    assert not other.exists()
    assert migrate._target_label("postgresql://someone:hunter2hunter2@db.example:5433/tf") == "db.example:5433/tf"


# ------------------------------------------------------------------ exact values

def _decimal_column(precision=12, scale=3):
    return migrate.ColumnSpec("x", "decimal", True, precision=precision, scale=scale)


@pytest.mark.parametrize("raw, stored", [
    (20.5, "20.500"), (0.1 + 0.2, "0.300"), (20, "20.000"), (b"19.999", "19.999"), (0.001, "0.001"),
    (999999999.999, "999999999.999"), (-1.5, "-1.500"),
])
def test_decimals_are_exact_at_their_column_scale(raw, stored):
    value, canonical = migrate.read_value(_decimal_column(), raw)
    assert value == Decimal(stored) and isinstance(value, Decimal) and canonical == stored


@pytest.mark.parametrize("raw, kind", [
    (1.2345, "too_many_decimals"), (b"1.2345", "too_many_decimals"), (b"abc", "not_a_number"), (b"NaN", "not_a_number"),
    (float("inf"), "not_a_number"), (1e12, "number_too_large"), (b"1e40", "number_too_large"),
])
def test_a_decimal_that_would_change_is_refused(raw, kind):
    with pytest.raises(migrate.Unusable) as refused:
        migrate.read_value(_decimal_column(), raw)
    assert refused.value.kind == kind


@pytest.mark.parametrize("raw", [b"2026-10-05 14:30:15.000123", b"2026-10-05T14:30:15.000123", b"2026-10-05 14:30:15.000123+00:00",
                                 b"2026-10-05 16:30:15.000123+02:00", b"2026-10-05T14:30:15.000123Z"])
def test_timestamps_become_utc(raw):
    aware = migrate.ColumnSpec("x", "timestamptz", True)
    value, canonical = migrate.read_value(aware, raw)
    assert value == datetime(2026, 10, 5, 14, 30, 15, 123, tzinfo=timezone.utc)
    assert canonical == "2026-10-05T14:30:15.000123Z"


def test_a_timestamp_with_no_zone_column_stays_naive():
    naive = migrate.ColumnSpec("x", "timestamp", True)
    value, canonical = migrate.read_value(naive, b"2026-10-05 14:30:15")
    assert value == datetime(2026, 10, 5, 14, 30, 15) and value.tzinfo is None
    assert canonical == "2026-10-05T14:30:15.000000"


def test_json_null_stays_different_from_blank_and_key_order_is_not_a_difference():
    column = migrate.ColumnSpec("x", "json", True)
    assert migrate.read_value(column, None) == (None, None)
    assert migrate.read_value(column, b"null") == ("null", "null")
    assert migrate.read_value(column, b'{"b": 1,  "a": [1,2]}')[1] == migrate.read_value(column, b'{"a":[1,2],"b":1}')[1]
    with pytest.raises(migrate.Unusable):
        migrate.read_value(column, b"NaN")


def test_booleans_and_required_columns_are_strict():
    boolean = migrate.ColumnSpec("x", "boolean", False)
    assert migrate.read_value(boolean, 1) == (True, True) and migrate.read_value(boolean, 0) == (False, False)
    for raw, kind in ((2, "not_a_boolean"), (b"true", "not_a_boolean"), (None, "null_in_required_column")):
        with pytest.raises(migrate.Unusable) as refused:
            migrate.read_value(boolean, raw)
        assert refused.value.kind == kind


def test_invalid_utf8_is_reported_rather_than_crashing():
    with pytest.raises(migrate.Unusable) as refused:
        migrate.read_value(migrate.ColumnSpec("x", "text", True), b"\xff\xfe")
    assert refused.value.kind == "not_utf8"


def test_every_table_and_column_is_covered():
    """A new table or column type can't be left out of the copy by accident."""
    specs = migrate.table_specs()
    assert {spec.name for spec in specs} == set(db_module.Base.metadata.tables)
    assert {column.kind for spec in specs for column in spec.columns} == {
        "text", "integer", "decimal", "float", "boolean", "date", "timestamp", "timestamptz", "json"}
    # Parents always come first, so foreign keys hold during the copy.
    order = [spec.name for spec in specs]
    for table in db_module.Base.metadata.sorted_tables:
        for key in table.foreign_keys:
            assert order.index(key.column.table.name) < order.index(table.name)


# ------------------------------------------------------------------ the copy's logic, on a stand-in

class SqliteStandIn(migrate.PostgresTarget):
    """Only for running the copy's logic where PostgreSQL isn't available. The real
    target is exercised below, in CI, by the tests marked needs_postgres."""

    def lock(self, specs):
        pass

    def _insert_sql(self, spec):
        columns = ", ".join(migrate._quote(column.name) for column in spec.columns)
        values = ", ".join(f":{column.name}" for column in spec.columns)
        return f"INSERT INTO {migrate._quote(spec.name)} ({columns}) VALUES ({values})"

    def insert(self, spec, rows):
        def plain(value):
            if isinstance(value, Decimal):
                return float(value)
            if isinstance(value, datetime):
                return value.strftime("%Y-%m-%d %H:%M:%S.%f")
            if isinstance(value, date):
                return value.isoformat()
            return value
        self.connection.execute(text(self._insert_sql(spec)), [{k: plain(v) for k, v in row.items()} for row in rows])

    def _select_sql(self, spec):
        columns = ", ".join(migrate._quote(column.name) for column in spec.columns)
        return f"SELECT {columns} FROM {migrate._quote(spec.name)} ORDER BY {migrate._quote(spec.pk)}"

    @staticmethod
    def _canonical(spec, value):
        return migrate.read_value(spec, value.encode() if isinstance(value, str) else value)[1]

    def set_sequences(self, specs):
        return {}

    def next_ids(self, specs):
        return {}


def _empty_sqlite_target(tmp_path) -> Path:
    path = tmp_path / "stand-in.db"
    command.upgrade(alembic_config(f"sqlite:///{path}"), "head")
    return path


def _copy_with_stand_in(source_path: Path, target_path: Path, *, after_copy=None):
    specs = migrate.table_specs()
    engine = create_engine(f"sqlite:///{target_path}", poolclass=NullPool)
    source = migrate.Source(source_path)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                result = migrate.copy_into(source, SqliteStandIn(connection), specs,
                                           source_digest=migrate.file_digest(source_path), after_copy=after_copy)
            except Exception:
                transaction.rollback()
                raise
            transaction.commit()
            return result
    finally:
        source.close()
        engine.dispose()


def _row_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {spec.name: connection.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0] for spec in migrate.table_specs()}
    finally:
        connection.close()


def _untouched(counts: dict[str, int]) -> bool:
    """A new database at the current migration: the migrations' placeholder company, nothing else."""
    return counts.pop("organizations") == 1 and set(counts.values()) == {0}


def test_a_new_migrated_database_counts_as_empty(tmp_path):
    """The migrations seed one placeholder company. Calling that "not empty" would
    make every copy into a real, freshly migrated target impossible."""
    assert _untouched(_row_counts(_empty_sqlite_target(tmp_path)))


def test_stand_in_copy_matches_every_row(small_source, tmp_path):
    target = _empty_sqlite_target(tmp_path)
    result = _copy_with_stand_in(small_source, target)
    assert all(table["match"] for table in result["tables"].values())
    assert result["tables"]["tickets"]["source_rows"] == 2
    assert result["problems"] == []
    assert _row_counts(target) == _row_counts(small_source)


def _tamper_text(target):
    target.connection.execute(text("UPDATE tickets SET material_type = 'Tampered' WHERE id = 1"))


def _tamper_extra_row(target):
    target.connection.execute(text(
        "INSERT INTO organizations (id, name) VALUES (99, 'Not in the source')"))


def _tamper_missing_row(target):
    target.connection.execute(text("DELETE FROM tickets WHERE id = 2"))


def _tamper_history(target):
    target.connection.execute(text(
        "INSERT INTO audit_events (ticket_id, field, created_at) VALUES (1, 'status', '2026-10-01 00:00:00')"))


@pytest.mark.parametrize("tamper, expected", [
    (_tamper_text, "tickets: row contents differ"),
    (_tamper_extra_row, "organizations: 3 row(s) in the source, 4 in the target"),
    (_tamper_missing_row, "tickets: 2 row(s) in the source, 1 in the target"),
    (_tamper_history, "Audit history counts differ"),
])
def test_any_difference_rolls_back_everything(small_source, tmp_path, tamper, expected):
    target = _empty_sqlite_target(tmp_path)
    with pytest.raises(migrate.CopyFailed) as failed:
        _copy_with_stand_in(small_source, target, after_copy=tamper)
    assert expected in str(failed.value)
    assert _untouched(_row_counts(target))


def test_a_wrong_cost_is_caught_by_the_company_totals_too(small_source, tmp_path):
    _change(small_source, "UPDATE tickets SET status = 'verified', tonnage_confirmed = 1, approved_cost = 100.50, "
                          "price_snapshot_state = 'priced', applied_unit_price = 4.9024")
    target = _empty_sqlite_target(tmp_path)

    def tamper(stand_in):
        stand_in.connection.execute(text("DROP TRIGGER tickets_approved_figures_locked"))
        stand_in.connection.execute(text("UPDATE tickets SET approved_cost = 100.51 WHERE id = 1"))

    with pytest.raises(migrate.CopyFailed) as failed:
        _copy_with_stand_in(small_source, target, after_copy=tamper)
    assert "Approved tons or cost differ" in str(failed.value)
    verification = failed.value.verification
    assert verification["company_totals"]["match"] is False
    source_first = next(iter(verification["company_totals"]["source"].values()))
    assert source_first == {"approved_tickets": 1, "approved_tons": "20.500", "approved_cost": "100.50", "without_cost": 0}


def test_a_source_changed_during_the_copy_rolls_back(small_source, tmp_path):
    target = _empty_sqlite_target(tmp_path)

    def someone_writes(_stand_in):
        _change(small_source, "UPDATE projects SET name = 'Changed' WHERE id = 1")

    with pytest.raises(migrate.CopyFailed) as failed:
        _copy_with_stand_in(small_source, target, after_copy=someone_writes)
    assert "The source file changed" in str(failed.value)
    assert _untouched(_row_counts(target))


def test_a_crash_partway_keeps_nothing(small_source, tmp_path):
    target = _empty_sqlite_target(tmp_path)

    def crash(_stand_in):
        raise RuntimeError("the machine lost power")

    with pytest.raises(RuntimeError):
        _copy_with_stand_in(small_source, target, after_copy=crash)
    assert _untouched(_row_counts(target))


def test_refuses_a_target_with_anything_in_it(small_source, tmp_path):
    target = _empty_sqlite_target(tmp_path)
    _change(target, "INSERT INTO organizations (id, name) VALUES (7, 'Already here')")
    with pytest.raises(migrate.CopyFailed, match="isn't empty: organizations"):
        _copy_with_stand_in(small_source, target)
    assert _row_counts(target)["organizations"] == 2 and _row_counts(target)["tickets"] == 0


def test_a_renamed_placeholder_company_means_someone_is_using_the_target(small_source, tmp_path):
    target = _empty_sqlite_target(tmp_path)
    _change(target, "UPDATE organizations SET name = 'A Real Company'")
    with pytest.raises(migrate.CopyFailed, match="isn't empty: organizations"):
        _copy_with_stand_in(small_source, target)


def test_refuses_a_target_at_another_migration(small_source, tmp_path):
    target = _empty_sqlite_target(tmp_path)
    _change(target, "UPDATE alembic_version SET version_num = '20261105_17'")
    with pytest.raises(migrate.CopyFailed, match="20261105_17"):
        _copy_with_stand_in(small_source, target)


def test_refuses_a_target_missing_a_unique_rule(small_source, tmp_path):
    target = _empty_sqlite_target(tmp_path)
    _change(target, "DROP INDEX uq_tickets_slot")
    with pytest.raises(migrate.CopyFailed, match="uq_tickets_slot"):
        _copy_with_stand_in(small_source, target)


# ------------------------------------------------------------------ a complete fictional business

def _scan(tickets) -> bytes:
    return make_scan_pdf(tickets)


def _fictional_tickets(prefix: str, supplier: str, material: str) -> list[dict]:
    return [dict(ticket_number=f"{prefix}{n}", delivery_date=f"2026-09-{10 + n:02d}", material_type=material, tonnage="20.5",
                 supplier=supplier, job_location="North Lot") for n in range(4)]


def _upload_and_fill(client, project_id, tickets, tons) -> list[dict]:
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(project_id)},
                           files=[("files", ("scan.pdf", _scan(tickets), "application/pdf"))])
    assert response.status_code == 200, response.text
    batch = response.json()["id"]
    rows = client.get(f"/api/batches/{batch}/tickets").json()
    assert len(rows) == 4
    filled = []
    for row, values, amount in zip(rows, tickets, tons):
        patched = client.patch(f"/api/tickets/{row['id']}", json=dict(values, tonnage=amount))
        assert patched.status_code == 200, patched.text
        filled.append(patched.json())
    return filled


def _approve(client, ticket) -> dict:
    response = client.post(f"/api/tickets/{ticket['id']}/verify")
    assert response.status_code == 200, response.text
    return response.json()


def _catalog(client, supplier, material, rate) -> None:
    supplier_id = client.post("/api/suppliers", json={"name": supplier}).json()["id"]
    material_id = client.post("/api/materials", json={"name": material}).json()["id"]
    assert client.put("/api/rates", json={"supplier_id": supplier_id, "material_id": material_id, "unit_price": rate}).status_code == 200


def build_fictional_business(url: str, storage: Path, monkeypatch) -> dict:
    """Two companies with one of everything, built through the real API, so the rows
    are what the app really writes. Decimals are chosen to trip float arithmetic."""
    with api_against(url, storage, monkeypatch) as (client, Session):
        with Session() as db:
            first = Organization(name="First Fictional Co")
            second = Organization(name="Second Fictional Co")
            db.add_all([first, second])
            db.flush()
            first_owner = User(organization_id=first.id, username="first-owner", display_name="First Owner", password_hash="x", role="owner")
            second_owner = User(organization_id=second.id, username="second-owner", display_name="Second Owner", password_hash="x", role="owner")
            db.add_all([first_owner, second_owner])
            db.flush()
            db.add(LoginSession(token_digest="a" * 64, user_id=first_owner.id, expires_at=datetime(2031, 1, 2, 3, 4, 5, 678)))
            db.commit()
            ids = {"first": first.id, "second": second.id, "first_owner": first_owner.id, "second_owner": second_owner.id}

        act_as(ids["first"], ids["first_owner"])
        project = client.post("/api/projects", json={"code": "26-100", "name": "Fictional Road"}).json()["id"]
        other_project = client.post("/api/projects", json={"code": "26-200", "name": "Fictional Bridge"}).json()["id"]
        _catalog(client, "Example Aggregates", "Sand", "28.1235")
        _catalog(client, "Sample Stone Co", "No. 57 Stone", "19.5")
        client.put("/api/rates", json={"supplier_id": client.get("/api/suppliers").json()[0]["id"],
                                       "material_id": client.get("/api/materials").json()[1]["id"], "unit_price": "33.3333"})

        sand = _upload_and_fill(client, project, _fictional_tickets("A", "Example Aggregates", "Sand"),
                                ["0.100", "0.200", "21.255", "19.999"])
        stone = _upload_and_fill(client, other_project, _fictional_tickets("B", "Sample Stone Co", "No. 57 Stone"),
                                 ["22.150", "21.900", "23.400", "18.125"])
        for ticket in sand[:3] + stone[:2]:
            _approve(client, ticket)
        client.post(f"/api/tickets/{sand[3]['id']}/flag", json={"note": "Check the scan"})
        client.post(f"/api/tickets/{stone[3]['id']}/skip")
        client.patch(f"/api/tickets/{stone[2]['id']}", json={"unit_price_override": "21.0000"})

        flagged_slot = next(slot for slot in client.get("/api/batches/1/manifest").json()["files"][0]["pages"][0]["slots"]
                            if slot["ticket_id"] == sand[3]["id"])
        assert client.post(f"/api/scan-slots/{flagged_slot['id']}/rotate", json={"rotation": 90}).status_code == 200
        assert client.patch("/api/batches/2/project", json={"project_id": project}).status_code == 200

        itemized = client.post("/api/invoice-comparisons", json={
            "project_id": project, "invoice_number": "EA-1", "supplier_name": "Example Aggregates", "material_name": "Sand",
            "date_from": "2026-09-10", "date_to": "2026-09-11", "billed_tons": "0.300", "billed_unit_price": "28.1235", "billed_subtotal": "8.44",
            "ticket_list": "A0 0.100\nA1 0.200"})
        assert itemized.status_code == 201, itemized.text
        line = itemized.json()
        linked = client.post(f"/api/invoice-comparisons/{line['id']}/ticket-links",
                             json={"ticket_ids": [row["ticket"]["ticket_id"] for row in line["ticket_check"] if row["line"] is not None]})
        assert linked.status_code == 200, linked.text
        totals_only = client.post("/api/invoice-comparisons", json={
            "project_id": project, "invoice_number": "EA-2", "supplier_name": "Example Aggregates", "material_name": "Sand",
            "date_from": "2026-09-12", "date_to": "2026-09-13", "billed_tons": "21.500", "billed_unit_price": "29", "billed_subtotal": "623.50"})
        assert totals_only.status_code == 201, totals_only.text
        corrected = client.put(f"/api/invoice-comparisons/{totals_only.json()['id']}", json={
            "project_id": project, "invoice_number": "EA-2", "supplier_name": "Example Aggregates", "material_name": "Sand",
            "date_from": "2026-09-12", "date_to": "2026-09-13", "billed_tons": "21.255", "billed_unit_price": "29", "billed_subtotal": "616.40"})
        assert corrected.status_code == 200, corrected.text

        document = client.post("/api/invoice-documents", data={"project_id": str(project)},
                               files={"file": ("invoice.pdf", make_invoice_pdf(), "application/pdf")})
        assert document.status_code == 201, document.text
        supplier_id = client.get("/api/suppliers").json()[0]["id"]
        assert client.post("/api/name-aliases", json={"kind": "supplier", "alias": "EA Inc", "target_id": supplier_id}).status_code == 201
        assert client.post("/api/saved-views", json={"name": "Our sheet", "filters": {"status": "verified"}, "columns": ["ticket_number", "tons"]}).status_code == 201
        assert client.post("/api/team/members", json={"username": "first-clerk", "display_name": "First Clerk", "role": "clerk"}).status_code == 201
        with Session() as db:
            db.add(ReviewTiming(organization_id=ids["first"], batch_id=1, intake_mode="manual", seconds=Decimal("41.5"), fields_corrected=2))
            db.commit()

        act_as(ids["second"], ids["second_owner"])
        their_project = client.post("/api/projects", json={"code": "26-100", "name": "Same Code, Other Company"}).json()["id"]
        _catalog(client, "Example Aggregates", "Sand", "31.0000")
        theirs = _upload_and_fill(client, their_project, _fictional_tickets("A", "Example Aggregates", "Sand")[::-1],
                                  ["5.555", "6.666", "7.777", "8.888"])
        for ticket in theirs[:2]:
            _approve(client, ticket)
        ids.update(projects=[project, other_project], their_project=their_project)
        return ids


def company_answers(client, ids: dict) -> dict:
    """Everything a person reads from a company's records, and the documents behind them."""
    answers = {}
    for name, organization, owner, projects in (("first", ids["first"], ids["first_owner"], ids["projects"]),
                                                ("second", ids["second"], ids["second_owner"], [ids["their_project"]])):
        act_as(organization, owner)
        lines = client.get("/api/invoice-comparisons").json()
        records = client.get("/api/records/search", params={"page_size": 200}).json()
        answers[name] = {
            "dashboard": client.get("/api/dashboard").json(),
            "dashboard_by_project": {p: client.get("/api/dashboard", params={"project_id": p}).json() for p in projects},
            "pilot": client.get("/api/reports/pilot").json(),
            "pilot_by_project": {p: client.get("/api/reports/pilot", params={"project_id": p}).json() for p in projects},
            "project_reports": {p: client.get(f"/api/reports/project/{p}").json() for p in projects},
            "invoice_lines": lines,
            "invoice_line_details": {line["id"]: client.get(f"/api/invoice-comparisons/{line['id']}").json() for line in lines},
            "invoice_overview": client.get("/api/invoice-comparisons/overview").json(),
            "records": records,
            "audit": {row["id"]: client.get(f"/api/tickets/{row['id']}/audit").json() for row in records["rows"]},
            "pricing_history": client.get("/api/pricing/history").json(),
            "documents": client.get("/api/invoice-documents").json(),
            "saved_views": client.get("/api/saved-views").json(),
        }
    return answers


def test_answers_dont_depend_on_which_database_holds_them(tmp_path, monkeypatch):
    """SQLite drops the time zone and sorts text differently from PostgreSQL. Left
    alone, a copied database would answer with different text for the same records."""
    path = tmp_path / "business.db"
    command.upgrade(alembic_config(f"sqlite:///{path}"), "head")
    ids = build_fictional_business(f"sqlite:///{path}", tmp_path / "storage", monkeypatch)
    with api_against(f"sqlite:///{path}", tmp_path / "storage", monkeypatch) as (client, Session):
        act_as(ids["first"], ids["first_owner"])
        with Session() as db:
            db.get(Ticket, 1).supplier = None  # Groups under "(unknown)", which sorts differently per database.
            db.commit()
        dashboard = client.get("/api/dashboard").json()
        names = [row["name"] for row in dashboard["by_supplier"]]
        assert "(unknown)" in names and names == sorted(names)
        assert all(row["created_at"].endswith("+00:00") for row in dashboard["recent_batches"])
        assert client.get("/api/batches").json()[0]["created_at"].endswith("Z")


def test_the_fictional_business_really_has_one_of_everything(tmp_path, monkeypatch):
    """Otherwise the copy test below would quietly prove less than it says."""
    path = tmp_path / "business.db"
    command.upgrade(alembic_config(f"sqlite:///{path}"), "head")
    build_fictional_business(f"sqlite:///{path}", tmp_path / "storage", monkeypatch)
    assert [table for table, rows in _row_counts(path).items() if not rows] == []


# ------------------------------------------------------------------ the real copy, on real PostgreSQL

@pytest.fixture()
def empty_postgres():
    """A new database at the current migration. Never the suite's own."""
    name = f"copy_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(normalized_url(TEST_DATABASE_URL), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    base, _, _ = TEST_DATABASE_URL.rpartition("/")
    url = f"{base}/{name}"
    command.upgrade(alembic_config(url), "head")
    try:
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _postgres_counts(url: str) -> dict[str, int]:
    engine = create_engine(normalized_url(url), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return {spec.name: connection.execute(text(f"SELECT COUNT(*) FROM {spec.name}")).scalar() for spec in migrate.table_specs()}
    finally:
        engine.dispose()


def _sequence_states(url: str) -> dict[str, tuple]:
    engine = create_engine(normalized_url(url), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            found = {}
            for spec in migrate.table_specs():
                if spec.has_id_sequence:
                    sequence = connection.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": spec.name}).scalar()
                    found[spec.name] = tuple(connection.execute(text(f"SELECT last_value, is_called FROM {sequence}")).one())
            return found
    finally:
        engine.dispose()


def _run_copy(source: Path, url: str, report: Path, *, after_copy=None) -> tuple[int, dict]:
    code = migrate.run(source_path=source, target_url=url, report_path=report, preflight_only=False, after_copy=after_copy)
    return code, json.loads(report.read_text())


@needs_postgres
def test_a_real_copy_answers_every_report_identically(tmp_path, monkeypatch, empty_postgres, capsys):
    source = tmp_path / "business.db"
    storage = tmp_path / "storage"
    command.upgrade(alembic_config(f"sqlite:///{source}"), "head")
    ids = build_fictional_business(f"sqlite:///{source}", storage, monkeypatch)
    with api_against(f"sqlite:///{source}", storage, monkeypatch) as (client, _Session):
        before = company_answers(client, ids)
    digest = migrate.file_digest(source)

    report_path = tmp_path / "report.json"
    code = migrate.main(["--source", str(source), "--target", empty_postgres, "--report", str(report_path)])
    report = json.loads(report_path.read_text())
    assert code == 0, capsys.readouterr().err
    assert report["outcome"] == "copied"
    assert migrate.file_digest(source) == digest
    assert report["source"]["sha256_before"] == report["source"]["sha256_after"] == digest
    assert _postgres_counts(empty_postgres) == _row_counts(source)
    verification = report["verification"]
    assert all(table["match"] and table["source_sha256"] == table["target_sha256"] for table in verification["tables"].values())
    assert verification["company_totals"]["match"] is True and verification["history"]["match"] is True
    assert set(verification["relationships"].values()) == {0} and verification["references_missing"] == 0
    assert verification["history"]["source"]["tables"]["audit_events"] > 0

    with api_against(empty_postgres, storage, monkeypatch) as (client, _Session):
        after = company_answers(client, ids)
        assert after == before
        # Dashboard totals agree with what the copy's own totals proved, to the cent.
        for name, organization in (("first", ids["first"]), ("second", ids["second"])):
            copied = verification["company_totals"]["target"][str(organization)]
            assert Decimal(copied["approved_tons"]) == Decimal(after[name]["dashboard"]["total_tons"])
            assert Decimal(copied["approved_cost"]) == Decimal(after[name]["dashboard"]["total_cost"])
        # The sequences moved past every copied id, so new records don't collide.
        act_as(ids["first"], ids["first_owner"])
        created = client.post("/api/projects", json={"code": "26-300", "name": "After The Copy"})
        assert created.status_code == 201, created.text
        assert created.json()["id"] == max(ids["projects"] + [ids["their_project"]]) + 1
        # A scan copied across is still found, because documents are looked up the same way.
        first_ticket = after["first"]["records"]["rows"][0]["id"]
        assert client.get(f"/api/tickets/{first_ticket}/image").status_code == 200


@needs_postgres
def test_a_second_copy_into_the_same_target_is_refused_and_changes_nothing(small_source, tmp_path, empty_postgres):
    code, _report = _run_copy(small_source, empty_postgres, tmp_path / "first.json")
    assert code == 0
    counts = _postgres_counts(empty_postgres)
    code, report = _run_copy(small_source, empty_postgres, tmp_path / "second.json")
    assert code == 1 and report["outcome"] == "rolled_back"
    assert "isn't empty" in report["problems"][0]
    assert _postgres_counts(empty_postgres) == counts


@needs_postgres
def test_a_target_behind_the_current_migration_is_refused(small_source, tmp_path, empty_postgres):
    command.stamp(alembic_config(empty_postgres), "20261105_17")
    code, report = _run_copy(small_source, empty_postgres, tmp_path / "r.json")
    assert code == 1 and "20261105_17" in report["problems"][0]
    assert _untouched(_postgres_counts(empty_postgres))


@needs_postgres
def test_an_unreachable_target_is_refused_without_printing_its_password(small_source, tmp_path, capsys):
    url = "postgresql://someone:hunter2hunter2@127.0.0.1:1/absent_test"
    code, report = _run_copy(small_source, url, tmp_path / "r.json")
    assert code == 1 and report["outcome"] == "refused"
    output = capsys.readouterr()
    assert "hunter2hunter2" not in output.out + output.err + (tmp_path / "r.json").read_text()


def _tamper_a_ticket(target):
    target.connection.execute(text("UPDATE tickets SET material_type = 'Tampered' WHERE id = 1"))


def _tamper_a_total(target):
    target.connection.execute(text("ALTER TABLE tickets DISABLE TRIGGER tickets_approved_figures_locked"))
    target.connection.execute(text("UPDATE tickets SET approved_cost = approved_cost + 0.01 WHERE id = 1"))


def _tamper_an_extra_history_row(target):
    target.connection.execute(text(
        "INSERT INTO audit_events (ticket_id, field, created_at) VALUES (1, 'status', '2026-10-01 00:00:00+00')"))


def _tamper_a_sequence(target):
    sequence = target.connection.execute(text("SELECT pg_get_serial_sequence('tickets', 'id')")).scalar()
    target.connection.execute(text(f"ALTER SEQUENCE {sequence} RESTART WITH 5000"))


def _crash(_target):
    raise RuntimeError("the machine lost power")


@needs_postgres
@pytest.mark.parametrize("tamper, expected", [
    (_tamper_a_ticket, "tickets: row contents differ"),
    (_tamper_a_total, "Approved tons or cost differ"),
    (_tamper_an_extra_history_row, "Audit history counts differ"),
    (_tamper_a_sequence, "An id sequence"),
    (_crash, "RuntimeError"),
])
def test_tampering_midway_rolls_back_everything(small_source, tmp_path, empty_postgres, tamper, expected):
    # One approved, priced ticket, so the locked-figure trigger and the totals have work to do.
    _change(small_source, "UPDATE tickets SET status = 'verified', tonnage_confirmed = 1, approved_cost = 100.50, "
                          "price_snapshot_state = 'priced', applied_unit_price = 4.9024 WHERE id = 1")
    sequences = _sequence_states(empty_postgres)
    code, report = _run_copy(small_source, empty_postgres, tmp_path / "r.json", after_copy=tamper)
    assert code == 1 and report["outcome"] == "rolled_back"
    assert expected in " ".join(report["problems"])
    assert _untouched(_postgres_counts(empty_postgres))
    # Rolled back as well: setval would have left these moved, on an empty database.
    assert _sequence_states(empty_postgres) == sequences


@needs_postgres
def test_a_crash_inside_a_later_table_keeps_the_earlier_tables_out_too(small_source, tmp_path, empty_postgres, monkeypatch):
    real_insert = migrate.PostgresTarget.insert

    def fail_on_tickets(self, spec, rows):
        if spec.name == "tickets":
            raise RuntimeError("the machine lost power")
        real_insert(self, spec, rows)

    monkeypatch.setattr(migrate.PostgresTarget, "insert", fail_on_tickets)
    code, report = _run_copy(small_source, empty_postgres, tmp_path / "r.json")
    assert code == 1 and report["outcome"] == "rolled_back"
    assert _untouched(_postgres_counts(empty_postgres))


@needs_postgres
def test_a_source_changed_midway_rolls_back(small_source, tmp_path, empty_postgres):
    def someone_writes(_target):
        _change(small_source, "UPDATE projects SET name = 'Changed' WHERE id = 1")

    code, report = _run_copy(small_source, empty_postgres, tmp_path / "r.json", after_copy=someone_writes)
    assert code == 1 and "The source file changed" in " ".join(report["problems"])
    assert _untouched(_postgres_counts(empty_postgres))


@needs_postgres
def test_a_refusal_from_postgresql_never_quotes_a_value(small_source, tmp_path, empty_postgres, capsys, monkeypatch):
    """PostgreSQL's own error text can quote a row. Only the kind of error is reported."""
    _change(small_source, f"UPDATE projects SET name = '{SECRET}' WHERE id = 1")
    real_insert = migrate.PostgresTarget.insert

    def collide(self, spec, rows):
        real_insert(self, spec, rows)
        if spec.name == "projects":
            real_insert(self, spec, rows)  # Same primary keys again, so the database refuses.

    monkeypatch.setattr(migrate.PostgresTarget, "insert", collide)
    code, report = _run_copy(small_source, empty_postgres, tmp_path / "r.json")
    assert code == 1 and report["outcome"] == "rolled_back"
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err + (tmp_path / "r.json").read_text()
    assert _untouched(_postgres_counts(empty_postgres))
