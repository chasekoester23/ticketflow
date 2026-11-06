"""Totals added up by the database must equal exact decimal arithmetic, to the cent."""
import random
from decimal import Decimal

import pytest

from app.models import Ticket
from app.services.pricing import to_cents

from .test_query_budgets import _seed, _session


def _rated(client, rate="28.5"):
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/materials", json={"name": "Sand"})
    client.put("/api/rates", json={"supplier_id": client.get("/api/suppliers").json()[0]["id"],
                                   "material_id": client.get("/api/materials").json()[0]["id"], "unit_price": rate})


@pytest.mark.parametrize("seed", range(6))
def test_database_totals_equal_decimal_arithmetic_on_awkward_values(client, seed):
    """Tons like 0.1 and 0.2 are the classic float trap. Sums must still be exact."""
    rng = random.Random(seed)
    _seed(60)
    _rated(client, str(Decimal(rng.randint(1000, 9000)) / 1000))
    tons = Decimal(0)
    cost = Decimal(0)
    for ticket_id in range(1, 61):
        value = Decimal(rng.randint(1, 60000)) / 1000
        client.patch(f"/api/tickets/{ticket_id}", json={"tonnage": str(value)})
        if rng.random() < 0.7:
            verified = client.post(f"/api/tickets/{ticket_id}/verify").json()
            tons += Decimal(verified["tonnage"])
            cost += Decimal(verified["cost"])
    dashboard = client.get("/api/dashboard").json()
    summary = client.get("/api/records/search", params={"page_size": 1}).json()["summary"]
    report = client.get("/api/reports/project/1").json()["totals"]
    pilot = client.get("/api/reports/pilot").json()["review"]
    for figure in (dashboard["total_tons"], summary["verified_tons"], report["verified_tons"], pilot["verified_tons"]):
        assert Decimal(figure) == tons
    for figure in (dashboard["total_cost"], summary["verified_cost"], report["cost"]):
        assert Decimal(figure) == cost


def test_approved_cost_is_kept_with_the_approval_and_cleared_when_withdrawn(client):
    _seed(1)
    _rated(client, "28.5")
    client.patch("/api/tickets/1", json={"tonnage": "21.250"})
    client.post("/api/tickets/1/verify")
    with _session() as db:
        assert db.get(Ticket, 1).approved_cost == to_cents(Decimal("21.250") * Decimal("28.5"))
    for withdraw in (lambda: client.patch("/api/tickets/1", json={"tonnage": "22"}),
                     lambda: client.post("/api/tickets/1/flag"), lambda: client.post("/api/tickets/1/skip")):
        client.post("/api/tickets/1/verify")
        withdraw()
        with _session() as db:
            assert db.get(Ticket, 1).approved_cost is None
    assert Decimal(client.get("/api/dashboard").json()["total_cost"]) == 0


def test_a_rate_change_never_touches_a_stored_cost(client):
    _seed(1)
    _rated(client, "10")
    client.post("/api/tickets/1/verify")
    before = client.get("/api/dashboard").json()["total_cost"]
    _rated(client, "99")
    assert client.get("/api/dashboard").json()["total_cost"] == before


def test_keys_follow_every_edit(client):
    _seed(1)
    client.patch("/api/tickets/1", json={"ticket_number": " 00a7 ", "supplier": "ABC  Aggregates.", "material_type": "No. 57 Stone"})
    with _session() as db:
        ticket = db.get(Ticket, 1)
        assert (ticket.number_key, ticket.number_loose) == ("00A7", "A7")
        assert (ticket.supplier_key, ticket.material_key) == ("abc aggregates", "no 57 stone")
    client.post("/api/tickets/bulk-edit", json={"ticket_ids": [1], "field": "supplier", "value": "Sample Stone"})
    with _session() as db:
        assert db.get(Ticket, 1).supplier_key == "sample stone"


def test_blank_values_go_last_whichever_way_the_register_sorts(client):
    _seed(3)
    with _session() as db:
        ticket = db.get(Ticket, 2)
        ticket.delivery_date = None
        db.commit()
    for direction in ("asc", "desc"):
        rows = client.get("/api/records/search", params={"sort": "date", "direction": direction}).json()["rows"]
        assert rows[-1]["id"] == 2


def test_the_report_that_failed_at_scale_has_no_limit_on_approved_tickets(client):
    """It used to pass every approved ticket's id to the database as a parameter,
    which fails beyond about 32,000. It must not build such a list again."""
    from pathlib import Path
    from app.services import pilot_report
    source = Path(pilot_report.__file__).read_text()
    assert "in_(verified_ids)" not in source and "in_(ids)" not in source
    _seed(3)
    client.post("/api/tickets/1/verify")
    assert client.get("/api/reports/pilot").json()["reconciliation"]["verified_tickets_not_linked"] == 1


def test_unpaged_records_refuse_huge_results_instead_of_running_out_of_memory(client, monkeypatch):
    from app.routers import reports
    monkeypatch.setattr(reports, "MAX_UNPAGED_RECORDS", 3)
    _seed(5)
    response = client.get("/api/records")
    assert response.status_code == 413 and "/api/records/search" in response.json()["detail"]


def test_excel_refuses_more_than_it_can_usefully_hold_and_points_to_csv(client, monkeypatch):
    from app.routers import reports
    monkeypatch.setattr(reports, "MAX_EXCEL_ROWS", 3)
    _seed(5)
    response = client.get("/api/records/export.xlsx")
    assert response.status_code == 413 and "CSV" in response.json()["detail"]
    assert client.get("/api/records/export.csv").status_code == 200


def test_invoice_summary_leaves_out_detail_and_keeps_every_figure(client):
    _seed(2)
    _rated(client, "10")
    client.post("/api/tickets/1/verify")
    line = client.post("/api/invoice-comparisons", json={
        "project_id": 1, "invoice_number": "I", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-30", "billed_tons": "15", "billed_unit_price": "10",
        "billed_subtotal": "150", "ticket_list": "10000\t15"}).json()
    summary = client.get("/api/invoice-comparisons", params={"summary": True}).json()[0]
    assert summary["ticket_check"] == [] and summary["tickets"] == [] and summary["diagnosis"] is None
    for field in ("status", "comparison_mode", "revision"):
        assert summary[field] == line[field], field
    for field in ("billed_tons", "verified_tons", "quantity_difference", "subtotal_difference"):
        assert Decimal(summary[field]) == Decimal(line[field]), field
    assert client.get(f"/api/invoice-comparisons/{line['id']}").json()["ticket_check"]


def test_a_ticket_only_counts_toward_a_line_when_its_keys_truly_match(client):
    """Moving matching to stored keys must not change what matches."""
    _seed(3)
    _rated(client, "10")
    from .test_records_at_scale import _set
    _set(2, supplier="EXAMPLE  aggregates.")  # Same supplier, different spelling.
    _set(3, supplier="Other Supplier")
    for ticket_id in (1, 2, 3):
        client.post(f"/api/tickets/{ticket_id}/verify", json={"confirm_duplicate": True})
    line = client.post("/api/invoice-comparisons", json={
        "project_id": 1, "invoice_number": "I", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-30", "billed_tons": "30.01", "billed_unit_price": "10", "billed_subtotal": "300.10"}).json()
    assert line["verified_count"] == 2 and Decimal(line["verified_tons"]) == Decimal("30.01")


def test_a_ticket_missing_a_value_still_keeps_a_line_incomplete(client):
    _seed(2)
    _rated(client, "10")
    client.post("/api/tickets/1/verify")
    from .test_records_at_scale import _set
    _set(2, supplier=None)
    line = client.post("/api/invoice-comparisons", json={
        "project_id": 1, "invoice_number": "I", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-30", "billed_tons": "15", "billed_unit_price": "10", "billed_subtotal": "150"}).json()
    assert line["unclassified_count"] == 1 and line["status"] == "incomplete"
