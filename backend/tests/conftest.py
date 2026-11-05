import os

# Tests must NEVER open the developer's real backend/ticketflow.db. Point the
# global engine at an in-memory database before any app module is imported
# (an explicit environment variable overrides backend/.env).
#
# TEST_DATABASE_URL runs the same suite on an empty, disposable PostgreSQL
# database. Its name must contain "test": every table in it is erased repeatedly.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if TEST_DATABASE_URL and "test" not in TEST_DATABASE_URL.rsplit("/", 1)[-1]:
    raise RuntimeError("TEST_DATABASE_URL must name a disposable database with 'test' in its name.")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL or "sqlite://"

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app import main as main_module
from app.auth import current_user
from app.models import Organization, User
from app.config import settings
from app.db import engine_options, normalized_url
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


# Tests insert fixed ids. On PostgreSQL that doesn't advance the id sequence, so
# the next automatic id would collide. This test-only trigger keeps them in step.
_ADVANCE_SEQUENCE = """
CREATE OR REPLACE FUNCTION test_advance_sequence() RETURNS trigger AS $$
DECLARE
  name text := pg_get_serial_sequence(TG_TABLE_NAME, 'id');
  seen bigint;
BEGIN
  EXECUTE format('SELECT last_value FROM %s', name) INTO seen;
  IF NEW.id >= seen THEN PERFORM setval(name, NEW.id); END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql"""


@pytest.fixture(scope="session")
def postgres_engine():
    """The disposable PostgreSQL database, built by the real migrations."""
    if not TEST_DATABASE_URL:
        yield None
        return
    from .migration_helpers import alembic_config
    from alembic import command
    engine = create_engine(normalized_url(TEST_DATABASE_URL), **engine_options(TEST_DATABASE_URL))
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    with engine.begin() as connection:
        connection.execute(text(_ADVANCE_SEQUENCE))
        for table in db_module.Base.metadata.sorted_tables:
            if "id" in table.columns:
                connection.execute(text(
                    f"CREATE TRIGGER test_advance AFTER INSERT ON {table.name} "
                    "FOR EACH ROW EXECUTE FUNCTION test_advance_sequence()"))
    yield engine
    engine.dispose()


def _reset_postgres(engine) -> None:
    names = ", ".join(table.name for table in db_module.Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def client(tmp_path, monkeypatch, postgres_engine):
    if postgres_engine is not None:
        engine = postgres_engine
        _reset_postgres(engine)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        db_module.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as session:
        session.add(Organization(id=1, name="Test Company"))
        # A real stored owner, so audit actor IDs refer to an actual account.
        session.add(User(id=1, organization_id=1, username="test-owner",
                         display_name="Test Owner", password_hash="fixture",
                         active=True, role="owner"))
        session.commit()
    # The app's startup schema checks (and create_all) run on this engine.
    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(api_module, "SessionLocal", Session)
    monkeypatch.setattr(api_module, "get_extractor", lambda: FixtureExtractor())
    # Dedicated workers are used in production. TestClient runs the same
    # persisted queue inline as a fixture-controlled post-response hook.
    monkeypatch.setattr(
        api_module, "_notify_worker",
        lambda background, job_id: background.add_task(api_module._drain_job_for_tests),
    )

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[db_module.get_db] = override
    # Existing app tests exercise business behavior with a known signed-in
    # single owner. Separate security tests exercise real cookie authorization.
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="test-owner", display_name="Test Owner",
        password_hash="fixture", active=True, role="owner", must_change_password=False,
    )
    with TestClient(app) as c:
        c.headers.update({"X-TicketFlow-Request": "1"})
        yield c
    app.dependency_overrides.clear()
