from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import Base, engine
from .routers import api, pricing, projects, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)  # Phase 1 convenience; Alembic migrations arrive with Phase 2 models
    yield


app = FastAPI(title="TicketFlow", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(api.router)
app.include_router(pricing.router)
app.include_router(projects.router)
app.include_router(reports.router)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    """Lets the UI warn when extraction isn't configured. Never returns the key itself."""
    return {"extraction_configured": bool(settings.anthropic_api_key), "model": settings.extraction_model}
