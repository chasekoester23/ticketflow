"""Row-level security on PostgreSQL: switch it on or off, and say what state it's in.

Migration 20261111_25 creates a policy on each company table, inert until row-level
security is enabled on that table. `enable` does that, creates the role company
transactions run as (db.TENANT_ROLE) and grants it the tables. The connecting role
owns the tables, so it passes the policies by: that is the bypass for migrations,
scripts and the worker's claiming. Defence in depth: every query already filters by
company, and this makes the database refuse a query that forgets.

Run as the role that owns the tables, from the deploy step, never from a request.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import TENANT_ROLE

POLICY = "ticketflow_tenant"
# Any constant shared with migrations/env.py, so enabling and migrating can't overlap.
SCHEMA_LOCK_KEY = 7_311_204_650


def policy_tables(connection: Connection) -> list[str]:
    return list(connection.scalars(text(
        "SELECT tablename FROM pg_policies WHERE policyname = :policy AND schemaname = current_schema() ORDER BY tablename"),
        {"policy": POLICY}))


def status(connection: Connection) -> dict:
    if connection.dialect.name != "postgresql":
        return {"supported": False, "role_exists": False, "tables": {}}
    enabled = dict(connection.execute(text(
        "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r'")).all())
    return {"supported": True,
            "role_exists": bool(connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": TENANT_ROLE})),
            "tables": {table: bool(enabled.get(table)) for table in policy_tables(connection)}}


def problems(connection: Connection) -> list[str]:
    """Why POSTGRES_RLS=on can't work on this database yet."""
    state = status(connection)
    if not state["supported"]:
        return ["POSTGRES_RLS needs PostgreSQL."]
    found = []
    if not state["role_exists"]:
        found.append(f"The role {TENANT_ROLE} doesn't exist.")
    if not state["tables"]:
        found.append("No row-level security policies exist: run python -m alembic upgrade head.")
    off = sorted(table for table, on in state["tables"].items() if not on)
    if off:
        found.append("Row-level security is off on " + ", ".join(off) + ".")
    return found


def enable(connection: Connection) -> list[str]:
    """Turn it on, in the caller's transaction. Returns the tables now protected."""
    if connection.dialect.name != "postgresql":
        raise RuntimeError("Row-level security needs PostgreSQL.")
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
    tables = policy_tables(connection)
    if not tables:
        raise RuntimeError("No policies found. Run python -m alembic upgrade head first.")
    connection.exec_driver_sql(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{TENANT_ROLE}') THEN "
        f"CREATE ROLE {TENANT_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$")
    connection.exec_driver_sql(f"GRANT {TENANT_ROLE} TO CURRENT_USER")
    connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {TENANT_ROLE}")
    connection.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {TENANT_ROLE}")
    connection.exec_driver_sql(f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {TENANT_ROLE}")
    # Tables a later migration adds are granted too, so turning this on is done once.
    connection.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {TENANT_ROLE}")
    connection.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {TENANT_ROLE}")
    for table in tables:
        connection.exec_driver_sql(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    return tables


def disable(connection: Connection) -> list[str]:
    """Turn it off again. The policies and the role stay, unused."""
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
    tables = policy_tables(connection)
    for table in tables:
        connection.exec_driver_sql(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    return tables
