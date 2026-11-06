"""Individual accounts, company roles, team administration and audit identity.

Every request here uses REAL session cookies (the conftest owner override is
removed), real FastAPI routes, synthetic tickets and no paid AI provider.
"""
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import sessionmaker

from app import auth as auth_module
from app.auth import COOKIE_NAME, current_user, password_hash
from app.main import app
from app.models import AccountEvent, AuditEvent, LoginSession, Organization, Ticket, User
from app.permissions import PERMISSIONS, ROLES, has_permission, permissions_for
from app.route_inventory import MUTATING, SELF_SERVICE, all_routes, openapi_paths
from app.routers import api as api_module
from app.services import accounts
from scripts import reset_password as reset_script

from .migration_helpers import alembic_config, head_revision
from .sample_data import make_scan_pdf

OWNER_PASSWORD = "owner-correct-horse-battery"
NEW_PASSWORD = "employee-chosen-long-password"


# ---------------------------------------------------------------- helpers

@pytest.fixture()
def real_auth(client):
    """Use genuine cookie authentication for this test."""
    previous = app.dependency_overrides.pop(current_user)
    auth_module.reset_login_throttle()
    with api_module.SessionLocal() as db:
        db.add(User(organization_id=1, username="boss", display_name="Pat Boss",
                    password_hash=password_hash(OWNER_PASSWORD), role="owner"))
        db.commit()
    try:
        yield client
    finally:
        auth_module.reset_login_throttle()
        app.dependency_overrides[current_user] = previous


def sign_in(client, username, password):
    client.cookies.clear()
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def use_session(client, token):
    """Present exactly this session. A cookie set by hand sits beside the one the
    server set rather than replacing it, and which of the two the server reads
    depends on the HTTP library under the test client (httpx and httpx2 differ)."""
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def add_member(client, username, role, display_name=None):
    response = client.post("/api/team/members", json={
        "username": username, "role": role, "display_name": display_name,
    })
    assert response.status_code == 201, response.text
    return response.json()


def onboard(client, username, role, display_name=None):
    """Owner adds a member; the member signs in and replaces the temporary password."""
    sign_in(client, "boss", OWNER_PASSWORD)
    created = add_member(client, username, role, display_name)
    sign_in(client, username, created["temporary_password"])
    changed = client.post("/api/auth/password", json={
        "current_password": created["temporary_password"], "new_password": NEW_PASSWORD,
    })
    assert changed.status_code == 200, changed.text
    return created["member"]["id"]


def upload_batch(client, project_id):
    response = client.post(
        "/api/batches", data={"layout": "grid_2x2", "project_id": str(project_id)},
        files=[("files", ("synthetic.pdf", make_scan_pdf(), "application/pdf"))],
    )
    assert response.status_code == 200, response.text
    batch_id = response.json()["id"]
    tickets = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert len(tickets) == 4
    return batch_id, tickets


def db_session():
    return api_module.SessionLocal()


# ---------------------------------------------------------------- permission model

def test_every_mutating_api_route_declares_a_permission():
    """A new endpoint that changes data cannot ship without an explicit permission."""
    routes = all_routes()
    # This test once passed vacuously when a FastAPI upgrade changed how
    # app.routes is structured. Prove the walk really sees the application.
    assert {(r.method, r.path) for r in routes} == openapi_paths()
    assert len(routes) > 30
    unguarded = [f"{r.method} {r.path}" for r in routes
                 if r.method in MUTATING and r.path not in SELF_SERVICE and not r.permissions]
    assert unguarded == []
    guarded = {r.path: r.permissions for r in routes if r.method == "POST"}
    assert guarded["/api/tickets/{ticket_id}/verify"] == ("tickets.verify",)


def test_every_api_route_rejects_anonymous_requests(client):
    """Reads are protected too; only the sign-in endpoints are public."""
    public = {"/api/auth/login", "/api/auth/logout"}
    app.dependency_overrides.pop(current_user, None)
    leaked = []
    for route in all_routes():
        if route.path in public:
            continue
        url = re.sub(r"{[^}]+}", "1", route.path)
        response = client.request(route.method, url, json={})
        if response.status_code != 401:
            leaked.append(f"{route.method} {route.path} -> {response.status_code}")
    assert leaked == []


def test_role_table_and_unknown_permissions_fail_closed():
    assert set(ROLES) == {"owner", "manager", "clerk", "viewer"}
    assert permissions_for("viewer") == ["records.read"]
    assert "tickets.verify" not in permissions_for("clerk")
    assert "tickets.verify" in permissions_for("manager")
    assert "team.manage" not in permissions_for("manager")
    assert set(permissions_for("owner")) == set(PERMISSIONS)
    assert permissions_for("superuser") == []
    with pytest.raises(KeyError):
        has_permission(User(role="owner"), "tickets.verfy")


# ---------------------------------------------------------------- role matrix

def test_role_matrix_is_enforced_by_the_server(real_auth):
    client = real_auth
    sign_in(client, "boss", OWNER_PASSWORD)
    project = client.post("/api/projects", json={"code": "P-1", "name": "Synthetic Job"}).json()["id"]
    batch_id, tickets = upload_batch(client, project)
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
    material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()
    ids = [t["id"] for t in tickets]

    for username, role in (("mia", "manager"), ("cal", "clerk"), ("vic", "viewer")):
        onboard(client, username, role)

    invoice = {
        "project_id": project, "invoice_number": "INV-1", "supplier_name": "Example Aggregates",
        "material_name": "No. 53 Crushed Stone", "date_from": "2026-10-15", "date_to": "2026-10-15",
        "billed_tons": "24.5", "billed_unit_price": "30", "billed_subtotal": "735.00",
    }
    # (method, path, body, which roles may do it)
    actions = [
        ("post", "/api/projects", {"code": "P-{u}", "name": "x"}, {"manager"}),
        ("put", "/api/rates", {"supplier_id": supplier["id"], "material_id": material["id"], "unit_price": "30"}, {"manager"}),
        ("patch", f"/api/tickets/{ids[0]}", {"job_location": "Gate {u}"}, {"manager", "clerk"}),
        ("patch", f"/api/tickets/{ids[1]}", {"unit_price_override": "31.{n}"}, {"manager"}),
        ("post", f"/api/tickets/{ids[2]}/flag", None, {"manager", "clerk"}),
        ("post", f"/api/tickets/{ids[3]}/verify", None, {"manager"}),
        ("post", f"/api/tickets/{ids[2]}/skip", None, {"manager"}),
        ("post", "/api/invoice-comparisons", invoice, {"manager"}),
        ("get", "/api/team/members", None, set()),
    ]
    reads = ["/api/records", "/api/dashboard", "/api/records/export.xlsx",
             f"/api/batches/{batch_id}/tickets", f"/api/tickets/{ids[0]}/image",
             f"/api/tickets/{ids[0]}/audit", "/api/invoice-comparisons"]

    for n, (username, role) in enumerate((("vic", "viewer"), ("cal", "clerk"), ("mia", "manager"))):
        sign_in(client, username, NEW_PASSWORD)
        for path in reads:
            assert client.get(path).status_code == 200, (role, path)
        for method, path, body, allowed in actions:
            if body is not None:
                body = {k: (v.format(u=username, n=n) if isinstance(v, str) else v) for k, v in body.items()}
            response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
            if role in allowed:
                assert response.status_code in (200, 201), (role, method, path, response.text)
            else:
                assert response.status_code == 403, (role, method, path, response.text)

    # A viewer could not upload; a clerk could.
    sign_in(client, "vic", NEW_PASSWORD)
    assert client.post("/api/batches", data={"layout": "single"}, files=[
        ("files", ("s.pdf", make_scan_pdf(), "application/pdf"))]).status_code == 403
    sign_in(client, "cal", NEW_PASSWORD)
    upload_batch(client, project)


# ---------------------------------------------------------------- audit identity

def test_verification_and_edits_are_attributed_to_individuals(real_auth):
    client = real_auth
    sign_in(client, "boss", OWNER_PASSWORD)
    project = client.post("/api/projects", json={"code": "A-1", "name": "Synthetic Job"}).json()["id"]
    batch_id, tickets = upload_batch(client, project)
    ticket = tickets[0]["id"]
    with db_session() as db:
        # History written before individual accounts existed has no actor.
        db.add(AuditEvent(ticket_id=ticket, field="supplier", old_value=None, new_value="legacy"))
        db.commit()
    manager_id = onboard(client, "mia", "manager", "Mia Manager")
    clerk_id = onboard(client, "cal", "clerk", "Cal Clerk")

    sign_in(client, "mia", NEW_PASSWORD)
    verified = client.post(f"/api/tickets/{ticket}/verify")
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified_by"] == "Mia Manager"
    assert verified.json()["verified_at"].endswith(("Z", "+00:00"))  # explicit UTC for browsers
    listed = {t["id"]: t for t in client.get(f"/api/batches/{batch_id}/tickets").json()}
    assert listed[ticket]["verified_by"] == "Mia Manager"

    # A clerk's correction withdraws the approval and is attributed to the clerk.
    sign_in(client, "cal", NEW_PASSWORD)
    edited = client.patch(f"/api/tickets/{ticket}", json={"job_location": "North gate"})
    assert edited.status_code == 200
    assert edited.json()["status"] == "needs_review"
    assert edited.json()["verified_by"] is None
    with db_session() as db:
        t = db.get(Ticket, ticket)
        assert t.verified_by_user_id is None and t.verified_at is None

    history = client.get(f"/api/tickets/{ticket}/audit").json()
    assert all(e["at"].endswith(("Z", "+00:00")) for e in history)
    assert history[0] == {**history[0], "new": "legacy", "actor": None}
    assert {"field": "status", "new": "verified", "actor": "Mia Manager"}.items() <= next(
        e for e in history if e["field"] == "status").items()
    assert next(e for e in history if e["field"] == "job_location")["actor"] == "Cal Clerk"
    with db_session() as db:
        actors = set(db.scalars(select(AuditEvent.actor_user_id).where(AuditEvent.ticket_id == ticket)))
        assert actors == {None, manager_id, clerk_id}


def test_upload_and_invoice_changes_record_their_author(real_auth):
    client = real_auth
    boss = sign_in(client, "boss", OWNER_PASSWORD)
    project = client.post("/api/projects", json={"code": "B-1", "name": "Synthetic Job"}).json()["id"]
    batch_id, _ = upload_batch(client, project)
    line = {
        "project_id": project, "invoice_number": "INV-9", "supplier_name": "Example Aggregates",
        "material_name": "Sand", "date_from": "2026-10-01", "date_to": "2026-10-02",
        "billed_tons": "10", "billed_unit_price": "5", "billed_subtotal": "50.00",
    }
    created = client.post("/api/invoice-comparisons", json=line)
    assert created.status_code == 201
    corrected = client.put(f"/api/invoice-comparisons/{created.json()['id']}",
                           json={**line, "billed_subtotal": "49.00"})
    assert corrected.status_code == 200
    history = client.get(f"/api/invoice-comparisons/{created.json()['id']}/audit").json()
    assert [e["actor"] for e in history] == ["Pat Boss"]
    with db_session() as db:
        from app.models import Batch, InvoiceComparison
        assert db.get(Batch, batch_id).created_by_user_id == boss["id"]
        assert db.get(InvoiceComparison, created.json()["id"]).created_by_user_id == boss["id"]


# ---------------------------------------------------------------- onboarding & sessions

def test_temporary_password_must_be_replaced_before_any_work(real_auth):
    client = real_auth
    sign_in(client, "boss", OWNER_PASSWORD)
    created = add_member(client, "Dana.Field", "clerk", "  Dana   Field ")
    assert created["member"]["username"] == "dana.field"
    assert created["member"]["display_name"] == "Dana Field"
    assert created["member"]["must_change_password"] is True
    temporary = created["temporary_password"]
    assert len(temporary) >= 16
    with db_session() as db:
        stored = db.scalar(select(User).where(User.username == "dana.field"))
        assert temporary not in stored.password_hash

    profile = sign_in(client, "dana.field", temporary)
    assert profile["must_change_password"] is True
    blocked = client.get("/api/records")
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "password_change_required"
    assert client.get("/api/auth/me").status_code == 200

    bad = client.post("/api/auth/password", json={"current_password": "wrong-password-x", "new_password": NEW_PASSWORD})
    assert bad.status_code == 400
    same = client.post("/api/auth/password", json={"current_password": temporary, "new_password": temporary})
    assert same.status_code == 422
    short = client.post("/api/auth/password", json={"current_password": temporary, "new_password": "short"})
    assert short.status_code == 422
    ok = client.post("/api/auth/password", json={"current_password": temporary, "new_password": NEW_PASSWORD})
    assert ok.status_code == 200 and ok.json()["must_change_password"] is False
    assert client.get("/api/records").status_code == 200   # same session continues
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "dana.field", "password": temporary}).status_code == 401


def test_password_change_signs_out_other_sessions_only(real_auth):
    client = real_auth
    sign_in(client, "boss", OWNER_PASSWORD)
    other_token = client.cookies.get(COOKIE_NAME)
    client.cookies.clear()
    sign_in(client, "boss", OWNER_PASSWORD)
    mine = client.cookies.get(COOKIE_NAME)
    assert client.post("/api/auth/password", json={
        "current_password": OWNER_PASSWORD, "new_password": NEW_PASSWORD,
    }).status_code == 200
    assert client.get("/api/records").status_code == 200
    use_session(client, other_token)
    assert client.get("/api/records").status_code == 401
    use_session(client, mine)
    assert client.get("/api/records").status_code == 200


def test_deactivation_role_change_and_reset_take_effect_immediately(real_auth):
    client = real_auth
    member_id = onboard(client, "cal", "clerk")
    clerk_token = client.cookies.get(COOKIE_NAME)
    assert client.get("/api/records").status_code == 200

    # Role change: the old session (with old permissions) ends at once.
    sign_in(client, "boss", OWNER_PASSWORD)
    owner_token = client.cookies.get(COOKIE_NAME)
    assert client.patch(f"/api/team/members/{member_id}", json={"role": "viewer"}).json()["role"] == "viewer"
    use_session(client, clerk_token)
    assert client.get("/api/records").status_code == 401
    sign_in(client, "cal", NEW_PASSWORD)
    assert client.post("/api/projects", json={"code": "Z", "name": "z"}).status_code == 403
    viewer_token = client.cookies.get(COOKIE_NAME)

    # Deactivation: next request is refused; sign-in is refused.
    use_session(client, owner_token)
    assert client.patch(f"/api/team/members/{member_id}", json={"active": False}).json()["active"] is False
    use_session(client, viewer_token)
    assert client.get("/api/records").status_code == 401
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "cal", "password": NEW_PASSWORD}).status_code == 401
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)
                         .where(LoginSession.user_id == member_id)) == 0

    # Reactivate + reset: old password stops working, a new temporary one is issued.
    use_session(client, owner_token)
    assert client.patch(f"/api/team/members/{member_id}", json={"active": True}).status_code == 200
    reset = client.post(f"/api/team/members/{member_id}/reset-password")
    assert reset.status_code == 200
    temporary = reset.json()["temporary_password"]
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "cal", "password": NEW_PASSWORD}).status_code == 401
    assert sign_in(client, "cal", temporary)["must_change_password"] is True

    sign_in(client, "boss", OWNER_PASSWORD)
    actions = [e["action"] for e in client.get("/api/team/events").json()]
    assert actions[:5] == ["password_reset", "reactivated", "deactivated", "role_changed", "password_changed"]
    assert all(e["actor"] == "Pat Boss" for e in client.get("/api/team/events").json()
               if e["action"] != "password_changed")


def test_owner_cannot_lock_the_company_out(real_auth):
    client = real_auth
    me = sign_in(client, "boss", OWNER_PASSWORD)
    assert client.patch(f"/api/team/members/{me['id']}", json={"active": False}).status_code == 409
    assert client.patch(f"/api/team/members/{me['id']}", json={"role": "manager"}).status_code == 409
    assert client.post(f"/api/team/members/{me['id']}/reset-password").status_code == 409

    # Service-level invariant (defense in depth): the last active owner can
    # never be demoted or deactivated, whoever the actor is.
    with db_session() as db:
        boss = db.scalar(select(User).where(User.username == "boss"))
        # Organization 1 also contains the fixture's "test-owner"; deactivate it.
        db.query(User).filter(User.username == "test-owner").update({"active": False})
        db.flush()
        other_admin = User(id=999, organization_id=1, role="owner", username="ghost")
        for change in ({"role": "clerk"}, {"active": False}):
            with pytest.raises(accounts.AccountError):
                accounts.update_member(db, actor=other_admin, target=boss, **change)
        db.rollback()

    # With a second owner, one owner may demote the other.
    second = add_member(client, "co-owner", "owner")["member"]["id"]
    assert client.patch(f"/api/team/members/{second}", json={"role": "manager"}).status_code == 200


def test_team_management_is_company_scoped_and_owner_only(real_auth):
    client = real_auth
    with db_session() as db:
        db.add(Organization(id=2, name="Other Contractor"))
        db.flush()
        db.add(User(id=50, organization_id=2, username="rival", role="owner",
                    password_hash=password_hash(OWNER_PASSWORD)))
        db.commit()
    sign_in(client, "boss", OWNER_PASSWORD)
    usernames = {m["username"] for m in client.get("/api/team/members").json()}
    assert "rival" not in usernames and "boss" in usernames
    assert client.patch("/api/team/members/50", json={"active": False}).status_code == 404
    assert client.post("/api/team/members/50/reset-password").status_code == 404
    # Global username taken by another company: refused without naming the company.
    taken = client.post("/api/team/members", json={"username": "rival", "role": "clerk"})
    assert taken.status_code == 409 and "Other Contractor" not in taken.text
    assert client.post("/api/team/members", json={"username": "x", "role": "clerk"}).status_code == 422
    assert client.post("/api/team/members", json={"username": "ok-name", "role": "admin"}).status_code == 422
    assert client.post("/api/team/members", json={
        "username": "ok-name", "role": "clerk", "organization_id": 2,
    }).status_code == 422  # company is never client-supplied
    response = client.get("/api/team/members")
    assert response.headers["cache-control"] == "private, no-store"
    with db_session() as db:
        assert db.get(User, 50).active is True
        assert db.scalar(select(func.count()).select_from(AccountEvent)
                         .where(AccountEvent.organization_id == 2)) == 0

    onboard(client, "mia", "manager")
    assert client.get("/api/team/members").status_code == 403
    assert client.get("/api/team/events").status_code == 403


# ---------------------------------------------------------------- login hardening

def test_lockout_cannot_be_flushed_by_username_spraying():
    auth_module.reset_login_throttle()
    try:
        victim = "203.0.113.5:boss"
        for _ in range(8):
            auth_module.record_login_failure(victim)
        assert auth_module.login_blocked(victim)
        for i in range(auth_module._MAX_TRACKED + 50):
            auth_module.record_login_failure(f"198.51.100.{i % 250}:spray{i}")
        # Old behaviour cleared every counter at the cap. The per-account
        # counter (keyed by real user ID) must survive regardless.
        for _ in range(auth_module._MAX_ACCOUNT_FAILURES):
            auth_module.record_login_failure(f"192.0.2.{_}:boss", user_id=1)
        for i in range(auth_module._MAX_TRACKED + 50):
            auth_module.record_login_failure(f"198.51.100.1:more{i}")
        assert auth_module.login_blocked("192.0.2.200:boss", user_id=1)
        assert len(auth_module._failures) <= auth_module._MAX_TRACKED
    finally:
        auth_module.reset_login_throttle()


def test_unknown_username_still_runs_password_hashing(real_auth, monkeypatch):
    calls = []
    original = auth_module.password_valid
    monkeypatch.setattr(auth_module, "password_valid", lambda p, e: calls.append(e) or original(p, e))
    response = real_auth.post("/api/auth/login", json={"username": "nobody", "password": "whatever-password"})
    assert response.status_code == 401
    assert calls == [auth_module._DUMMY_HASH]


def test_login_rotates_presented_session_and_purges_expired(real_auth):
    client = real_auth
    sign_in(client, "boss", OWNER_PASSWORD)
    first = client.cookies.get(COOKIE_NAME)
    with db_session() as db:
        boss = db.scalar(select(User).where(User.username == "boss"))
        db.add(LoginSession(token_digest="e" * 64, user_id=boss.id,
                            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)))
        db.commit()
    response = client.post("/api/auth/login", json={"username": "boss", "password": OWNER_PASSWORD})
    assert response.status_code == 200
    assert client.cookies.get(COOKIE_NAME) != first
    with db_session() as db:
        digests = set(db.scalars(select(LoginSession.token_digest)))
    assert auth_module.digest_token(first) not in digests and "e" * 64 not in digests
    assert len(digests) == 1


def test_unknown_role_in_database_fails_closed(real_auth):
    client = real_auth
    sign_in(client, "boss", OWNER_PASSWORD)
    with db_session() as db:
        db.query(User).filter(User.username == "boss").update({"role": "superadmin"})
        db.commit()
    assert client.get("/api/records").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------- terminal recovery

def test_terminal_password_recovery_resets_and_signs_out(real_auth):
    client = real_auth
    sign_in(client, "boss", OWNER_PASSWORD)
    old = client.cookies.get(COOKIE_NAME)
    answers = iter(["recovered-long-password", "recovered-long-password"])
    assert reset_script.main(["--username", "BOSS"], prompt=lambda _: next(answers),
                             session_factory=api_module.SessionLocal) == 0
    use_session(client, old)
    assert client.get("/api/records").status_code == 401
    assert sign_in(client, "boss", "recovered-long-password")["must_change_password"] is False
    events = client.get("/api/team/events").json()
    assert events[0]["action"] == "password_reset_terminal" and events[0]["actor_kind"] == "terminal"

    mismatch = iter(["recovered-long-password", "different-long-password"])
    assert reset_script.main(["--username", "boss"], prompt=lambda _: next(mismatch),
                             session_factory=api_module.SessionLocal) == 1
    assert reset_script.main(["--username", "missing"], prompt=lambda _: "x" * 20,
                             session_factory=api_module.SessionLocal) == 1


# ---------------------------------------------------------------- migration

LEGACY_06_SCHEMA = """
CREATE TABLE organizations (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL);
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL UNIQUE,
  organization_id INTEGER NOT NULL REFERENCES organizations(id),
  password_hash VARCHAR(255) NOT NULL, active BOOLEAN NOT NULL);
CREATE TABLE login_sessions (token_digest VARCHAR(64) PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id), expires_at DATETIME NOT NULL);
CREATE TABLE projects (id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL, code VARCHAR(50),
  name VARCHAR(200), job_location VARCHAR(200), description TEXT, status VARCHAR(20), created_at DATETIME);
CREATE TABLE batches (id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL, project_id INTEGER,
  layout VARCHAR(20), intake_mode VARCHAR(20) NOT NULL DEFAULT 'extract', status VARCHAR(20), created_at DATETIME);
CREATE TABLE uploaded_files (id INTEGER PRIMARY KEY, batch_id INTEGER, original_name VARCHAR(255),
  storage_path VARCHAR(500), sha256 VARCHAR(64), status VARCHAR(20), error TEXT);
CREATE TABLE tickets (id INTEGER PRIMARY KEY, batch_id INTEGER, file_id INTEGER, source_page INTEGER,
  position INTEGER, crop_box JSON, crop_path VARCHAR(500), crop_sha256 VARCHAR(64),
  ticket_number VARCHAR(100), delivery_date DATE, material_type VARCHAR(200), tonnage NUMERIC(12,3),
  supplier VARCHAR(200), job_location VARCHAR(200), unit_price_override NUMERIC(12,4),
  price_snapshot_state VARCHAR(20), applied_unit_price NUMERIC(12,4), applied_price_source VARCHAR(20),
  raw_extraction JSON, flags JSON, status VARCHAR(20), tonnage_confirmed BOOLEAN, error TEXT, created_at DATETIME);
CREATE TABLE audit_events (id INTEGER PRIMARY KEY, ticket_id INTEGER REFERENCES tickets(id),
  field VARCHAR(50), old_value TEXT, new_value TEXT, created_at DATETIME);
CREATE TABLE invoice_comparisons (id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL,
  project_id INTEGER NOT NULL, invoice_number VARCHAR(100) NOT NULL, supplier_name VARCHAR(200) NOT NULL,
  supplier_key VARCHAR(200) NOT NULL, material_name VARCHAR(200) NOT NULL, material_key VARCHAR(200) NOT NULL,
  date_from DATE NOT NULL, date_to DATE NOT NULL, billed_tons NUMERIC(12,3) NOT NULL,
  billed_unit_price NUMERIC(12,4) NOT NULL, billed_subtotal NUMERIC(14,2) NOT NULL, created_at DATETIME NOT NULL);
CREATE TABLE invoice_comparison_events (id INTEGER PRIMARY KEY, comparison_id INTEGER NOT NULL,
  field VARCHAR(60) NOT NULL, old_value TEXT, new_value TEXT, created_at DATETIME NOT NULL);
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version VALUES ('20261029_06');
INSERT INTO organizations VALUES (1, 'Original workspace'), (2, 'Demo Company B');
INSERT INTO users (id, username, password_hash, organization_id, active)
  VALUES (7, 'owner', 'existing-digest', 1, 1), (8, 'companybowner', 'digest-b', 2, 1);
INSERT INTO login_sessions VALUES ('a', 7, '2099-01-01 00:00:00');
INSERT INTO batches VALUES (3, 1, NULL, 'grid_2x2', 'extract', 'ready_for_review', '2026-10-30');
INSERT INTO tickets (id, batch_id, file_id, source_page, position, crop_box, crop_path, crop_sha256,
  tonnage, status, tonnage_confirmed, price_snapshot_state, applied_unit_price, flags)
  VALUES (21, 3, 1, 1, 0, '[0,0,1,1]', 'x.png', 'abc', 24.5, 'verified', 1, 'priced', 30, '[]');
INSERT INTO audit_events VALUES (5, 21, 'status', 'needs_review', 'verified', '2026-10-30');
INSERT INTO invoice_comparisons VALUES (4, 1, 1, 'INV-1', 'S', 's', 'M', 'm', '2026-10-01',
  '2026-10-02', 1, 1, 1, '2026-10-30');
INSERT INTO invoice_comparison_events VALUES (6, 4, 'billed_tons', '1', '2', '2026-10-30');
"""


def test_team_migration_is_additive_on_an_existing_database(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_06_SCHEMA)
    counts_before = {}
    with sqlite3.connect(path) as db:
        for table in ("users", "login_sessions", "tickets", "audit_events",
                      "invoice_comparisons", "invoice_comparison_events", "batches"):
            counts_before[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # repeat run is a no-op

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision()
        for table, count in counts_before.items():
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count, table
        # Existing accounts were created as company owners; nothing else changes.
        assert db.execute(
            "SELECT id, username, password_hash, organization_id, role, must_change_password, "
            "display_name, created_at FROM users ORDER BY id").fetchall() == [
            (7, "owner", "existing-digest", 1, "owner", 0, None, None),
            (8, "companybowner", "digest-b", 2, "owner", 0, None, None),
        ]
        # History is NOT retroactively attributed to anyone.
        assert db.execute("SELECT actor_user_id FROM audit_events").fetchall() == [(None,)]
        assert db.execute("SELECT actor_user_id FROM invoice_comparison_events").fetchall() == [(None,)]
        assert db.execute(
            "SELECT status, tonnage_confirmed, applied_unit_price, verified_by_user_id, verified_at "
            "FROM tickets").fetchall() == [("verified", 1, 30, None, None)]
        assert db.execute("SELECT created_by_user_id FROM batches").fetchall() == [(None,)]
        assert db.execute("SELECT COUNT(*) FROM account_events").fetchone()[0] == 0
        assert db.execute("SELECT user_id FROM login_sessions").fetchall() == [(7,)]

    inspector = inspect(create_engine(f"sqlite:///{path}"))
    for table, column in (("audit_events", "actor_user_id"), ("tickets", "verified_by_user_id"),
                          ("batches", "created_by_user_id"), ("invoice_comparisons", "created_by_user_id"),
                          ("invoice_comparison_events", "actor_user_id")):
        assert any(fk["constrained_columns"] == [column] and fk["referred_table"] == "users"
                   for fk in inspector.get_foreign_keys(table)), (table, column)


def test_fresh_install_upgrades_to_head_and_serves_requests(tmp_path):
    path = tmp_path / "fresh.db"
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{path}")
    inspector = inspect(engine)
    assert inspector.has_table("account_events")
    assert {"role", "display_name", "must_change_password", "created_at"} <= {
        c["name"] for c in inspector.get_columns("users")}
    Session = sessionmaker(bind=engine)
    with Session() as db:
        org = db.scalar(select(Organization))
        db.add(User(organization_id=org.id, username="first", password_hash="x"))
        db.commit()
        assert db.scalar(select(User.role)) == "owner"
        assert db.scalar(select(User.must_change_password)) is False
