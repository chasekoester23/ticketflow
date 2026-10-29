"""Direct IDOR tests: a signed-in company's owner cannot touch another's data.

Uses real authenticated cookies with TWO owners, the actual FastAPI routes,
and fixture extraction that never calls a paid AI provider.
"""
from sqlalchemy import select

from app.auth import current_user, password_hash
from app.db import get_db
from app.main import app
from app.models import Organization, User
from .sample_data import make_scan_pdf


def _login(client, username):
    r = client.post("/api/auth/login", json={
        "username": username, "password": "correct-long-password",
    })
    assert r.status_code == 200, r.text
    assert client.get("/api/auth/me").status_code == 200


def _seed_users():
    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        second = Organization(id=2, name="Second Contractor")
        db.add(second)
        db.flush()
        db.add_all([
            User(organization_id=1, username="alice",
                 password_hash=password_hash("correct-long-password")),
            User(organization_id=2, username="bob",
                 password_hash=password_hash("correct-long-password")),
        ])
        db.commit()
    finally:
        gen.close()


def _upload(client, project_id):
    r = client.post(
        "/api/batches", data={"layout": "grid_2x2", "project_id": str(project_id)},
        files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))],
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_companies_cannot_read_modify_or_price_each_others_data(client):
    _seed_users()
    previous = app.dependency_overrides.pop(current_user)
    try:
        _login(client, "alice")
        p = client.post("/api/projects", json={"code": "TEST-001", "name": "Alice Project"})
        assert p.status_code == 201, p.text
        a_project = p.json()["id"]
        a_batch = _upload(client, a_project)
        a_tickets = client.get(f"/api/batches/{a_batch}/tickets").json()
        a_ticket = a_tickets[0]["id"]
        assert client.post(f"/api/tickets/{a_ticket}/verify").status_code == 200
        a_supplier = client.post("/api/suppliers", json={"name": "Common Quarry"}).json()["id"]
        a_material = client.post("/api/materials", json={"name": "Common Stone"}).json()["id"]
        a_rate = client.put("/api/rates", json={
            "supplier_id": a_supplier, "material_id": a_material, "unit_price": "18",
        }).json()["id"]
        assert client.get("/api/dashboard").json()["verified"] == 1

        _login(client, "bob")
        assert client.get("/api/projects").json() == []
        assert client.get("/api/batches").json() == []
        assert client.get("/api/records").json() == []
        assert client.get("/api/records/export.xlsx").status_code == 200
        assert client.get("/api/dashboard").json()["total_tons"] in (0, "0")
        assert client.get("/api/suppliers").json() == []
        assert client.get("/api/materials").json() == []
        assert client.get("/api/rates").json() == []

        # Guessing a numeric ID must not leak the other company's existence.
        for path in (
            f"/api/batches/{a_batch}", f"/api/batches/{a_batch}/tickets",
            f"/api/batches/{a_batch}/export.xlsx",
            f"/api/tickets/{a_ticket}/image", f"/api/tickets/{a_ticket}/audit",
        ):
            r = client.get(path)
            assert r.status_code == 404, (path, r.status_code, r.text)

        for path in (
            f"/api/tickets/{a_ticket}/verify",
            f"/api/tickets/{a_ticket}/flag",
            f"/api/tickets/{a_ticket}/skip",
        ):
            assert client.post(path).status_code == 404
        assert client.patch(f"/api/tickets/{a_ticket}", json={"tonnage": "99"}).status_code == 404
        assert client.patch(f"/api/projects/{a_project}", json={"status": "archived"}).status_code == 404
        assert client.post(f"/api/batches/{a_batch}/retry").status_code == 404
        assert client.delete(f"/api/rates/{a_rate}").status_code == 404
        assert client.delete(f"/api/suppliers/{a_supplier}").status_code == 404
        assert client.delete(f"/api/materials/{a_material}").status_code == 404

        # Cannot smuggle Company A's IDs via a Company B form or rate update.
        rejected = client.post("/api/batches", data={
            "project_id": str(a_project), "layout": "grid_2x2",
        }, files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
        assert rejected.status_code == 404
        assert client.put("/api/rates", json={
            "supplier_id": a_supplier, "material_id": a_material, "unit_price": "1",
        }).status_code == 404

        # Same customer-facing names and codes can coexist in different orgs.
        b_project_resp = client.post("/api/projects", json={"code": "TEST-001", "name": "Bob Project"})
        assert b_project_resp.status_code == 201, b_project_resp.text
        b_project = b_project_resp.json()["id"]
        b_supplier_resp = client.post("/api/suppliers", json={"name": "Common Quarry"})
        b_material_resp = client.post("/api/materials", json={"name": "Common Stone"})
        assert b_supplier_resp.status_code == b_material_resp.status_code == 201
        b_batch = _upload(client, b_project)
        b_tickets = client.get(f"/api/batches/{b_batch}/tickets").json()
        assert len(b_tickets) == 4
        assert all(t["duplicates"] == [] for t in b_tickets), "Duplicate flags leaked across orgs"
        assert len(client.get("/api/records").json()) == 4
        assert len(client.get("/api/projects").json()) == 1
        assert client.get("/api/dashboard").json()["verified"] == 0

        # Verify old company still sees only its own records and prices.
        _login(client, "alice")
        assert len(client.get("/api/projects").json()) == 1
        assert len(client.get("/api/records").json()) == 4
        assert client.get("/api/dashboard").json()["verified"] == 1
        assert len(client.get("/api/rates").json()) == 1
        assert client.get(f"/api/batches/{b_batch}").status_code == 404
        assert client.get(f"/api/tickets/{b_tickets[0]['id']}/image").status_code == 404
    finally:
        app.dependency_overrides[current_user] = previous


def test_new_users_cannot_choose_company_by_query_or_header(client):
    _seed_users()
    previous = app.dependency_overrides.pop(current_user)
    try:
        _login(client, "bob")
        r = client.post("/api/projects?organization_id=1", headers={
            "X-Organization-ID": "1",
        }, json={"code": "SECRET", "name": "B owned project"})
        assert r.status_code == 201
        gen = app.dependency_overrides[get_db]()
        db = next(gen)
        try:
            from app.models import Project
            project = db.scalar(select(Project).where(Project.code == "SECRET"))
            assert project.organization_id == 2
        finally:
            gen.close()
    finally:
        app.dependency_overrides[current_user] = previous
