"""Row-level security on PostgreSQL: the database itself keeps each company to its own rows.

PostgreSQL only (CI). The two fictional companies come from the tenant sweep, built
through the real API. Row-level security is switched on for each test and off again
after, so the rest of the suite runs exactly as before.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, Request
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import db as db_module
from app.auth import current_user, password_hash
from app.config import settings
from app.db import TENANT_ROLE, scope_to_organization
from app.main import app
from app.models import Batch, User
from app.routers import api as api_module
from app.services import row_security, system_health

from .conftest import TEST_DATABASE_URL
from .test_t_tenant_sweep import MARK, NOT_SHOWING_MARKS, _as, _identity, _routes, _send, _text_of, world  # noqa: F401

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="Row-level security is PostgreSQL's (CI).")
COMPANY_COLUMN = {"batches", "tickets", "projects", "invoice_comparisons", "invoice_documents", "extraction_usage"}


@pytest.fixture()
def rls(world, postgres_engine, monkeypatch):
    with postgres_engine.begin() as connection:
        tables = row_security.enable(connection)
        assert row_security.problems(connection) == []
    monkeypatch.setattr(settings, "postgres_rls", True)

    def scoped_identity(request: Request, db: Session = Depends(db_module.get_db)) -> User:
        user = _identity(request)
        scope_to_organization(db, user.organization_id)  # As auth.authenticated_user does.
        return user

    app.dependency_overrides[current_user] = scoped_identity
    try:
        yield {"world": world, "tables": tables, "engine": postgres_engine}
    finally:
        with postgres_engine.begin() as connection:
            row_security.disable(connection)


def _as_tenant(engine, org: int | None, statement: str, **params):
    """One statement as the company role, with app.organization_id set to org, or unset."""
    with engine.begin() as connection:
        connection.exec_driver_sql(f"SET LOCAL ROLE {TENANT_ROLE}")
        if org is not None:
            connection.execute(text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(org)})
        return connection.execute(text(statement), params).all()


def test_every_core_table_has_a_policy_and_it_is_on(rls):
    assert set(rls["tables"]) >= {"tickets", "batches", "uploaded_files", "projects", "audit_events", "invoice_comparisons",
                                  "invoice_documents", "invoice_ticket_refs", "invoice_ticket_links", "invoice_draft_lines",
                                  "invoice_comparison_events", "invoice_document_events", "scan_slots", "scan_events",
                                  "extraction_usage"}
    with rls["engine"].connect() as connection:
        assert all(row_security.status(connection)["tables"].values())


def test_without_a_company_the_role_sees_nothing_at_all(rls):
    for table in rls["tables"]:
        assert _as_tenant(rls["engine"], None, f"SELECT count(*) FROM {table}")[0][0] == 0, table


def test_a_company_sees_only_its_own_rows_in_every_table_and_another_sees_nothing(rls):
    engine = rls["engine"]
    with engine.connect() as owner:
        everything = {t: owner.scalar(text(f"SELECT count(*) FROM {t}")) for t in rls["tables"]}
    seen = {org: {t: _as_tenant(engine, org, f"SELECT count(*) FROM {t}")[0][0] for t in rls["tables"]} for org in (1, 2)}
    for table in rls["tables"]:
        assert seen[1][table] + seen[2][table] == everything[table], table
        assert _as_tenant(engine, 99, f"SELECT count(*) FROM {table}")[0][0] == 0, f"a company with no rows saw {table}"
    for table in ("tickets", "batches", "uploaded_files", "projects", "audit_events", "scan_slots", "invoice_comparisons", "invoice_documents"):
        assert seen[1][table] > 0 and seen[2][table] > 0, table
    for table in COMPANY_COLUMN & set(rls["tables"]):
        for org in (1, 2):
            assert {row[0] for row in _as_tenant(engine, org, f"SELECT DISTINCT organization_id FROM {table}")} <= {org}, table


def test_a_query_that_forgets_the_company_filter_still_gets_only_its_own(rls):
    with api_module.SessionLocal() as db:
        scope_to_organization(db, 2)
        texts = " ".join(job for job in db.scalars(text("SELECT job_location FROM tickets")) if job)
        assert MARK[2] in texts and MARK[1] not in texts
        assert {b.organization_id for b in db.scalars(select(Batch))} == {2}


def test_writing_into_another_companys_records_is_refused(rls):
    batch = rls["world"][2]["batch"]
    with pytest.raises(DBAPIError, match="row-level security"):
        _as_tenant(rls["engine"], 1, "INSERT INTO batches (organization_id, layout, intake_mode, status, created_at) "
                                     "VALUES (2, 'single', 'manual', 'uploaded', now())")
    assert _as_tenant(rls["engine"], 1, "UPDATE batches SET status = 'failed' WHERE id = :id RETURNING id", id=batch) == []
    with rls["engine"].connect() as owner:
        assert owner.scalar(text("SELECT status FROM batches WHERE id = :id"), {"id": batch}) != "failed"


def test_history_stays_append_only_for_the_company_role_too(rls):
    with pytest.raises(DBAPIError, match="append-only"):
        _as_tenant(rls["engine"], 1, "UPDATE audit_events SET field = 'changed'")
    with pytest.raises(DBAPIError, match="append-only"):
        _as_tenant(rls["engine"], 1, "DELETE FROM audit_events")


@pytest.mark.parametrize("me, other", [(1, 2), (2, 1)])
def test_every_read_in_the_app_still_works_with_row_security_on(rls, client, me, other):
    _as(client, me)
    read = 0
    for method, route in _routes():
        if method != "GET":
            continue
        response = _send(client, method, route.path, rls["world"][me])
        assert response.status_code == 200, (route.path, response.status_code, response.text[:200])
        assert MARK[other] not in _text_of(response), route.path
        assert MARK[me] in _text_of(response) or not response.headers.get("content-type", "").startswith(("application/json", "text/csv")) \
            or route.path in NOT_SHOWING_MARKS, route.path
        read += 1
    assert read > 35


def test_work_and_the_worker_still_work_with_row_security_on(rls, client):
    """Upload, read by the worker as the company, edit and approve."""
    _as(client, 2)
    ids = rls["world"][2]
    from .test_t_tenant_sweep import SCAN
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "extract", "project_id": str(ids["project"])},
                        files=[("files", ("again.pdf", SCAN, "application/pdf"))])
    assert batch.status_code == 200, batch.text
    tickets = client.get(f"/api/batches/{batch.json()['id']}/tickets").json()
    assert len(tickets) == 4 and client.get(f"/api/batches/{batch.json()['id']}").json()["status"] == "ready_for_review"
    edited = client.patch(f"/api/tickets/{tickets[0]['id']}", json=dict(
        ticket_number="77001", delivery_date="2026-09-16", material_type="Sand", tonnage="12.500",
        supplier="Example Aggregates", job_location=MARK[2]))
    assert edited.status_code == 200, edited.text
    assert client.post(f"/api/tickets/{tickets[0]['id']}/verify").status_code == 200
    assert _as_tenant(rls["engine"], 2, "SELECT count(*) FROM extraction_usage")[0][0] == 4
    assert _as_tenant(rls["engine"], 1, "SELECT count(*) FROM extraction_usage")[0][0] == 0


def test_a_real_sign_in_scopes_the_request_to_its_company(rls, client, monkeypatch):
    with api_module.SessionLocal() as db:
        db.add(User(organization_id=2, username="rls-owner", display_name="RLS Owner",
                    password_hash=password_hash("fictional-rls-password"), role="owner"))
        db.commit()
    app.dependency_overrides.pop(current_user)
    seen = []
    real = system_health.build

    def build(db, organization_id):
        seen.append(tuple(db.execute(text("SELECT current_user, current_setting('app.organization_id', true)")).one()))
        return real(db, organization_id)

    monkeypatch.setattr(system_health, "build", build)
    client.headers.pop("X-Test-As", None)
    assert client.post("/api/auth/login", json={"username": "rls-owner", "password": "fictional-rls-password"}).status_code == 200
    assert client.get("/api/system/status").status_code == 200
    assert seen == [(TENANT_ROLE, "2")]
    batches = client.get("/api/batches").json()
    assert batches and MARK[1] not in str(batches)
