"""Security regression suite: no anonymous access to construction records."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.auth import COOKIE_NAME, current_user, password_hash, password_valid, _failures
from app.db import get_db
from app.main import app
from app.models import LoginSession, User


def _seed_owner():
    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        user = User(organization_id=1, username="owner", password_hash=password_hash("correct-long-password"))
        db.add(user)
        db.commit()
        return user.id
    finally:
        gen.close()


def test_scrypt_hashes_password_with_random_salt_and_validates():
    password = "sample-long-password"
    one = password_hash(password)
    two = password_hash(password)
    assert one != two and password not in one
    assert password_valid(password, one)
    assert not password_valid("different", one)
    assert not password_valid(password, "plaintext")
    try:
        password_hash("short")
    except ValueError:
        pass
    else:
        raise AssertionError("Short password was accepted")


def test_protected_routes_session_rotation_logout_and_expiry(client):
    _seed_owner()
    override = app.dependency_overrides.pop(current_user)
    try:
        # Do not allow an unauthenticated request to read *any* saved scan,
        # tickets, project metadata, cost, or Excel downloads.
        for path in (
            "/api/status", "/api/projects", "/api/suppliers", "/api/materials",
            "/api/rates", "/api/dashboard", "/api/records",
            "/api/records/export.xlsx", "/api/batches", "/api/batches/1",
            "/api/batches/1/tickets", "/api/batches/1/export.xlsx",
            "/api/tickets/1/image", "/api/tickets/1/audit",
        ):
            response = client.get(path)
            assert response.status_code == 401, (path, response.text)
        assert client.get("/health").status_code == 200
        assert client.get("/api/auth/me").status_code == 401

        denied = client.post("/api/auth/login", json={
            "username": "owner", "password": "wrong-password",
        })
        assert denied.status_code == 401
        login = client.post("/api/auth/login", json={
            "username": "owner", "password": "correct-long-password",
        })
        assert login.status_code == 200
        assert login.json() == {"id": 1, "username": "owner"}
        cookie = login.headers["set-cookie"]
        assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
        assert "correct-long-password" not in cookie
        token = client.cookies.get(COOKIE_NAME)
        assert token
        assert client.get("/api/auth/me").json()["username"] == "owner"
        assert client.get("/api/projects").status_code == 200
        export = client.get("/api/records/export.xlsx")
        assert export.status_code == 200
        assert export.headers["cache-control"] == "private, no-store"
        assert export.headers["x-content-type-options"] == "nosniff"

        # HTML form / foreign-site POSTs must not be able to use the cookie.
        assert client.post("/api/auth/logout", headers={"X-TicketFlow-Request": ""}).status_code == 403
        assert client.post("/api/auth/logout", headers={
            "Origin": "https://attacker.example",
        }).status_code == 403
        assert client.get("/api/auth/me").status_code == 200

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        # A copied old cookie must not resurrect the revoked session.
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/api/records").status_code == 401

        assert client.post("/api/auth/login", json={
            "username": "owner", "password": "correct-long-password",
        }).status_code == 200
        gen = app.dependency_overrides[get_db]()
        db = next(gen)
        try:
            active = db.scalar(select(LoginSession))
            assert active
            active.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            db.commit()
        finally:
            gen.close()
        assert client.get("/api/auth/me").status_code == 401
        client.cookies.set(COOKIE_NAME, "garbage-session-value")
        assert client.get("/api/dashboard").status_code == 401
        assert client.post("/api/auth/logout").status_code == 200
    finally:
        app.dependency_overrides[current_user] = override


def test_login_lockout_and_no_public_registration(client):
    _seed_owner()
    _failures.clear()
    override = app.dependency_overrides.pop(current_user)
    try:
        for _ in range(8):
            response = client.post("/api/auth/login", json={
                "username": "owner", "password": "wrong",
            })
            assert response.status_code == 401
        blocked = client.post("/api/auth/login", json={
            "username": "owner", "password": "correct-long-password",
        })
        assert blocked.status_code == 429
        assert client.post("/api/auth/signup", json={
            "username": "intruder", "password": "long-password",
        }).status_code == 404
    finally:
        _failures.clear()
        app.dependency_overrides[current_user] = override
