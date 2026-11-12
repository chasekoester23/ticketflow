from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

# The role a company's transactions run as when POSTGRES_RLS is on. It owns nothing and
# can't bypass row-level security, so the policies apply. The connecting role owns the
# tables and so passes them by: migrations, scripts, and the worker's claiming do.
TENANT_ROLE = "ticketflow_tenant"


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
    # pool_recycle replaces a connection before a proxy or server drops it for being idle.
    return {"hide_parameters": True, "pool_pre_ping": settings.database_pool_pre_ping,
            "pool_size": settings.database_pool_size, "max_overflow": settings.database_pool_overflow,
            "pool_recycle": settings.database_pool_recycle_seconds, "pool_timeout": settings.database_pool_timeout_seconds,
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


# ------------------------------------------------------------------ row-level security

def _scope(connection, organization_id: int) -> None:
    connection.exec_driver_sql(f"SET LOCAL ROLE {TENANT_ROLE}")
    connection.execute(text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(int(organization_id))})


def scope_to_organization(session: Session, organization_id: int) -> None:
    """From here on, this session works for one company. With POSTGRES_RLS on PostgreSQL,
    every transaction it runs is held to that company's rows by the database itself,
    whatever a query forgets to filter. Otherwise it only records the company."""
    session.info["organization_id"] = organization_id
    if settings.postgres_rls and session.in_transaction():
        connection = session.connection()
        if connection.dialect.name == "postgresql":
            _scope(connection, organization_id)


@event.listens_for(Session, "after_begin")
def _scope_each_transaction(session: Session, _transaction, connection) -> None:
    organization_id = session.info.get("organization_id")
    if organization_id is not None and settings.postgres_rls and connection.dialect.name == "postgresql":
        _scope(connection, organization_id)


# ------------------------------------------------------------------ fencing a worker's writes

class LeaseLost(BaseException):
    """This worker no longer holds its job, so nothing more of its work may be saved.

    A BaseException, so the pipeline's handlers for a bad file or ticket, which catch
    Exception and save a failure, can't swallow it and write once more."""


@event.listens_for(Session, "before_commit")
def _check_fence(session: Session) -> None:
    """A worker's session carries the job it holds. Every commit first proves the job is
    still its own, holding the job's row until the commit lands, so a worker whose lease
    lapsed and was taken over can't overwrite the new holder's work."""
    fence = session.info.get("fence")
    if fence is None:
        return
    if session.info.get("lease_lost"):
        raise LeaseLost(fence[0])
    from .models import ProcessingJob  # noqa: PLC0415 - models import this module

    session.flush()
    held = session.scalar(select(ProcessingJob.id).where(
        ProcessingJob.id == fence[0], ProcessingJob.claim_token == fence[1], ProcessingJob.status == "running",
    ).with_for_update())
    if held is None:
        session.info["lease_lost"] = True
        raise LeaseLost(fence[0])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
