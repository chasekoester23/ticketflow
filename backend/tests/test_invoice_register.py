"""The invoice register: supplier and trucking invoices in one list, one row per invoice,
with exact totals. An incomplete comparison is never shown as a difference, and nothing
here approves a payment. Fictional data."""
from .test_invoice_comparisons import _line, _post, _project, _rate, _tickets
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)


def test_supplier_lines_group_into_one_invoice_with_exact_totals(client):
    project = _project(client)
    _rate(client)
    _, tickets = _tickets(client, project)
    for ticket in tickets[:2]:
        edited = client.patch(f"/api/tickets/{ticket['id']}", json={
            "tonnage": "10.00", "supplier": "Example Aggregates", "material_type": "No. 53 Crushed Stone", "delivery_date": "2026-10-15"}).json()
        assert client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]}).status_code == 200
    # Two lines of one invoice (a second, identical line would be refused as a repeat).
    for day in ("2026-10-15", "2026-10-16"):
        assert _post(client, _line(project, billed_tons="10.000", billed_subtotal="300.10", date_from=day, date_to=day)).status_code == 201
    register = client.get("/api/invoice-comparisons/register").json()
    [row] = register["invoices"]
    assert (row["kind"], row["invoice_number"], row["lines"], row["billed"]) == ("supplier", "INV-4401", 2, "600.20")
    assert register["totals"]["billed"] == "600.20" and register["totals"]["invoices"] == 1
    assert row["ticket_level"] is False  # no ticket numbers entered: compared in total only
    assert (row["period_from"], row["period_to"]) == ("2026-10-15", "2026-10-16")
    assert row["project"]["code"] == "24-115" and row["href"].startswith("/invoices/lines?line=")


def test_an_invoice_that_cant_be_priced_is_incomplete_not_a_difference(client):
    project = _project(client)  # no rate, no approved tickets
    assert _post(client, _line(project)).status_code == 201
    [row] = client.get("/api/invoice-comparisons/register").json()["invoices"]
    # Nothing approved yet: the deliveries may simply not be checked, so it's not called a difference.
    assert row["status"] == "incomplete" and row["billed"] == "735.00"


def test_trucking_invoices_join_the_register_and_follow_the_project(client, yard):
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    created = client.post("/api/hauling-invoices", json={
        "hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": "FH-1", "period_from": "2026-11-01",
        "period_to": "2026-11-30", "lines": [{"kind": "hours", "truck_identifier": "T-12", "work_date": "2026-11-03", "hours": "10",
                                               "rate": "110", "amount": "1100.00"}]})
    assert created.status_code == 201, created.text
    [row] = client.get("/api/invoice-comparisons/register").json()["invoices"]
    assert (row["kind"], row["billed"], row["compared"], row["difference"], row["status"]) == ("trucking", "1100.00", "990.00", "110.00", "discrepancy")
    assert row["href"] == f"/trucking?tab=invoices&invoice={created.json()['id']}"
    assert client.get("/api/invoice-comparisons/register", params={"project_id": yard["other"]}).json()["invoices"] == []
    assert client.get("/api/invoice-comparisons/register", params={"project_id": 99999}).status_code == 404
