"""Human-confirmed invoice ticket links. See docs/engineering/invoice-ticket-evidence-spec.md.
All fictional; no network or AI."""
import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.auth import current_user
from app.main import app
from app.models import (
    Batch, InvoiceComparison, InvoiceTicketLink, InvoiceTicketRef, Organization, Project, Ticket, UploadedFile, User,
)
from app.routers import api as api_module
from app.services import ticket_evidence
from .migration_helpers import alembic_config, head_revision
from .test_invoice_ticket_matching import _deliveries, _line, _project, _rate, _rows


def _post(client, project, **values):
    response = client.post("/api/invoice-comparisons", json=_line(project, **values))
    assert response.status_code == 201, response.text
    return response.json()


def _two(client):
    project = _project(client)
    _rate(client)
    return project, _deliveries(client, project, [("10482", "24.50"), ("10483", "22.10")])


def _link(client, line, ids):
    return client.post(f"/api/invoice-comparisons/{line}/ticket-links", json={"ticket_ids": ids})


LIST = "10482\t24.50\n10483\t22.10"


def test_manager_confirms_clean_matches_and_summary_counts_them(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    assert line["evidence"]["candidates"] == 2 and line["evidence"]["confirmed"] == 0
    assert all(r["evidence"] == "candidate" for r in line["ticket_check"])
    # A clean arithmetic result isn't people's confirmation, and both are reported.
    assert line["status"] == "matches"

    response = _link(client, line["id"], ids[:1])
    assert response.status_code == 200, response.text
    partly = response.json()
    assert partly["evidence"]["confirmed"] == 1 and partly["evidence"]["candidates"] == 1
    assert Decimal(partly["evidence"]["confirmed_tons"]) == Decimal("24.500")
    [link] = partly["ticket_links"]
    assert link["state"] == "confirmed" and link["confirmed_by"] == "Test Owner" and link["listed_number"] == "10482"

    done = _link(client, line["id"], ids).json()
    assert done["evidence"]["confirmed"] == 2 and done["evidence"]["candidates"] == 0
    assert Decimal(done["evidence"]["confirmed_tons"]) == Decimal("46.600")
    history = client.get(f"/api/invoice-comparisons/{line['id']}/audit").json()
    assert [e["new"] for e in history if e["field"] == "ticket_evidence"] == ["linked ticket 10482", "linked ticket 10483"]


def test_confirmed_tons_are_exact(client):
    project = _project(client)
    _rate(client)
    ids = _deliveries(client, project, [("1", "0.100"), ("2", "0.200"), ("3", "0.300")])
    line = _post(client, project, ticket_list="1\n2\n3", billed_tons="0.600", billed_subtotal="18.00")
    result = _link(client, line["id"], ids).json()
    assert Decimal(result["evidence"]["confirmed_tons"]) == Decimal("0.600")  # 0.1+0.2+0.3 in floats isn't 0.6


def test_repeating_a_confirm_is_harmless(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    for _ in range(3):
        assert _link(client, line["id"], ids).status_code == 200
    with api_module.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceTicketLink)) == 2


def test_rows_that_are_not_clean_cannot_be_linked(client):
    project = _project(client)
    _rate(client)
    tons, wrong_date, same_a, same_b, ok = _deliveries(client, project, [
        ("10482", "24.50"), ("10484", "21.00", {"delivery_date": "2026-11-02"}),
        ("10485", "20.00"), ("10485", "19.00"), ("10486", "18.00")])
    line = _post(client, project, ticket_list="10482\t25.00\n10484\n10485\n10486\t18.00")
    rows = _rows(line)
    assert rows["10482"]["evidence"] == "conflicting"  # billed differs: partial billing is unsupported
    assert rows["10484"]["evidence"] == "unmatched"  # wrong date
    assert rows["10485"]["evidence"] == "conflicting"  # ambiguous
    assert rows["10486"]["evidence"] == "candidate"
    for bad in (tons, wrong_date, same_a, same_b):
        refused = _link(client, line["id"], [ok, bad])
        assert refused.status_code == 409, bad
        assert "Nothing was linked" in refused.json()["detail"]
    # All or nothing: the good ticket wasn't linked either.
    assert client.get(f"/api/invoice-comparisons/{line['id']}").json()["ticket_links"] == []
    # A ticket the line doesn't list can't be claimed.
    assert _link(client, _post(client, project, date_from="2026-12-01", date_to="2026-12-31",
                               ticket_list="99999")["id"], [ok]).status_code == 409


def test_same_number_from_another_supplier_is_not_linked(client):
    project = _project(client)
    _rate(client)
    ours, theirs = _deliveries(client, project, [
        ("10482", "24.50"), ("10482", "24.50", {"supplier": "Other Quarry"})])
    line = _post(client, project, ticket_list="10482\t24.50", billed_tons="24.500", billed_subtotal="735.00")
    [row] = line["ticket_check"]
    assert row["ticket"]["ticket_id"] == ours and row["evidence"] == "candidate"
    assert _link(client, line["id"], [theirs]).status_code == 409
    assert _link(client, line["id"], [ours]).status_code == 200


def _legacy_overlap(project_id, number):
    """Older data could overlap; the API can't create this today."""
    with api_module.SessionLocal() as db:
        item = InvoiceComparison(
            organization_id=1, project_id=project_id, invoice_number=number, supplier_name="Example Aggregates",
            supplier_key="example aggregates", material_name="No 53 Crushed Stone", material_key="no 53 crushed stone",
            date_from=date(2026, 10, 1), date_to=date(2026, 10, 31),
            billed_tons=Decimal("24.5"), billed_unit_price=Decimal("30"), billed_subtotal=Decimal("735"))
        db.add(item)
        db.commit()
        return item.id


def test_ticket_already_linked_elsewhere_is_conflicting(client):
    project, ids = _two(client)
    first = _post(client, project, ticket_list=LIST)
    assert _link(client, first["id"], ids).status_code == 200
    second_id = _legacy_overlap(project, "EA-OLD")
    edited = client.put(f"/api/invoice-comparisons/{second_id}", json=_line(
        project, invoice_number="EA-OLD", ticket_list="10482", date_from="2026-10-01", date_to="2026-10-31"))
    assert edited.status_code == 409  # the overlap guard still refuses this correction
    with api_module.SessionLocal() as db:
        db.add(InvoiceTicketRef(comparison_id=second_id, position=1, ticket_number="10482", ticket_key="10482"))
        db.commit()
    second = client.get(f"/api/invoice-comparisons/{second_id}").json()
    [row] = second["ticket_check"][:1]
    assert row["evidence"] == "conflicting" and any("Already linked to invoice" in n for n in row["notes"])
    assert second["status"] != "matches"
    assert any("already linked to another invoice line" in w for w in second["warnings"])
    assert _link(client, second_id, [ids[0]]).status_code == 409


def test_database_refuses_a_second_active_link_even_without_app_checks(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    _link(client, line["id"], ids)
    other = _legacy_overlap(project, "EA-OLD")
    with api_module.SessionLocal() as db:
        db.add(InvoiceTicketLink(comparison_id=other, ticket_id=ids[0], listed_number="10482", verified_tons=Decimal("24.5")))
        with pytest.raises(IntegrityError):
            db.commit()


def test_concurrent_claims_on_one_ticket_produce_one_link(tmp_path):
    """Real threads and connections on a file database, not the shared test session."""
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 15})
    db_module.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        db.add(Organization(id=1, name="Fictional"))
        db.add(Project(id=1, organization_id=1, code="R", name="Race"))
        db.add(Batch(id=1, organization_id=1, project_id=1, layout="single"))
        db.add(UploadedFile(id=1, batch_id=1, original_name="x.pdf", storage_path="x", sha256="0" * 64))
        db.add(Ticket(id=1, batch_id=1, file_id=1, source_page=1, position=0, crop_box=[0, 0, 1, 1], crop_path="x",
                      crop_sha256="0" * 64, ticket_number="10482", status="verified", tonnage=Decimal("24.5"),
                      tonnage_confirmed=True, flags=[]))
        for i in range(1, 9):
            db.add(InvoiceComparison(
                id=i, organization_id=1, project_id=1, invoice_number=f"I{i}", supplier_name="S", supplier_key="s",
                material_name="M", material_key="m", date_from=date(2026, 10, 1),
                date_to=date(2026, 10, 31), billed_tons=Decimal("1"),
                billed_unit_price=Decimal("1"), billed_subtotal=Decimal("1")))
        db.commit()

    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def attempt(comparison_id: int) -> None:
        with Session() as db:
            ticket = db.get(Ticket, 1)
            barrier.wait()
            try:
                ticket_evidence.claim(db, comparison_id, [(ticket, "10482")], None)
                db.commit()
                outcomes.append("won")
            except ticket_evidence.ClaimConflict:
                outcomes.append("refused")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["refused"] * 7 + ["won"]
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceTicketLink)) == 1


def test_changing_a_linked_delivery_invalidates_the_link(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    _link(client, line["id"], ids)
    assert client.get(f"/api/invoice-comparisons/{line['id']}").json()["status"] == "matches"

    # An edit withdraws approval, and the link no longer holds.
    assert client.patch(f"/api/tickets/{ids[0]}", json={"tonnage": "25.000"}).status_code == 200
    changed = client.get(f"/api/invoice-comparisons/{line['id']}").json()
    assert changed["status"] != "matches"
    assert any("no longer hold" in w for w in changed["warnings"])
    link = next(l for l in changed["ticket_links"] if l["ticket_id"] == ids[0])
    assert link["state"] == "invalidated" and "no longer approved" in " ".join(link["reasons"])
    assert Decimal(changed["evidence"]["confirmed_tons"]) == Decimal("22.100")
    # Link still holds its claim, so the ticket can't quietly go elsewhere.
    row = _rows(changed)["10482"]
    assert row["evidence"] == "invalidated" and row["link_id"] == link["id"]

    # Restored and re-approved: still needs a person to say so again.
    assert client.patch(f"/api/tickets/{ids[0]}", json={"tonnage": "24.500"}).status_code == 200
    assert client.post(f"/api/tickets/{ids[0]}/verify").status_code == 200
    again = client.get(f"/api/invoice-comparisons/{line['id']}").json()
    assert _rows(again)["10482"]["evidence"] == "invalidated"
    assert any("approved again" in r for l in again["ticket_links"] for r in l["reasons"])
    assert again["status"] != "matches"

    fixed = _link(client, line["id"], [ids[0]]).json()
    assert _rows(fixed)["10482"]["evidence"] == "confirmed" and fixed["status"] == "matches"
    with api_module.SessionLocal() as db:  # the old link stays as history
        rows = list(db.scalars(select(InvoiceTicketLink).where(InvoiceTicketLink.ticket_id == ids[0])))
        assert len(rows) == 2 and sum(1 for r in rows if r.released_at is None) == 1


def test_changed_number_supplier_or_flag_also_invalidate(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    _link(client, line["id"], ids)
    assert client.post(f"/api/tickets/{ids[1]}/flag").status_code == 200
    assert client.patch(f"/api/tickets/{ids[0]}", json={"ticket_number": "99999"}).status_code == 200
    result = client.get(f"/api/invoice-comparisons/{line['id']}").json()
    by_ticket = {l["ticket_id"]: l for l in result["ticket_links"]}
    assert "number changed" in " ".join(by_ticket[ids[0]]["reasons"])
    assert by_ticket[ids[1]]["state"] == "invalidated"


def test_correcting_the_line_releases_links_it_no_longer_lists(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    _link(client, line["id"], ids)
    corrected = client.put(f"/api/invoice-comparisons/{line['id']}", json=_line(project, ticket_list="10482\t24.50"))
    assert corrected.status_code == 200
    body = corrected.json()
    assert [l["ticket_id"] for l in body["ticket_links"]] == [ids[0]]
    history = client.get(f"/api/invoice-comparisons/{line['id']}/audit").json()
    assert any(e["field"] == "ticket_evidence" and "invoice line corrected" in (e["new"] or "") for e in history)
    # Going totals-only releases the rest, with no evidence left to report.
    totals = client.put(f"/api/invoice-comparisons/{line['id']}", json=_line(project)).json()
    assert totals["ticket_links"] == [] and totals["evidence"] is None and totals["comparison_mode"] == "totals_only"


def test_release_is_explicit_recorded_and_frees_the_ticket(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    linked = _link(client, line["id"], ids).json()
    link = linked["ticket_links"][0]
    url = f"/api/invoice-comparisons/{line['id']}/ticket-links/{link['id']}/release"
    released = client.post(url).json()
    assert released["evidence"]["confirmed"] == 1 and released["evidence"]["candidates"] == 1
    assert client.post(url).status_code == 200  # repeatable
    events = [e for e in client.get(f"/api/invoice-comparisons/{line['id']}/audit").json() if e["field"] == "ticket_evidence"]
    assert events[-1]["new"] == "released: released by a person" and events[-1]["actor"] == "Test Owner"
    assert _link(client, line["id"], [link["ticket_id"]]).status_code == 200
    assert client.post(f"/api/invoice-comparisons/{line['id']}/ticket-links/9999/release").status_code == 404


def test_links_never_change_tickets_or_prices(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    batch = client.get("/api/records").json()[0]["batch_id"]
    before = client.get(f"/api/batches/{batch}/tickets").json()
    audit = client.get(f"/api/tickets/{ids[0]}/audit").json()
    dashboard = client.get("/api/dashboard").json()
    linked = _link(client, line["id"], ids).json()
    client.post(f"/api/invoice-comparisons/{line['id']}/ticket-links/{linked['ticket_links'][0]['id']}/release")
    assert client.get(f"/api/batches/{batch}/tickets").json() == before
    assert client.get(f"/api/tickets/{ids[0]}/audit").json() == audit
    assert client.get("/api/dashboard").json() == dashboard


def test_rate_mismatch_is_still_reported_when_linked(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST, billed_unit_price="31", billed_subtotal="1444.60")
    assert line["rate_mismatch_count"] == 2
    linked = _link(client, line["id"], ids).json()
    assert linked["rate_mismatch_count"] == 2 and linked["status"] == "discrepancy"
    assert linked["evidence"]["confirmed"] == 2  # tickets are confirmed; the rate difference stays visible


def test_legacy_approval_without_a_time_can_link(client):
    project, ids = _two(client)
    with api_module.SessionLocal() as db:
        ticket = db.get(Ticket, ids[0])
        ticket.verified_at, ticket.verified_by_user_id = None, None
        db.commit()
    line = _post(client, project, ticket_list=LIST)
    result = _link(client, line["id"], ids[:1]).json()
    assert result["ticket_links"][0]["state"] == "confirmed"


def test_links_are_company_scoped(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    link = _link(client, line["id"], ids).json()["ticket_links"][0]
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        other_project = Project(organization_id=2, code="OTHER", name="Other job")
        db.add(other_project)
        db.flush()
        theirs = InvoiceComparison(
            organization_id=2, project_id=other_project.id, invoice_number="THEIRS", supplier_name="Example Aggregates",
            supplier_key="example aggregates", material_name="x", material_key="x",
            date_from=date(2026, 10, 1), date_to=date(2026, 10, 31),
            billed_tons=Decimal("1"), billed_unit_price=Decimal("1"), billed_subtotal=Decimal("1"))
        db.add(theirs)
        db.commit()
        theirs_id = theirs.id
    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=2, username="other", password_hash="fixture", active=True, role="owner",
        must_change_password=False)
    assert _link(client, line["id"], ids).status_code == 404
    assert client.post(f"/api/invoice-comparisons/{line['id']}/ticket-links/{link['id']}/release").status_code == 404
    # Their own line can't claim our ticket, or release our link through it.
    assert _link(client, theirs_id, [ids[0]]).status_code == 409
    assert client.post(f"/api/invoice-comparisons/{theirs_id}/ticket-links/{link['id']}/release").status_code == 404


@pytest.mark.parametrize("role", ["clerk", "viewer"])
def test_only_managers_and_owners_link(client, role):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    link = _link(client, line["id"], ids[:1]).json()["ticket_links"][0]
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="test-owner", password_hash="fixture", active=True, role=role,
        must_change_password=False)
    assert client.get(f"/api/invoice-comparisons/{line['id']}").status_code == 200
    assert _link(client, line["id"], ids).status_code == 403
    assert client.post(f"/api/invoice-comparisons/{line['id']}/ticket-links/{link['id']}/release").status_code == 403


def test_report_lists_confirmed_evidence(client):
    project, ids = _two(client)
    line = _post(client, project, ticket_list=LIST)
    _link(client, line["id"], ids)
    report = client.get(f"/api/invoice-comparisons/{line['id']}/report.csv").text
    assert "Ticket evidence confirmed by people" in report and "Test Owner" in report


def test_totals_only_line_has_no_evidence_and_the_overlap_guard_is_still_there(client):
    project, ids = _two(client)
    line = _post(client, project)
    assert line["evidence"] is None
    assert _link(client, line["id"], ids).status_code == 409
    assert client.post("/api/invoice-comparisons", json=_line(project, invoice_number="OTHER")).status_code == 409


def test_migration_is_additive(tmp_path):
    import sqlite3
    from alembic import command

    path = tmp_path / "before-links.db"
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "20261105_12")
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision()
        assert conn.execute("SELECT COUNT(*) FROM invoice_ticket_links").fetchone()[0] == 0
