"""Two companies attacking each other with REAL sign-ins: real passwords, real session
cookies, the real current_user and company lookups. No identity override anywhere.

test_t_tenant_sweep.py proves the routes with a stand-in identity. This proves the same
with the whole authentication chain in place, then guesses ids: every id of every kind,
on every read with an id in its path, must look exactly like an id that doesn't exist
unless it's the caller's own. Company 1's attempts must change nothing of company 2's,
and its dashboard, activity, health and queue numbers must never include company 2.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth as auth_module
from app.auth import current_user, password_hash
from app.config import settings
from app.main import app
from app.models import (Batch, InvoiceComparison, InvoiceDocument, InvoiceDraftLine, InvoiceTicketLink, Material, NameAlias,
                        ProcessingJob, Project, SavedView, ScanSlot, Supplier, SupplierRate, Ticket, UploadedFile, User)

from .test_query_budgets import _session
from .test_t_tenant_sweep import MARK, NOWHERE, _answer, _record, _routes, _send, _text_of, world  # noqa: F401

PASSWORDS = {1: "alpha-owner-long-password", 2: "bravo-owner-long-password"}

# Which company a record belongs to, straight from the database.
_OWNER = {
    "batch": lambda i: select(Batch.organization_id).where(Batch.id == i),
    "ticket": lambda i: select(Ticket.organization_id).where(Ticket.id == i),
    "file": lambda i: select(Batch.organization_id).join(UploadedFile, UploadedFile.batch_id == Batch.id).where(UploadedFile.id == i),
    "slot": lambda i: select(Batch.organization_id).join(ScanSlot, ScanSlot.batch_id == Batch.id).where(ScanSlot.id == i),
    "document": lambda i: select(InvoiceDocument.organization_id).where(InvoiceDocument.id == i),
    "draft": lambda i: select(InvoiceDocument.organization_id).join(
        InvoiceDraftLine, InvoiceDraftLine.document_id == InvoiceDocument.id).where(InvoiceDraftLine.id == i),
    "line": lambda i: select(InvoiceComparison.organization_id).where(InvoiceComparison.id == i),
    "link": lambda i: select(InvoiceComparison.organization_id).join(
        InvoiceTicketLink, InvoiceTicketLink.comparison_id == InvoiceComparison.id).where(InvoiceTicketLink.id == i),
    "view": lambda i: select(SavedView.organization_id).where(SavedView.id == i),
    "member": lambda i: select(User.organization_id).where(User.id == i),
    "supplier": lambda i: select(Supplier.organization_id).where(Supplier.id == i),
    "material": lambda i: select(Material.organization_id).where(Material.id == i),
    "rate": lambda i: select(SupplierRate.organization_id).where(SupplierRate.id == i),
    "alias": lambda i: select(NameAlias.organization_id).where(NameAlias.id == i),
    "project": lambda i: select(Project.organization_id).where(Project.id == i),
}


def _owner_of(kind: str, record_id: int) -> int | None:
    with _session() as db:
        return db.scalar(_OWNER[kind](record_id))


def _signed_in(org: int) -> TestClient:
    browser = TestClient(app)
    browser.headers.update({"X-TicketFlow-Request": "1"})
    response = browser.post("/api/auth/login", json={"username": f"owner-{org}", "password": PASSWORDS[org]})
    assert response.status_code == 200, response.text
    return browser


@pytest.fixture()
def sessions(client, world, monkeypatch):
    """The same two companies as the sweep, then real accounts signed in for each. The
    per-person limits are lifted: guessing makes hundreds of requests, and a 429 says
    nothing about whose record it is."""
    for bucket in ("uploads", "exports", "reports"):
        monkeypatch.setattr(settings, f"rate_limit_{bucket}_per_minute", 0)
    with _session() as db:
        for org in (1, 2):
            owner = db.get(User, org * 10)
            owner.password_hash = password_hash(PASSWORDS[org])
            owner.must_change_password = False
        db.commit()
    app.dependency_overrides.pop(current_user)
    auth_module.reset_login_throttle()
    client.headers.pop("X-Test-As", None)
    yield {org: _signed_in(org) for org in (1, 2)}
    auth_module.reset_login_throttle()


def _view(browser: TestClient) -> list:
    """Everything a company can read about itself, compared before and after an attack."""
    return [browser.get(path).json() for path in (
        "/api/projects", "/api/batches", "/api/records", "/api/suppliers", "/api/materials", "/api/rates", "/api/name-aliases",
        "/api/saved-views", "/api/invoice-comparisons", "/api/invoice-documents", "/api/team/members", "/api/dashboard",
        "/api/team/events", "/api/pricing/history")]


def test_each_session_is_its_own_company_whatever_the_request_says(sessions, world):
    me = sessions[1]
    assert me.get("/api/auth/me").json()["organization_name"] != sessions[2].get("/api/auth/me").json()["organization_name"]
    # A company id in a header or a body is never believed.
    created = me.post("/api/projects", json={"code": "CLAIM", "name": "Claimed", "organization_id": 2},
                      headers={"X-Test-As": "2:owner", "X-Organization-Id": "2"})
    assert created.status_code in (201, 422)
    if created.status_code == 201:
        assert _owner_of("project", created.json()["id"]) == 1
    assert all(project["code"] != "CLAIM" for project in sessions[2].get("/api/projects").json())


def test_another_companys_ids_in_any_path_look_missing_with_real_sessions(sessions, world):
    me, mine, theirs = sessions[1], world[1], world[2]
    before = _view(sessions[2])
    checked = 0
    for method, route in _routes():
        names = [n for n in re.findall(r"{([^}]+)}", route.path) if n != "page_no"]
        if not names:
            continue
        for chosen in [names] + ([[n] for n in names] if len(names) > 1 else []):
            other = {n: theirs[_record(n, route.path)] for n in chosen}
            missing = {n: NOWHERE for n in chosen}
            leaked = _answer(_send(me, method, route.path, mine, path_swap=other))
            absent = _answer(_send(me, method, route.path, mine, path_swap=missing))
            assert leaked == absent, f"{method} {route.path} with company 2's {chosen}: {leaked} but a missing id gives {absent}"
            assert leaked[0] == 404, (method, route.path, leaked)
            checked += 1
    assert checked > 50
    assert _view(sessions[2]) == before


def _guessable_reads() -> list[tuple[str, str]]:
    """GET routes naming exactly one record in their path."""
    found = []
    for method, route in _routes():
        names = [n for n in re.findall(r"{([^}]+)}", route.path) if n != "page_no"]
        if method == "GET" and len(names) == 1:
            found.append((route.path, names[0]))
    return found


def test_guessing_every_id_finds_only_your_own_records(sessions, world):
    """Counting up through every id, as an attacker would. Ids of the other company must be
    indistinguishable from ids nobody has; ids of your own must open."""
    me, mine = sessions[1], world[1]
    highest = 3 + max(value for ids in world.values() for value in ids.values())
    reads = _guessable_reads()
    assert len(reads) >= 20
    tried = theirs_tried = 0
    for path, name in reads:
        kind = _record(name, path)
        absent = _answer(_send(me, "GET", path, mine, path_swap={name: NOWHERE}), NOWHERE)
        for guess in range(1, highest + 1):
            owner = _owner_of(kind, guess)
            response = _send(me, "GET", path, mine, path_swap={name: guess})
            if owner == 1:
                assert response.status_code != 404, (path, guess, response.text[:200])
                assert MARK[2] not in _text_of(response), (path, guess)
            else:
                assert _answer(response, guess) == absent, f"GET {path} id {guess} (company {owner}): {_answer(response, guess)}"
                theirs_tried += owner == 2
            tried += 1
    assert tried > 300 and theirs_tried >= 25


def test_batches_retries_dashboard_activity_and_health_stay_within_each_company(sessions, world):
    me, them = sessions[1], sessions[2]
    their_batch = world[2]["batch"]
    before = _view(them)
    # Retrying, re-reading or reassigning someone else's batch.
    for path in (f"/api/batches/{their_batch}/retry", f"/api/batches/{their_batch}/manifest/rebuild"):
        assert me.post(path).status_code == 404, path
    # A job waiting in company 2's queue is company 2's business only.
    with _session() as db:
        db.add(ProcessingJob(batch_id=their_batch, status="pending"))
        db.commit()
    mine_status, theirs_status = me.get("/api/system/status").json(), them.get("/api/system/status").json()
    assert mine_status["queue"]["pending"] == 0 and mine_status["metrics"]["queue"]["pending"] == 0
    assert theirs_status["queue"]["pending"] == 1 and theirs_status["metrics"]["queue"]["pending"] == 1
    # Dashboards, activity and history never include the other company.
    for path in ("/api/dashboard", "/api/team/events", "/api/team/members", "/api/pricing/history", "/api/onboarding",
                 "/api/system/status", "/api/batches", "/api/batches/queue", "/api/records/export.csv", "/api/reports/pilot"):
        for org, browser in sessions.items():
            response = browser.get(path)
            assert response.status_code == 200, (path, response.status_code)
            assert MARK[3 - org] not in _text_of(response), (path, org)
            assert f"owner-{3 - org}" not in response.text and f"Owner {3 - org}" not in response.text, (path, org)
    with _session() as db:
        db.query(ProcessingJob).filter(ProcessingJob.status == "pending", ProcessingJob.batch_id == their_batch).delete()
        db.commit()
    assert _view(them) == before


def test_a_signed_out_or_foreign_session_reaches_nothing(sessions, world):
    me = sessions[1]
    token = me.cookies.get(auth_module.COOKIE_NAME)
    assert me.post("/api/auth/logout").status_code == 200
    replay = TestClient(app)
    replay.cookies.set(auth_module.COOKIE_NAME, token)
    for path in ("/api/records", f"/api/tickets/{world[1]['ticket']}/image", f"/api/batches/{world[2]['batch']}", "/api/system/status"):
        assert replay.get(path).status_code == 401, path


def test_logs_hold_no_passwords_keys_document_text_or_values(sessions, world, caplog, monkeypatch):
    """Everything an owner does in a session, logged at DEBUG by every logger: none of the
    secrets, typed values or searched-for text may appear in any line."""
    import logging
    secrets_ = {"AIzaTEST-NOT-A-REAL-KEY-0001", "c2VjcmV0LWJhY2t1cC1rZXktbm90LXJlYWwtMDAwMDA=", "https://hooks.example/T0/SECRET-HOOK"}
    monkeypatch.setattr(settings, "gemini_api_key", "AIzaTEST-NOT-A-REAL-KEY-0001")
    monkeypatch.setattr(settings, "backup_encryption_key", "c2VjcmV0LWJhY2t1cC1rZXktbm90LXJlYWwtMDAwMDA=")
    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.example/T0/SECRET-HOOK")
    me, ids = sessions[1], world[1]
    with caplog.at_level(logging.DEBUG):
        assert me.post("/api/auth/login", json={"username": "owner-1", "password": "wrong-guess-WRONGPASS"}).status_code == 401
        me = _signed_in(1)
        assert me.patch(f"/api/tickets/{ids['ticket2']}", json={"ticket_number": "TYPED-NUMBER-77123", "job_location": "TYPED-PLACE-Q"}).status_code == 200
        assert me.get("/api/records/search", params={"q": "SEARCHED-FOR-TEXT"}).status_code == 200
        assert me.get("/api/records/export.csv").status_code == 200
        assert me.get(f"/api/tickets/{ids['ticket']}/image").status_code == 200
        assert me.get("/api/system/status").status_code == 200
        assert me.post("/api/auth/password", json={"current_password": PASSWORDS[1], "new_password": "brand-new-OWNER-PASSPHRASE"}).status_code == 200
    # httpx here is the test's own client, not the server: it logs the URL it requested.
    server = [record for record in caplog.records if not record.name.startswith(("httpx", "httpcore"))]
    logged = " ".join(f"{record.name} {record.getMessage()} {record.args}" for record in server)
    assert caplog.records, "nothing was logged, so this checked nothing"
    for secret in [*secrets_, PASSWORDS[1], "wrong-guess-WRONGPASS", "brand-new-OWNER-PASSPHRASE", "TYPED-NUMBER-77123",
                   "TYPED-PLACE-Q", "SEARCHED-FOR-TEXT", MARK[1], MARK[2], "10482"]:
        assert secret not in logged, secret
