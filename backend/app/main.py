import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm.exc import StaleDataError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import rate_limit
from .config import configuration_problems, settings
from .db import Base, engine
from .routers import access_requests, api, auth as auth_router, invoice_documents, invoices, pricing, projects, records, reports, setup, system, team, scans
from .services import system_health
from .services.job_log import frames
from .auth import current_user

request_log = logging.getLogger("ticketflow.request")
if not request_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    request_log.addHandler(_handler)
    request_log.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = configuration_problems(settings)
    if problems:
        raise RuntimeError(" ".join(problems))
    rate_limit.reset()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)  # new local installs only; never alters existing tables
    else:
        # A server database changes only through migrations: never implicitly at startup.
        current, expected = system_health.schema_revisions(engine)
        if current != expected:
            raise RuntimeError(f"Database is at {current or 'no version'}, but this code needs {expected}. "
                               "Back up, then run 'cd backend && python -m alembic upgrade head'.")
    columns = {c["name"] for c in inspect(engine).get_columns("tickets")}
    required = {"price_snapshot_state", "applied_unit_price", "applied_price_source"}
    if not required.issubset(columns):
        raise RuntimeError(
            "TicketFlow database migration required. Back up backend/ticketflow.db, "
            "then run 'cd backend && alembic upgrade head'. Do not delete your database."
        )
    for name in ("users", "login_sessions"):
        if not inspect(engine).has_table(name):
            raise RuntimeError("Authentication migration missing: run 'cd backend && alembic upgrade head'.")
    # Fail closed if the organization migration was not applied. create_all()
    # does not alter existing tables, so a legacy user must migrate first.
    for name in ("users", "projects", "batches", "suppliers", "materials", "supplier_rates"):
        columns = {c["name"] for c in inspect(engine).get_columns(name)}
        if "organization_id" not in columns:
            raise RuntimeError(
                "Company ownership migration required: back up the database, "
                "then run 'cd backend && python -m alembic upgrade head'."
            )
    if not inspect(engine).has_table("organizations"):
        raise RuntimeError("Organization migration missing; run alembic upgrade head.")
    if "intake_mode" not in {
        column["name"] for column in inspect(engine).get_columns("batches")
    }:
        raise RuntimeError(
            "Manual ticket intake migration required: back up backend/ticketflow.db, "
            "then run 'cd backend && python -m alembic upgrade head'."
        )
    # Individual accounts: roles and actor attribution must exist before any
    # request is served, otherwise permission checks could not be enforced.
    inspector = inspect(engine)
    user_columns = {c["name"] for c in inspector.get_columns("users")}
    audit_columns = {c["name"] for c in inspector.get_columns("audit_events")}
    if (
        not {"role", "must_change_password"}.issubset(user_columns)
        or "actor_user_id" not in audit_columns
        or not inspector.has_table("account_events")
    ):
        raise RuntimeError(
            "Team accounts migration required: back up backend/ticketflow.db, "
            "then run 'cd backend && python -m alembic upgrade head'."
        )
    if "flag_note" not in {c["name"] for c in inspector.get_columns("tickets")}:
        raise RuntimeError(
            "Review workspace migration required: back up backend/ticketflow.db, "
            "then run 'cd backend && python -m alembic upgrade head'."
        )
    if "expected_ticket_count" not in {c["name"] for c in inspector.get_columns("batches")}:
        raise RuntimeError(
            "Scan check migration required: back up backend/ticketflow.db, "
            "then run 'cd backend && python -m alembic upgrade head'."
        )
    if "last_seen_at" not in {c["name"] for c in inspector.get_columns("login_sessions")}:
        raise RuntimeError(
            "Session migration required: back up backend/ticketflow.db, "
            "then run 'cd backend && python -m alembic upgrade head'."
        )
    yield


def _documentation() -> dict:
    """Off unless ENABLE_API_DOCS=true. app.openapi() still works either way, which
    the route inventory relies on."""
    if settings.enable_api_docs:
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(title="TicketFlow", lifespan=lifespan, **_documentation())
_PLAIN_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One line per request: enough to diagnose, with nothing from the documents.

    Logs the route's template and never its query, since filters can hold names.
    Added first, so errors pass back through CORS and the browser can read them.
    """
    supplied = request.headers.get("X-Request-ID", "")
    # Kept only if it's plain, so it can't break a log line or a header.
    request_id = supplied if _PLAIN_ID.fullmatch(supplied) else uuid.uuid4().hex[:12]
    started = time.perf_counter()
    status, failure = 500, None
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as exc:
        failure = type(exc).__name__
        # The error's type and where it happened. Never its message, which can quote a record.
        request_log.error(json.dumps({"request_id": request_id, "error": failure, "trace": frames(exc.__traceback__)}))
        response = JSONResponse({"detail": "Something went wrong on our side, and nothing further was changed. "
                                           f"If it keeps happening, quote reference {request_id}.", "request_id": request_id},
                                status_code=500)
    route = request.scope.get("route")
    request_log.info(json.dumps({
        "request_id": request_id, "method": request.method, "route": getattr(route, "path", "unmatched"),
        "status": status, "ms": round((time.perf_counter() - started) * 1000, 1),
        "user_id": getattr(request.state, "user_id", None),
        **({"error": failure} if failure else {}),
    }))
    response.headers["X-Request-ID"] = request_id
    return response


# Database refusals become clear answers. In every case the request's transaction
# is rolled back, so nothing was saved. The database's message is never shown or
# logged, because it can quote the values involved.
@app.exception_handler(RequestValidationError)
async def _invalid_request(_request: Request, exc: RequestValidationError):
    """Which fields, and why. FastAPI's default also repeats what was sent, which here is
    a ticket's values, so that's left out."""
    return JSONResponse({"detail": [{"type": error.get("type"), "loc": list(error.get("loc", ())), "msg": error.get("msg")}
                                    for error in exc.errors()]}, status_code=422)


@app.exception_handler(StaleDataError)
async def _changed_at_once(_request: Request, _exc: StaleDataError):
    return JSONResponse({"detail": {"code": "record_changed", "message": "Someone else saved a change to this at the same moment. "
                                    "Nothing was saved. The latest values are shown now: check them."}}, status_code=409)


@app.exception_handler(IntegrityError)
async def _refused_by_a_rule(_request: Request, _exc: IntegrityError):
    return JSONResponse({"detail": {"code": "conflict", "message": "That conflicts with a record already saved, possibly at this "
                                    "same moment. Nothing was saved. Refresh and check."}}, status_code=409)


@app.exception_handler(DataError)
async def _unusable_value(_request: Request, _exc: DataError):
    return JSONResponse({"detail": "A value is too long or can't be stored in that field. Nothing was saved."}, status_code=422)


@app.exception_handler(OverflowError)
async def _number_out_of_range(_request: Request, _exc: OverflowError):
    return JSONResponse({"detail": "That number is too large to be a real record."}, status_code=422)


@app.exception_handler(OperationalError)
async def _database_unavailable(_request: Request, exc: OperationalError):
    request_log.warning(json.dumps({"event": "database_unavailable", "error": type(exc.orig).__name__}))
    return JSONResponse({"detail": "The database is busy or unavailable. Nothing was saved. Try again in a moment."},
                        status_code=503, headers={"Retry-After": "5"})


@app.exception_handler(PoolTimeoutError)
async def _no_free_connection(_request: Request, _exc: PoolTimeoutError):
    request_log.warning(json.dumps({"event": "database_pool_exhausted"}))
    return JSONResponse({"detail": "The database is busy or unavailable. Nothing was saved. Try again in a moment."},
                        status_code=503, headers={"Retry-After": "5"})


app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins,
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    # Retry-After lets the page say how long to wait after a 429 or 503.
    expose_headers=["X-Request-ID", "Retry-After"],
)
app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts, www_redirect=False,
)


@app.middleware("http")
async def csrf_header_gate(request: Request, call_next):
    # A custom header prevents ordinary cross-site HTML forms from issuing
    # state-changing requests with a victim's authentication cookie.
    if request.url.path.startswith("/api/") and request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        if request.headers.get("X-TicketFlow-Request") != "1":
            return JSONResponse({"detail": "Missing required request verification header"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in settings.cors_origins:
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if settings.session_secure_cookie:
        # Secure cookies mean this is served over HTTPS: keep browsers from ever trying plain HTTP.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        # No browser/proxy cache may serve a previous owner's tickets, images,
        # financial summaries, or login responses.
        response.headers["Cache-Control"] = "private, no-store"
    return response


app.include_router(auth_router.router)
app.include_router(setup.router)  # Public until the first user exists. See routers/setup.py.
app.include_router(access_requests.router)  # Public, and grants nothing. See routers/access_requests.py.
app.include_router(api.router, dependencies=[Depends(current_user)])
app.include_router(pricing.router, dependencies=[Depends(current_user)])
app.include_router(invoices.router, dependencies=[Depends(current_user)])
app.include_router(projects.router, dependencies=[Depends(current_user)])
app.include_router(reports.router, dependencies=[Depends(current_user)])
app.include_router(records.router, dependencies=[Depends(current_user)])
app.include_router(system.router, dependencies=[Depends(current_user)])
app.include_router(team.router, dependencies=[Depends(current_user)])
app.include_router(scans.router, dependencies=[Depends(current_user)])
app.include_router(invoice_documents.router, dependencies=[Depends(current_user)])


@app.get("/health")
def health():
    """Liveness: the process answers. It doesn't touch the database, so a database
    outage doesn't make a supervisor restart a healthy API."""
    return {"ok": True}


@app.get("/health/ready")
def ready():
    """Readiness, for a load balancer: the database answers. Says nothing else, to anyone."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # Any failure at all means not ready.
        request_log.warning(json.dumps({"event": "not_ready", "error": type(exc).__name__}))
        return JSONResponse({"ok": False}, status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "5"})
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@app.get("/api/status", dependencies=[Depends(current_user)])
def status():
    """Lets the UI warn when extraction isn't configured. Never returns the key itself."""
    result = {**_extraction_status(), "worker_running": system_health.worker_running()}
    if settings.review_timing_enabled:
        result["review_timing_enabled"] = True
    return result


def _extraction_status():
    if settings.extraction_provider == "gemini":
        return {"extraction_configured": bool(settings.gemini_api_key), "model": settings.gemini_model}
    if settings.extraction_provider == "anthropic":
        return {"extraction_configured": bool(settings.anthropic_api_key), "model": settings.extraction_model}
    return {"extraction_configured": False, "model": "Invalid extraction provider configuration"}
