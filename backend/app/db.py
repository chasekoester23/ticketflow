from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings



def engine_options(url: str) -> dict:
    """Connection settings for each database. One place, so scripts and tests match the app."""
    if url.startswith("sqlite"):
        # SQLite allows one writer at a time. Wait for the worker or a colleague's
        # save rather than failing a person's approval with "database is locked".
        return {"connect_args": {"check_same_thread": False, "timeout": 30}, "hide_parameters": True}
    # A connection dropped by a restart or failover is replaced instead of failing
    # someone's request. A request stuck on a lock fails clearly instead of hanging.
    # hide_parameters keeps ticket values out of error messages and logs. Timestamps
    # come back in UTC whatever the server's own setting is, so a date never shifts.
    return {"hide_parameters": True, "pool_pre_ping": True, "pool_size": settings.database_pool_size, "max_overflow": settings.database_pool_overflow,
            "connect_args": {"options": f"-c lock_timeout={settings.database_lock_timeout_ms} "
                                        f"-c statement_timeout={settings.database_statement_timeout_ms} -c timezone=UTC"}}


def normalized_url(url: str) -> str:
    """The driver we support, whichever way the URL was written."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def enforce_references(target) -> None:
    """SQLite ignores foreign keys unless asked, on every connection. Without this a
    row could point at something that doesn't exist, and PostgreSQL would behave
    differently. Migrations use their own engine, so table rebuilds can't cascade."""
    if target.dialect.name == "sqlite":
        event.listen(target, "connect", lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"))


engine = create_engine(normalized_url(settings.database_url), **engine_options(settings.database_url))
enforce_references(engine)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
