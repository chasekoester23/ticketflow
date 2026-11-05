from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request
from sqlalchemy import inspect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import settings
from .db import Base, engine
from .routers import api, auth as auth_router, invoice_documents, invoices, pricing, projects, reports, team, scans
from .auth import current_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.trusted_hosts or any("*" in host for host in settings.trusted_hosts):
        raise RuntimeError("TRUSTED_HOSTS must list explicit API hostnames, never wildcards.")
    if not settings.session_secure_cookie and any(
        not origin.startswith(("http://localhost:", "http://127.0.0.1:"))
        for origin in settings.cors_origins
    ):
        raise RuntimeError("Set SESSION_SECURE_COOKIE=true before using a non-localhost frontend.")
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)  # new local installs only; never alters existing tables
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
    yield


app = FastAPI(title="TicketFlow", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins,
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
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
    if request.url.path.startswith("/api/"):
        # No browser/proxy cache may serve a previous owner's tickets, images,
        # financial summaries, or login responses.
        response.headers["Cache-Control"] = "private, no-store"
    return response


app.include_router(auth_router.router)
app.include_router(api.router, dependencies=[Depends(current_user)])
app.include_router(pricing.router, dependencies=[Depends(current_user)])
app.include_router(invoices.router, dependencies=[Depends(current_user)])
app.include_router(projects.router, dependencies=[Depends(current_user)])
app.include_router(reports.router, dependencies=[Depends(current_user)])
app.include_router(team.router, dependencies=[Depends(current_user)])
app.include_router(scans.router, dependencies=[Depends(current_user)])
app.include_router(invoice_documents.router, dependencies=[Depends(current_user)])


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/status", dependencies=[Depends(current_user)])
def status():
    """Lets the UI warn when extraction isn't configured. Never returns the key itself."""
    if settings.review_timing_enabled:
        return {**_extraction_status(), "review_timing_enabled": True}
    return _extraction_status()


def _extraction_status():
    if settings.extraction_provider == "gemini":
        return {"extraction_configured": bool(settings.gemini_api_key), "model": settings.gemini_model}
    if settings.extraction_provider == "anthropic":
        return {"extraction_configured": bool(settings.anthropic_api_key), "model": settings.extraction_model}
    return {"extraction_configured": False, "model": "Invalid extraction provider configuration"}
