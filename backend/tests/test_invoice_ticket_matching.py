"""Ticket-by-ticket invoice checking. Every supplier, ticket and invoice here
is fictional; nothing calls an AI model or reads a real document."""
import csv
from decimal import Decimal
from io import StringIO

import pytest

from app.auth import current_user
from app.main import app
from app.models import Organization, Project, User
from app.routers import api as api_module
from app.services.invoice_matching import TicketListError, parse_ticket_list, ticket_key
from .migration_helpers import alembic_config, head_revision
from .sample_data import make_scan_pdf

STONE = dict(delivery_date="2026-10-15", material_type="No. 53 Crushed Stone",
             supplier="Example Aggregates", job_location="North Lot")


def _project(client, code="24-115"):
    response = client.post("/api/projects", json={"code": code, "name": "Fictional Road Job"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _rate(client):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
    material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()
    assert client.put("/api/rates", json={
        "supplier_id": supplier["id"], "material_id": material["id"], "unit_price": "30",
    }).status_code == 200


def _deliveries(client, project_id, specs, verify=True):
    """Create delivery records with exact values through the manual-entry
    workflow, as an office clerk would. Returns ticket ids in order."""
    ids, full = [], []
    for number, tons, *extra in specs:
        full.append(dict(STONE, ticket_number=number, tonnage=tons, **(extra[0] if extra else {})))
    for start in range(0, len(full), 4):
        chunk = full[start:start + 4]
        response = client.post(
            "/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual",
                                  "project_id": str(project_id)},
            files=[("files", ("fictional.pdf", make_scan_pdf(chunk), "application/pdf"))],
        )
        assert response.status_code == 200, response.text
        tickets = client.get(f"/api/batches/{response.json()['id']}/tickets").json()
        assert len(tickets) == len(chunk)
        for ticket, values in zip(tickets, chunk):
            assert client.patch(f"/api/tickets/{ticket['id']}", json=values).status_code == 200
            if verify:
                verified = client.post(f"/api/tickets/{ticket['id']}/verify")
                assert verified.status_code == 200, verified.text
            ids.append(ticket["id"])
    return ids


def _line(project_id, **values):
    data = dict(project_id=project_id, invoice_number="EA-2026-1107",
                supplier_name="Example Aggregates", material_name="No. 53 Crushed Stone",
                date_from="2026-10-01", date_to="2026-10-31", billed_tons="46.600",
                billed_unit_price="30", billed_subtotal="1398.00", ticket_list="")
    data.update(values)
    return data


def _rows(result):
    return {(r["invoice_ticket_number"] or r["ticket"]["ticket_number"]): r
            for r in result["ticket_check"]}


# ---------------------------------------------------------------- the core risk

def test_matching_totals_with_different_ticket_numbers_is_a_discrepancy(client):
    project = _project(client)
    _rate(client)
    ids = _deliveries(client, project, [("10482", "24.50"), ("10483", "22.10")])

    totals = client.post("/api/invoice-comparisons", json=_line(project))
    assert totals.status_code == 201, totals.text
    body = totals.json()
    # Totals agree, and that is all a totals-only comparison can say.
    assert body["status"] == "matches"
    assert body["comparison_mode"] == "totals_only"
    assert body["ticket_check"] == []

    # The invoice actually lists 10482 and 10490: same 46.6 tons in total.
    corrected = client.put(f"/api/invoice-comparisons/{body['id']}", json=_line(
        project, ticket_list="10482\t24.50\n10490\t22.10\n"))
    assert corrected.status_code == 200, corrected.text
    result = corrected.json()
    assert result["comparison_mode"] == "itemized"
    assert result["status"] == "discrepancy"
    assert Decimal(result["quantity_difference"]) == 0  # totals still agree
    rows = _rows(result)
    assert rows["10482"]["result"] == "matched"
    assert rows["10482"]["ticket"]["ticket_id"] == ids[0]
    assert rows["10490"]["result"] == "not_found"
    assert rows["10490"]["ticket"] is None
    assert rows["10483"]["result"] == "not_on_invoice"
    assert rows["10483"]["line"] is None and rows["10483"]["ticket"]["ticket_id"] == ids[1]
    assert result["ticket_check_counts"]["not_found"] == 1
    assert result["ticket_check_counts"]["not_on_invoice"] == 1
    assert any("no matching delivery record" in w for w in result["warnings"])
    assert any("not listed on the invoice" in w for w in result["warnings"])

    history = client.get(f"/api/invoice-comparisons/{body['id']}/audit").json()
    [event] = [e for e in history if e["field"] == "ticket_list"]
    assert event["old"] == "(totals only)"
    assert event["new"] == "10482 24.500 t; 10490 22.100 t"
    assert event["actor"] == "Test Owner"


def test_clean_itemized_match_links_each_ticket_to_its_record(client):
    project = _project(client)
    _rate(client)
    ids = _deliveries(client, project, [("10482", "24.50"), ("10483", "22.10")])
    result = client.post("/api/invoice-comparisons", json=_line(
        project, ticket_list="Ticket No.\tNet Tons\r\n10482\t24.5\t\r\n\r\n10483\t22.100\r\n")).json()
    assert result["status"] == "matches", result["warnings"]
    assert result["warnings"] == []
    assert result["comparison_mode"] == "itemized"
    assert Decimal(result["itemized_billed_tons"]) == Decimal("46.6")
    lines = [r for r in result["ticket_check"] if r["line"] is not None]
    assert [r["line"] for r in lines] == [1, 2]  # header row skipped
    assert {r["ticket"]["ticket_id"] for r in lines} == set(ids)
    for row in lines:
        assert row["result"] == "matched" and Decimal(row["tons_difference"]) == 0
        assert row["ticket"]["batch_id"] and row["ticket"]["review_status"] == "verified"

    # Saving the same list again records no history.
    again = client.put(f"/api/invoice-comparisons/{result['id']}", json=_line(
        project, ticket_list="10482\t24.500\n10483\t22.1"))
    assert again.status_code == 200
    assert client.get(f"/api/invoice-comparisons/{result['id']}/audit").json() == []


def test_duplicate_invoice_references_are_flagged(client):
    project = _project(client)
    _rate(client)
    _deliveries(client, project, [("10482", "24.50"), ("10483", "22.10")])
    result = client.post("/api/invoice-comparisons", json=_line(
        project, billed_tons="49.000", billed_subtotal="1470.00",
        ticket_list="10482 24.50\n10482 24.50")).json()
    assert result["status"] == "discrepancy"
    repeated = [r for r in result["ticket_check"] if r["line"] is not None]
    assert all("duplicate_on_invoice" in r["flags"] for r in repeated)
    assert result["ticket_check_counts"]["duplicate_on_invoice"] == 2
    assert any("repeat a ticket number" in w for w in result["warnings"])


def test_leading_zeros_are_significant_and_preserved(client):
    project = _project(client)
    _rate(client)
    [zero_id] = _deliveries(client, project, [("010482", "24.50")])
    near = client.post("/api/invoice-comparisons", json=_line(
        project, billed_tons="24.500", billed_subtotal="735.00", ticket_list="10482\t24.50")).json()
    rows = _rows(near)
    assert rows["10482"]["result"] == "not_found"
    assert [c["ticket_id"] for c in rows["10482"]["candidates"]] == [zero_id]
    assert any("leading zeros" in n for n in rows["10482"]["notes"])
    # The near miss is NOT counted: the delivery is still missing from the invoice.
    assert rows["010482"]["result"] == "not_on_invoice"
    assert near["status"] == "discrepancy"

    exact = client.put(f"/api/invoice-comparisons/{near['id']}", json=_line(
        project, billed_tons="24.500", billed_subtotal="735.00", ticket_list="010482\t24.50")).json()
    assert exact["status"] == "matches"
    assert exact["ticket_check"][0]["invoice_ticket_number"] == "010482"
    assert exact["ticket_check"][0]["ticket"]["ticket_number"] == "010482"


def test_ambiguous_ticket_numbers_are_never_matched(client):
    project = _project(client)
    _rate(client)
    ids = _deliveries(client, project, [("10482", "24.50"), ("10482", "22.10")])
    result = client.post("/api/invoice-comparisons", json=_line(
        project, ticket_list="10482\t24.50\n")).json()
    [row] = result["ticket_check"]  # neither record is reported twice as "missing"
    assert row["result"] == "ambiguous"
    assert row["ticket"] is None and row["verified_tons"] is None
    assert sorted(c["ticket_id"] for c in row["candidates"]) == sorted(ids)
    assert result["status"] == "incomplete"
    assert any("more than one delivery record" in w for w in result["warnings"])


def test_unverified_or_incomplete_deliveries_block_reconciliation(client):
    project = _project(client)
    _rate(client)
    _deliveries(client, project, [("10482", "24.50")])
    [pending] = _deliveries(client, project, [("10483", "22.10")], verify=False)
    result = client.post("/api/invoice-comparisons", json=_line(
        project, ticket_list="10482\t24.50\n10483\t22.10")).json()
    rows = _rows(result)
    assert rows["10483"]["result"] == "needs_review"
    assert rows["10483"]["verified_tons"] is None
    assert any("not verified" in n for n in rows["10483"]["notes"])
    assert result["status"] == "incomplete"

    # A record missing its delivery date is reported as incomplete, never guessed.
    assert client.patch(f"/api/tickets/{pending}", json={"delivery_date": None}).status_code == 200
    rows = _rows(client.get(f"/api/invoice-comparisons/{result['id']}").json())
    assert rows["10483"]["result"] == "needs_review"
    assert any("delivery date missing" in n for n in rows["10483"]["notes"])


def test_billed_tons_that_differ_per_ticket_are_reported(client):
    project = _project(client)
    _rate(client)
    _deliveries(client, project, [("10482", "24.50"), ("10483", "22.10")])
    result = client.post("/api/invoice-comparisons", json=_line(
        project, ticket_list="10482\t25.00\n10483\t22.10\n")).json()
    rows = _rows(result)
    assert rows["10482"]["result"] == "tons_differ"
    assert Decimal(rows["10482"]["tons_difference"]) == Decimal("0.500")
    assert Decimal(rows["10482"]["verified_tons"]) == Decimal("24.500")  # never altered
    assert result["status"] == "discrepancy"
    # The itemized lines (47.1) no longer add up to the invoice line (46.6).
    assert any("add up to 47.100" in w for w in result["warnings"])


def test_ticket_from_another_material_date_or_project_is_outside_scope(client):
    project = _project(client)
    other_project = _project(client, code="24-200")
    _rate(client)
    _deliveries(client, project, [("10482", "24.50")])
    _deliveries(client, project, [("10483", "22.10", {"material_type": "No. 8 Stone"})])
    _deliveries(client, project, [("10484", "21.00", {"delivery_date": "2026-11-02"})])
    _deliveries(client, other_project, [("10485", "20.00")])
    result = client.post("/api/invoice-comparisons", json=_line(
        project, ticket_list="10482\n10483\n10484\n10485")).json()
    rows = _rows(result)
    assert rows["10482"]["result"] == "matched"
    for number, reason in (("10483", "material No. 8 Stone"), ("10484", "delivered 2026-11-02"),
                           ("10485", "project 24-200")):
        assert rows[number]["result"] == "outside_scope", number
        assert rows[number]["ticket"] is None
        assert any(reason in n for n in rows[number]["notes"]), rows[number]["notes"]
    assert result["status"] == "discrepancy"


def test_same_ticket_on_two_saved_invoices_is_flagged(client):
    project = _project(client)
    _rate(client)
    _deliveries(client, project, [("10482", "24.50")])
    october = client.post("/api/invoice-comparisons", json=_line(
        project, billed_tons="24.500", billed_subtotal="735.00", ticket_list="10482")).json()
    assert october["status"] == "matches"
    november = client.post("/api/invoice-comparisons", json=_line(
        project, invoice_number="EA-2026-1188", date_from="2026-11-01", date_to="2026-11-30",
        billed_tons="24.500", billed_subtotal="735.00", ticket_list="10482")).json()
    [row] = november["ticket_check"]
    assert row["result"] == "outside_scope"
    assert "on_another_invoice" in row["flags"]
    assert any("EA-2026-1107" in n for n in row["notes"])
    refreshed = client.get(f"/api/invoice-comparisons/{october['id']}").json()
    assert refreshed["status"] == "discrepancy"
    assert refreshed["ticket_check"][0]["flags"] == ["on_another_invoice"]


def test_identical_ticket_numbers_in_another_company_are_invisible(client):
    project = _project(client)
    _rate(client)
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        db.add(User(id=2, organization_id=2, username="other-owner", password_hash="fixture",
                    active=True, role="owner"))
        db.commit()
    other_owner = User(id=2, organization_id=2, username="other-owner", password_hash="fixture",
                       active=True, role="owner", must_change_password=False)
    original = app.dependency_overrides[current_user]
    app.dependency_overrides[current_user] = lambda: other_owner
    other_project = _project(client, code="OT-1")
    _rate(client)
    _deliveries(client, other_project, [("10482", "24.50")])
    theirs = client.post("/api/invoice-comparisons", json=_line(
        other_project, billed_tons="24.500", billed_subtotal="735.00", ticket_list="10482"))
    assert theirs.status_code == 201 and theirs.json()["status"] == "matches"

    app.dependency_overrides[current_user] = original
    ours = client.post("/api/invoice-comparisons", json=_line(
        project, billed_tons="24.500", billed_subtotal="735.00", ticket_list="10482")).json()
    [row] = ours["ticket_check"]
    assert row["result"] == "not_found" and row["candidates"] == []
    assert row["flags"] == []  # the other company's invoice is not "another invoice"
    assert ours["status"] == "no_verified_tickets"


# ---------------------------------------------------------------- input safety

@pytest.mark.parametrize("text, message", [
    ("10482\t24.50\nabc\t22.1", "line 2: 'abc' does not look like a ticket number"),
    ("10482\t24.50\t2026-10-15", "copy only two columns"),
    ("1.0482E+04\t24.5", "scientific notation"),
    ("10482\t24.5 tons", "is not a number of tons"),
    ("10482\t-3", "must be a positive number"),
    ("10482\t24.5005", "more than 3 decimal places"),
    ("1" * 101, "longer than 100 characters"),
])
def test_unreadable_ticket_lists_are_rejected_by_line_and_save_nothing(client, text, message):
    project = _project(client)
    response = client.post("/api/invoice-comparisons", json=_line(project, ticket_list=text))
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert "Nothing was saved" in response.json()["detail"]
    assert client.get("/api/invoice-comparisons").json() == []


def test_failed_correction_keeps_the_previous_list(client):
    project = _project(client)
    saved = client.post("/api/invoice-comparisons", json=_line(project, ticket_list="10482\n10483")).json()
    bad = client.put(f"/api/invoice-comparisons/{saved['id']}", json=_line(project, ticket_list="10482\nxyz"))
    assert bad.status_code == 422
    kept = client.get(f"/api/invoice-comparisons/{saved['id']}").json()
    assert [r["invoice_ticket_number"] for r in kept["ticket_check"]] == ["10482", "10483"]
    assert client.get(f"/api/invoice-comparisons/{saved['id']}/audit").json() == []


def test_parser_formats_and_keys():
    refs = parse_ticket_list("Ticket\n 0042 , 24.5\nA-77;19\nT 9001\t20.125\n\n")
    assert [(r.position, r.ticket_number, r.billed_tons) for r in refs] == [
        (1, "0042", Decimal("24.500")), (2, "A-77", Decimal("19.000")),
        (3, "T 9001", Decimal("20.125")),
    ]
    assert parse_ticket_list("") == [] and parse_ticket_list("   \n") == []
    assert ticket_key(" a-77 ") == "A-77" and ticket_key("0042") != ticket_key("42")
    with pytest.raises(TicketListError, match="at most 2000"):
        parse_ticket_list("\n".join(str(10000 + i) for i in range(2001)))


def test_clerks_and_viewers_cannot_enter_ticket_lists(client):
    project = _project(client)
    for role in ("clerk", "viewer"):
        app.dependency_overrides[current_user] = lambda role=role: User(
            id=1, organization_id=1, username="test-owner", password_hash="fixture",
            active=True, role=role, must_change_password=False)
        response = client.post("/api/invoice-comparisons", json=_line(project, ticket_list="10482"))
        assert response.status_code == 403, role
    assert client.get("/api/invoice-comparisons").json() == []


def test_report_csv_shows_mode_and_every_ticket_check(client):
    project = _project(client)
    _rate(client)
    _deliveries(client, project, [("010482", "24.50"), ("10483", "22.10")])
    totals = client.post("/api/invoice-comparisons", json=_line(project)).json()
    rows = list(csv.reader(StringIO(
        client.get(f"/api/invoice-comparisons/{totals['id']}/report.csv").text)))
    assert ["Comparison type", "Totals-only comparison - individual tickets not checked"] in rows
    assert ["Ticket-by-ticket check"] not in rows

    itemized = client.put(f"/api/invoice-comparisons/{totals['id']}", json=_line(
        project, ticket_list="010482\t24.50\n=99\t22.10")).json()
    rows = list(csv.reader(StringIO(
        client.get(f"/api/invoice-comparisons/{itemized['id']}/report.csv").text)))
    assert ["Comparison type", "Itemized - each invoice ticket number checked"] in rows
    assert ["Ticket-by-ticket check"] in rows
    by_number = {r[1]: r for r in rows if len(r) == 13}
    assert by_number["010482"][3] == "Matched"
    assert by_number["010482"][7] == "010482"
    assert by_number["'=99"][3] == "Not found in TicketFlow"  # formula neutralized
    missing = [r for r in rows if r and r[0] == "NOT ON INVOICE"]
    assert len(missing) == 1 and missing[0][7] == "10483"


def test_migration_adds_table_and_keeps_existing_comparisons(tmp_path):
    import sqlite3
    from alembic import command

    dbpath = tmp_path / "before-ticket-refs.db"
    cfg = alembic_config(f"sqlite:///{dbpath}")
    command.upgrade(cfg, "20261103_08")
    with sqlite3.connect(dbpath) as conn:
        conn.execute("INSERT INTO projects(id,organization_id,code,name,status,created_at) "
                     "VALUES(1,1,'OLD','Existing project','active',CURRENT_TIMESTAMP)")
        conn.execute(
            "INSERT INTO invoice_comparisons(id,organization_id,project_id,invoice_number,"
            "supplier_name,supplier_key,material_name,material_key,date_from,date_to,"
            "billed_tons,billed_unit_price,billed_subtotal,created_at) VALUES(7,1,1,'OLD-1',"
            "'Example Aggregates','example aggregates','Sand','sand','2026-10-01','2026-10-31',"
            "'10.000','20.0000','200.00',CURRENT_TIMESTAMP)")
        conn.commit()
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # idempotent
    with sqlite3.connect(dbpath) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision()
        assert conn.execute("SELECT invoice_number FROM invoice_comparisons WHERE id=7").fetchone() == ("OLD-1",)
        assert conn.execute("SELECT COUNT(*) FROM invoice_ticket_refs").fetchone()[0] == 0
