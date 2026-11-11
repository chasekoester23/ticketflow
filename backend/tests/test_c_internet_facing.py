"""Running on the internet, behind a platform's proxy: who is asking, over what, from where.

Proxy headers are believed only from TRUSTED_PROXY_IPS; plain HTTP is refused once HTTPS
is required; the session cookie is __Host- prefixed with secure cookies; the CSRF gate
still holds when requests arrive through a proxy; sign-in throttling follows the real
client address; errors show no trace or path; and the probes and metrics say only what
they should.
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app import auth as auth_module
from app.auth import COOKIE_NAME, SECURE_COOKIE_NAME, current_user, password_hash
from app.config import configuration_problems, settings
from app.main import app
from app.models import User
from app.proxy import resolve
from app.routers import api as api_module
from app.services import system_health

PROXIES = ("10.0.0.0/8", "fd00::/8")
EDGE = "10.1.2.3"
PASSWORD = "proxy-test-long-password"


# ------------------------------------------------------------------ resolving the client

@pytest.mark.parametrize("peer, xff, xfp, expected", [
    # Not from a proxy we trust: the headers are a client's own claim, and ignored.
    ("198.51.100.7", "1.2.3.4", "https", ("198.51.100.7", "http")),
    # From our proxy: the rightmost address that isn't ours is the client.
    (EDGE, "203.0.113.9", "https", ("203.0.113.9", "https")),
    # A client writing its own X-Forwarded-For only adds to the left, which is never reached.
    (EDGE, "127.0.0.1, 203.0.113.9", "https", ("203.0.113.9", "https")),
    # Two proxies of ours (the platform's edge, then the Next server), each appending.
    (EDGE, "203.0.113.9, 10.9.9.9", "https,http", ("203.0.113.9", "https")),
    (EDGE, "2001:db8::5, fd12::1", "https, http", ("2001:db8::5", "https")),
    # Garbage stops the walk at the last proxy we trust.
    (EDGE, "not-an-address, 10.9.9.9", "https", ("10.9.9.9", "https")),
    # Nothing forwarded: the proxy itself is the client, over what it says.
    (EDGE, "", "", (EDGE, "http")),
    (EDGE, "203.0.113.9:5555", "HTTPS", ("203.0.113.9", "https")),
])
def test_forwarded_headers_count_only_from_trusted_proxies(peer, xff, xfp, expected):
    assert resolve(peer, xff, xfp, "http", PROXIES) == expected


def test_nothing_is_trusted_by_default():
    assert settings.trusted_proxy_ips == []
    assert resolve(EDGE, "203.0.113.9", "https", "http", ()) == (EDGE, "http")


@pytest.mark.parametrize("value, expected", [
    (["0.0.0.0/0"], "never every address"), (["::/0"], "never every address"), (["*"], "isn't an address"),
    (["10.0.0.0/8", "proxy.internal"], "isn't an address"),
])
def test_trusting_everyone_or_a_name_refuses_to_start(monkeypatch, value, expected):
    monkeypatch.setattr(settings, "trusted_proxy_ips", value)
    assert any(expected in problem for problem in configuration_problems(settings))


def _through(peer: str) -> TestClient:
    """A client connecting from this address. No lifespan: the fixture's app is running."""
    client = TestClient(app, client=(peer, 40000), follow_redirects=False)
    client.headers.update({"X-TicketFlow-Request": "1"})
    return client


@pytest.fixture()
def real_owner(client):
    previous = app.dependency_overrides.pop(current_user)
    auth_module.reset_login_throttle()
    with api_module.SessionLocal() as db:
        db.add(User(organization_id=1, username="proxied-owner", display_name="Proxied Owner",
                    password_hash=password_hash(PASSWORD), role="owner"))
        db.commit()
    yield
    auth_module.reset_login_throttle()
    app.dependency_overrides[current_user] = previous


def _sign_in(client, password=PASSWORD, **headers):
    return client.post("/api/auth/login", json={"username": "proxied-owner", "password": password}, headers=headers)


def test_sign_in_throttling_follows_the_real_client_behind_the_proxy(client, real_owner, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_ips", list(PROXIES))
    edge = _through(EDGE)
    for _ in range(8):
        assert _sign_in(edge, "wrong", **{"X-Forwarded-For": "203.0.113.9"}).status_code == 401
    # That address is locked out, whatever it now claims to be at the left end.
    assert _sign_in(edge, **{"X-Forwarded-For": "198.51.100.1, 203.0.113.9"}).status_code == 429
    # Everyone else behind the same proxy is not: the proxy's own address isn't the key.
    assert _sign_in(edge, **{"X-Forwarded-For": "198.51.100.20"}).status_code == 200


def test_a_client_cant_escape_a_lockout_by_inventing_addresses(client, real_owner, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_ips", list(PROXIES))
    direct = _through("198.51.100.7")  # not a proxy of ours
    for number in range(8):
        assert _sign_in(direct, "wrong", **{"X-Forwarded-For": f"192.0.2.{number}"}).status_code == 401
    assert _sign_in(direct, **{"X-Forwarded-For": "192.0.2.200"}).status_code == 429


def test_first_run_setup_stays_local_only_behind_a_trusted_local_proxy(client, monkeypatch):
    """A proxy on this machine makes every visitor look local. Trusting it must not open setup."""
    with api_module.SessionLocal() as db:
        db.query(User).delete()
        db.commit()
    monkeypatch.setattr(settings, "trusted_proxy_ips", ["127.0.0.0/8"])
    body = {"company_name": "Fictional Paving", "username": "first-owner", "display_name": "First Owner",
            "password": "first-owner-long-password"}
    refused = _through("127.0.0.1").post("/api/setup", json=body, headers={"X-Forwarded-For": "203.0.113.9"})
    assert refused.status_code == 403


# ------------------------------------------------------------------ HTTPS only

def test_plain_http_is_redirected_or_refused_once_https_is_required(client, monkeypatch):
    monkeypatch.setattr(settings, "require_https", True)
    plain = _through("198.51.100.7")
    redirected = plain.get("/api/records?status=verified")
    assert redirected.status_code == 308
    assert redirected.headers["location"] == "https://testserver/api/records?status=verified"
    refused = plain.post("/api/projects", json={"code": "X", "name": "Y"})
    assert refused.status_code == 403 and "HTTPS" in refused.json()["detail"]
    # The platform's health checker talks plain HTTP inside its network.
    for probe in ("/health", "/health/live", "/health/ready"):
        assert plain.get(probe).status_code == 200


def test_https_reported_by_a_trusted_proxy_is_accepted_and_a_client_claim_is_not(client, monkeypatch):
    monkeypatch.setattr(settings, "require_https", True)
    monkeypatch.setattr(settings, "trusted_proxy_ips", list(PROXIES))
    assert _through(EDGE).get("/api/records", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}).status_code == 200
    assert _through("198.51.100.7").get("/api/records", headers={"X-Forwarded-Proto": "https"}).status_code == 308


def test_a_redirect_never_points_at_an_unapproved_host(client, monkeypatch):
    monkeypatch.setattr(settings, "require_https", True)
    response = _through("198.51.100.7").get("/api/records", headers={"Host": "evil.example"})
    assert response.status_code == 400 and "location" not in response.headers


def test_production_always_requires_https():
    from app import main
    assert main.https_required() is False
    settings.ticketflow_env, previous = "production", settings.ticketflow_env
    try:
        assert main.https_required() is True
    finally:
        settings.ticketflow_env = previous


# ------------------------------------------------------------------ hosts

def test_a_platform_health_checker_may_reach_the_probes_and_nothing_else(client, monkeypatch):
    monkeypatch.setattr(settings, "healthcheck_hosts", ["healthcheck.railway.app"])
    checker = {"Host": "healthcheck.railway.app"}
    for probe in ("/health", "/health/live", "/health/ready"):
        assert client.get(probe, headers=checker).status_code == 200
    assert client.get("/api/records", headers=checker).status_code == 400
    assert client.post("/api/auth/login", json={"username": "a", "password": "b"}, headers=checker).status_code == 400
    assert client.get("/health/ready", headers={"Host": "unapproved.example"}).status_code == 400


def test_wildcard_health_check_hosts_refuse_to_start(monkeypatch):
    monkeypatch.setattr(settings, "healthcheck_hosts", ["*.railway.app"])
    assert any("HEALTHCHECK_HOSTS" in problem for problem in configuration_problems(settings))


# ------------------------------------------------------------------ CORS and CSRF

def test_same_origin_production_needs_no_cors_but_must_name_its_origin(monkeypatch):
    values = dict(ticketflow_env="production", database_url="postgresql://tf@db.internal/ticketflow", session_secure_cookie=True,
                  cors_origins=[], trusted_hosts=["api.ticketflow.example"], enable_api_docs=False)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    assert any("APP_ORIGIN" in problem for problem in configuration_problems(settings))
    monkeypatch.setattr(settings, "app_origin", "https://app.ticketflow.example")
    assert configuration_problems(settings) == []
    for bad in ("http://app.ticketflow.example", "https://*.ticketflow.example", "https://localhost", "https://app.example/path"):
        monkeypatch.setattr(settings, "app_origin", bad)
        assert any("APP_ORIGIN" in problem for problem in configuration_problems(settings)), bad


def test_the_csrf_gate_holds_through_the_proxy(client, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_ips", list(PROXIES))
    monkeypatch.setattr(settings, "cors_origins", [])
    monkeypatch.setattr(settings, "app_origin", "https://app.ticketflow.example")
    edge = TestClient(app, client=(EDGE, 40000))
    forwarded = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
    body = {"code": "CSRF", "name": "Fictional"}
    # No verification header: an ordinary cross-site form, relayed by the proxy.
    assert edge.post("/api/projects", json=body, headers=forwarded).status_code == 403
    # The header, from a foreign page.
    foreign = {**forwarded, "X-TicketFlow-Request": "1", "Origin": "https://evil.example"}
    assert edge.post("/api/projects", json=body, headers=foreign).status_code == 403
    # A forwarded host naming the app doesn't make a foreign origin acceptable.
    assert edge.post("/api/projects", json=body, headers={**foreign, "X-Forwarded-Host": "app.ticketflow.example"}).status_code == 403
    # The app's own origin is accepted.
    own = {**forwarded, "X-TicketFlow-Request": "1", "Origin": "https://app.ticketflow.example"}
    assert edge.post("/api/projects", json=body, headers=own).status_code == 201
    # Same-origin means no CORS: no browser is told another origin may read anything.
    preflight = edge.options("/api/projects", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in preflight.headers


# ------------------------------------------------------------------ the session cookie

def test_secure_cookies_use_the_host_prefix_and_local_mode_keeps_its_name(client, real_owner, monkeypatch):
    local = _sign_in(client)
    assert local.status_code == 200 and client.cookies.get(COOKIE_NAME)
    assert "secure" not in local.headers["set-cookie"].lower()

    monkeypatch.setattr(settings, "session_secure_cookie", True)
    monkeypatch.setattr(settings, "session_cookie_samesite", "strict")
    secure = TestClient(app, base_url="https://testserver")
    secure.headers.update({"X-TicketFlow-Request": "1"})
    response = _sign_in(secure)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SECURE_COOKIE_NAME}=")
    lowered = cookie.lower()
    assert "secure" in lowered and "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert "domain=" not in lowered and PASSWORD not in cookie
    assert secure.get("/api/auth/me").status_code == 200
    # The old name is not read once cookies are secure, so a plain-HTTP cookie can't stand in.
    token = secure.cookies.get(SECURE_COOKIE_NAME)
    secure.cookies.clear()
    secure.cookies.set(COOKIE_NAME, token)
    assert secure.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------------ errors

_boom = APIRouter()


@_boom.get("/api/c-test-boom")
def _explode():
    raise ValueError("/Users/someone/secret/path.db: ticket 10482 for EXAMPLE-SECRET-CUSTOMER")


def test_an_unexpected_error_shows_no_trace_path_or_message(client, caplog):
    app.include_router(_boom)
    try:
        with caplog.at_level(logging.ERROR, logger="ticketflow.request"):
            response = client.get("/api/c-test-boom")
    finally:
        app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", "") != "/api/c-test-boom"]
    assert response.status_code == 500
    for leaked in ("/Users/", "secret", "10482", "EXAMPLE-SECRET-CUSTOMER", "Traceback", "ValueError", ".py"):
        assert leaked not in response.text
    assert response.json()["request_id"] in response.json()["detail"]
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "ValueError" in logged and "EXAMPLE-SECRET-CUSTOMER" not in logged and "/Users/someone" not in logged


@pytest.mark.parametrize("method, path", [("GET", "/api/does-not-exist"), ("DELETE", "/health"), ("GET", "/api/tickets/not-a-number")])
def test_ordinary_refusals_say_nothing_about_the_code(client, method, path):
    response = client.request(method, path)
    assert response.status_code in (404, 405, 422)
    assert "Traceback" not in response.text and ".py" not in response.text and "/Users" not in response.text


# ------------------------------------------------------------------ probes and metrics

def test_liveness_answers_without_the_database_and_says_nothing_else(client):
    response = client.get("/health/live")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"


def test_metrics_report_errors_reachability_queue_and_backups(client):
    before = system_health.request_counts()
    app.include_router(_boom)
    try:
        client.get("/api/c-test-boom")
    finally:
        app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", "") != "/api/c-test-boom"]
    metrics = client.get("/api/system/status").json()["metrics"]
    assert metrics["api"]["server_errors"] >= before["server_errors"] + 1
    assert metrics["api"]["requests"] > before["requests"] and 0 < metrics["api"]["error_rate"] <= 1
    assert metrics["database"]["reachable"] is True and metrics["storage"]["reachable"] is True
    assert metrics["queue"] == {"pending": 0, "oldest_pending_seconds": None}
    assert set(metrics["jobs_24h"]) == {"finished", "failed", "pending", "running"}
    assert metrics["backups"]["local_age_hours"] is None and metrics["backups"]["postgres_age_hours"] is None


def test_metrics_say_when_storage_cant_be_reached_without_saying_why(client, monkeypatch):
    class Broken:
        def exists(self, _reference):
            raise OSError("/srv/private/storage: permission denied for EXAMPLE-SECRET")
    monkeypatch.setattr(system_health.storage, "current", lambda: Broken())
    response = client.get("/api/system/status")
    assert response.json()["metrics"]["storage"]["reachable"] is False
    assert "EXAMPLE-SECRET" not in response.text and "/srv/private" not in response.text


def test_a_postgres_backup_record_is_read_for_freshness(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", tmp_path)
    from datetime import datetime, timedelta, timezone
    finished = datetime.now(timezone.utc) - timedelta(hours=3)
    (tmp_path / system_health.PG_BACKUP_STATUS_FILE).write_text(json.dumps({"finished_at": finished.isoformat()}))
    age = client.get("/api/system/status").json()["metrics"]["backups"]["postgres_age_hours"]
    assert 2.9 <= age <= 3.1


def test_metrics_need_system_read(client, monkeypatch):
    from app.permissions import ROLES
    for role in ROLES:
        app.dependency_overrides[current_user] = lambda role=role: User(
            id=1, organization_id=1, username="test-owner", display_name="T", password_hash="x", active=True, role=role,
            must_change_password=False)
        response = client.get("/api/system/status")
        assert (response.status_code == 200) == (role in ("owner", "manager")), (role, response.status_code)


def test_a_real_uvicorn_access_log_never_holds_a_query(tmp_path):
    """Run the real server, search for a name, and read what it logged."""
    import os
    import socket
    import subprocess
    import sys
    import time
    import httpx
    from pathlib import Path
    backend = Path(__file__).resolve().parents[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("DATABASE_URL", "TICKETFLOW_", "GEMINI", "ANTHROPIC"))}
    env.update(DATABASE_URL=f"sqlite:///{tmp_path / 'access.db'}", STORAGE_DIR=str(tmp_path / "storage"), BACKUP_DIR=str(tmp_path / "b"),
               TRUSTED_HOSTS='["127.0.0.1"]', GEMINI_API_KEY="", PYTHONPATH=str(backend))
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=backend, env=env, check=True, capture_output=True)
    log = tmp_path / "server.log"
    with log.open("w") as out:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=tmp_path, env=env,
                                  stdout=out, stderr=out)
        try:
            for _ in range(100):
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            httpx.get(f"http://127.0.0.1:{port}/api/records/search?q=SEARCHED-SUPPLIER-NAME", timeout=5)
            httpx.get(f"http://127.0.0.1:{port}/health?token=QUERY-SECRET", timeout=5)
        finally:
            server.terminate()
            server.wait(10)
    text = log.read_text()
    assert "/api/records/search" in text and "/health" in text, text[-2000:]
    assert "SEARCHED-SUPPLIER-NAME" not in text and "QUERY-SECRET" not in text
