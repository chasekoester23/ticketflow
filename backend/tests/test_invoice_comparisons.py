"""Synthetic, offline contractor invoice-line reconciliation regressions.

Every field here comes from fake/manual entries; no real invoices or paid OCR.
The endpoint compares amounts but NEVER approves an invoice for payment.
"""
from decimal import Decimal

import pytest

from app import db as db_module
from app.auth import current_user
from app.main import app
from app.models import Organization, Project, User
from app.routers import api as api_module
from .sample_data import make_scan_pdf


def _project(client):
    response = client.post("/api/projects", json={
        "code": "24-115", "name": "Test Highway Replacement",
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _tickets(client, project_id):
    response = client.post(
        "/api/batches",
        data={"layout": "grid_2x2", "project_id": str(project_id)},
        files=[("files", ("synthetic.pdf", make_scan_pdf(), "application/pdf"))],
    )
    assert response.status_code == 200, response.text
    bid = response.json()["id"]
    tickets = client.get(f"/api/batches/{bid}/tickets").json()
    assert len(tickets) == 4
    return bid, tickets


def _rate(client, amount="30"):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
    material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()
    response = client.put("/api/rates", json={
        "supplier_id": supplier["id"], "material_id": material["id"],
        "unit_price": amount,
    })
    assert response.status_code == 200, response.text
    return supplier, material


def _line(project_id, **values):
    data = dict(
        project_id=project_id,
        invoice_number="INV-4401",
        supplier_name=" example  aggregates. ",  # normalized matching
        material_name="No 53 Crushed Stone",
        date_from="2026-10-15",
        date_to="2026-10-15",
        billed_tons="24.500",
        billed_unit_price="30.0000",
        billed_subtotal="735.00",
    )
    data.update(values)
    return data


def _post(client, body):
    return client.post("/api/invoice-comparisons", json=body)


def _decimal(value):
    return Decimal(str(value))


def test_exact_match_only_after_verification_and_live_recalculation(client):
    project = _project(client)
    supplier, material = _rate(client)
    bid, tickets = _tickets(client, project)
    created = _post(client, _line(project))
    assert created.status_code == 201, created.text
    initial = created.json()
    assert initial["status"] == "no_verified_tickets"
    assert initial["pending_count"] == 1
    assert _decimal(initial["verified_tons"]) == 0
    assert initial["verified_subtotal"] is None

    tid = tickets[0]["id"]
    accepted = client.post(f"/api/tickets/{tid}/verify")
    assert accepted.status_code == 200, accepted.text
    cid = initial["id"]
    matched = client.get(f"/api/invoice-comparisons/{cid}")
    assert matched.status_code == 200
    result = matched.json()
    assert result["status"] == "matches"
    assert _decimal(result["verified_tons"]) == Decimal("24.500")
    assert _decimal(result["verified_subtotal"]) == Decimal("735.00")
    assert _decimal(result["quantity_difference"]) == 0
    assert _decimal(result["subtotal_difference"]) == 0
    assert _decimal(result["invoice_arithmetic_difference"]) == 0
    assert result["warnings"] == []
    assert result["tickets"][0]["ticket_id"] == tid
    assert result["tickets"][0]["batch_id"] == bid

    # Today the rate is 50, but the approved ticket's historical rate is 30.
    changed = client.put("/api/rates", json={
        "supplier_id": supplier["id"], "material_id": material["id"],
        "unit_price": "50",
    })
    assert changed.status_code == 200
    kept = client.get(f"/api/invoice-comparisons/{cid}").json()
    assert kept["status"] == "matches"
    assert _decimal(kept["verified_subtotal"]) == Decimal("735")

    # Editing a verified quantity revokes approval, making the line incomplete.
    client.patch(f"/api/tickets/{tid}", json={"tonnage": "25.000"})
    unapproved = client.get(f"/api/invoice-comparisons/{cid}").json()
    assert unapproved["status"] == "no_verified_tickets"
    assert unapproved["pending_count"] == 1
    assert unapproved["verified_subtotal"] is None
    assert _decimal(unapproved["verified_tons"]) == 0


def test_wrong_quantity_rate_and_subtotal_cannot_show_a_false_match(client):
    project = _project(client)
    _rate(client)
    _, tickets = _tickets(client, project)
    assert client.post(f"/api/tickets/{tickets[0]['id']}/verify").status_code == 200

    bad = _post(client, _line(
        project, billed_tons="25.000", billed_unit_price="31",
        billed_subtotal="780.00",
    ))
    assert bad.status_code == 201
    result = bad.json()
    assert result["status"] == "discrepancy"
    assert _decimal(result["quantity_difference"]) == Decimal(".500")
    assert _decimal(result["subtotal_difference"]) == Decimal("45.00")
    assert result["rate_mismatch_count"] == 1
    assert _decimal(result["invoice_arithmetic_difference"]) == Decimal("5.00")
    assert any("rate" in warning for warning in result["warnings"])
    assert any("subtotal" in warning for warning in result["warnings"])


def test_unpriced_approved_tickets_leave_subtotal_unresolved(client):
    project = _project(client)
    _, tickets = _tickets(client, project)
    tid = tickets[0]["id"]
    assert client.post(f"/api/tickets/{tid}/verify").status_code == 200
    result = _post(client, _line(project)).json()
    assert result["status"] == "incomplete"
    assert result["unpriced_count"] == 1
    assert result["verified_subtotal"] is None
    assert result["subtotal_difference"] is None
    assert _decimal(result["verified_tons"]) == Decimal("24.500")


def test_unverified_matching_tickets_block_complete_match(client):
    project = _project(client)
    _rate(client)
    _, tickets = _tickets(client, project)
    # Make a second unique ticket match the same supplier/material/date.
    tid2 = tickets[1]["id"]
    assert client.patch(f"/api/tickets/{tid2}", json={
        "material_type": "No. 53 Crushed Stone",
    }).status_code == 200
    assert client.post(f"/api/tickets/{tickets[0]['id']}/verify").status_code == 200
    result = _post(client, _line(project)).json()
    assert result["status"] == "incomplete"
    assert result["pending_count"] == 1
    assert result["verified_count"] == 1
    assert _decimal(result["verified_subtotal"]) == Decimal("735")
    assert any("still need review" in warning for warning in result["warnings"])


def test_rejects_overlapping_invoice_scopes_and_preserves_original(client):
    project = _project(client)
    first = _post(client, _line(project))
    assert first.status_code == 201
    duplicate = _post(client, _line(
        project, invoice_number="INV-OTHER", date_from="2026-10-15",
        date_to="2026-10-20",
    ))
    assert duplicate.status_code == 409
    assert len(client.get("/api/invoice-comparisons").json()) == 1
    # Nonoverlapping date windows for same supplier and material are permitted.
    second = _post(client, _line(
        project, invoice_number="INV-NEXT", date_from="2026-10-16",
        date_to="2026-10-18",
    ))
    assert second.status_code == 201
    # Separate material lines within a single invoice are allowed too.
    other = _post(client, _line(
        project, invoice_number="INV-4401", material_name="No. 8 Stone",
    ))
    assert other.status_code == 201


def test_correction_is_audited_and_cannot_override_another_line_scope(client):
    project = _project(client)
    row = _line(project)
    original = _post(client, row).json()
    edited = dict(row, billed_tons="24.75", invoice_number="INV-FIXED")
    result = client.put(f"/api/invoice-comparisons/{original['id']}", json=edited)
    assert result.status_code == 200, result.text
    assert _decimal(result.json()["billed_tons"]) == Decimal("24.75")
    audit = client.get(f"/api/invoice-comparisons/{original['id']}/audit")
    assert audit.status_code == 200
    assert {event["field"] for event in audit.json()} == {"invoice_number", "billed_tons"}
    assert client.put(f"/api/invoice-comparisons/{original['id']}", json=edited).status_code == 200
    assert len(client.get(f"/api/invoice-comparisons/{original['id']}/audit").json()) == 2

    other = _post(client, _line(
        project, invoice_number="NEXT", date_from="2026-10-16", date_to="2026-10-16",
    )).json()
    bad = client.put(f"/api/invoice-comparisons/{other['id']}", json=_line(
        project, date_from="2026-10-15", date_to="2026-10-16",
    ))
    assert bad.status_code == 409
    assert client.get(f"/api/invoice-comparisons/{other['id']}").json()["date_from"] == "2026-10-16"
    assert client.get(f"/api/invoice-comparisons/{other['id']}/audit").json() == []


@pytest.mark.parametrize("change", [
    {"date_from": "2026-10-16", "date_to": "2026-10-15"},
    {"billed_tons": "-2"},
    {"billed_tons": "1000000000"},
    {"billed_tons": "2.2345"},
    {"billed_unit_price": "0"},
    {"billed_subtotal": "NaN"},
    {"supplier_name": "   "},
    {"material_name": "   "},
    {"billed_subtotal": "-0.01"},
    {"unexpected_field": "not allowed"},
])
def test_rejects_invalid_invoice_inputs_without_writing(client, change):
    project = _project(client)
    bad = _post(client, _line(project, **change))
    assert bad.status_code == 422, bad.text
    assert client.get("/api/invoice-comparisons").json() == []


def test_project_and_company_isolation(client):
    project = _project(client)
    row = _post(client, _line(project))
    assert row.status_code == 201
    foreign_id = row.json()["id"]

    # An unknown project ID cannot be used to insert another company's data.
    assert _post(client, _line(99999)).status_code == 404

    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Second Customer"))
        db.flush()
        other_project = Project(
            organization_id=2, code="OTHER", name="Other Customer's Project",
        )
        db.add(other_project)
        db.commit()
        other_project_id = other_project.id

    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=2, username="other-test-owner",
        password_hash="fixture", active=True,
    )
    assert client.get("/api/invoice-comparisons").json() == []
    for path in (
        f"/api/invoice-comparisons/{foreign_id}",
        f"/api/invoice-comparisons/{foreign_id}/audit",
    ):
        assert client.get(path).status_code == 404
    assert client.put(f"/api/invoice-comparisons/{foreign_id}", json=_line(
        other_project_id,
    )).status_code == 404
    assert _post(client, _line(project)).status_code == 404
    isolated = _post(client, _line(other_project_id))
    assert isolated.status_code == 201
    assert isolated.json()["status"] == "no_verified_tickets"
    assert len(client.get("/api/invoice-comparisons").json()) == 1

    # Cross-org tickets must never contribute to this organization's figures.
    assert client.get(f"/api/invoice-comparisons/{isolated.json()['id']}").json()["verified_count"] == 0


def test_invoice_migration_is_additive_and_keeps_old_tickets(tmp_path):
    import sqlite3
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    dbpath = tmp_path / "before-invoice.db"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dbpath}")
    command.upgrade(cfg, "20261029_05")
    with sqlite3.connect(dbpath) as conn:
        conn.execute("INSERT INTO organizations(id,name) VALUES(1,'Existing contractor')")
        conn.execute(
            "INSERT INTO projects(id,organization_id,code,name,status) "
            "VALUES(1,1,'ABC','Existing project','active')"
        )
        conn.commit()
    command.upgrade(cfg, "head")
    with sqlite3.connect(dbpath) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261029_06"
        assert conn.execute("SELECT name FROM projects WHERE id=1").fetchone()[0] == "Existing project"
        assert conn.execute("SELECT COUNT(*) FROM invoice_comparisons").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM invoice_comparison_events").fetchone()[0] == 0
