"""The cost of a person's edit, approval or list view mustn't grow with history.

Everything here counts SQL statements or rows loaded, not seconds, so the tests
don't depend on the machine.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app import db as db_module
from app.main import app
from app.models import Batch, InvoiceComparison, InvoiceTicketRef, Project, Ticket, UploadedFile
from app.services import duplicates


def _session():
    return next(app.dependency_overrides[db_module.get_db]())


def _seed(count: int, *, identical: bool = False, organization_id: int = 1, batch_id: int = 1, start: int = 0) -> None:
    with _session() as db:
        if db.get(Project, organization_id) is None:
            db.add(Project(id=organization_id, organization_id=organization_id, code=f"P{organization_id}", name="Fictional"))
            db.flush()
        db.add(Batch(id=batch_id, organization_id=organization_id, project_id=organization_id, layout="single",
                     status="ready_for_review", intake_mode="manual"))
        db.flush()
        db.add(UploadedFile(id=batch_id, batch_id=batch_id, original_name="f.pdf", storage_path="x", sha256="0" * 64,
                            status="ready_for_review"))
        db.flush()
        db.add_all(Ticket(
            batch_id=batch_id, file_id=batch_id, source_page=n, position=0, crop_box=[0, 0, 1, 1], crop_path="x",
            crop_sha256=f"{batch_id:08d}{n + start:056d}", ticket_number=str(10000 + n + start),
            delivery_date=date(2026, 10, 15) if identical else date(2026, 9, 1) + timedelta(days=(n + start) % 28),
            material_type="Sand", supplier="Example Aggregates", job_location="Lot",
            tonnage=Decimal("20.500") if identical else Decimal(1500 + n + start) / 100, flags=[],
            status="needs_review",
        ) for n in range(count))
        db.commit()


class _Count:
    """How many statements ran, and how many Ticket rows were materialized."""

    def __enter__(self):
        self.engine = _session().get_bind()
        self.statements = 0
        self.tickets = 0
        event.listen(self.engine, "before_cursor_execute", self._statement)
        event.listen(Session, "loaded_as_persistent", self._loaded)
        return self

    def __exit__(self, *_):
        event.remove(self.engine, "before_cursor_execute", self._statement)
        event.remove(Session, "loaded_as_persistent", self._loaded)

    def _statement(self, *_):
        self.statements += 1

    def _loaded(self, _session, instance):
        if isinstance(instance, Ticket):
            self.tickets += 1


@pytest.mark.parametrize("action", ["patch", "verify"])
def test_edit_and_approve_read_only_what_could_be_a_duplicate(client, action):
    _seed(400)
    call = (lambda: client.patch("/api/tickets/5", json={"job_location": "Lot B"})) if action == "patch" \
        else (lambda: client.post("/api/tickets/5/verify"))
    with _Count() as count:
        assert call().status_code == 200
    # Before the targeted lookup, this loaded all 400.
    assert count.tickets <= 3


def test_edit_cost_does_not_grow_with_company_history(client):
    _seed(20)
    with _Count() as small:
        client.patch("/api/tickets/5", json={"job_location": "Lot B"})
    _seed(1000, batch_id=2, start=20)
    with _Count() as large:
        client.patch("/api/tickets/5", json={"job_location": "Lot C"})
    assert large.statements == small.statements
    assert large.tickets == small.tickets


def test_batch_list_reads_only_matches_outside_the_batch(client):
    _seed(10)
    _seed(500, batch_id=2, start=10)
    with _Count() as count:
        assert len(client.get("/api/batches/1/tickets").json()) == 10
    assert count.tickets <= 15


def test_identical_loads_are_bounded_and_still_reported(client):
    _seed(300, identical=True)
    response = client.get("/api/batches/1/tickets")
    assert response.status_code == 200
    reasons = response.json()[150]["duplicates"]
    assert len(reasons) == duplicates.MAX_REASONS + 1
    assert all("Same supplier, date, material and tonnage" in r for r in reasons[:-1])
    assert "at least 289 more" in reasons[-1]


def test_small_duplicate_groups_are_listed_exactly(client):
    _seed(3, identical=True)
    reasons = client.get("/api/batches/1/tickets").json()[0]["duplicates"]
    assert len(reasons) == 2
    assert "ticket 1" in reasons[0] and "more" not in reasons[-1]


def test_targeted_duplicates_never_cross_companies(client):
    """Fictional other company with identical documents must stay invisible."""
    with _session() as db:
        from app.models import Organization
        db.add(Organization(id=2, name="Another Company"))
        db.commit()
    _seed(2, identical=True)
    _seed(2, identical=True, organization_id=2, batch_id=2)
    with _session() as db:
        for ticket in db.query(Ticket).filter(Ticket.batch_id == 2):
            ticket.crop_sha256 = db.query(Ticket).filter(Ticket.batch_id == 1).first().crop_sha256
        db.commit()
    tickets = client.get("/api/batches/1/tickets").json()
    assert all("batch 2" not in reason for t in tickets for reason in t["duplicates"])


def _invoice_lines(lines: int, tickets_each: int = 5) -> None:
    with _session() as db:
        for line in range(lines):
            day = date(2026, 9, 1) + timedelta(days=line)
            item = InvoiceComparison(
                organization_id=1, project_id=1, invoice_number=f"I-{line}", supplier_name="Example Aggregates",
                supplier_key="example aggregates", material_name="Sand", material_key="sand", date_from=day, date_to=day,
                billed_tons=Decimal("10"), billed_unit_price=Decimal("30"), billed_subtotal=Decimal("300"))
            db.add(item)
            db.flush()
            db.add_all(InvoiceTicketRef(comparison_id=item.id, position=i + 1, ticket_number=str(10000 + line + i),
                                        ticket_key=str(10000 + line + i)) for i in range(tickets_each))
        db.commit()


def test_invoice_list_query_count_does_not_grow_with_lines(client):
    _seed(60)
    _invoice_lines(2)
    with _Count() as few:
        assert len(client.get("/api/invoice-comparisons").json()) == 2
    _invoice_lines(0)
    with _session() as db:
        db.query(InvoiceTicketRef).delete()
        db.query(InvoiceComparison).delete()
        db.commit()
    _invoice_lines(20)
    with _Count() as many:
        assert len(client.get("/api/invoice-comparisons").json()) == 20
    assert many.statements == few.statements


def test_list_and_single_read_compute_the_same_comparison(client):
    """The shared context is only an optimization: every figure must agree."""
    _seed(60)
    _invoice_lines(8)
    listed = client.get("/api/invoice-comparisons").json()
    assert len(listed) == 8
    for line in listed:
        assert client.get(f"/api/invoice-comparisons/{line['id']}").json() == line


def test_dashboard_and_pilot_totals_unchanged_by_lightweight_rows(client):
    _seed(10)
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    assert client.post("/api/tickets/3/verify").status_code == 200
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["tickets"] == 10
    assert dashboard["verified"] == 1
    assert Decimal(dashboard["total_tons"]) == Decimal("15.02")
    assert dashboard["by_supplier"][0]["name"] == "Example Aggregates"
    report = client.get("/api/reports/pilot").json()
    assert report["review"]["verified"] == 1
    assert Decimal(report["review"]["verified_tons"]) == Decimal("15.02")
