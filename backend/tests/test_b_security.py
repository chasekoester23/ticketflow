"""Security hardening that can be tested on one machine.

None of this is a penetration test or a security review. Passing these says the
controls described in SECURITY_PRELAUNCH.md behave as written, and nothing more.
"""
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import rate_limit
from app.auth import COOKIE_NAME, current_user, digest_token, too_common, validate_new_password
from app.config import configuration_problems, settings
from app.main import app
from app.models import LoginSession, User
from app.route_inventory import all_routes

from .test_query_budgets import _seed, _session
from .test_team_accounts import OWNER_PASSWORD, use_session

BACKEND = Path(__file__).resolve().parents[1]
SECRET = "SECRET-TICKET-VALUE"


def _production(monkeypatch, **overrides):
    values = dict(ticketflow_env="production", database_url="postgresql://tf@db.internal/ticketflow", session_secure_cookie=True,
                  cors_origins=["https://app.ticketflow.example"], trusted_hosts=["api.ticketflow.example"], enable_api_docs=False,
                  # Production also refuses local document storage (test_deploy_storage.py covers it).
                  storage_backend="s3", s3_endpoint_url="https://account.r2.example", s3_bucket="tf-test",
                  s3_access_key_id="placeholder", s3_secret_access_key="placeholder")
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


# ------------------------------------------------------------------ API documentation

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_api_documentation_is_off_by_default(client, path):
    assert client.get(path).status_code == 404


def test_the_route_inventory_still_sees_every_route_with_documentation_off(client):
    """The permission tests compare our routers with the OpenAPI schema. Turning the
    documentation off must not make that comparison vacuous."""
    assert client.get("/openapi.json").status_code == 404
    assert len(app.openapi()["paths"]) > 30 and len(all_routes()) > 30


def _run(code: str, **env) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if key not in ("DATABASE_URL", "TICKETFLOW_ENV", "ENABLE_API_DOCS")}
    environment.update({"DATABASE_URL": "sqlite://", "PYTHONPATH": str(BACKEND), **env})
    return subprocess.run([sys.executable, "-c", code], cwd=BACKEND.parent, env=environment, capture_output=True, text=True, timeout=120)


def test_api_documentation_can_be_turned_on_deliberately():
    result = _run("from fastapi.testclient import TestClient\nfrom app.main import app\n"
                  "with TestClient(app) as c: print(c.get('/openapi.json').status_code, c.get('/docs').status_code)",
                  ENABLE_API_DOCS="true")
    assert result.stdout.split() == ["200", "200"], result.stderr[-500:]


# ------------------------------------------------------------------ production fails closed

def test_a_correct_production_configuration_starts(monkeypatch):
    _production(monkeypatch)
    assert configuration_problems(settings) == []


@pytest.mark.parametrize("override, expected", [
    ({"database_url": "sqlite:///ticketflow.db"}, "PostgreSQL"),
    ({"session_secure_cookie": False}, "SESSION_SECURE_COOKIE"),
    ({"cors_origins": []}, "CORS_ORIGINS"),
    ({"cors_origins": ["http://app.ticketflow.example"]}, "CORS_ORIGINS"),
    ({"cors_origins": ["https://*.ticketflow.example"]}, "CORS_ORIGINS"),
    ({"cors_origins": ["https://localhost:3000"]}, "CORS_ORIGINS"),
    ({"trusted_hosts": ["localhost", "api.ticketflow.example"]}, "TRUSTED_HOSTS"),
    ({"trusted_hosts": ["testserver"]}, "TRUSTED_HOSTS"),
    ({"trusted_hosts": ["127.0.0.1"]}, "TRUSTED_HOSTS"),
    ({"trusted_hosts": []}, "TRUSTED_HOSTS"),
    ({"enable_api_docs": True}, "ENABLE_API_DOCS"),
])
def test_production_refuses_anything_unsafe(monkeypatch, override, expected):
    _production(monkeypatch, **override)
    problems = configuration_problems(settings)
    assert any(expected in problem for problem in problems), problems


def test_a_mistyped_environment_name_fails_closed(monkeypatch):
    """'prod' must not quietly mean development."""
    monkeypatch.setattr(settings, "ticketflow_env", "prod")
    assert any("TICKETFLOW_ENV" in problem for problem in configuration_problems(settings))


def test_startup_refuses_an_unsafe_production_configuration(client, monkeypatch):
    _production(monkeypatch, session_secure_cookie=False)
    with pytest.raises(RuntimeError, match="SESSION_SECURE_COOKIE"):
        with TestClient(app):
            pass


def test_the_worker_refuses_an_unsafe_production_configuration(monkeypatch, capsys):
    from scripts import run_worker
    _production(monkeypatch, enable_api_docs=True)
    assert run_worker.run(once=True, poll_seconds=0.25) == 2
    assert "ENABLE_API_DOCS" in capsys.readouterr().err


def test_a_real_process_started_in_production_mode_refuses():
    result = _run("from fastapi.testclient import TestClient\nfrom app.main import app\nwith TestClient(app): pass",
                  TICKETFLOW_ENV="production")
    assert result.returncode != 0 and "PostgreSQL" in result.stderr


# ------------------------------------------------------------------ HSTS

def test_hsts_is_sent_only_when_cookies_are_secure(client, monkeypatch):
    assert "strict-transport-security" not in client.get("/health").headers
    monkeypatch.setattr(settings, "session_secure_cookie", True)
    header = client.get("/health").headers["strict-transport-security"]
    assert "max-age=31536000" in header and "includeSubDomains" in header


# ------------------------------------------------------------------ rate limits

EXPECTED_LIMITS = {
    ("POST", "/api/batches"): "uploads", ("POST", "/api/batches/{batch_id}/retry"): "uploads",
    ("POST", "/api/invoice-documents"): "uploads",
    ("GET", "/api/records/export.xlsx"): "exports", ("GET", "/api/records/export.csv"): "exports",
    ("GET", "/api/batches/{batch_id}/export.xlsx"): "exports", ("GET", "/api/reports/project/{project_id}/report.xlsx"): "exports",
    ("GET", "/api/reports/pilot.csv"): "exports", ("GET", "/api/invoice-comparisons/{line_id}/report.csv"): "exports",
    ("GET", "/api/reports/project/{project_id}"): "reports", ("GET", "/api/reports/pilot"): "reports",
    ("GET", "/api/invoice-comparisons/overview"): "reports",
    ("GET", "/api/reports/project/{project_id}/unbilled"): "reports", ("GET", "/api/reports/project/{project_id}/unbilled.csv"): "exports",
    ("GET", "/api/invoice-comparisons/discrepancies.csv"): "exports",
}


def _buckets(dependant) -> list[str]:
    found = []
    for dependency in dependant.dependencies:
        if getattr(dependency.call, "bucket", None):
            found.append(dependency.call.bucket)
        found.extend(_buckets(dependency))
    return found


def limited_routes() -> dict[tuple[str, str], str]:
    from fastapi.routing import APIRoute
    from app.route_inventory import ROUTERS
    found = {}
    for router in ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute):
                for bucket in _buckets(route.dependant):
                    for method in route.methods - {"HEAD", "OPTIONS"}:
                        found[(method, route.path)] = bucket
    return found


def test_every_expensive_endpoint_is_rate_limited():
    found = limited_routes()
    missing = {route: bucket for route, bucket in EXPECTED_LIMITS.items() if found.get(route) != bucket}
    assert missing == {}


def test_an_export_beyond_the_limit_gets_429_with_a_retry_time(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_exports_per_minute", 3)
    _seed(1)
    codes = [client.get("/api/records/export.csv").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    refused = client.get("/api/records/export.csv")
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert "per minute" in refused.json()["detail"]
    # Other kinds of request aren't affected.
    assert client.get("/api/records").status_code == 200
    assert client.get("/api/reports/pilot").status_code == 200


def test_limits_are_per_person(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_reports_per_minute", 1)
    assert client.get("/api/reports/pilot").status_code == 200
    assert client.get("/api/reports/pilot").status_code == 429
    app.dependency_overrides[current_user] = lambda: User(id=2, organization_id=1, username="other", password_hash="x", active=True,
                                                         role="owner", must_change_password=False)
    assert client.get("/api/reports/pilot").status_code == 200


def test_a_limit_resets_after_its_minute(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])
    rate_limit.reset()
    assert rate_limit.allowed("exports", 1, 2) is None and rate_limit.allowed("exports", 1, 2) is None
    assert rate_limit.allowed("exports", 1, 2) == 60
    clock[0] += 59.5
    assert rate_limit.allowed("exports", 1, 2) == 1
    clock[0] += 1
    assert rate_limit.allowed("exports", 1, 2) is None


def test_zero_turns_a_limit_off(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_reports_per_minute", 0)
    assert all(client.get("/api/reports/pilot").status_code == 200 for _ in range(5))


def test_the_counters_cant_grow_without_limit(monkeypatch):
    rate_limit.reset()
    monkeypatch.setattr(rate_limit, "_MAX_TRACKED", 50)
    for user in range(500):
        rate_limit.allowed("reports", user, 10)
    assert len(rate_limit._windows) <= 50


# ------------------------------------------------------------------ passwords

@pytest.mark.parametrize("password", ["password1234", "Password123!", "PASSWORD2026", "123456789012", "qwertyuiop123", "aaaaaaaaaaaa",
                                      "abababababab", "iloveyou1234", "abcdefghijklm", "qwertyuiopasdf", "letmein12345", "welcome2026!",
                                      "admin1234567", "ticketflow123", "1q2w3e4r5t6y", "passwordpassword", "0987654321098"])
def test_very_common_passwords_are_refused(password):
    assert too_common(password)
    with pytest.raises(ValueError, match="too common"):
        validate_new_password(password)


@pytest.mark.parametrize("password", ["correct-long-password", OWNER_PASSWORD, "employee-chosen-long-password",
                                      "gravel-truck-at-dawn-9", "demo-password-not-real", "x7#Kp2!vQz9@Lm"])
def test_ordinary_long_passwords_are_accepted(password):
    assert not too_common(password)
    validate_new_password(password)


def test_a_common_password_is_refused_when_changing_your_own(real_auth_client):
    client, _token = real_auth_client
    response = client.post("/api/auth/password", json={"current_password": OWNER_PASSWORD, "new_password": "Password1234"})
    assert response.status_code == 422 and "too common" in response.json()["detail"]


# ------------------------------------------------------------------ sessions

@pytest.fixture()
def real_auth_client(client):
    """Signed in for real, through the cookie, not the test override."""
    from app.auth import password_hash, reset_login_throttle
    reset_login_throttle()
    with _session() as db:
        user = db.get(User, 1)
        user.password_hash = password_hash(OWNER_PASSWORD)
        db.commit()
    app.dependency_overrides.pop(current_user, None)
    response = client.post("/api/auth/login", json={"username": "test-owner", "password": OWNER_PASSWORD})
    assert response.status_code == 200, response.text
    return client, client.cookies.get(COOKIE_NAME)


def _age_session(token: str, minutes: float) -> None:
    with _session() as db:
        session = db.get(LoginSession, digest_token(token))
        session.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes)
        db.commit()


def test_a_session_left_idle_too_long_is_ended(real_auth_client, monkeypatch):
    client, token = real_auth_client
    monkeypatch.setattr(settings, "session_idle_minutes", 30)
    assert client.get("/api/records").status_code == 200
    _age_session(token, 31)
    refused = client.get("/api/records")
    assert refused.status_code == 401 and "without activity" in refused.json()["detail"]
    with _session() as db:
        assert db.get(LoginSession, digest_token(token)) is None  # Ended, not merely refused.
    use_session(client, token)
    assert client.get("/api/records").status_code == 401


def test_activity_keeps_a_session_alive(real_auth_client, monkeypatch):
    client, token = real_auth_client
    monkeypatch.setattr(settings, "session_idle_minutes", 30)
    _age_session(token, 29)
    assert client.get("/api/records").status_code == 200
    with _session() as db:
        seen = db.get(LoginSession, digest_token(token)).last_seen_at
    assert datetime.now(timezone.utc).replace(tzinfo=None) - seen < timedelta(minutes=1)


def test_the_twelve_hour_limit_still_applies_to_an_active_session(real_auth_client):
    client, token = real_auth_client
    with _session() as db:
        session = db.get(LoginSession, digest_token(token))
        session.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/records").status_code == 401


def test_a_session_from_before_this_existed_counts_as_active(real_auth_client):
    client, token = real_auth_client
    with _session() as db:
        db.get(LoginSession, digest_token(token)).last_seen_at = None
        db.commit()
    assert client.get("/api/records").status_code == 200
    with _session() as db:
        assert db.get(LoginSession, digest_token(token)).last_seen_at is not None


def test_recording_activity_doesnt_write_on_every_request(real_auth_client):
    client, _token = real_auth_client
    client.get("/api/records")
    writes = []

    def count(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("UPDATE LOGIN_SESSIONS"):
            writes.append(statement)

    engine = _session().get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        for _ in range(5):
            assert client.get("/api/records").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert writes == []


# ------------------------------------------------------------------ logs and error responses

def test_request_log_lines_name_the_person(real_auth_client, caplog):
    client, _token = real_auth_client
    with caplog.at_level(logging.INFO, logger="ticketflow.request"):
        response = client.get("/api/records")
    line = next(json.loads(r.getMessage()) for r in caplog.records if response.headers["x-request-id"] in r.getMessage())
    assert line["user_id"] == 1


def test_an_anonymous_request_logs_no_person(client, caplog):
    with caplog.at_level(logging.INFO, logger="ticketflow.request"):
        response = client.get("/health")
    line = next(json.loads(r.getMessage()) for r in caplog.records if response.headers["x-request-id"] in r.getMessage())
    assert line["user_id"] is None


@pytest.mark.parametrize("supplied, kept", [("abc-123_X.y", True), ("a" * 64, True), ("a" * 65, False), ("has space", False),
                                            ('quote"', False), ("new\\nline", False), ("", False)])
def test_a_request_id_from_outside_is_kept_only_if_it_is_plain(client, supplied, kept):
    returned = client.get("/health", headers={"X-Request-ID": supplied}).headers["x-request-id"]
    assert (returned == supplied) is kept
    assert len(returned) <= 64


def test_an_unexpected_error_logs_its_kind_but_not_its_message(client, monkeypatch, caplog):
    _seed(1)

    def boom(*_args, **_kwargs):
        raise RuntimeError(f"could not price ticket {SECRET}")

    monkeypatch.setattr("app.routers.api.pricing_svc.rate_table", boom)
    with caplog.at_level(logging.INFO, logger="ticketflow.request"):
        response = client.get("/api/batches/1/tickets")
    assert response.status_code == 500
    assert SECRET not in caplog.text and SECRET not in response.text
    error = next(json.loads(r.getMessage()) for r in caplog.records if '"error"' in r.getMessage() and "trace" in r.getMessage())
    assert error["error"] == "RuntimeError" and error["request_id"] == response.json()["request_id"]
    assert any("boom" in frame for frame in error["trace"])


def test_a_rejected_value_isnt_echoed_back(client):
    """FastAPI's default 422 repeats what was submitted. Here that's a ticket's values."""
    _seed(1)
    response = client.patch("/api/tickets/1", json={"tonnage": f"{SECRET}", "delivery_date": f"{SECRET}-date"})
    assert response.status_code == 422
    assert SECRET not in response.text
    detail = response.json()["detail"]
    assert {entry["loc"][-1] for entry in detail} == {"tonnage", "delivery_date"}
    assert all(set(entry) <= {"type", "loc", "msg"} for entry in detail)


def test_a_malformed_body_isnt_echoed_back_either(client):
    response = client.post("/api/projects", content=f'{{"code": "{SECRET}", "name": ', headers={"content-type": "application/json"})
    assert response.status_code == 422 and SECRET not in response.text


def test_a_readers_bad_answer_isnt_quoted_on_the_ticket():
    """The validation error quotes what the reader returned: a ticket's values. Its message
    is saved on the ticket and returned by the API."""
    from app.services import extraction
    with pytest.raises(extraction.ExtractionError) as failed:
        extraction.validated_ticket({"ticket_number": {"nested": SECRET}, "tonnage": [SECRET]})
    assert SECRET not in str(failed.value)


def test_database_errors_never_show_values(client):
    """hide_parameters on every engine, and handlers that never repeat the database's text."""
    from app.db import engine_options
    assert engine_options("sqlite:///x")["hide_parameters"] is True
    assert engine_options("postgresql://x/y")["hide_parameters"] is True


def test_stored_documents_are_never_content_sniffed(client):
    """A document from a supplier is served with exactly the type it was checked as."""
    from .sample_data import make_invoice_pdf
    document = client.post("/api/invoice-documents", files={"file": ("i.pdf", make_invoice_pdf(), "application/pdf")}).json()
    original = client.get(f"/api/invoice-documents/{document['id']}/original")
    assert original.headers["content-type"] == "application/pdf" and original.headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------------ company scoping, by inventory

def test_every_route_learns_who_is_asking_unless_deliberately_public():
    """Company scoping starts from the signed-in person. A route that never asks who that
    is can't be scoping anything. Generated from the live route list."""
    from fastapi.routing import APIRoute
    from app.route_inventory import ROUTERS
    public = {"/api/auth/login", "/api/auth/logout", "/api/setup", "/api/setup/status", "/api/access-requests"}
    identity = {"current_organization_id", "authenticated_user", "current_user"}
    unscoped = [f"{sorted(route.methods)} {route.path}" for router in ROUTERS for route in router.routes
                if isinstance(route, APIRoute) and route.path not in public
                and not (_dependency_names(route.dependant) & identity) and not any(
                    getattr(d.call, "permission", None) for d in route.dependant.dependencies)]
    assert unscoped == []


def _dependency_names(dependant) -> set[str]:
    names = set()
    for dependency in dependant.dependencies:
        names.add(getattr(dependency.call, "__name__", ""))
        names |= _dependency_names(dependency)
    return names

