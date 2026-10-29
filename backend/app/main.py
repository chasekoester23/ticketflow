from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request
from sqlalchemy import inspect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import Base, engine
from .routers import api, auth as auth_router, pricing, projects, reports
from .auth import current_user


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    yield


app = FastAPI(title="TicketFlow", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins,
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
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
    if request.url.path.startswith("/api/"):
        # No browser/proxy cache may serve a previous owner's tickets, images,
        # financial summaries, or login responses.
        response.headers["Cache-Control"] = "private, no-store"
    return response


app.include_router(auth_router.router)
app.include_router(api.router, dependencies=[Depends(current_user)])
app.include_router(pricing.router, dependencies=[Depends(current_user)])
app.include_router(projects.router, dependencies=[Depends(current_user)])
app.include_router(reports.router, dependencies=[Depends(current_user)])


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/status", dependencies=[Depends(current_user)])
def status():
    """Lets the UI warn when extraction isn't configured. Never returns the key itself."""
    return {"extraction_configured": bool(settings.anthropic_api_key), "model": settings.extraction_model}
