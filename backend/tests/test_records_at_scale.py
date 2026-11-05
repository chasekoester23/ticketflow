"""Search, totals, exports, bulk tidying and project reports."""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from app.auth import current_user
from app.main import app
from app.models import (
    AuditEvent, Batch, InvoiceTicketLink, Organization, Project, ScanEvent, Ticket, UploadedFile, User,
)
from .test_query_budgets import _Count, _seed, _session


def _set(ticket_id, **fields):
    with _session() as db:
        ticket = db.get(Ticket, ticket_id)
        for key, value in fields.items():
            setattr(ticket, key, value)
        db.commit()


def _as(role):
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="t", password_hash="x", active=True, role=role, must_change_password=False)


def _search(client, **params):
    response = client.get("/api/records/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_pages_cover_every_record_exactly_once(client):
    _seed(25)
    seen = []
    for page in (1, 2, 3):
        result = _search(client, page=page, page_size=10, sort="ticket_number", direction="asc")
        assert result["total"] == 25
        seen += [row["id"] for row in result["rows"]]
    assert len(seen) == 25 and len(set(seen)) == 25
    assert _search(client, page=4, page_size=10)["rows"] == []


def test_totals_cover_all_matching_rows_not_just_this_page(client):
    _seed(12)
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/materials", json={"name": "Sand"})
    suppliers = client.get("/api/suppliers").json()
    materials = client.get("/api/materials").json()
    client.put("/api/rates", json={"supplier_id": suppliers[0]["id"], "material_id": materials[0]["id"], "unit_price": "10"})
    expected_tons = Decimal(0)
    expected_cost = Decimal(0)
    for ticket_id in range(1, 9):
        verified = client.post(f"/api/tickets/{ticket_id}/verify").json()
        expected_tons += Decimal(verified["tonnage"])
        expected_cost += Decimal(verified["cost"])
    result = _search(client, page_size=2)
    assert len(result["rows"]) == 2
    summary = result["summary"]
    assert summary["records"] == 12
    assert Decimal(summary["verified_tons"]) == expected_tons
    assert Decimal(summary["verified_cost"]) == expected_cost
    assert summary["by_status"] == {"verified": 8, "needs_review": 4}
    assert summary["verified_unpriced"] == 0


def test_unverified_tons_never_reach_the_totals(client):
    _seed(3)
    summary = _search(client)["summary"]
    assert Decimal(summary["verified_tons"]) == 0 and Decimal(summary["verified_cost"]) == 0


def test_a_status_filter_other_than_verified_shows_no_verified_totals(client):
    _seed(3)
    client.post("/api/tickets/1/verify")
    summary = _search(client, status="needs_review")["summary"]
    assert summary["records"] == 2
    assert Decimal(summary["verified_tons"]) == 0


def test_approved_ticket_with_no_price_is_counted_not_costed_at_zero(client):
    _seed(1)
    client.post("/api/tickets/1/verify")
    summary = _search(client)["summary"]
    assert summary["verified_unpriced"] == 1
    assert Decimal(summary["verified_cost"]) == 0


@pytest.mark.parametrize("query, expected", [("10003", 1), ("example", 5), ("SAND", 5), ("lot", 5), ("nothing", 0)])
def test_search_looks_across_number_supplier_material_and_job(client, query, expected):
    _seed(5)
    assert _search(client, q=query)["total"] == expected


def test_search_treats_wildcards_as_ordinary_characters(client):
    _seed(3)
    _set(1, ticket_number="50%_OFF")
    assert _search(client, q="%")["total"] == 1
    assert _search(client, q="_")["total"] == 1


@pytest.mark.parametrize("sort", ["date", "ticket_number", "tons", "supplier", "material", "status", "job"])
def test_every_sort_is_stable_across_pages(client, sort):
    _seed(10)
    first = _search(client, sort=sort, direction="asc", page_size=5)["rows"]
    second = _search(client, sort=sort, direction="asc", page_size=5, page=2)["rows"]
    assert not {r["id"] for r in first} & {r["id"] for r in second}


def test_unknown_sort_and_oversized_pages_are_refused(client):
    assert client.get("/api/records/search", params={"sort": "password_hash"}).status_code == 422
    assert client.get("/api/records/search", params={"page_size": 201}).status_code == 422
    assert client.get("/api/records/search", params={"page": 0}).status_code == 422


def test_search_never_shows_another_company(client):
    with _session() as db:
        db.add(Organization(id=2, name="Another Company"))
        db.commit()
    _seed(2)
    _seed(2, organization_id=2, batch_id=2, start=100)
    result = _search(client)
    assert result["total"] == 2
    assert all(row["batch_id"] == 1 for row in result["rows"])
    assert result["summary"]["records"] == 2


def test_search_cost_does_not_grow_with_the_table(client):
    _seed(30)
    with _Count() as small:
        _search(client, page_size=10)
    _seed(600, batch_id=2, start=30)
    with _Count() as large:
        _search(client, page_size=10)
    assert large.statements == small.statements
    assert large.tickets <= small.tickets + 2


def test_csv_keeps_exact_decimals_leading_zeros_and_neutralizes_formulas(client):
    _seed(2)
    _set(1, ticket_number="010482", tonnage=Decimal("24.125"), supplier='=HYPERLINK("http://x")')
    response = client.get("/api/records/export.csv", params={"columns": "ticket_number,tons,supplier"})
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Ticket Number", "Tonnage", "Supplier"]
    row = next(r for r in rows[1:] if r[0] == "010482")
    assert row[1] == "24.125"
    assert row[2].startswith("'=")


def test_export_columns_are_configurable_and_the_default_never_moves(client):
    _seed(2)
    default = load_workbook(io.BytesIO(client.get("/api/records/export.xlsx").content))["Delivery Records"]
    assert [c.value for c in default[1]] == [
        "Ticket Number", "Delivery Date", "Material Type", "Tonnage", "Supplier", "Job Location", "Unit Price",
        "Calculated Cost", "Verification Status", "Source File Reference", "Approved By", "Approved At (UTC)"]
    chosen = client.get("/api/records/export.xlsx", params={"columns": "project,ticket_number,tons,batch"})
    sheet = load_workbook(io.BytesIO(chosen.content))["Delivery Records"]
    assert [c.value for c in sheet[1]] == ["Project", "Ticket Number", "Tonnage", "Batch"]
    assert sheet.cell(2, 1).value == "P1"
    assert sheet.cell(2, 4).value == 1


def test_unknown_export_column_is_refused(client):
    assert client.get("/api/records/export.csv", params={"columns": "ticket_number,password_hash"}).status_code == 422
    assert client.get("/api/records/export.xlsx", params={"columns": "password_hash"}).status_code == 422


def test_export_follows_the_same_search_as_the_register(client):
    _seed(5)
    rows = list(csv.reader(io.StringIO(client.get("/api/records/export.csv", params={"q": "10003"}).text)))
    assert len(rows) == 2 and rows[1][0] == "10003"


# ------------------------------------------------------------ bulk changes

def test_bulk_edit_changes_only_unapproved_tickets_and_audits_each(client):
    _seed(4)
    client.post("/api/tickets/1/verify")
    client.post("/api/tickets/2/flag")
    client.post("/api/tickets/3/skip")
    before = client.get("/api/batches/1/tickets").json()[0]
    result = client.post("/api/tickets/bulk-edit", json={"ticket_ids": [1, 2, 3, 4], "field": "supplier", "value": "Sample Stone Co"}).json()
    assert result == {"changed": 2, "unchanged": 0, "left_alone": {"verified": 1, "skipped": 1}}
    tickets = {t["id"]: t for t in client.get("/api/batches/1/tickets").json()}
    assert tickets[1]["supplier"] == before["supplier"]
    assert tickets[1]["status"] == "verified" and tickets[1]["tonnage_confirmed"] is True
    assert tickets[2]["supplier"] == "Sample Stone Co" and tickets[2]["status"] == "flagged"
    assert tickets[4]["supplier"] == "Sample Stone Co" and tickets[4]["status"] == "needs_review"
    history = client.get("/api/tickets/4/audit").json()
    assert history[-1]["field"] == "supplier" and history[-1]["new"] == "Sample Stone Co" and history[-1]["actor"] == "Test Owner"


def test_bulk_edit_is_all_or_nothing_for_unknown_or_foreign_tickets(client):
    with _session() as db:
        db.add(Organization(id=2, name="Another Company"))
        db.commit()
    _seed(1)
    _seed(1, organization_id=2, batch_id=2, start=9)
    response = client.post("/api/tickets/bulk-edit", json={"ticket_ids": [1, 2], "field": "supplier", "value": "X"})
    assert response.status_code == 404
    assert client.get("/api/batches/1/tickets").json()[0]["supplier"] == "Example Aggregates"


@pytest.mark.parametrize("field", ["tonnage", "ticket_number", "status", "unit_price_override"])
def test_bulk_edit_can_never_touch_quantities_numbers_or_prices(client, field):
    _seed(1)
    response = client.post("/api/tickets/bulk-edit", json={"ticket_ids": [1], "field": field, "value": "99"})
    assert response.status_code == 422


def test_bulk_date_is_validated(client):
    _seed(1)
    bad = client.post("/api/tickets/bulk-edit", json={"ticket_ids": [1], "field": "delivery_date", "value": "10/15/26"})
    assert bad.status_code == 422
    good = client.post("/api/tickets/bulk-edit", json={"ticket_ids": [1], "field": "delivery_date", "value": "2026-10-15"})
    assert good.json()["changed"] == 1


def test_bulk_edit_refreshes_flags_like_a_single_edit(client):
    _seed(1)
    _set(1, supplier=None, flags=["Supplier missing"])
    client.post("/api/tickets/bulk-edit", json={"ticket_ids": [1], "field": "supplier", "value": "Sample Stone Co"})
    assert "Supplier missing" not in client.get("/api/batches/1/tickets").json()[0]["flags"]


def test_viewer_cannot_bulk_edit_or_rename(client):
    _seed(1)
    _as("viewer")
    assert client.post("/api/tickets/bulk-edit", json={"ticket_ids": [1], "field": "supplier", "value": "X"}).status_code == 403
    assert client.post("/api/records/rename", json={"field": "supplier", "from_value": "a", "to_value": "b"}).status_code == 403


def test_rename_requires_a_catalog_name_so_typos_cant_spread(client):
    _seed(3)
    response = client.post("/api/records/rename", json={"field": "supplier", "from_value": "Example Aggregates", "to_value": "Exmple Agregates"})
    assert response.status_code == 422
    assert client.get("/api/batches/1/tickets").json()[0]["supplier"] == "Example Aggregates"


def test_rename_changes_exactly_one_spelling_and_leaves_approved_tickets(client):
    _seed(4)
    client.post("/api/suppliers", json={"name": "Example Aggregates Inc"})
    _set(4, supplier="Different Supplier")
    client.post("/api/tickets/1/verify")
    result = client.post("/api/records/rename", json={"field": "supplier", "from_value": "Example Aggregates", "to_value": "Example Aggregates Inc"}).json()
    assert result == {"changed": 2, "unchanged": 0, "left_alone": {"verified": 1}}
    tickets = {t["id"]: t["supplier"] for t in client.get("/api/batches/1/tickets").json()}
    assert tickets == {1: "Example Aggregates", 2: "Example Aggregates Inc", 3: "Example Aggregates Inc", 4: "Different Supplier"}


def test_name_variants_explain_each_spelling_and_only_suggest(client):
    _seed(4)
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/suppliers", json={"name": "Sample Stone Co"})
    suppliers = {s["name"]: s["id"] for s in client.get("/api/suppliers").json()}
    client.post("/api/name-aliases", json={"kind": "supplier", "alias": "SSC", "target_id": suppliers["Sample Stone Co"]})
    _set(1, supplier="EXAMPLE AGGREGATES.")   # same once normalized
    _set(2, supplier="Examle Aggregates")     # a typo
    _set(3, supplier="SSC")                   # a saved alias
    client.post("/api/tickets/4/verify")      # exact name, approved
    rows = {r["value"]: r for r in client.get("/api/records/name-variants", params={"field": "supplier"}).json()["rows"]}
    assert rows["Examle Aggregates"]["state"] == "unknown"
    assert rows["Examle Aggregates"]["target"] == "Example Aggregates"
    assert rows["EXAMPLE AGGREGATES."]["state"] == "same_name"
    assert rows["SSC"]["state"] == "alias" and rows["SSC"]["target"] == "Sample Stone Co"
    assert rows["Example Aggregates"]["state"] == "catalog" and rows["Example Aggregates"]["verified"] == 1
    # Asking changes nothing.
    assert client.get("/api/batches/1/tickets").json()[1]["supplier"] == "Examle Aggregates"


# ------------------------------------------------------------ saved views

def test_saved_view_round_trip_and_duplicate_names(client):
    body = {"name": "October stone", "filters": {"material": "stone", "date_from": "2026-10-01"}, "columns": ["ticket_number", "tons"]}
    created = client.post("/api/saved-views", json=body)
    assert created.status_code == 201
    listed = client.get("/api/saved-views").json()
    assert listed[0]["filters"] == body["filters"] and listed[0]["columns"] == body["columns"]
    assert client.post("/api/saved-views", json=body).status_code == 409
    assert client.delete(f"/api/saved-views/{created.json()['id']}").status_code == 204
    assert client.get("/api/saved-views").json() == []


@pytest.mark.parametrize("body", [
    {"name": "x", "filters": {"organization_id": "2"}},
    {"name": "x", "columns": ["password_hash"]},
    {"name": "   "},
])
def test_saved_view_accepts_only_known_keys(client, body):
    assert client.post("/api/saved-views", json=body).status_code == 422


def test_saved_views_are_company_scoped_and_removal_is_restricted(client):
    with _session() as db:
        db.add(Organization(id=2, name="Another Company"))
        db.add(User(id=2, organization_id=1, username="other", password_hash="x", role="viewer"))
        db.commit()
    view = client.post("/api/saved-views", json={"name": "Mine"}).json()
    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=1, username="other", password_hash="x", active=True, role="viewer", must_change_password=False)
    assert client.delete(f"/api/saved-views/{view['id']}").status_code == 403
    app.dependency_overrides[current_user] = lambda: User(
        id=9, organization_id=2, username="o", password_hash="x", active=True, role="owner", must_change_password=False)
    assert client.get("/api/saved-views").json() == []
    assert client.delete(f"/api/saved-views/{view['id']}").status_code == 404


# ------------------------------------------------------------ moving a batch

def _second_project(**fields):
    with _session() as db:
        db.add(Project(id=7, organization_id=1, code="P7", name="Right Project", **fields))
        db.commit()


def test_batch_moves_and_comparisons_and_history_follow(client):
    _seed(2)
    _second_project()
    moved = client.patch("/api/batches/1/project", json={"project_id": 7})
    assert moved.status_code == 200 and moved.json()["project_id"] == 7
    assert _search(client, project_id=7)["total"] == 2
    assert _search(client, project_id=1)["total"] == 0
    with _session() as db:
        event = db.query(ScanEvent).filter(ScanEvent.action == "project_changed").one()
        assert (event.old_value, event.new_value, event.actor_user_id) == ("P1", "P7", 1)


def test_batch_move_refuses_archived_foreign_and_clerks(client):
    _seed(1)
    _second_project(status="archived")
    assert client.patch("/api/batches/1/project", json={"project_id": 7}).status_code == 409
    assert client.patch("/api/batches/1/project", json={"project_id": 999}).status_code == 404
    _as("clerk")
    assert client.patch("/api/batches/1/project", json={"project_id": 1}).status_code == 403


def test_batch_move_refuses_tickets_linked_to_an_invoice_line(client):
    _seed(1)
    _second_project()
    from app.models import InvoiceComparison
    with _session() as db:
        item = InvoiceComparison(organization_id=1, project_id=1, invoice_number="I", supplier_name="S", supplier_key="s",
                                 material_name="M", material_key="m", date_from=date(2026, 9, 1), date_to=date(2026, 9, 2),
                                 billed_tons=Decimal("1"), billed_unit_price=Decimal("1"), billed_subtotal=Decimal("1"))
        db.add(item)
        db.flush()
        db.add(InvoiceTicketLink(comparison_id=item.id, ticket_id=1, listed_number="1", verified_tons=Decimal("1")))
        db.commit()
    response = client.patch("/api/batches/1/project", json={"project_id": 7})
    assert response.status_code == 409 and "Release" in response.json()["detail"]
    assert client.get("/api/batches/1").json()["project_id"] == 1


# ------------------------------------------------------------ project report

def _rated(client):
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/materials", json={"name": "Sand"})
    supplier = client.get("/api/suppliers").json()[0]["id"]
    material = client.get("/api/materials").json()[0]["id"]
    client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "10"})


def test_project_report_counts_only_approved_quantities(client):
    _seed(4)
    _rated(client)
    client.post("/api/tickets/1/verify")
    client.post("/api/tickets/2/verify")
    report = client.get("/api/reports/project/1").json()
    totals = report["totals"]
    assert totals["tickets"] == 4 and totals["verified_tickets"] == 2 and totals["pending_tickets"] == 2
    assert Decimal(totals["verified_tons"]) == Decimal("15.00") + Decimal("15.01")
    assert Decimal(totals["cost"]) == Decimal("300.10")
    assert Decimal(totals["pending_tons"]) == Decimal("15.02") + Decimal("15.03")
    pair = report["by_supplier_material"][0]
    assert (pair["supplier"], pair["material"]) == ("Example Aggregates", "Sand")
    assert report["by_month"][0]["month"] == "2026-09"
    assert report["first_delivery"] == "2026-09-01"


def test_project_report_keeps_unknown_figures_unresolved(client):
    _seed(1)
    client.post("/api/tickets/1/verify")  # no rate, so no locked price
    line = client.post("/api/invoice-comparisons", json={
        "project_id": 1, "invoice_number": "I-1", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-01", "billed_tons": "15", "billed_unit_price": "10", "billed_subtotal": "150"})
    assert line.status_code == 201
    report = client.get("/api/reports/project/1").json()
    invoices = report["invoices"]
    assert invoices["lines_unresolved"] == 1
    assert invoices["lines"][0]["verified_subtotal"] is None
    assert Decimal(invoices["verified_total"]) == 0
    sheet = load_workbook(io.BytesIO(client.get("/api/reports/project/1/report.xlsx").content))["Invoice lines"]
    assert sheet.cell(2, 11).value == "UNRESOLVED"
    assert totals_unit(report) == 1


def totals_unit(report):
    return report["totals"]["unpriced"]


def test_project_report_is_company_scoped(client):
    with _session() as db:
        db.add(Organization(id=2, name="Another Company"))
        db.commit()
    _seed(1, organization_id=2, batch_id=2)
    assert client.get("/api/reports/project/2").status_code == 404
    assert client.get("/api/reports/project/2/report.xlsx").status_code == 404


def test_project_report_workbook_neutralizes_formulas_and_labels_scope(client):
    _seed(1)
    _set(1, supplier="=SUM(1,1)")
    book = load_workbook(io.BytesIO(client.get("/api/reports/project/1/report.xlsx").content))
    assert book["By supplier and material"].cell(2, 1).value == "'=SUM(1,1)"
    text = " ".join(str(c.value) for row in book["Summary"].iter_rows() for c in row)
    assert "Not an approval for payment" in text
