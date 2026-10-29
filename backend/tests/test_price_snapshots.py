"""Historical pricing must never be re-derived from changed supplier rates."""
import io
import sqlite3
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from openpyxl import load_workbook

from .sample_data import make_scan_pdf


def _tickets(client):
    response = client.post(
        "/api/batches",
        data={"layout": "grid_2x2"},
        files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))],
    )
    assert response.status_code == 200
    bid = response.json()["id"]
    return bid, client.get(f"/api/batches/{bid}/tickets").json()


def _rate(client, price):
    suppliers = client.get("/api/suppliers").json()
    materials = client.get("/api/materials").json()
    if not suppliers:
        supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
        material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()
    else:
        supplier, material = suppliers[0], materials[0]
    response = client.put("/api/rates", json={
        "supplier_id": supplier["id"], "material_id": material["id"], "unit_price": str(price),
    })
    assert response.status_code == 200


def test_verified_price_does_not_change_with_supplier_rates(client):
    _rate(client, "30")
    bid, tickets = _tickets(client)
    first, second = tickets[:2]
    first_id, second_id = first["id"], second["id"]

    locked = client.post(f"/api/tickets/{first_id}/verify").json()
    assert locked["price_snapshot_state"] == "priced"
    assert locked["price_source"] == "rate"
    assert Decimal(locked["unit_price"]) == Decimal("30")
    assert Decimal(locked["cost"]) == Decimal("735")

    _rate(client, "45")
    # Ticket 2 is a *different material* (No. 8 Stone). Set its own rate;
    # TicketFlow correctly matches by both supplier and material.
    supplier_id = client.get("/api/suppliers").json()[0]["id"]
    stone8 = client.post("/api/materials", json={"name": "No. 8 Stone"}).json()
    assert client.put("/api/rates", json={
        "supplier_id": supplier_id, "material_id": stone8["id"], "unit_price": "45",
    }).status_code == 200
    unchanged = client.get(f"/api/batches/{bid}/tickets").json()[0]
    assert unchanged["price_snapshot_state"] == "priced"
    assert Decimal(unchanged["unit_price"]) == Decimal("30")
    assert Decimal(unchanged["cost"]) == Decimal("735")

    # Pressing Verify again must not silently change the frozen price.
    again = client.post(f"/api/tickets/{first_id}/verify").json()
    assert Decimal(again["unit_price"]) == Decimal("30")

    # Unverified tickets should preview the new current price and lock it when approved.
    preview = client.get(f"/api/batches/{bid}/tickets").json()[1]
    assert preview["price_snapshot_state"] == "pending"
    assert Decimal(preview["unit_price"]) == Decimal("45")
    approved = client.post(f"/api/tickets/{second_id}/verify").json()
    assert approved["price_snapshot_state"] == "priced"
    assert Decimal(approved["unit_price"]) == Decimal("45")

    dashboard = client.get("/api/dashboard").json()
    assert Decimal(str(dashboard["total_cost"])) == Decimal("1729.50")

    response = client.get(f"/api/batches/{bid}/export.xlsx")
    workbook = load_workbook(io.BytesIO(response.content))
    assert workbook["Delivery Records"]["G2"].value == 30
    assert workbook["Delivery Records"]["H2"].value == 735
    assert workbook["Delivery Records"]["G3"].value == 45
    assert workbook["Delivery Records"]["H3"].value == 994.5
    assert client.get("/api/records/export.xlsx").status_code == 200


def test_verified_without_rate_does_not_silently_gain_price(client):
    bid, tickets = _tickets(client)
    tid = tickets[0]["id"]
    verified = client.post(f"/api/tickets/{tid}/verify").json()
    assert verified["price_snapshot_state"] == "unpriced"
    assert verified["unit_price"] is None
    assert verified["cost"] is None

    _rate(client, "30")
    still = client.get(f"/api/batches/{bid}/tickets").json()[0]
    assert still["price_snapshot_state"] == "unpriced"
    assert still["unit_price"] is None
    assert still["cost"] is None
    assert "no rate was available" in still["pricing_notes"][0].lower()

    changed = client.patch(f"/api/tickets/{tid}", json={"unit_price_override": "28"}).json()
    assert changed["status"] == "needs_review"
    assert changed["price_snapshot_state"] == "pending"
    locked = client.post(f"/api/tickets/{tid}/verify").json()
    assert Decimal(locked["unit_price"]) == Decimal("28")
    assert locked["price_source"] == "override"
    assert Decimal(locked["cost"]) == Decimal("686")
    _rate(client, "60")
    assert Decimal(client.get(f"/api/batches/{bid}/tickets").json()[0]["cost"]) == Decimal("686")


def test_migration_preserves_legacy_verification_without_guessing_rate(tmp_path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, status VARCHAR(20))")
    connection.execute("INSERT INTO tickets (id, status) VALUES (1, 'verified')")
    connection.execute("INSERT INTO tickets (id, status) VALUES (2, 'needs_review')")
    connection.commit()
    connection.close()

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + str(database))
    command.upgrade(cfg, "head")

    with sqlite3.connect(database) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(tickets)")}
        assert {"price_snapshot_state", "applied_unit_price", "applied_price_source"} <= cols
        assert con.execute("SELECT price_snapshot_state FROM tickets WHERE id=1").fetchone()[0] is None
        assert con.execute("SELECT applied_unit_price FROM tickets WHERE id=1").fetchone()[0] is None
        assert con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261029_01"

    # Running the migration twice should be safe/idempotent.
    command.upgrade(cfg, "head")
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2


def test_migration_can_initialize_new_database(tmp_path):
    database = tmp_path / "new.db"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + str(database))
    command.upgrade(cfg, "head")
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='tickets'").fetchone()
        assert con.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone()


def test_legacy_verified_ticket_requires_documented_rate_before_relocking(client):
    from app.db import get_db
    from app.main import app
    from app.models import Ticket

    _rate(client, "30")
    bid, tickets = _tickets(client)
    tid = tickets[0]["id"]
    assert client.post(f"/api/tickets/{tid}/verify").json()["price_snapshot_state"] == "priced"

    # Simulate a pre-migration verified record, with no rate snapshot.
    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        record = db.get(Ticket, tid)
        record.applied_unit_price = None
        record.applied_price_source = None
        record.price_snapshot_state = None
        db.commit()
    finally:
        generator.close()

    old = client.get(f"/api/batches/{bid}/tickets").json()[0]
    assert old["status"] == "verified"
    assert old["price_snapshot_state"] is None
    assert old["unit_price"] is None
    assert old["cost"] is None
    assert "historical rate is unknown" in old["pricing_notes"][0].lower()

    rejected = client.post(f"/api/tickets/{tid}/verify")
    assert rejected.status_code == 409

    changed = client.patch(f"/api/tickets/{tid}", json={"unit_price_override": "29"}).json()
    assert changed["status"] == "needs_review"
    assert changed["price_snapshot_state"] == "pending"
    relocked = client.post(f"/api/tickets/{tid}/verify").json()
    assert relocked["price_snapshot_state"] == "priced"
    assert relocked["price_source"] == "override"
    assert Decimal(relocked["cost"]) == Decimal("710.50")
