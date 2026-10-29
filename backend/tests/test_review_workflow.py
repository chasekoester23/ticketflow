"""Contractor-facing review actions: explicit accept, defer, and financial safety."""
import io
from decimal import Decimal

from openpyxl import load_workbook
from .sample_data import make_scan_pdf


def _upload(client):
    r = client.post(
        "/api/batches",
        data={"layout": "grid_2x2"},
        files=[("files", ("tickets.pdf", make_scan_pdf(), "application/pdf"))],
    )
    assert r.status_code == 200
    bid = r.json()["id"]
    ts = client.get(f"/api/batches/{bid}/tickets").json()
    assert len(ts) == 4
    return bid, ts


def _price(client):
    s = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
    m = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()
    assert client.put("/api/rates", json={
        "supplier_id": s["id"], "material_id": m["id"], "unit_price": "30"
    }).status_code == 200


def test_flag_defers_ticket_without_approval_or_money(client):
    _price(client)
    bid, tickets = _upload(client)
    tid = tickets[0]["id"]
    # AI extraction alone must not mark anything approved.
    assert all(t["status"] == "needs_review" for t in tickets)
    assert all(not t["tonnage_confirmed"] for t in tickets)
    flagged = client.post(f"/api/tickets/{tid}/flag").json()
    assert flagged["status"] == "flagged"
    assert flagged["tonnage_confirmed"] is False
    assert flagged["cost"] is None

    overview = client.get("/api/dashboard").json()
    assert overview["flagged"] == 1
    assert overview["needs_review"] == 4
    assert Decimal(str(overview["total_tons"])) == 0
    assert len(client.get("/api/records?status=flagged").json()) == 1
    assert len(client.get("/api/records?status=needs_review").json()) == 3

    wb = load_workbook(io.BytesIO(client.get(f"/api/batches/{bid}/export.xlsx").content))
    assert wb["Delivery Records"]["I2"].value == "Flagged for review"

    audit = client.get(f"/api/tickets/{tid}/audit").json()
    assert any(a["field"] == "status" and a["new"] == "flagged" for a in audit)
    assert client.post(f"/api/tickets/{tid}/flag").status_code == 200
    assert len(client.get(f"/api/tickets/{tid}/audit").json()) == len(audit)


def test_acceptance_after_flag_requires_manual_verify(client):
    bid, tickets = _upload(client)
    tid = tickets[0]["id"]
    flagged = client.post(f"/api/tickets/{tid}/flag").json()
    assert flagged["status"] == "flagged"
    patched = client.patch(f"/api/tickets/{tid}", json={"tonnage": "25.5"}).json()
    assert patched["status"] == "flagged"
    assert patched["tonnage_confirmed"] is False
    assert patched["tonnage"] == "25.500"
    accepted = client.post(f"/api/tickets/{tid}/verify").json()
    assert accepted["status"] == "verified"
    assert accepted["tonnage_confirmed"] is True
    assert accepted["tonnage"] == "25.500"
    assert client.get("/api/dashboard").json()["flagged"] == 0


def test_flagging_previously_verified_ticket_removes_cost_and_price_snapshot(client):
    _price(client)
    bid, tickets = _upload(client)
    tid = tickets[0]["id"]
    approved = client.post(f"/api/tickets/{tid}/verify").json()
    assert approved["status"] == "verified"
    assert Decimal(approved["cost"]) == Decimal("735")
    flagged = client.post(f"/api/tickets/{tid}/flag").json()
    assert flagged["status"] == "flagged"
    assert flagged["tonnage_confirmed"] is False
    assert flagged["price_snapshot_state"] == "pending"
    assert flagged["cost"] is None
    assert Decimal(str(client.get("/api/dashboard").json()["total_cost"])) == 0

    # A new manual acceptance is required, capturing a new rate snapshot.
    accepted_again = client.post(f"/api/tickets/{tid}/verify").json()
    assert accepted_again["status"] == "verified"
    assert Decimal(accepted_again["cost"]) == Decimal("735")
