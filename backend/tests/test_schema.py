"""A database built by the migrations must be exactly what the code expects."""
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from app.db import Base, engine_options, normalized_url
from app.models import Ticket  # noqa: F401 - registers the tables

from .conftest import TEST_DATABASE_URL
from .migration_helpers import alembic_config, head_revision


@pytest.fixture()
def blank_database(tmp_path):
    """A brand new, empty database of whichever kind this run is testing."""
    if not TEST_DATABASE_URL:
        yield f"sqlite:///{tmp_path / 'fresh.db'}"
        return
    name = f"migrate_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(normalized_url(TEST_DATABASE_URL), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    base, _, _ = TEST_DATABASE_URL.rpartition("/")
    try:
        yield f"{base}/{name}"
    finally:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _differences(url: str) -> list:
    engine = create_engine(normalized_url(url), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            return compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()


def test_a_fresh_database_built_by_migrations_matches_the_code(blank_database):
    command.upgrade(alembic_config(blank_database), "head")
    assert _differences(blank_database) == []


def test_migrating_twice_changes_nothing(blank_database):
    command.upgrade(alembic_config(blank_database), "head")
    command.stamp(alembic_config(blank_database), "20261029_01")
    command.upgrade(alembic_config(blank_database), "head")  # Every step must tolerate what's already there.
    engine = create_engine(normalized_url(blank_database), poolclass=NullPool)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == head_revision()
    engine.dispose()
    assert _differences(blank_database) == []


def test_the_baseline_doesnt_depend_on_the_current_models():
    """Migrations must not import the application's models to build tables."""
    versions = Path(__file__).parents[1] / "migrations" / "versions"
    for path in versions.glob("*.py"):
        text_ = path.read_text()
        assert "Base.metadata" not in text_, path.name
        assert "from app import models" not in text_, path.name


def test_every_money_and_quantity_column_is_exact():
    """Floats would lose cents. The one float column is an image measure."""
    from sqlalchemy import Float, Numeric
    floats = [f"{t.name}.{c.name}" for t in Base.metadata.sorted_tables for c in t.columns if isinstance(c.type, Float) and not isinstance(c.type, Numeric)]
    assert floats == ["scan_slots.ink_fraction"]
    endings = ("tons", "tonnage", "unit_price", "unit_price_override", "subtotal", "quantity", "cost")
    money = [(t.name, c) for t in Base.metadata.sorted_tables for c in t.columns if c.name.endswith(endings)]
    assert len(money) >= 10
    for table, column in money:
        assert isinstance(column.type, Numeric), f"{table}.{column.name}"


def test_postgres_startup_refuses_an_unmigrated_database(blank_database, monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("PostgreSQL only: SQLite local installs may create tables at startup")
    from fastapi.testclient import TestClient
    from app import main as main_module
    engine = create_engine(normalized_url(blank_database), poolclass=NullPool)
    monkeypatch.setattr(main_module, "engine", engine)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        with TestClient(main_module.app):
            pass
    assert inspect(engine).get_table_names() == []  # Nothing was created behind anyone's back.
    engine.dispose()
