"""Migrating safely in production: one deploy at a time, and the pool settings a server needs."""
import threading
import time
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import engine_options, normalized_url
from app.services.row_security import SCHEMA_LOCK_KEY

from .conftest import TEST_DATABASE_URL
from .migration_helpers import alembic_config, head_revision


def test_postgres_connections_use_the_configured_pool(monkeypatch):
    monkeypatch.setattr(settings, "database_pool_size", 7)
    monkeypatch.setattr(settings, "database_pool_overflow", 3)
    monkeypatch.setattr(settings, "database_pool_recycle_seconds", 600)
    monkeypatch.setattr(settings, "database_pool_timeout_seconds", 12)
    monkeypatch.setattr(settings, "database_pool_pre_ping", True)
    options = engine_options("postgresql://example/ticketflow")
    assert {k: options[k] for k in ("pool_size", "max_overflow", "pool_recycle", "pool_timeout", "pool_pre_ping")} == {
        "pool_size": 7, "max_overflow": 3, "pool_recycle": 600, "pool_timeout": 12, "pool_pre_ping": True}
    assert "statement_timeout=" in options["connect_args"]["options"] and "lock_timeout=" in options["connect_args"]["options"]
    assert "pool_size" not in engine_options("sqlite:///x.db")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="Advisory locks are PostgreSQL's (CI).")
def test_a_second_deploy_waits_for_the_first_to_finish_migrating():
    name = f"lock_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(normalized_url(TEST_DATABASE_URL), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    url = f"{TEST_DATABASE_URL.rpartition('/')[0]}/{name}"
    engine = create_engine(normalized_url(url), poolclass=NullPool)
    try:
        with engine.connect() as first_deploy:
            first_deploy.execute(text("SELECT pg_advisory_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
            first_deploy.commit()
            errors = []
            second = threading.Thread(target=lambda: _upgrade(url, errors))
            second.start()
            time.sleep(3)
            assert second.is_alive(), "the second deploy migrated while the first held the lock"
            first_deploy.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEMA_LOCK_KEY})
            first_deploy.commit()
            second.join(180)
            assert not second.is_alive() and errors == []
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == head_revision()
            # The lock was released: another deploy finds nothing to do and doesn't wait.
            assert connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEMA_LOCK_KEY}).scalar() is True
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _upgrade(url, errors):
    try:
        command.upgrade(alembic_config(url), "head")
    except Exception as exc:  # noqa: BLE001 - reported by the test
        errors.append(repr(exc))
