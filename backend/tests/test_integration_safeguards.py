"""Safeguards added while integrating the branches, each answering a bug a test found.

The tests for the bugs themselves live in test_t_failures.py and test_t_odd_data.py.
These check the other side: that each safeguard doesn't refuse what it shouldn't.
"""
from datetime import date
from decimal import Decimal

import pymupdf
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Material, Supplier, SupplierRate

from .sample_data import TICKETS, make_page, make_pdf
from .test_query_budgets import _session
from .test_t_failures import _long_invoice
from .test_t_odd_data import _catalog, _deliveries


def _untidy(whole: bytes, how: str = "wrong index") -> bytes:
    """A complete file MuPDF has to rebuild: its index points at the wrong place, as
    some scanners write, or something has been added after its end."""
    untidy = whole.replace(b"startxref\n", b"startxref\n1", 1) if how == "wrong index" else whole + b"\n%" + b"x" * 50_000
    assert untidy != whole
    with pymupdf.open(stream=untidy, filetype="pdf") as doc:
        assert doc.is_repaired, "the case the cut-off check has to let through"
    return untidy


@pytest.mark.parametrize("how", ["wrong index", "junk after the end"])
def test_a_complete_but_untidy_scan_is_still_read_in_full(client, how):
    project = client.post("/api/projects", json={"code": "T1", "name": "Fictional"}).json()["id"]
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(project)},
                           files=[("files", ("untidy.pdf", _untidy(make_pdf([make_page(TICKETS), make_page(TICKETS)]), how),
                                             "application/pdf"))])
    assert response.status_code == 200, response.text
    batch = client.get(f"/api/batches/{response.json()['id']}").json()
    assert batch["status"] != "failed", batch
    assert len(client.get(f"/api/batches/{batch['id']}/tickets").json()) == 8


def test_a_complete_invoice_with_an_untidy_index_is_still_read_in_full(client):
    response = client.post("/api/invoice-documents", files={"file": ("untidy.pdf", _untidy(_long_invoice()), "application/pdf")})
    assert response.status_code == 201, response.text
    assert sum(Decimal(line["billed_quantity"]) for line in response.json()["lines"]) == Decimal("150.000")


def _twin_material(price: str) -> None:
    """Saved before names were checked: 'SAND' beside 'Sand', with its own rate."""
    with _session() as db:
        supplier = db.scalar(select(Supplier))
        twin = Material(organization_id=1, name="SAND", unit="ton")
        db.add(twin)
        db.flush()
        db.add(SupplierRate(organization_id=1, supplier_id=supplier.id, material_id=twin.id, unit_price=Decimal(price)))
        db.commit()


def test_two_spellings_already_saved_with_different_prices_lock_neither(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    _twin_material("99")
    [ticket] = _deliveries([dict(ticket_number="D1", delivery_date=date(2026, 9, 1), tonnage=Decimal("10.000"))])
    approved = client.post(f"/api/tickets/{ticket}/verify")
    assert approved.status_code == 200, approved.text
    assert approved.json()["unit_price"] is None, "neither 10 nor 99 may be chosen for a person"


def test_two_spellings_already_saved_with_the_same_price_still_price(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    _twin_material("10")
    [ticket] = _deliveries([dict(ticket_number="D1", delivery_date=date(2026, 9, 1), tonnage=Decimal("10.000"))])
    assert Decimal(client.post(f"/api/tickets/{ticket}/verify").json()["unit_price"]) == 10


def test_an_exact_repeat_of_a_catalog_name_keeps_its_plain_message(client):
    client.post("/api/materials", json={"name": "Sand"})
    response = client.post("/api/materials", json={"name": "Sand"})
    assert response.status_code == 409 and response.json()["detail"] == "A material with that name already exists"


def test_a_page_can_read_how_long_to_wait(client):
    """After a 429 or 503 the page says how long to wait, which needs Retry-After exposed."""
    response = client.get("/health/ready", headers={"Origin": settings.cors_origins[0]})
    exposed = response.headers.get("access-control-expose-headers", "").lower()
    assert "retry-after" in exposed and "x-request-id" in exposed, response.headers
