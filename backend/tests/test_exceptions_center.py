"""The invoice exceptions center: what it finds, that money is counted once, and how
people own, dispute and settle each exception without any figure changing.

Scenario (fictional), project P1, supplier Example Aggregates:
- Ticket 10482, No. 53 Crushed Stone, 24.50 t on 2026-10-15, approved at the P1 agreement $16.75.
- Ticket 10483, No. 8 Stone, 22.10 t, approved at $20.00 and never invoiced.
- INV-1 (October, No. 53): bills 10482 at list $18.00, plus 10499 (5 t) that has no record.
- INV-2 (November, No. 53): lists 10482 again.
"""
from decimal import Decimal

import pytest

from .sample_data import make_scan_pdf


@pytest.fixture()
def scenario(client):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    stone53 = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()["id"]
    stone8 = client.post("/api/materials", json={"name": "No. 8 Stone"}).json()["id"]
    p1 = client.post("/api/projects", json={"code": "P1", "name": "Fictional P1"}).json()["id"]
    for material, price, project in ((stone53, "18.00", None), (stone53, "16.75", p1), (stone8, "20.00", None)):
        assert client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": price,
                                               "project_id": project}).status_code == 200
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(p1)},
                        files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))]).json()
    tickets = client.get(f"/api/batches/{batch['id']}/tickets").json()
    for ticket in tickets[:2]:
        assert client.post(f"/api/tickets/{ticket['id']}/verify", headers={"If-Match": ticket["revision"]}).status_code == 200
    for ticket in tickets[2:]:
        client.post(f"/api/tickets/{ticket['id']}/skip")  # another supplier's sand: not part of this story

    def line(number, start, end, tons, subtotal, tickets_listed):
        response = client.post("/api/invoice-comparisons", json={
            "project_id": p1, "invoice_number": number, "supplier_name": "Example Aggregates",
            "material_name": "No. 53 Crushed Stone", "date_from": start, "date_to": end, "billed_tons": tons,
            "billed_unit_price": "18.00", "billed_subtotal": subtotal, "ticket_list": tickets_listed})
        assert response.status_code == 201, response.text
        return response.json()

    inv1 = line("INV-1", "2026-10-01", "2026-10-31", "29.500", "531.00", "10482\t24.50\n10499\t5.00")
    inv2 = line("INV-2", "2026-11-01", "2026-11-30", "24.500", "441.00", "10482\t24.50")
    return {"p1": p1, "inv1": inv1, "inv2": inv2, "tickets": tickets}


def _list(client, **params):
    response = client.get("/api/exceptions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _by_category(data):
    found = {}
    for item in data["items"]:
        found.setdefault(item["category"], []).append(item)
    return found


def _act(client, item, revision=None, **changes):
    return client.post("/api/exceptions/review", json={"key": item["key"], **changes},
                       headers={"If-Match": revision or item["revision"]})


def test_every_kind_of_problem_in_the_scenario_is_found_once(client, scenario):
    data = _list(client)
    found = _by_category(data)
    assert {k: len(v) for k, v in found.items()} == {"duplicate": 1, "missing_ticket": 2, "rate": 1, "not_invoiced": 1}
    duplicate = found["duplicate"][0]
    assert duplicate["severity"] == "high" and duplicate["invoice_numbers"] == ["INV-1", "INV-2"]
    assert duplicate["amount"] is None  # its tons are already inside INV-1's causes
    missing = next(i for i in found["missing_ticket"] if i["ticket_number"] == "10499")
    assert missing["amount"] == "90.00" and missing["line_billed"] == "531.00" and missing["line_approved"] == "410.38"
    rate = found["rate"][0]
    assert rate["amount"] == "30.63" and "16.7500" in rate["explanation"] and rate["project"] == "P1"
    unbilled = found["not_invoiced"][0]
    assert unbilled["amount"] is None and unbilled["unbilled_tickets"] == 1 and unbilled["approved_tons"] == "22.100"


def test_potential_overcharge_never_exceeds_what_a_line_billed_above_approved(client, scenario):
    summary = _list(client)["summary"]
    # INV-1 billed $531.00 against $410.38 approved: $120.62. Its causes are $90.00 and $30.63,
    # a cent more, because per-ticket costs round separately. The total is the line's real figure.
    assert Decimal(scenario["inv1"]["subtotal_difference"]) == Decimal("120.62")
    assert summary["potential_overcharge"] == "120.62"
    assert summary["open"] == 5 and summary["by_severity"] == {"high": 1, "medium": 3, "low": 1}
    assert summary["recovered"] == "0"  # nothing is recovered until a person records it


def test_owning_disputing_and_recording_a_credit_is_audited_and_changes_no_figure(client, scenario):
    rate = _by_category(_list(client))["rate"][0]
    owner_id = 1  # the fixture's signed-in owner
    taken = _act(client, rate, assignee_user_id=owner_id, status="investigating", note="Asked the yard for the quote.")
    assert taken.status_code == 200, taken.text
    taken = taken.json()
    assert taken["status"] == "investigating" and taken["assignee"] == "Test Owner" and taken["revision"] == "1"

    stale = _act(client, rate, revision="new", status="disputed")
    assert stale.status_code == 409  # someone acted after this page loaded
    assert _act(client, taken, status="recovered").status_code == 422  # a credit needs its amount
    assert _act(client, taken, status="recovered", recovered_amount="30.63").status_code == 422  # and a reason
    settled = _act(client, taken, status="recovered", recovered_amount="30.63", note="Credit memo CM-881 received.")
    assert settled.status_code == 200, settled.text

    after = _list(client, status="all")
    assert after["summary"]["recovered"] == "30.63"
    assert after["summary"]["potential_overcharge"] == "90.00"  # only open causes count
    history = client.get("/api/exceptions/history", params={"key": rate["key"]}).json()
    assert [(e["field"], e["new"]) for e in history] == [
        ("status", "investigating"), ("assignee", "Test Owner"), ("note", "Asked the yard for the quote."),
        ("status", "recovered"), ("recovered_amount", "30.63"), ("note", "Credit memo CM-881 received.")]
    line = client.get(f"/api/invoice-comparisons/{scenario['inv1']['id']}").json()
    assert line["subtotal_difference"] == scenario["inv1"]["subtotal_difference"]  # the invoice itself is untouched


def test_a_review_needs_a_revision_and_the_right_role(client, scenario, monkeypatch):
    rate = _by_category(_list(client))["rate"][0]
    no_revision = client.post("/api/exceptions/review", json={"key": rate["key"], "status": "investigating"})
    assert no_revision.status_code == 428
    assert _act(client, {"key": "L999:rate:0", "revision": "new"}, status="investigating").status_code == 404
    from app.auth import current_user
    from app.main import app
    from app.models import User
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="test-owner", display_name="Test Owner", password_hash="fixture",
        active=True, role="viewer", must_change_password=False)
    assert _act(client, rate, status="investigating").status_code == 403
    assert _list(client)["can_manage"] is False  # viewers still see the list


def test_a_fixed_invoice_clears_the_exception_and_keeps_its_history(client, scenario):
    missing = next(i for i in _by_category(_list(client))["missing_ticket"] if i["ticket_number"] == "10499")
    assert _act(client, missing, status="disputed", note="Supplier asked for the paper ticket.").status_code == 200
    line = scenario["inv1"]
    corrected = client.put(f"/api/invoice-comparisons/{line['id']}", headers={"If-Match": line["revision"]}, json={
        "project_id": scenario["p1"], "invoice_number": "INV-1", "supplier_name": "Example Aggregates",
        "material_name": "No. 53 Crushed Stone", "date_from": "2026-10-01", "date_to": "2026-10-31",
        "billed_tons": "24.500", "billed_unit_price": "18.00", "billed_subtotal": "441.00", "ticket_list": "10482\t24.50"})
    assert corrected.status_code == 200, corrected.text
    data = _list(client)
    assert all(i["key"] != missing["key"] for i in data["items"])
    cleared = next(c for c in data["cleared"] if c["key"] == missing["key"])
    assert cleared["status"] == "disputed" and "10499" in cleared["title"]
    assert len(client.get("/api/exceptions/history", params={"key": missing["key"]}).json()) == 2


def test_filters_and_the_csv(client, scenario):
    assert {i["category"] for i in _list(client, category="rate")["items"]} == {"rate"}
    assert {i["severity"] for i in _list(client, severity="high")["items"]} == {"high"}
    assert _list(client, assignee="me")["items"] == []
    assert _list(client, project_id=scenario["p1"] + 100)["items"] == []
    assert client.get("/api/exceptions", params={"category": "nonsense"}).status_code == 422
    csv_text = client.get("/api/exceptions.csv").text
    assert csv_text.splitlines()[0].startswith("Severity,Category,Status")
    assert "10499" in csv_text and "90.00" in csv_text and len(csv_text.splitlines()) == 6


def test_the_history_is_append_only_in_the_database(client, scenario):
    import sqlalchemy
    from app.models import ExceptionEvent
    from app.routers import api as api_module

    rate = _by_category(_list(client))["rate"][0]
    assert _act(client, rate, status="investigating").status_code == 200
    with api_module.SessionLocal() as db:
        event = db.query(ExceptionEvent).first()
        event.new_value = "dismissed"
        with pytest.raises(sqlalchemy.exc.DatabaseError):
            db.commit()


def test_the_one_pass_unbilled_gaps_match_the_per_project_report(client, scenario):
    from app.models import Project
    from app.routers import api as api_module
    from app.services import reconciliation

    client.post("/api/projects", json={"code": "P2", "name": "Fictional P2"})  # a project with nothing unbilled
    with api_module.SessionLocal() as db:
        together = reconciliation.unbilled_gaps_by_project(db, 1)
        one_by_one = {p.id: reconciliation.unbilled(db, 1, p)["gaps"] for p in db.query(Project).filter(Project.organization_id == 1)}
    assert together == {k: v for k, v in one_by_one.items() if v} and together  # the scenario has one unbilled run


def _approve_another_no8(client, scenario, *, number, delivered):
    from .sample_data import TICKETS
    papers = [{**TICKETS[0], "ticket_number": number + "A"}, {**TICKETS[1], "ticket_number": number}, *TICKETS[2:]]
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(scenario["p1"])},
                        files=[("files", ("more.pdf", make_scan_pdf(papers), "application/pdf"))]).json()
    tickets = client.get(f"/api/batches/{batch['id']}/tickets").json()
    no8 = client.patch(f"/api/tickets/{tickets[1]['id']}", json={"ticket_number": number, "delivery_date": delivered}).json()
    assert client.post(f"/api/tickets/{no8['id']}/verify", headers={"If-Match": no8["revision"]}).status_code == 200
    for other in (tickets[0], *tickets[2:]):
        client.post(f"/api/tickets/{other['id']}/skip")


def test_an_unbilled_exception_keeps_its_identity_when_an_earlier_delivery_joins(client, scenario):
    unbilled = _by_category(_list(client))["not_invoiced"][0]
    assert unbilled["key"] == f"U:{scenario['p1']}:example aggregates:no 8 stone"
    resolved = _act(client, unbilled, status="resolved", note="Bill expected with the November statement.")
    assert resolved.status_code == 200, resolved.text

    _approve_another_no8(client, scenario, number="20483", delivered="2026-10-02")  # earlier than the first
    after = _list(client)
    same = next(i for i in after["items"] if i["key"] == unbilled["key"])
    assert all(c["key"] != unbilled["key"] for c in after["cleared"])  # not orphaned under a new key
    assert same["unbilled_tickets"] == 2 and "2026-10-02" in same["title"]
    # Closed by a person, then its figures changed: shown again, their decision left as it was.
    assert same["status"] == "resolved" and same["changed_since_closed"] is True and same["needs_attention"] is True
    assert after["summary"]["changed_since_closed"] == 1
    history = client.get("/api/exceptions/history", params={"key": unbilled["key"]}).json()
    assert [(e["field"], e["new"]) for e in history] == [("status", "resolved"), ("note", "Bill expected with the November statement.")]


def test_a_closed_exception_whose_figures_change_needs_a_person_again_until_they_look(client, scenario):
    rate = _by_category(_list(client))["rate"][0]
    closed = _act(client, rate, status="dismissed", note="Agreed by phone.").json()
    assert all(i["key"] != rate["key"] for i in _list(client)["items"])  # closed and unchanged: off the open list
    line = scenario["inv1"]
    corrected = client.put(f"/api/invoice-comparisons/{line['id']}", headers={"If-Match": line["revision"]}, json={
        "project_id": scenario["p1"], "invoice_number": "INV-1", "supplier_name": "Example Aggregates",
        "material_name": "No. 53 Crushed Stone", "date_from": "2026-10-01", "date_to": "2026-10-31", "billed_tons": "29.500",
        "billed_unit_price": "19.00", "billed_subtotal": "560.50", "ticket_list": "10482\\t24.50\\n10499\\t5.00"})
    assert corrected.status_code == 200, corrected.text
    back = next(i for i in _list(client)["items"] if i["key"] == rate["key"])
    assert back["status"] == "dismissed" and back["changed_since_closed"] is True and back["amount"] == "55.13"
    # The person looks again and decides: it's closed on the new figures.
    again = _act(client, back, revision=closed["revision"], note="Rate rise confirmed in writing.")
    assert again.status_code == 200, again.text
    assert all(i["key"] != rate["key"] for i in _list(client)["items"])
