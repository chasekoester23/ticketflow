"""Alternate spellings suggest; only a person changes a ticket. All data fictional."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth import current_user
from app.main import app
from app.models import NameAlias, Organization, User
from app.routers import api as api_module
from .conftest import FixtureExtractor
from .migration_helpers import alembic_config, head_revision
from .sample_data import make_page, make_pdf

READ = dict(ticket_number="88120", delivery_date="2026-10-15", material_type="53 Stone",
            tonnage="24.50", supplier="Ex. Agg. North Yard", job_location="North Lot")


def _catalog(client, rate="30"):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
    material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()
    assert client.put("/api/rates", json={"supplier_id": supplier["id"], "material_id": material["id"],
                                          "unit_price": rate}).status_code == 200
    return supplier, material


def _alias(client, kind, alias, target):
    return client.post("/api/name-aliases", json={"kind": kind, "alias": alias, "target_id": target})


def _read_ticket(client, monkeypatch, values=READ):
    monkeypatch.setattr(api_module, "get_extractor", lambda: FixtureExtractor([values]))
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "extract"},
                        files=[("files", ("fictional.pdf", make_pdf([make_page([values, None, None, None])]), "application/pdf"))])
    assert batch.status_code == 200, batch.text
    return batch.json()["id"], client.get(f"/api/batches/{batch.json()['id']}/tickets").json()[0]


def test_alias_suggests_but_never_changes_the_ticket_or_its_price(client, monkeypatch):
    supplier, material = _catalog(client)
    assert _alias(client, "supplier", "EX AGG north-yard", supplier["id"]).status_code == 201
    assert _alias(client, "material", "53 stone", material["id"]).status_code == 201
    _, ticket = _read_ticket(client, monkeypatch)

    assert ticket["suggestions"] == {"supplier": "Example Aggregates", "material_type": "No. 53 Crushed Stone"}
    # Still as written, with no rate: the alias alone changes nothing.
    assert ticket["supplier"] == READ["supplier"] and ticket["material_type"] == READ["material_type"]
    assert ticket["unit_price"] is None
    assert ticket["as_read"]["supplier"] == READ["supplier"]

    applied = client.patch(f"/api/tickets/{ticket['id']}", json={
        "supplier": "Example Aggregates", "material_type": "No. 53 Crushed Stone"}).json()
    assert applied["suggestions"] == {}
    assert Decimal(applied["unit_price"]) == Decimal("30")
    assert applied["as_read"]["supplier"] == READ["supplier"]  # what was read stays visible
    assert applied["status"] == "needs_review"  # still a person's decision
    audit = client.get(f"/api/tickets/{ticket['id']}/audit").json()
    assert {(e["field"], e["old"], e["new"], e["actor"]) for e in audit} >= {
        ("supplier", READ["supplier"], "Example Aggregates", "Test Owner"),
    }


def test_matching_is_exact_never_fuzzy(client, monkeypatch):
    supplier, _ = _catalog(client)
    _alias(client, "supplier", "Example Aggregate North", supplier["id"])
    _, ticket = _read_ticket(client, monkeypatch, dict(READ, supplier="Example Aggregates North"))
    assert ticket["suggestions"] == {}


def test_new_alias_never_reprices_or_changes_approved_tickets(client, monkeypatch):
    supplier, material = _catalog(client)
    _, ticket = _read_ticket(client, monkeypatch)
    client.patch(f"/api/tickets/{ticket['id']}", json={"unit_price_override": "12.5"})
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    before = client.get(f"/api/tickets/{ticket['id']}/audit").json()
    total = client.get("/api/dashboard").json()["total_cost"]

    _alias(client, "supplier", READ["supplier"], supplier["id"])
    _alias(client, "material", READ["material_type"], material["id"])
    client.put("/api/rates", json={"supplier_id": supplier["id"], "material_id": material["id"], "unit_price": "99"})

    tickets = client.get(f"/api/batches/{ticket['batch_id']}/tickets").json()
    assert tickets[0]["status"] == "verified" and tickets[0]["supplier"] == READ["supplier"]
    assert Decimal(tickets[0]["unit_price"]) == Decimal("12.5")
    assert client.get("/api/dashboard").json()["total_cost"] == total
    assert client.get(f"/api/tickets/{ticket['id']}/audit").json() == before
    # A suggestion may show, but applying it is an explicit edit that withdraws approval.
    assert tickets[0]["suggestions"]["supplier"] == "Example Aggregates"
    edited = client.patch(f"/api/tickets/{ticket['id']}", json={"supplier": "Example Aggregates"}).json()
    assert edited["status"] == "needs_review" and edited["verified_by"] is None


def test_aliases_and_rates_never_cross_companies(client, monkeypatch):
    supplier, _ = _catalog(client)
    assert _alias(client, "supplier", READ["supplier"], supplier["id"]).status_code == 201
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        db.flush()
        db.add(User(id=2, organization_id=2, username="other", password_hash="fixture", role="owner"))
        db.commit()
    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=2, username="other", password_hash="fixture", active=True,
        role="owner", must_change_password=False)
    assert client.get("/api/name-aliases").json() == []
    # Another company can't aim an alias at our supplier, or remove ours.
    assert _alias(client, "supplier", "Anything", supplier["id"]).status_code == 404
    assert client.delete("/api/name-aliases/1").status_code == 404
    # The same spelling means nothing here, and can mean something else.
    _, ticket = _read_ticket(client, monkeypatch)
    assert ticket["suggestions"] == {} and ticket["unit_price"] is None
    own = client.post("/api/suppliers", json={"name": "Different Quarry"}).json()
    assert _alias(client, "supplier", READ["supplier"], own["id"]).status_code == 201
    assert client.get(f"/api/batches/{ticket['batch_id']}/tickets").json()[0]["suggestions"]["supplier"] == "Different Quarry"


def test_one_spelling_means_one_thing(client):
    supplier, material = _catalog(client)
    other = client.post("/api/suppliers", json={"name": "Other Quarry"}).json()
    assert _alias(client, "supplier", "ex agg", supplier["id"]).status_code == 201
    assert _alias(client, "supplier", "EX. AGG.", other["id"]).status_code == 409
    assert _alias(client, "supplier", "example aggregates", other["id"]).status_code == 409  # a real name
    assert _alias(client, "supplier", "...", supplier["id"]).status_code == 422
    assert client.post("/api/suppliers", json={"name": "Ex Agg"}).status_code == 409
    # The same text as a material is a separate thing.
    assert _alias(client, "material", "ex agg", material["id"]).status_code == 201


def test_removal_keeps_history_and_a_deleted_supplier_ends_its_aliases(client, monkeypatch):
    supplier, _ = _catalog(client)
    created = _alias(client, "supplier", READ["supplier"], supplier["id"]).json()
    assert client.delete(f"/api/name-aliases/{created['id']}").status_code == 204
    assert client.delete(f"/api/name-aliases/{created['id']}").status_code == 204  # repeatable
    [row] = client.get("/api/name-aliases").json()
    assert row["removed_by"] == "Test Owner" and row["added_by"] == "Test Owner"
    _, ticket = _read_ticket(client, monkeypatch)
    assert ticket["suggestions"] == {}

    # The spelling can be saved again, then ends with its supplier.
    assert _alias(client, "supplier", READ["supplier"], supplier["id"]).status_code == 201
    assert client.delete(f"/api/suppliers/{supplier['id']}").status_code == 204
    assert all(a["removed_at"] for a in client.get("/api/name-aliases").json())
    assert client.get(f"/api/batches/{ticket['batch_id']}/tickets").json()[0]["suggestions"] == {}


def test_unit_problems_stay_visible_and_are_never_converted(client, monkeypatch):
    supplier, _ = _catalog(client)
    _alias(client, "supplier", READ["supplier"], supplier["id"])
    _, ticket = _read_ticket(client, monkeypatch, dict(READ, tonnage=None))
    assert ticket["tonnage"] is None and "as_read" in ticket and "tonnage" not in ticket["as_read"]
    assert any("Tonnage" in flag for flag in ticket["flags"])
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 422


@pytest.mark.parametrize("role, status", [("clerk", 403), ("viewer", 403), ("manager", 201)])
def test_only_managers_and_owners_change_aliases(client, role, status):
    supplier, _ = _catalog(client)
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="test-owner", password_hash="fixture", active=True,
        role=role, must_change_password=False)
    assert _alias(client, "supplier", "ex agg", supplier["id"]).status_code == status
    assert client.get("/api/name-aliases").status_code == 200


def test_migration_is_additive(tmp_path):
    import sqlite3
    from alembic import command

    path = tmp_path / "before-aliases.db"
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "20261105_10")
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision()
        assert conn.execute("SELECT COUNT(*) FROM name_aliases").fetchone()[0] == 0
