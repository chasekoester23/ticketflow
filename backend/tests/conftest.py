import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.config import settings
from app.main import app
from app.routers import api as api_module
from app.services.validation import ExtractedTicket

from .sample_data import TICKETS


class FixtureExtractor:
    """TEST ONLY: returns known ground truth in order, standing in for the vision API.
    Production code never uses this; with no API key the real pipeline fails loudly."""

    def __init__(self, truths=None):
        self.truths = list(truths or TICKETS)
        self.calls = 0

    def extract(self, image: Image.Image):
        raw = self.truths[self.calls % len(self.truths)]
        self.calls += 1
        return ExtractedTicket.model_validate(raw), dict(raw)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db_module.Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(api_module, "SessionLocal", Session)
    monkeypatch.setattr(api_module, "get_extractor", lambda: FixtureExtractor())

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[db_module.get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
