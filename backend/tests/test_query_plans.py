"""A filter to one project must read that project's tickets, not the whole company's."""
import pytest
from sqlalchemy import select, text

from app.models import Ticket
from app.routers.reports import filtered_query
from app.services import aggregates

from .test_query_budgets import _session

ONE_PROJECT = dict(organization_id=1, project_id=1, supplier=None, material=None,
                   status=None, date_from=None, date_to=None)


def _plan(db, query) -> str:
    sql = str(query.compile(db.get_bind(), compile_kwargs={"literal_binds": True}))
    return " | ".join(row[3] for row in db.execute(text("EXPLAIN QUERY PLAN " + sql)))


@pytest.mark.parametrize("query", [
    pytest.param(lambda: select(aggregates.counted_tons(), aggregates.counted_cost(), aggregates.unpriced()), id="totals"),
    pytest.param(lambda: select(Ticket.id).order_by(Ticket.tonnage.desc(), Ticket.id.desc()).limit(50).offset(2000),
                 id="page sorted by tons"),
])
def test_one_project_is_read_through_its_batches(client, query):
    with _session() as db:
        if db.get_bind().dialect.name != "sqlite":
            pytest.skip("SQLite's plan is fixed by its rules. PostgreSQL chooses from table statistics.")
        plan = _plan(db, filtered_query(query(), **ONE_PROJECT))
    assert "(organization_id=? AND batch_id=?" in plan, plan


def test_a_check_for_copies_of_one_scan_reads_only_that_image(client):
    """Every approval and ticket read looks for copies of the same scanned image."""
    with _session() as db:
        if db.get_bind().dialect.name != "sqlite":
            pytest.skip("SQLite's plan is fixed by its rules. PostgreSQL chooses from table statistics.")
        query = select(Ticket.id).where(Ticket.organization_id == 1, Ticket.status != "skipped", Ticket.crop_sha256.in_(["a" * 64]))
        plan = _plan(db, query)
    assert "(organization_id=? AND crop_sha256=?)" in plan, plan
