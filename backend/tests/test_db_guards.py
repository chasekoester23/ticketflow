"""Rules the database enforces by itself, and the arithmetic everything relies on."""
import sqlite3
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import engine_options
from app.models import AuditEvent, SupplierRate, Ticket
from app.services import pricing, system_health
from app.services.pricing import to_cents

from .conftest import TEST_DATABASE_URL
from .migration_helpers import alembic_config
from .test_query_budgets import _seed, _session


def _rated(client, rate="10"):
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/materials", json={"name": "Sand"})
    client.put("/api/rates", json={"supplier_id": client.get("/api/suppliers").json()[0]["id"],
                                   "material_id": client.get("/api/materials").json()[0]["id"], "unit_price": rate})


# ------------------------------------------------------------ history can't be rewritten

def test_ticket_history_cant_be_edited_or_deleted_even_by_direct_database_access(client):
    _seed(1)
    client.patch("/api/tickets/1", json={"tonnage": "20"})
    with _session() as db:
        event = db.query(AuditEvent).first()
        event.new_value = "99"
        with pytest.raises(DBAPIError):
            db.commit()
        db.rollback()
    with _session() as db:
        db.delete(db.query(AuditEvent).first())
        with pytest.raises(DBAPIError):
            db.commit()
        db.rollback()
    assert client.get("/api/tickets/1/audit").json()[0]["new"] == "20.000"


@pytest.mark.parametrize("table", ["audit_events", "pricing_events", "account_events", "scan_events",
                                   "invoice_comparison_events", "invoice_document_events"])
def test_every_history_table_is_append_only(client, table):
    """Each table has its guard. audit_events is exercised end to end above."""
    assert any(name.startswith(table) for name in _trigger_names()), f"{table} has no guard"


def _trigger_names():
    with _session() as db:
        if db.get_bind().dialect.name == "sqlite":
            return [row[0] for row in db.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))]
        return [row[0] for row in db.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))]


# ------------------------------------------------------------ approved figures

def test_approved_tons_and_price_cant_change_while_it_stays_approved(client):
    _seed(1)
    _rated(client)
    client.post("/api/tickets/1/verify")
    for change in ({"tonnage": Decimal("99")}, {"applied_unit_price": Decimal("99")}):
        with _session() as db:
            ticket = db.get(Ticket, 1)
            for name, value in change.items():
                setattr(ticket, name, value)
            with pytest.raises(DBAPIError):
                db.commit()
            db.rollback()
    with _session() as db:
        ticket = db.get(Ticket, 1)
        assert ticket.tonnage == Decimal("15.000") and ticket.applied_unit_price == Decimal("10")


def test_withdrawing_approval_while_changing_tons_is_allowed(client):
    _seed(1)
    _rated(client)
    client.post("/api/tickets/1/verify")
    edited = client.patch("/api/tickets/1", json={"tonnage": "99"}).json()
    assert edited["status"] == "needs_review" and Decimal(edited["tonnage"]) == 99


def test_a_direct_second_approval_of_one_scan_is_refused_by_the_database(client):
    _seed(2)
    with _session() as db:
        first, second = db.get(Ticket, 1), db.get(Ticket, 2)
        second.crop_sha256 = first.crop_sha256
        first.status = "verified"
        second.status = "verified"
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_two_rates_for_one_pair_are_refused_by_the_database(client):
    _rated(client)
    with _session() as db:
        rate = db.query(SupplierRate).first()
        db.add(SupplierRate(organization_id=1, supplier_id=rate.supplier_id, material_id=rate.material_id, unit_price=Decimal("5")))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_new_tickets_carry_their_company(client):
    _seed(1)
    with _session() as db:
        assert db.get(Ticket, 1).organization_id == 1


# ------------------------------------------------------------ refusals people can act on

def test_a_stale_edit_that_loses_at_commit_gets_a_clear_answer(client, monkeypatch):
    """Two saves crossing between read and write: the loser is told, and nothing half-saves."""
    from app.routers import api as api_module
    _seed(1)
    original = api_module._refresh_flags

    def another_save_arrives(ticket):
        original(ticket)
        with _session() as db:
            db.get(Ticket, ticket.id).job_location = "Saved by someone else"
            db.commit()

    monkeypatch.setattr(api_module, "_refresh_flags", another_save_arrives)
    response = client.patch("/api/tickets/1", json={"tonnage": "33"})
    monkeypatch.setattr(api_module, "_refresh_flags", original)
    if TEST_DATABASE_URL:
        return  # On PostgreSQL the row lock makes the other save wait instead.
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "record_changed"
    ticket = client.get("/api/batches/1/tickets").json()[0]
    assert ticket["job_location"] == "Saved by someone else" and Decimal(ticket["tonnage"]) == Decimal("15.000")
    assert client.get("/api/tickets/1/audit").json() == []  # The losing save's history went with it.


@pytest.mark.parametrize("path", ["/api/tickets/99999999999999999999/audit", "/api/batches/99999999999999999999",
                                  "/api/invoice-comparisons/99999999999999999999"])
def test_absurd_ids_get_a_clean_answer(client, path):
    assert client.get(path).status_code in (404, 422)


def test_text_with_a_null_byte_is_refused_cleanly(client):
    _seed(1)
    response = client.patch("/api/tickets/1", json={"supplier": "Bad\u0000Name"})
    assert response.status_code in (200, 422)
    assert client.get("/api/batches/1/tickets").status_code == 200


@pytest.mark.parametrize("field", ["ticket_number", "material_type", "supplier", "job_location"])
def test_overlong_ticket_text_is_refused_rather_than_cut(client, field):
    _seed(1)
    assert client.patch("/api/tickets/1", json={field: "x" * 5000}).status_code == 422


# ------------------------------------------------------------ rounding

@pytest.mark.parametrize("tons, rate, expected", [
    ("21.250", "28.5000", "605.63"),  # 605.625: half a cent rounds up, as suppliers do
    ("20.125", "1.0000", "20.13"),
    ("10.000", "1.0005", "10.01"),   # 10.005
    ("22.150", "28.5000", "631.28"),
    ("0.001", "0.0001", "0.00"),
])
def test_half_a_cent_rounds_up(tons, rate, expected):
    assert to_cents(Decimal(tons) * Decimal(rate)) == Decimal(expected)


def test_a_half_cent_ticket_matches_the_suppliers_invoice_to_the_cent(client):
    """Rounding to even made this show a one-cent difference that didn't exist."""
    _seed(1)
    _rated(client, "28.5")
    client.patch("/api/tickets/1", json={"tonnage": "21.250"})
    verified = client.post("/api/tickets/1/verify").json()
    assert verified["cost"] == "605.63"
    line = client.post("/api/invoice-comparisons", json={
        "project_id": 1, "invoice_number": "I", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-30", "billed_tons": "21.250", "billed_unit_price": "28.5", "billed_subtotal": "605.63"}).json()
    assert Decimal(line["subtotal_difference"]) == 0 and Decimal(line["invoice_arithmetic_difference"]) == 0
    assert line["status"] == "matches"


def test_every_cost_in_the_code_goes_through_one_rounding_rule():
    """A stray quantize would quietly bring back rounding to even."""
    from pathlib import Path
    root = Path(pricing.__file__).parents[1]
    offenders = [str(path.relative_to(root)) for path in root.rglob("*.py")
                 if "quantize(CENTS" in path.read_text() and path.name != "pricing.py"]
    assert offenders == []


# ------------------------------------------------------------ the migration, on old data

@pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="Builds a SQLite file by hand")
def test_old_data_with_duplicates_is_left_alone_and_reported(tmp_path, client):
    path = tmp_path / "old.db"
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "20261105_17")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO batches (id, organization_id, layout, intake_mode, status, created_at) VALUES (1, 1, 'single', 'manual', 'ready_for_review', '2026-01-01')")
        db.execute("INSERT INTO uploaded_files (id, batch_id, original_name, storage_path, sha256, status) VALUES (1, 1, 'f.pdf', 'x', 'y', 'ready_for_review')")
        for position in (0, 1):
            db.execute("INSERT INTO tickets (id, batch_id, file_id, source_page, position, crop_box, crop_path, crop_sha256, flags, status, tonnage, tonnage_confirmed, created_at) "
                       "VALUES (?, 1, 1, 1, ?, '[]', 'x', 'same', '[]', 'verified', 10, 1, '2026-01-01')", (position + 1, position))
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url, **engine_options(url))
    names = {index["name"] for index in inspect(engine).get_indexes("tickets")}
    assert "uq_tickets_approved_scan" not in names  # Can't be created over these rows...
    assert "uq_tickets_slot" in names               # ...and the others still are.
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM tickets WHERE status = 'verified'").fetchone()[0] == 2  # Nothing changed.
        assert db.execute("SELECT DISTINCT organization_id FROM tickets").fetchall() == [(1,)]           # New column filled.
    from sqlalchemy.orm import sessionmaker
    with sessionmaker(bind=engine)() as db:
        assert system_health.missing_safeguards(db) == ["uq_tickets_approved_scan"]
    engine.dispose()


def test_system_health_reports_the_safeguards_are_on(client):
    checks = client.get("/api/system/status").json()["checks"]
    safeguards = next(c for c in checks if c["name"] == "Duplicate safeguards")
    assert safeguards["state"] == "ok"
