"""Regression tests for verification and financial reporting integrity."""
import io
from decimal import Decimal

from openpyxl import load_workbook

from .sample_data import make_scan_pdf


def upload_tickets(client):
    response = client.post(
        "/api/batches",
        data={"layout": "grid_2x2"},
        files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))],
    )
    assert response.status_code == 200
    return client.get(f"/api/batches/{response.json()['id']}/tickets").json()


def add_rate(client):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
    material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()
    response = client.put(
        "/api/rates",
        json={"supplier_id": supplier["id"], "material_id": material["id"], "unit_price": "30"},
    )
    assert response.status_code == 200


def test_editing_verified_ticket_revokes_confirmation_and_cost(client):
    add_rate(client)
    tickets = upload_tickets(client)
    tid = tickets[0]["id"]
    verified = client.post(f"/api/tickets/{tid}/verify").json()
    assert verified["status"] == "verified"
    assert Decimal(verified["cost"]) == Decimal("735.00")

    changed = client.patch(f"/api/tickets/{tid}", json={"unit_price_override": "32"}).json()
    assert changed["status"] == "needs_review"
    assert changed["tonnage_confirmed"] is False
    assert changed["cost"] is None

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["total_tons"] == 0 or Decimal(str(dashboard["total_tons"])) == 0
    assert Decimal(str(dashboard["pending_tons"])) == Decimal("86.65")
    assert Decimal(str(dashboard["total_cost"])) == 0

    workbook = load_workbook(io.BytesIO(client.get(
        f"/api/batches/{tickets[0]['batch_id']}/export.xlsx"
    ).content))
    assert workbook["Delivery Records"]["H2"].value is None
    assert workbook["Summary"]["B4"].value == 0  # verified tons

    verified_again = client.post(f"/api/tickets/{tid}/verify").json()
    assert verified_again["tonnage_confirmed"] is True
    assert verified_again["status"] == "verified"
    assert Decimal(verified_again["cost"]) == Decimal("784.00")


def test_verify_requires_complete_current_values(client):
    tickets = upload_tickets(client)
    tid = tickets[0]["id"]
    for field in ("supplier", "material_type", "job_location", "delivery_date"):
        response = client.patch(f"/api/tickets/{tid}", json={field: None})
        assert response.status_code == 200
        response = client.post(f"/api/tickets/{tid}/verify")
        assert response.status_code == 422
        assert response.json()["detail"].startswith("Resolve ticket issues")
        # Restore the original value for the next missing-field check.
        assert client.patch(f"/api/tickets/{tid}", json={field: tickets[0][field]}).status_code == 200

    client.patch(f"/api/tickets/{tid}", json={"delivery_date": "2035-01-01"})
    rejected = client.post(f"/api/tickets/{tid}/verify")
    assert rejected.status_code == 422
    assert "future" in rejected.json()["detail"]


def test_skipping_verified_ticket_removes_cost_and_confirmation(client):
    add_rate(client)
    tickets = upload_tickets(client)
    tid = tickets[0]["id"]
    assert client.post(f"/api/tickets/{tid}/verify").json()["cost"] is not None

    skipped = client.post(f"/api/tickets/{tid}/skip").json()
    assert skipped["status"] == "skipped"
    assert skipped["tonnage_confirmed"] is False
    assert skipped["cost"] is None
    dashboard = client.get("/api/dashboard").json()
    assert Decimal(str(dashboard["total_cost"])) == 0
    assert Decimal(str(dashboard["total_tons"])) == 0
