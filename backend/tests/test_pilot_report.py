"""Pilot report, opt-in timing, and exact decimals. All fictional; no network or AI."""
import csv
from decimal import Decimal
from io import StringIO

import pytest
from sqlalchemy import event, func, select

from app.auth import current_user
from app.config import settings
from app.main import app
from app.models import Organization, ReviewTiming, User
from app.routers import api as api_module
from .sample_data import TICKETS, make_invoice_pdf, make_page, make_pdf
from .test_invoice_ticket_matching import _line, _project, _rate

A, B, *_ = TICKETS
STONE_B = dict(B, material_type=A["material_type"])


def _batch(client, slots, project=None, **data):
    form = {"layout": "grid_2x2", "intake_mode": "manual", **data}
    if project:
        form["project_id"] = str(project)
    response = client.post("/api/batches", data=form,
                           files=[("files", ("t.pdf", make_pdf([make_page(slots)]), "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _tickets(client, batch):
    return client.get(f"/api/batches/{batch}/tickets").json()


def test_empty_workspace_reports_zero_without_claiming_anything_is_complete(client):
    report = client.get("/api/reports/pilot").json()
    assert report["unresolved_items"] == 0 and report["batches_needing_attention"] == []
    assert "percent" not in str(report).lower() and "complete" not in str(report).lower()


def test_report_counts_each_stage_and_points_to_where_to_look(client):
    project = _project(client)
    _rate(client)
    batch = _batch(client, ["faint", A, None, STONE_B], project, expected_ticket_count="3")
    client.post("/api/batches", data={"layout": "single", "intake_mode": "manual", "project_id": str(project)},
                files=[("files", ("broken.pdf", b"%PDF-1.4 broken", "application/pdf"))])
    tickets = _tickets(client, batch)
    assert client.patch(f"/api/tickets/{tickets[0]['id']}", json=A).status_code == 200
    assert client.post(f"/api/tickets/{tickets[0]['id']}/verify").status_code == 200
    assert client.post(f"/api/tickets/{tickets[1]['id']}/flag").status_code == 200

    report = client.get(f"/api/reports/pilot?project_id={project}").json()
    intake, review = report["intake"], report["review"]
    assert intake["batches"] == 2 and intake["files"] == 2 and intake["failed_files"] == 1
    assert intake["pages"] == 1 and intake["slots"] == 4
    assert intake["faint_unclassified"] == 1 and intake["empty"] == 1 and intake["confirmed_blank"] == 0
    assert intake["paper_count_mismatches"] == 1  # 2 on record, 3 expected
    assert review["records"] == 2 and review["verified"] == 1 and review["flagged"] == 1
    assert Decimal(review["verified_tons"]) == Decimal(A["tonnage"])
    # faint + failed file + count mismatch + flagged
    assert report["unresolved_items"] == 4
    reasons = {row["batch_id"]: row["reasons"] for row in report["batches_needing_attention"]}
    assert any("faint" in text for text in reasons[batch]) and any("expected" in text for text in reasons[batch])
    assert any("failed file" in text for rows in reasons.values() for text in rows)


def test_report_never_counts_unapproved_tons_or_treats_missing_prices_as_zero(client):
    project = _project(client)
    batch = _batch(client, [A, None, None, None], project)  # no rate configured
    ticket = _tickets(client, batch)[0]
    assert client.patch(f"/api/tickets/{ticket['id']}", json=A).status_code == 200
    assert Decimal(client.get("/api/reports/pilot").json()["review"]["verified_tons"]) == 0
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    report = client.get("/api/reports/pilot").json()
    assert report["review"]["verified_unpriced"] == 1 and report["unresolved_items"] == 1


def test_reconciliation_counts_linked_and_totals_only_lines(client):
    project = _project(client)
    _rate(client)
    batch = _batch(client, [A, STONE_B, None, None], project)
    ids = []
    for ticket, values in zip(_tickets(client, batch), (A, STONE_B)):
        assert client.patch(f"/api/tickets/{ticket['id']}", json=values).status_code == 200
        assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
        ids.append(ticket["id"])
    line = client.post("/api/invoice-comparisons", json=_line(project, ticket_list="10482\t24.50\n10483\t22.10")).json()
    client.post("/api/invoice-comparisons", json=_line(project, date_from="2026-11-01", date_to="2026-11-30"))
    before = client.get("/api/reports/pilot").json()["reconciliation"]
    assert before["invoice_lines"] == 2 and before["itemized_lines"] == 1 and before["totals_only_lines"] == 1
    assert before["verified_tickets_linked"] == 0 and before["verified_tickets_not_linked"] == 2
    assert before["lines_by_status"]["matches"] == 1
    # A line that doesn't reconcile needs a person, and the headline count says so.
    assert before["lines_needing_attention"] == 1
    assert client.get("/api/reports/pilot").json()["unresolved_items"] == 1

    client.post(f"/api/invoice-comparisons/{line['id']}/ticket-links", json={"ticket_ids": ids[:1]})
    after = client.get("/api/reports/pilot").json()["reconciliation"]
    assert after["verified_tickets_linked"] == 1 and Decimal(after["tons_linked"]) == Decimal(A["tonnage"])
    assert Decimal(after["tons_not_linked"]) == Decimal(STONE_B["tonnage"])


def test_report_csv_has_the_same_figures_and_no_formula_text(client):
    project = _project(client)
    _batch(client, ["faint", A, None, None], project)
    response = client.get(f"/api/reports/pilot.csv?project_id={project}")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert "no-store" in response.headers["cache-control"]
    rows = list(csv.reader(StringIO(response.text)))
    assert ["faint_unclassified", "1"] in rows and ["empty", "2"] in rows
    assert rows[0][0].startswith("TicketFlow pilot report - counts of records, not an approval")


def test_report_is_company_scoped(client):
    project = _project(client)
    _batch(client, [A, None, None, None], project)
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        db.commit()
    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=2, username="other", password_hash="fixture", active=True, role="viewer",
        must_change_password=False)
    report = client.get("/api/reports/pilot").json()
    assert report["intake"]["slots"] == 0 and report["review"]["records"] == 0
    assert client.get(f"/api/reports/pilot?project_id={project}").status_code == 404
    assert client.get(f"/api/reports/pilot.csv?project_id={project}").status_code == 404


def test_report_query_count_does_not_grow_with_tickets(client):
    project = _project(client)
    _batch(client, [A, None, None, None], project)

    def count_queries() -> int:
        engine = api_module.SessionLocal.kw["bind"]
        statements = []

        def counter(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", counter)
        try:
            assert client.get("/api/reports/pilot").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", counter)
        return len(statements)

    small = count_queries()
    # More tickets, in the same number of batches' worth of queries per batch.
    _batch(client, [dict(A, ticket_number=str(9000 + i)) for i in range(4)], project)
    _batch(client, [dict(A, ticket_number=str(9100 + i)) for i in range(4)], project)
    assert count_queries() <= small + 1


def test_timing_is_off_by_default_and_stores_nothing(client):
    batch = _batch(client, [A, None, None, None])
    ticket = _tickets(client, batch)[0]
    assert "review_timing_enabled" not in client.get("/api/status").json()
    response = client.post("/api/review-timings", json={"ticket_id": ticket["id"], "seconds": "12.5"})
    assert response.status_code == 404
    with api_module.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReviewTiming)) == 0
    assert client.get("/api/reports/pilot").json()["review_timing"]["enabled"] is False


def test_opt_in_timing_stores_only_a_duration_and_correction_count(client, monkeypatch):
    monkeypatch.setattr(settings, "review_timing_enabled", True)
    assert client.get("/api/status").json()["review_timing_enabled"] is True
    batch = _batch(client, [A, STONE_B, None, None])
    first, second = _tickets(client, batch)
    assert client.patch(f"/api/tickets/{first['id']}", json=A).status_code == 200
    assert client.post("/api/review-timings", json={"ticket_id": first["id"], "seconds": "10.0"}).status_code == 204
    assert client.post("/api/review-timings", json={"ticket_id": second["id"], "seconds": "20.0"}).status_code == 204
    with api_module.SessionLocal() as db:
        rows = list(db.scalars(select(ReviewTiming)))
        assert {column.name for column in ReviewTiming.__table__.columns} == {
            "id", "organization_id", "batch_id", "intake_mode", "seconds", "fields_corrected", "created_at"}
        assert sorted(r.fields_corrected > 0 for r in rows) == [False, True]
    manual = client.get("/api/reports/pilot").json()["review_timing"]["manual"]
    assert manual["samples"] == 2 and Decimal(manual["median_seconds"]) == Decimal("15") and manual["with_corrections"] == 1


@pytest.mark.parametrize("seconds", ["0", "-1", "3601", "1.23"])
def test_timing_values_are_validated(client, monkeypatch, seconds):
    monkeypatch.setattr(settings, "review_timing_enabled", True)
    ticket = _tickets(client, _batch(client, [A, None, None, None]))[0]
    assert client.post("/api/review-timings", json={"ticket_id": ticket["id"], "seconds": seconds}).status_code == 422


def test_timing_cannot_touch_another_companys_ticket_and_viewers_cannot_post(client, monkeypatch):
    monkeypatch.setattr(settings, "review_timing_enabled", True)
    ticket = _tickets(client, _batch(client, [A, None, None, None]))[0]
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        db.commit()
    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=2, username="other", password_hash="fixture", active=True, role="owner",
        must_change_password=False)
    assert client.post("/api/review-timings", json={"ticket_id": ticket["id"], "seconds": "5.0"}).status_code == 404
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="test-owner", password_hash="fixture", active=True, role="viewer",
        must_change_password=False)
    assert client.post("/api/review-timings", json={"ticket_id": ticket["id"], "seconds": "5.0"}).status_code == 403


def test_dashboard_sends_money_and_tons_as_exact_strings(client):
    project = _project(client)
    _rate(client)
    batch = _batch(client, [dict(A, tonnage="0.100"), dict(STONE_B, tonnage="0.200"), None, None], project)
    for ticket, tons in zip(_tickets(client, batch), ("0.100", "0.200")):
        assert client.patch(f"/api/tickets/{ticket['id']}", json=dict(A, tonnage=tons)).status_code == 200
        assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    data = client.get("/api/dashboard").json()
    assert data["total_tons"] == "0.300"  # floats would give 0.30000000000000004
    assert isinstance(data["total_cost"], str) and isinstance(data["pending_tons"], str)
    assert isinstance(data["by_supplier"][0]["tons"], str) and isinstance(data["by_material"][0]["cost"], str)


def test_migration_is_additive(tmp_path):
    import sqlite3
    from alembic import command
    from .migration_helpers import alembic_config, head_revision

    path = tmp_path / "before-timings.db"
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "20261105_13")
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision()
        assert conn.execute("SELECT COUNT(*) FROM review_timings").fetchone()[0] == 0
