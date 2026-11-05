"""Several employees acting at the same moment, on real connections.

Each race is forced, not left to chance: requests pause at the most dangerous
point until the other arrives. Runs on a real file database with SQLite, and on
real transactions with PostgreSQL when TEST_DATABASE_URL is set.
"""
import threading
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.auth import current_user
from app.db import Base, enforce_references, engine_options
from app.main import app
from app.models import (
    AuditEvent, Batch, InvoiceComparison, InvoiceDocument, Material, Organization, Project, Supplier, SupplierRate, Ticket, UploadedFile, User,
)
from app.routers import invoices as invoices_router
from app.services import pricing as pricing_svc

from .conftest import TEST_DATABASE_URL, _reset_postgres
from .sample_invoices import letterhead_supplier


class Race:
    """Real threads, real connections, and a meeting point inside the request."""

    def __init__(self, session_factory, client):
        self.Session = session_factory
        self.client = client

    def run(self, *calls):
        results = [None] * len(calls)

        def call(index, fn):
            results[index] = fn()

        threads = [threading.Thread(target=call, args=(i, fn)) for i, fn in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not any(thread.is_alive() for thread in threads), "A request never finished: a deadlock"
        return results

    @contextmanager
    def meeting_at(self, monkeypatch, owner, name, parties=2):
        """Everyone calling owner.name waits for the others, so all have finished
        their checks before any can commit. Gives up after a moment, because with
        a correct lock the others can't arrive until the first one is done."""
        original = getattr(owner, name)
        barrier = threading.Barrier(parties)

        def waiting(*args, **kwargs):
            try:
                barrier.wait(timeout=1.5)
            except threading.BrokenBarrierError:
                pass
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, waiting)
        try:
            yield
        finally:
            monkeypatch.setattr(owner, name, original)


@pytest.fixture()
def race(tmp_path, monkeypatch, postgres_engine):
    from app.config import settings
    if postgres_engine is not None:
        engine = postgres_engine
        _reset_postgres(engine)
    else:
        url = f"sqlite:///{tmp_path / 'race.db'}"
        engine = create_engine(url, **engine_options(url))
        enforce_references(engine)
        Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as db:
        db.add(Organization(id=1, name="Fictional"))
        db.flush()
        db.add(User(id=1, organization_id=1, username="a-manager", display_name="A Manager", password_hash="x", role="owner"))
        db.add(Project(id=1, organization_id=1, code="P1", name="Fictional"))
        db.flush()
        db.add(Batch(id=1, organization_id=1, project_id=1, layout="single", status="ready_for_review", intake_mode="manual"))
        db.flush()
        db.add(UploadedFile(id=1, batch_id=1, original_name="f.pdf", storage_path="x", sha256="0" * 64, status="ready_for_review"))
        db.commit()
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr("app.main.engine", engine)

    def override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[db_module.get_db] = override
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="a-manager", display_name="A Manager", password_hash="x", active=True, role="owner", must_change_password=False)
    with TestClient(app, headers={"X-TicketFlow-Request": "1"}) as client:
        yield Race(Session, client)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)
    if postgres_engine is None:
        engine.dispose()


def _ticket(race, position, *, number="1000", sha=None, tons="20.000", status="needs_review", **fields) -> int:
    with race.Session() as db:
        ticket = Ticket(
            batch_id=1, file_id=1, source_page=1, position=position, crop_box=[0, 0, 1, 1], crop_path="x",
            crop_sha256=sha or f"{position:064d}", ticket_number=number, delivery_date=date(2026, 9, 1), material_type="Sand",
            supplier="Example Aggregates", job_location="Lot", tonnage=Decimal(tons), flags=[], status=status,
            **fields)
        db.add(ticket)
        db.commit()
        return ticket.id


def _rate(race, price="10"):
    with race.Session() as db:
        supplier = db.scalar(select(Supplier).where(Supplier.name == "Example Aggregates")) or Supplier(organization_id=1, name="Example Aggregates")
        material = db.scalar(select(Material).where(Material.name == "Sand")) or Material(organization_id=1, name="Sand")
        db.add_all([supplier, material])
        db.flush()
        db.add(SupplierRate(organization_id=1, supplier_id=supplier.id, material_id=material.id, unit_price=Decimal(price)))
        db.commit()


def _state(race, ticket_id):
    with race.Session() as db:
        return db.get(Ticket, ticket_id)


def _verified_tons(race) -> Decimal:
    with race.Session() as db:
        return sum((t.tonnage for t in db.scalars(select(Ticket).where(Ticket.status == "verified", Ticket.tonnage_confirmed.is_(True)))), Decimal(0))


def test_two_managers_cant_both_approve_copies_of_one_scan(race, monkeypatch):
    """The double count that matters most: the same paper ticket uploaded twice."""
    first = _ticket(race, 0, number="A", sha="f" * 64)
    second = _ticket(race, 1, number="B", sha="f" * 64)
    _rate(race)
    with race.meeting_at(monkeypatch, pricing_svc, "rate_table"):
        results = race.run(lambda: race.client.post(f"/api/tickets/{first}/verify"),
                           lambda: race.client.post(f"/api/tickets/{second}/verify"))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert _verified_tons(race) == Decimal("20.000")


def test_a_save_and_an_approval_at_the_same_moment_cant_approve_unseen_tons(race, monkeypatch):
    """One person corrects the tons while another approves what they were looking at."""
    ticket_id = _ticket(race, 0)
    _rate(race)
    revision = race.client.get("/api/batches/1/tickets").json()[0]["revision"]
    with race.meeting_at(monkeypatch, pricing_svc, "rate_table"):
        approve, edit = race.run(
            lambda: race.client.post(f"/api/tickets/{ticket_id}/verify", headers={"If-Match": revision}),
            lambda: race.client.patch(f"/api/tickets/{ticket_id}", json={"tonnage": "99.000"}, headers={"If-Match": revision}),
        )
    assert sorted((approve.status_code, edit.status_code)) in ([200, 409],)
    ticket = _state(race, ticket_id)
    if approve.status_code == 200:
        assert ticket.tonnage == Decimal("20.000") and ticket.status == "verified" and ticket.tonnage_confirmed
    else:
        assert ticket.tonnage == Decimal("99.000") and ticket.status == "needs_review" and not ticket.tonnage_confirmed


def test_approving_after_a_rate_change_you_didnt_see_is_refused(race):
    """The price locks at approval, so it has to be the price on the person's screen."""
    ticket_id = _ticket(race, 0)
    _rate(race, "10")
    seen = race.client.get("/api/batches/1/tickets").json()[0]
    assert Decimal(seen["unit_price"]) == 10
    with race.Session() as db:
        db.scalar(select(SupplierRate)).unit_price = Decimal("99")
        db.commit()
    response = race.client.post(f"/api/tickets/{ticket_id}/verify", headers={"If-Match": seen["revision"]})
    assert response.status_code == 409
    assert _state(race, ticket_id).status == "needs_review"
    fresh = race.client.get("/api/batches/1/tickets").json()[0]
    locked = race.client.post(f"/api/tickets/{ticket_id}/verify", headers={"If-Match": fresh["revision"]}).json()
    assert Decimal(locked["unit_price"]) == 99


def test_many_simultaneous_approvals_record_one_approval_and_one_price(race, monkeypatch):
    ticket_id = _ticket(race, 0)
    _rate(race)
    revision = race.client.get("/api/batches/1/tickets").json()[0]["revision"]
    with race.meeting_at(monkeypatch, pricing_svc, "rate_table", parties=8):
        results = race.run(*[lambda: race.client.post(f"/api/tickets/{ticket_id}/verify", headers={"If-Match": revision})] * 8)
    assert set(r.status_code for r in results) <= {200, 409}
    with race.Session() as db:
        approvals = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.ticket_id == ticket_id, AuditEvent.new_value == "verified"))
    assert approvals == 1
    assert _verified_tons(race) == Decimal("20.000")


def test_simultaneous_rate_saves_leave_one_rate(race, monkeypatch):
    """Two rows for one supplier and material would make the locked price arbitrary."""
    with race.Session() as db:
        db.add_all([Supplier(id=1, organization_id=1, name="Example Aggregates"), Material(id=1, organization_id=1, name="Sand")])
        db.commit()
    body = lambda price: {"supplier_id": 1, "material_id": 1, "unit_price": price}  # noqa: E731
    results = race.run(*[(lambda p=price: race.client.put("/api/rates", json=body(str(p)))) for price in (10, 20, 30, 40)])
    assert all(r.status_code in (200, 409) for r in results), [r.status_code for r in results]
    with race.Session() as db:
        assert db.scalar(select(func.count()).select_from(SupplierRate)) == 1


LINE = {"project_id": 1, "invoice_number": "I-1", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-30", "billed_tons": "20", "billed_unit_price": "10", "billed_subtotal": "200"}


def test_two_overlapping_invoice_lines_cant_both_be_saved(race, monkeypatch):
    """One delivery counted against two lines would make both look paid for."""
    _ticket(race, 0, status="verified", tonnage_confirmed=True, price_snapshot_state="priced", applied_unit_price=Decimal("10"))
    with race.meeting_at(monkeypatch, invoices_router, "_parse_tickets"):
        results = race.run(lambda: race.client.post("/api/invoice-comparisons", json=LINE),
                           lambda: race.client.post("/api/invoice-comparisons", json={**LINE, "invoice_number": "I-2"}))
    assert sorted(r.status_code for r in results) == [201, 409]
    with race.Session() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceComparison)) == 1


def test_two_people_correcting_one_invoice_line_cant_silently_overwrite(race):
    created = race.client.post("/api/invoice-comparisons", json=LINE).json()
    revision = created["revision"]
    first = race.client.put(f"/api/invoice-comparisons/{created['id']}", json={**LINE, "billed_tons": "21", "billed_subtotal": "210"}, headers={"If-Match": revision})
    second = race.client.put(f"/api/invoice-comparisons/{created['id']}", json={**LINE, "billed_tons": "22", "billed_subtotal": "220"}, headers={"If-Match": revision})
    assert first.status_code == 200 and second.status_code == 409
    assert second.json()["detail"]["code"] == "line_changed"
    assert Decimal(race.client.get(f"/api/invoice-comparisons/{created['id']}").json()["billed_tons"]) == 21


def test_the_same_invoice_file_uploaded_twice_at_once_is_kept_once(race, monkeypatch):
    from app.routers import invoice_documents
    pdf = letterhead_supplier()

    def send():
        return race.client.post("/api/invoice-documents", files={"file": ("same.pdf", pdf, "application/pdf")})

    with race.meeting_at(monkeypatch, invoice_documents.invoice_extraction, "get_invoice_extractor"):
        results = race.run(send, send)
    assert sorted(r.status_code for r in results) == [201, 409]
    with race.Session() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceDocument)) == 1


def test_a_second_ticket_cant_be_created_for_one_physical_slot(race):
    """A retried worker and a person acting at once mustn't produce two records of one ticket."""
    from sqlalchemy.exc import IntegrityError
    _ticket(race, 0)
    with race.Session() as db:
        db.add(Ticket(batch_id=1, file_id=1, source_page=1, position=0, crop_box=[0, 0, 1, 1], crop_path="x", crop_sha256="e" * 64, flags=[], status="needs_review"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_a_second_approval_of_the_same_supplier_ticket_number_needs_a_deliberate_override(race):
    first = _ticket(race, 0, number="10482", sha="a" * 64)
    second = _ticket(race, 1, number="10482", sha="b" * 64, tons="21.000")
    assert race.client.post(f"/api/tickets/{first}/verify").status_code == 200
    refused = race.client.post(f"/api/tickets/{second}/verify")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "possible_duplicate"
    assert _verified_tons(race) == Decimal("20.000")
    allowed = race.client.post(f"/api/tickets/{second}/verify", json={"confirm_duplicate": True})
    assert allowed.status_code == 200
    history = race.client.get(f"/api/tickets/{second}/audit").json()
    assert any(event["field"] == "duplicate_override" and event["actor"] == "A Manager" for event in history)


def test_two_racing_approvals_of_one_ticket_number_need_the_same_override(race, monkeypatch):
    first = _ticket(race, 0, number="10482", sha="a" * 64)
    second = _ticket(race, 1, number="10482", sha="b" * 64, tons="21.000")
    _rate(race)
    with race.meeting_at(monkeypatch, pricing_svc, "rate_table"):
        results = race.run(lambda: race.client.post(f"/api/tickets/{first}/verify"),
                           lambda: race.client.post(f"/api/tickets/{second}/verify"))
    assert sorted(r.status_code for r in results) == [200, 409]


def test_reports_stay_consistent_while_people_edit_and_approve(race):
    """Many tickets, many people, one truth afterwards."""
    ids = [_ticket(race, n, number=f"N{n}") for n in range(12)]
    _rate(race)

    def worker(ticket_id, n):
        def act():
            outcomes = []
            for step in range(3):
                revision = next(t for t in race.client.get("/api/batches/1/tickets").json() if t["id"] == ticket_id)["revision"]
                if (n + step) % 3 == 0:
                    outcomes.append(race.client.post(f"/api/tickets/{ticket_id}/verify", headers={"If-Match": revision}).status_code)
                elif (n + step) % 3 == 1:
                    outcomes.append(race.client.patch(f"/api/tickets/{ticket_id}", json={"tonnage": str(20 + step)}, headers={"If-Match": revision}).status_code)
                else:
                    outcomes.append(race.client.post(f"/api/tickets/{ticket_id}/verify", headers={"If-Match": revision}).status_code)
            return outcomes
        return act

    results = race.run(*[worker(ticket_id, n) for n, ticket_id in enumerate(ids)])
    assert all(code in (200, 409, 503) for outcome in results for code in outcome), results
    tickets = race.client.get("/api/batches/1/tickets").json()
    tons = sum((Decimal(t["tonnage"]) for t in tickets if t["status"] == "verified"), Decimal(0))
    cost = sum((Decimal(t["cost"]) for t in tickets if t["status"] == "verified"), Decimal(0))
    dashboard = race.client.get("/api/dashboard").json()
    assert Decimal(dashboard["total_tons"]) == tons
    assert Decimal(dashboard["total_cost"]) == cost
    for ticket in tickets:
        if ticket["status"] == "verified":
            assert ticket["tonnage_confirmed"] and ticket["verified_by"] == "A Manager"
