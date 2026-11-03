"""Pilot hardening: attributable pricing history and approver columns in exports.

Synthetic data only; no AI provider calls.
"""
import io
import sqlite3

from alembic import command
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.auth import current_user
from app.main import app
from app.models import Organization, PricingEvent, Ticket, User
from app.routers import api as api_module
from app.services.export import HEADERS, LEGACY_APPROVER

from .migration_helpers import alembic_config, head_revision
from .sample_data import make_scan_pdf
from .test_team_accounts import LEGACY_06_SCHEMA


def _as(user_id, org_id, role, username):
    app.dependency_overrides[current_user] = lambda: User(
        id=user_id, organization_id=org_id, username=username, display_name=None,
        password_hash="fixture", active=True, role=role, must_change_password=False,
    )


def _history(client):
    response = client.get("/api/pricing/history")
    assert response.status_code == 200, response.text
    return list(reversed(response.json()))  # oldest first for readable assertions


# ---------------------------------------------------------------- pricing history

def test_every_pricing_change_records_who_and_what(client):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
    material = client.post("/api/materials", json={"name": "No. 53 Stone"}).json()
    other = client.post("/api/materials", json={"name": "Sand"}).json()
    rate = {"supplier_id": supplier["id"], "material_id": material["id"]}
    assert client.put("/api/rates", json={**rate, "unit_price": "30"}).status_code == 200
    assert client.put("/api/rates", json={**rate, "unit_price": "30.00"}).status_code == 200  # no change
    assert client.put("/api/rates", json={**rate, "unit_price": "31.25"}).status_code == 200
    assert client.put("/api/rates", json={
        "supplier_id": supplier["id"], "material_id": other["id"], "unit_price": "12"}).status_code == 200
    # Removing a supplier also removes its rates; each removal is recorded.
    assert client.delete(f"/api/suppliers/{supplier['id']}").status_code == 204

    events = _history(client)
    summary = [(e["action"], e["supplier"], e["material"], e["old"], e["new"]) for e in events]
    assert summary[:6] == [
        ("supplier_added", "Example Aggregates", None, None, None),
        ("material_added", None, "No. 53 Stone", None, None),
        ("material_added", None, "Sand", None, None),
        ("rate_set", "Example Aggregates", "No. 53 Stone", None, "30.0000"),
        ("rate_changed", "Example Aggregates", "No. 53 Stone", "30.0000", "31.2500"),
        ("rate_set", "Example Aggregates", "Sand", None, "12.0000"),
    ]
    assert sorted(summary[6:8]) == [
        ("rate_removed", "Example Aggregates", "No. 53 Stone", "31.2500", None),
        ("rate_removed", "Example Aggregates", "Sand", "12.0000", None),
    ]
    assert summary[8] == ("supplier_removed", "Example Aggregates", None, None, None)
    assert len(summary) == 9  # the unchanged 30 -> 30.00 save added nothing
    assert {e["actor"] for e in events} == {"Test Owner"}
    assert all(e["at"].endswith(("Z", "+00:00")) for e in events)


def test_rejected_pricing_change_leaves_no_history(client):
    assert client.post("/api/suppliers", json={"name": "Example Aggregates"}).status_code == 201
    assert client.post("/api/suppliers", json={"name": "Example Aggregates"}).status_code == 409
    assert client.put("/api/rates", json={"supplier_id": 999, "material_id": 999, "unit_price": "5"}).status_code == 404
    assert [e["action"] for e in _history(client)] == ["supplier_added"]


def test_pricing_history_is_company_scoped_and_read_only_for_clerks(client):
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Contractor"))
        db.add(User(id=2, organization_id=1, username="cal", role="clerk", password_hash="x"))
        db.add(User(id=3, organization_id=2, username="rival", role="owner", password_hash="x"))
        db.commit()
    previous = app.dependency_overrides[current_user]
    try:
        _as(2, 1, "clerk", "cal")
        assert len(_history(client)) == 1  # everyone relying on costs can see rate history
        assert client.post("/api/suppliers", json={"name": "Sneaky"}).status_code == 403
        _as(3, 2, "owner", "rival")
        assert _history(client) == []
        client.post("/api/suppliers", json={"name": "Rival Supply"})
        assert [e["supplier"] for e in _history(client)] == ["Rival Supply"]
    finally:
        app.dependency_overrides[current_user] = previous
    assert [e["supplier"] for e in _history(client)] == ["Example Aggregates"]
    with api_module.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PricingEvent)) == 2


# ---------------------------------------------------------------- export

def test_excel_export_names_the_approver_without_moving_columns(client):
    project = client.post("/api/projects", json={"code": "P-1", "name": "Synthetic"}).json()["id"]
    batch = client.post(
        "/api/batches", data={"layout": "grid_2x2", "project_id": str(project)},
        files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))],
    ).json()["id"]
    tickets = client.get(f"/api/batches/{batch}/tickets").json()
    by_number = {t["ticket_number"]: t["id"] for t in tickets}
    assert client.post(f"/api/tickets/{by_number['10482']}/verify").status_code == 200
    assert client.post(f"/api/tickets/{by_number['10483']}/verify").status_code == 200
    with api_module.SessionLocal() as db:
        # Simulate a ticket approved before individual accounts existed.
        legacy = db.get(Ticket, by_number["10483"])
        legacy.verified_by_user_id = None
        legacy.verified_at = None
        db.commit()

    for url in (f"/api/batches/{batch}/export.xlsx", "/api/records/export.xlsx"):
        sheet = load_workbook(io.BytesIO(client.get(url).content))["Delivery Records"]
        header = [c.value for c in sheet[1]]
        assert header[:10] == HEADERS[:10]  # original layout unchanged
        assert header[10:] == ["Approved By", "Approved At (UTC)"]
        rows = {r[0]: r for r in sheet.iter_rows(min_row=2, values_only=True)}
        assert rows["10482"][10] == "Test Owner"
        assert rows["10482"][11] is not None and rows["10482"][11].year >= 2026
        assert rows["10483"][10] == LEGACY_APPROVER and rows["10483"][11] is None
        assert rows["77120"][10] is None and rows["77120"][11] is None  # not approved: blank


# ---------------------------------------------------------------- migration

def test_pricing_history_migration_is_additive(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_06_SCHEMA)
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision()
        assert db.execute("SELECT COUNT(*) FROM pricing_events").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
