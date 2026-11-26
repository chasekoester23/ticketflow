"""Hauler invoices billed per ton or per load, reconciled against approved deliveries
that carry that hauler's locked charge. Every way it can be billed wrong, and a person
resolving a line that matched more than one shift. Fictional data."""
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)
from .test_trucking_billing import _delivery, _haul_rate, _material


def _invoice(client, yard, lines, number="HV-1", project="project", period=("2026-11-01", "2026-11-30")):
    response = client.post("/api/hauling-invoices", json={
        "hauler_id": yard["hauler"], "project_id": yard[project] if project else None, "invoice_number": number,
        "period_from": period[0], "period_to": period[1], "lines": lines})
    assert response.status_code == 201, response.text
    return response.json()


def _findings(client, number=None):
    items = client.get("/api/exceptions", params={"status": "all"}).json()["items"]
    return {(i["category"], i["material"]) for i in items if number is None or number in i["invoice_numbers"]}


def _tons(tickets="", quantity="45.000", rate="4.0000", amount="180.00", **extra):
    return {"kind": "tons", "tickets": tickets, "quantity": quantity, "rate": rate, "amount": amount, **extra}


def test_a_per_ton_line_listing_its_tickets_matches_their_locked_hauling(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    a, b = _delivery(client, yard, number="70001", tons="25.00"), _delivery(client, yard, number="70002", tons="20.00")
    invoice = _invoice(client, yard, [_tons("70001, 70002")])
    [line] = invoice["lines"]
    assert (line["result"], line["supported"], line["difference"]) == ("matched", "180.00", "0.00")
    assert sorted(line["ticket_ids"]) == sorted([a["id"], b["id"]]) and invoice["exceptions"] == 0
    [row] = [r for r in client.get("/api/invoice-comparisons/register").json()["invoices"] if r["kind"] == "trucking"]
    assert (row["billed"], row["compared"], row["status"]) == ("180.00", "180.00", "matched")


def test_without_ticket_numbers_the_days_deliveries_are_used(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _delivery(client, yard, number="70001", tons="25.00")
    _delivery(client, yard, number="70002", tons="20.00")
    [line] = _invoice(client, yard, [_tons(work_date="2026-11-03")])["lines"]
    assert (line["result"], line["supported"]) == ("matched", "180.00")


def test_wrong_tons_and_wrong_rate_are_split_exactly(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _delivery(client, yard, number="70001", tons="25.00")
    _delivery(client, yard, number="70002", tons="20.00")
    # 47 t at $4.50 = $211.50 against 45 t at $4.00 = $180.00: +2 t at $4.50 = $9.00, +$0.50 on 45 t = $22.50.
    [line] = _invoice(client, yard, [_tons("70001 70002", quantity="47.000", rate="4.5000", amount="211.50")])["lines"]
    assert (line["result"], line["difference"]) == ("differs", "31.50")
    items = client.get("/api/exceptions").json()["items"]
    effects = {i["category"]: i["amount"] for i in items if i["material"] == "Hauling per ton"}
    assert effects == {"quantity": "9.00", "rate": "22.50"}


def test_billing_a_delivery_twice_is_a_duplicate_and_unsupported(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _delivery(client, yard, number="70001", tons="25.00")
    _invoice(client, yard, [_tons("70001", quantity="25.000", amount="100.00")])
    second = _invoice(client, yard, [_tons("70001", quantity="25.000", amount="100.00")], number="HV-2")
    [line] = second["lines"]
    assert (line["result"], line["supported"], line["difference"]) == ("repeat", "0.00", "100.00")
    assert ("duplicate", "Hauling per ton") in _findings(client, "HV-2")
    # One problem, one exception, carrying the line's dollars: not a second "amount differs" beside it.
    items = [i for i in client.get("/api/exceptions").json()["items"] if "HV-2" in i["invoice_numbers"]]
    assert [(i["category"], i["amount"]) for i in items] == [("duplicate", "100.00")]


def test_a_delivery_on_another_project_is_flagged_not_supported(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    other = dict(yard, project=yard["other"])
    _delivery(client, other, number="80001", tons="25.00")
    [line] = _invoice(client, yard, [_tons("80001", quantity="25.000", amount="100.00")])["lines"]
    assert line["supported"] == "0.00" and line["difference"] == "100.00"
    assert ("wrong_project", "Hauling per ton") in _findings(client, "HV-1")
    items = [i for i in client.get("/api/exceptions").json()["items"] if "HV-1" in i["invoice_numbers"]]
    assert [(i["category"], i["amount"]) for i in items] == [("wrong_project", "100.00")]


def test_hauling_already_paid_elsewhere_is_never_supported_again(client, yard):
    # Freight in the material price, and a delivery carried on an hourly shift: billing either per ton pays twice.
    _material(client, freight="included", price="24.00")
    _haul_rate(client, yard, "per_ton", "4.00")
    delivered = _delivery(client, yard, number="70001", tons="25.00")
    assert delivered["haul_billing"] == "freight_included"
    [line] = _invoice(client, yard, [_tons("70001", quantity="25.000", amount="100.00")])["lines"]
    assert (line["supported"], line["difference"]) == ("0.00", "100.00")
    texts = [i["title"] for i in client.get("/api/exceptions").json()["items"] if i["category"] == "duplicate"]
    assert any("Freight already in the material price" in t for t in texts)


def test_a_delivery_paid_by_an_hourly_shift_isnt_paid_per_ton_too(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    pending = _delivery(client, yard, number="70001", approve=False)
    shift = _shift(client, yard)
    linked = client.put(f"/api/truck-shifts/{shift['id']}/deliveries", json={"ticket_ids": [pending["id"]]}).json()
    client.post(f"/api/tickets/{pending['id']}/verify")
    _approve(client, linked, work_hours="9", standby_hours="0")
    [line] = _invoice(client, yard, [_tons("70001", quantity="25.000", amount="100.00")])["lines"]
    assert line["supported"] == "0.00"
    assert any("Already paid by the hour" in i["title"] for i in client.get("/api/exceptions").json()["items"])


def test_per_load_lines_count_loads_and_catch_an_extra_load(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_load", "85.00")
    for n in range(3):
        _delivery(client, yard, number=f"7000{n}", tons="20.00")
    good = _invoice(client, yard, [{"kind": "loads", "tickets": "70000 70001 70002", "quantity": "3", "rate": "85", "amount": "255.00"}])
    assert good["lines"][0]["result"] == "matched"
    # The same invoice, corrected to bill by date: 4 loads billed for 3 delivered.
    body = {"hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": "HV-1", "period_from": "2026-11-01",
            "period_to": "2026-11-30", "lines": [{"kind": "loads", "work_date": "2026-11-03", "quantity": "4", "rate": "85", "amount": "340.00"}]}
    more = client.put(f"/api/hauling-invoices/{good['id']}", json=body, headers={"If-Match": good["revision"]}).json()
    assert (more["lines"][0]["result"], more["lines"][0]["difference"]) == ("differs", "85.00")
    effects = {i["category"]: i["amount"] for i in client.get("/api/exceptions").json()["items"] if i["material"] == "Hauling per load"}
    assert effects == {"quantity": "85.00"}


def test_a_ticket_number_with_no_record_is_billed_with_nothing_behind_it(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    [line] = _invoice(client, yard, [_tons("99999", quantity="22.000", amount="88.00")])["lines"]
    assert line["supported"] == "0.00" and line["difference"] == "88.00"
    assert ("missing_ticket", "Hauling per ton") in _findings(client, "HV-1")


def test_partial_billing_leaves_the_rest_unbilled_without_calling_the_invoice_wrong(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _delivery(client, yard, number="70001", tons="25.00")
    _delivery(client, yard, number="70002", tons="20.00")
    invoice = _invoice(client, yard, [_tons("70001", quantity="25.000", amount="100.00")])
    assert (invoice["exceptions"], invoice["unbilled_in_period"]) == (0, 1)
    [row] = [r for r in client.get("/api/invoice-comparisons/register").json()["invoices"] if r["kind"] == "trucking"]
    assert row["status"] == "matched"
    unbilled = client.get("/api/exceptions", params={"category": "not_invoiced"}).json()["items"]
    assert any("ticket 70002" in i["title"] for i in unbilled)


def test_approved_hauling_no_invoice_covers_is_listed_as_not_billed_yet(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _delivery(client, yard, number="70001", tons="25.00")
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    unbilled = [i for i in client.get("/api/exceptions", params={"category": "not_invoiced"}).json()["items"] if i["material"] == "Trucking"]
    assert len(unbilled) == 1
    assert "1 approved shift and 1 approved delivery hauled per ton or per load" in unbilled[0]["title"]


def test_a_person_chooses_the_shift_for_a_line_that_matched_two(client, yard):
    first = _approve(client, _shift(client, yard), work_hours="9", standby_hours="0").json()
    _approve(client, _shift(client, yard, truck=1), work_hours="9", standby_hours="0")
    invoice = _invoice(client, yard, [{"kind": "hours", "work_date": "2026-11-03", "hours": "9", "rate": "110", "amount": "990.00"}])
    [line] = invoice["lines"]
    assert line["result"] == "ambiguous" and {c["truck"] for c in line["candidates"]} == {"T-12", "T-14"}
    body = {"hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": "HV-1", "period_from": "2026-11-01",
            "period_to": "2026-11-30", "lines": [{"kind": "hours", "work_date": "2026-11-03", "hours": "9", "rate": "110",
                                                  "amount": "990.00", "shift_id": first["id"]}]}
    chosen = client.put(f"/api/hauling-invoices/{invoice['id']}", json=body, headers={"If-Match": invoice["revision"]})
    assert chosen.status_code == 200, chosen.text
    [line] = chosen.json()["lines"]
    assert (line["result"], line["shift_id"], line["chosen_shift_id"]) == ("matched", first["id"], first["id"])
    history = client.get(f"/api/hauling-invoices/{invoice['id']}/history").json()
    assert history  # the correction is on the record


def test_lines_billed_per_ton_or_per_load_must_say_what_they_bill(client, yard):
    bad = [{"kind": "tons", "tickets": "1", "amount": "10.00"},
           {"kind": "loads", "tickets": "1", "quantity": "1.5", "amount": "10.00"},
           {"kind": "tons", "quantity": "1", "amount": "10.00"},
           {"kind": "tons", "tickets": "1", "quantity": "1", "hours": "2", "amount": "10.00"},
           {"kind": "hours", "work_date": "2026-11-03", "hours": "2", "quantity": "1", "amount": "10.00"}]
    for line in bad:
        response = client.post("/api/hauling-invoices", json={"hauler_id": yard["hauler"], "invoice_number": "X", "period_from": "2026-11-01",
                                                              "period_to": "2026-11-30", "lines": [line]})
        assert response.status_code == 422, line


def test_an_hours_line_for_a_shift_worked_on_another_project_is_flagged_and_not_paid(client, yard):
    _approve(client, _shift(client, yard, project_id=yard["other"]), work_hours="9", standby_hours="0")
    invoice = _invoice(client, yard, [{"kind": "hours", "truck_identifier": "T-12", "work_date": "2026-11-03", "hours": "9",
                                       "rate": "110", "amount": "990.00"}])
    [line] = invoice["lines"]
    assert (line["supported"], line["difference"]) == ("0.00", "990.00")
    items = [i for i in client.get("/api/exceptions").json()["items"] if "HV-1" in i["invoice_numbers"]]
    assert [(i["category"], i["amount"]) for i in items] == [("wrong_project", "990.00")]


def test_on_a_projects_invoice_the_projects_own_shift_is_the_match_not_a_question(client, yard):
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    _approve(client, _shift(client, yard, truck=1, project_id=yard["other"]), work_hours="9", standby_hours="0")
    [line] = _invoice(client, yard, [{"kind": "hours", "work_date": "2026-11-03", "hours": "9", "rate": "110", "amount": "990.00"}])["lines"]
    assert (line["result"], line["supported"]) == ("matched", "990.00")  # the other project's T-14 isn't a candidate
