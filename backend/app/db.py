from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings



def engine_options(url: str) -> dict:
    """Connection settings for each database. One place, so scripts and tests match the app."""
    if url.startswith("sqlite"):
        # SQLite allows one writer at a time. Wait for the worker or a colleague's
        # save rather than failing a person's approval with "database is locked".
        return {"connect_args": {"check_same_thread": False, "timeout": 30}}
    # A connection dropped by a restart or failover is replaced instead of failing
    # someone's request. A request stuck on a lock fails clearly instead of hanging.
    return {"pool_pre_ping": True, "pool_size": settings.database_pool_size, "max_overflow": settings.database_pool_overflow,
            "connect_args": {"options": f"-c lock_timeout={settings.database_lock_timeout_ms} "
                                        f"-c statement_timeout={settings.database_statement_timeout_ms}"}}


def normalized_url(url: str) -> str:
    """The driver we support, whichever way the URL was written."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(normalized_url(settings.database_url), **engine_options(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
