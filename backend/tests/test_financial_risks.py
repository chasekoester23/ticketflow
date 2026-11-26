"""Financial risks reviewed in docs/engineering/financial-risk-review.md: tickets billed
outside an invoice's dates, partial billing of one delivery, a truck shift split across
projects and billed per project, and what is (and isn't) a duplicate charge.
Fictional data."""
from decimal import Decimal

from app.services import reconciliation
from app.routers.invoices import _line_out
from app.models import InvoiceComparison

from .sample_data import TICKETS, make_scan_pdf
from .test_query_budgets import _session
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)
from .test_trucking_billing import _haul_rate, _material


def _delivery(client, yard, *, number, tons="25.00", day="2026-11-03", hauler=True, project="project"):
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(yard[project])},
                        files=[("files", ("d.pdf", make_scan_pdf([{**TICKETS[0], "ticket_number": number}]), "application/pdf"))]).json()
    ticket = client.get(f"/api/batches/{batch['id']}/tickets").json()[0]
    body = {"ticket_number": number, "delivery_date": day, "supplier": "Example Aggregates", "material_type": "No. 57 Stone",
            "tonnage": tons, **({"hauler_id": yard["hauler"], "truck_number": "T-12"} if hauler else {})}
    edited = client.patch(f"/api/tickets/{ticket['id']}", json=body).json()
    verified = client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]})
    assert verified.status_code == 200, verified.text
    return verified.json()


def _supplier_line(client, yard, *, number="INV-1", date_from="2026-11-01", date_to="2026-11-30", tons, subtotal, tickets=""):
    response = client.post("/api/invoice-comparisons", json={
        "project_id": yard["project"], "invoice_number": number, "supplier_name": "Example Aggregates",
        "material_name": "No. 57 Stone", "date_from": date_from, "date_to": date_to, "billed_tons": tons,
        "billed_unit_price": "20.0000", "billed_subtotal": subtotal, "ticket_list": tickets})
    assert response.status_code == 201, response.text
    return response.json()


def _hauler_invoice(client, yard, lines, *, number="HV-1", project="project", period=("2026-11-01", "2026-11-30")):
    response = client.post("/api/hauling-invoices", json={
        "hauler_id": yard["hauler"], "project_id": yard[project] if project else None, "invoice_number": number,
        "period_from": period[0], "period_to": period[1], "lines": lines})
    assert response.status_code == 201, response.text
    return response.json()


def _exceptions(client, number=None):
    items = client.get("/api/exceptions").json()["items"]
    return [i for i in items if number is None or number in i["invoice_numbers"]]


def _differences(line_id):
    with _session() as db:
        item = db.get(InvoiceComparison, line_id)
        return reconciliation.differences(_line_out(db, item, item.organization_id)), _line_out(db, item, item.organization_id)


# ---------------------------------------------------------------- 1. tickets outside the billed dates

def test_a_supplier_line_listing_a_ticket_delivered_after_its_dates_reports_it_and_adds_up_exactly(client, yard):
    _material(client)
    _delivery(client, yard, number="70001", day="2026-11-02", hauler=False)
    _delivery(client, yard, number="70002", day="2026-11-03", hauler=False)
    line = _supplier_line(client, yard, date_to="2026-11-02", tons="45.000", subtotal="900.00", tickets="70001\t25\n70002\t20")
    rows = {r["invoice_ticket_number"]: r for r in line["ticket_check"] if r["line"] is not None}
    assert rows["70001"]["result"] == "matched"
    assert rows["70002"]["result"] == "outside_scope" and any("delivered 2026-11-03" in n for n in rows["70002"]["notes"])
    # Only the delivery inside the dates is approved against this line: 25 t, $500.
    assert (line["status"], line["verified_tons"], line["verified_subtotal"]) == ("discrepancy", "25.000", "500.00")
    assert (line["quantity_difference"], line["subtotal_difference"]) == ("20.000", "400.00")
    found, out = _differences(line["id"])
    assert sum(e["dollar_effect"] for e in found) == out.subtotal_difference == Decimal("400.00")
    [outside] = [e for e in found if e["kind"] == "outside_scope"]
    assert (outside["ticket_number"], outside["dollar_effect"]) == ("70002", Decimal("400.00"))
    money = [(i["category"], i["amount"]) for i in _exceptions(client, "INV-1")]
    assert money == [("missing_ticket", "400.00")]


def test_a_hauler_line_listing_a_ticket_delivered_outside_the_invoice_period_is_questioned_not_hidden(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _delivery(client, yard, number="70001", day="2026-11-03")
    invoice = _hauler_invoice(client, yard, [{"kind": "tons", "tickets": "70001", "quantity": "25.000", "rate": "4.0000",
                                              "amount": "100.00"}], period=("2026-11-10", "2026-11-30"))
    [line] = invoice["lines"]
    # The hauling is owed once whichever invoice bills it, so the money is still supported...
    assert (line["supported"], line["difference"]) == ("100.00", "0.00")
    # ...but a delivery from before the period is a question for a person, not a silent match.
    assert line["result"] == "differs" and invoice["exceptions"] == 1
    [item] = _exceptions(client, "HV-1")
    assert item["category"] == "needs_review" and item["amount"] is None
    assert "2026-11-03" in item["explanation"] and "outside" in item["title"]
    # Inside the period: a clean match, as before.
    inside = _hauler_invoice(client, yard, [{"kind": "tons", "tickets": "70001", "quantity": "25.000", "rate": "4.0000",
                                             "amount": "100.00"}], number="HV-2", period=("2026-11-01", "2026-11-30"))
    assert inside["lines"][0]["result"] == "repeat"  # HV-1 billed it first: never supported twice


# ---------------------------------------------------------------- 2. partial billing of one delivery

def test_billing_part_of_a_delivery_shows_the_rest_as_billed_below_approved_and_a_second_bill_is_questioned(client, yard):
    _material(client)
    _delivery(client, yard, number="70001", day="2026-11-03", hauler=False)  # 25 t at $20 = $500
    first = _supplier_line(client, yard, date_to="2026-11-15", tons="10.000", subtotal="200.00", tickets="70001\t10")
    [row] = [r for r in first["ticket_check"] if r["line"] is not None]
    assert (row["result"], row["tons_difference"]) == ("tons_differ", "-15.000")
    assert (first["status"], first["subtotal_difference"]) == ("discrepancy", "-300.00")
    found, _out = _differences(first["id"])
    assert sum(e["dollar_effect"] for e in found) == Decimal("-300.00")
    # A listed ticket counts as billed: it isn't also reported as unbilled (that would count its tons twice).
    unbilled = client.get(f"/api/reports/project/{yard['project']}/unbilled").json()
    assert unbilled["total_tickets"] == 0
    summary = client.get("/api/exceptions").json()["summary"]
    assert (Decimal(summary["billed_below_approved"]), Decimal(summary["potential_overcharge"])) == (300, 0)
    # The other 15 t on a later invoice: flagged as the same ticket on two invoices, never silently paid.
    second = _supplier_line(client, yard, number="INV-2", date_from="2026-11-16", tons="15.000", subtotal="300.00",
                            tickets="70001\t15")
    [row] = [r for r in second["ticket_check"] if r["line"] is not None]
    assert row["result"] == "outside_scope" and "on_another_invoice" in row["flags"]
    duplicates = [i for i in _exceptions(client) if i["category"] == "duplicate"]
    assert len(duplicates) == 1 and sorted(duplicates[0]["invoice_numbers"]) == ["INV-1", "INV-2"]
    # Links can't be confirmed for a partly billed ticket.
    refused = client.post(f"/api/invoice-comparisons/{first['id']}/ticket-links", json={"ticket_ids": [row["candidates"][0]["ticket_id"]]})
    assert refused.status_code == 409


# ---------------------------------------------------------------- 3. a shift split across projects

def _split_shift(client, yard):
    shift = _approve(client, _shift(client, yard), work_hours="9", standby_hours="0", allocations=[
        {"project_id": yard["project"], "hours": "6"}, {"project_id": yard["other"], "hours": "3"}])
    assert shift.status_code == 200, shift.text
    return shift.json()


def _hours(hours, amount):
    return {"kind": "hours", "truck_identifier": "T-12", "work_date": "2026-11-03", "hours": hours, "rate": "110", "amount": amount}


def test_a_split_shift_billed_by_each_project_for_its_share_matches_once_everywhere(client, yard):
    _split_shift(client, yard)  # 9 h at $110 = $990: 6 h ($660) to P1, 3 h ($330) to P2
    p1 = _hauler_invoice(client, yard, [_hours("6", "660.00")], number="HV-P1")
    p2 = _hauler_invoice(client, yard, [_hours("3", "330.00")], number="HV-P2", project="other")
    assert [(i["lines"][0]["result"], i["supported"], i["difference"], i["exceptions"]) for i in (p1, p2)] == [
        ("matched", "660.00", "0.00", 0), ("matched", "330.00", "0.00", 0)]
    assert _exceptions(client) == []
    # The cost report, the project directory and the invoices agree, project by project.
    report = {p["id"]: p["trucking"] for p in client.get("/api/reports/summary").json()["by_project"]}
    directory = {p["id"]: p["trucking_cost"] for p in client.get("/api/projects/directory").json()}
    assert (report[yard["project"]], report[yard["other"]]) == ("660.00", "330.00")
    assert (directory[yard["project"]], directory[yard["other"]]) == ("660.00", "330.00")
    register = {r["invoice_number"]: (r["billed"], r["compared"], r["status"])
                for r in client.get("/api/invoice-comparisons/register").json()["invoices"]}
    assert register == {"HV-P1": ("660.00", "660.00", "matched"), "HV-P2": ("330.00", "330.00", "matched")}


def test_a_split_shift_billed_in_full_on_one_project_is_over_by_the_other_share_and_never_counted_twice(client, yard):
    _split_shift(client, yard)
    p1 = _hauler_invoice(client, yard, [_hours("9", "990.00")], number="HV-P1")
    assert (p1["supported"], p1["difference"]) == ("660.00", "330.00")
    # P2's 3 h aren't billed yet, by an invoice covering P2: owed later, not an overcharge.
    unbilled = [i for i in _exceptions(client) if i["category"] == "not_invoiced"]
    assert [(i["project_id"], i["amount"]) for i in unbilled] == [(yard["other"], None)]
    p2 = _hauler_invoice(client, yard, [_hours("3", "330.00")], number="HV-P2", project="other")
    assert (p2["lines"][0]["result"], p2["supported"]) == ("matched", "330.00")
    summary = client.get("/api/exceptions").json()["summary"]
    assert summary["potential_overcharge"] == "330.00"  # billed $1,320 for $990 approved
    # Billing P2's share twice is a repeat.
    again = _hauler_invoice(client, yard, [_hours("3", "330.00")], number="HV-P2B", project="other")
    assert (again["lines"][0]["result"], again["supported"]) == ("repeat", "0.00")


def test_a_split_shift_billed_once_without_a_project_is_compared_in_full(client, yard):
    _split_shift(client, yard)
    whole = _hauler_invoice(client, yard, [_hours("9", "990.00")], number="HV-ALL", project=None)
    assert (whole["lines"][0]["result"], whole["supported"]) == ("matched", "990.00")
    share = _hauler_invoice(client, yard, [_hours("3", "330.00")], number="HV-P2", project="other")
    assert (share["lines"][0]["result"], share["supported"]) == ("repeat", "0.00")


def test_a_split_shift_with_standby_is_left_to_a_person_not_guessed():
    from datetime import date

    from app.services.hauling_invoices import LineFacts, ShiftFacts, reconcile
    shift = ShiftFacts(id=1, hauler_id=1, truck_key="T12", truck="T-12", work_date=date(2026, 11, 3), project_id=1, status="approved",
                       work=Decimal("8"), standby=Decimal("1"), top_up=Decimal(0), hourly=Decimal("110"), standby_rate=Decimal("90"),
                       projects=frozenset({1, 2}), shares=((1, Decimal("6"), Decimal("663.33")), (2, Decimal("3"), Decimal("331.67"))))
    line = LineFacts(invoice_id=1, invoice_number="HV-P2", hauler_id=1, hauler="Fictional Hauling LLC", project_id=2,
                     period_from=date(2026, 11, 1), period_to=date(2026, 11, 30), line_no=1, kind="hours", truck_key="T12",
                     truck="T-12", work_date=date(2026, 11, 3), hours=Decimal("3"), rate=Decimal("110"), amount=Decimal("330.00"))
    [result], findings = reconcile([line], [shift])
    assert (result.result, result.expected, result.difference) == ("needs_review", None, None)
    # The line's own question, with no dollars; the shift's parts stay owed until a person settles it.
    assert [(f.category, f.amount) for f in findings if f.line_no == 1] == [("needs_review", None)]


# ---------------------------------------------------------------- 4. what is a duplicate

def test_a_ticket_on_a_supplier_invoice_and_on_a_hauler_invoice_is_material_and_hauling_not_a_duplicate(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _delivery(client, yard, number="70001")
    material = _supplier_line(client, yard, tons="25.000", subtotal="500.00", tickets="70001\t25")
    hauling = _hauler_invoice(client, yard, [{"kind": "tons", "tickets": "70001", "quantity": "25.000", "rate": "4.0000",
                                              "amount": "100.00"}])
    assert material["status"] == "matches" and not any(r["flags"] for r in material["ticket_check"])
    assert (hauling["lines"][0]["result"], hauling["exceptions"]) == ("matched", 0)
    assert _exceptions(client) == []


def test_every_kind_of_second_bill_is_caught(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _delivery(client, yard, number="70001")
    _approve(client, _shift(client, yard, truck=1), work_hours="9", standby_hours="0")
    _supplier_line(client, yard, date_to="2026-11-15", tons="25.000", subtotal="500.00", tickets="70001\t25")
    _supplier_line(client, yard, number="INV-2", date_from="2026-11-16", tons="25.000", subtotal="500.00", tickets="70001\t25")
    hours = {"kind": "hours", "truck_identifier": "T-14", "work_date": "2026-11-03", "hours": "9", "rate": "110", "amount": "990.00"}
    tons = {"kind": "tons", "tickets": "70001", "quantity": "25.000", "rate": "4.0000", "amount": "100.00"}
    _hauler_invoice(client, yard, [hours, tons])
    again = _hauler_invoice(client, yard, [hours, tons], number="HV-2")
    assert [line["result"] for line in again["lines"]] == ["repeat", "repeat"]
    duplicates = sorted((i["invoice_numbers"][0] if len(i["invoice_numbers"]) == 1 else ",".join(i["invoice_numbers"]), i["amount"])
                        for i in _exceptions(client) if i["category"] == "duplicate")
    assert duplicates == [("HV-2", "100.00"), ("HV-2", "990.00"), ("INV-1,INV-2", None)]


# ---------------------------------------------------------------- 6. query budget

def test_the_hauler_invoice_list_and_register_dont_query_once_per_invoice(client, yard):
    from .test_query_budgets import _Count
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")

    def statements(url):
        with _Count() as count:
            assert client.get(url).status_code == 200
        return count.statements

    _hauler_invoice(client, yard, [_hours("9", "990.00")], number="HV-0")
    few = {url: statements(url) for url in ("/api/hauling-invoices", "/api/invoice-comparisons/register")}
    for n in range(1, 8):
        _hauler_invoice(client, yard, [_hours("9", "990.00"), {"kind": "fee", "amount": "25.00"}], number=f"HV-{n}")
    many = {url: statements(url) for url in few}
    assert many == few  # one query reads every invoice's lines (it was one query per invoice)
