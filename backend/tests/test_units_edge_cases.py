"""Edge cases for units of measure and per-ton/per-load hauler invoices: pounds hauled per
ton, yards on a tons line, unit changes on an unapproved ticket, a ticket listed twice on
one invoice, partial billing across days, cent rounding, and an hours line for a truck
that worked another project. Fictional data."""
from .test_hauling_unit_invoices import _findings, _invoice, _tons
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)
from .test_trucking_billing import _haul_rate
from .test_units_of_measure import _catalog, _ticket


def _items(client, number):
    return [i for i in client.get("/api/exceptions", params={"status": "all"}).json()["items"] if number in i["invoice_numbers"]]


def test_a_ticket_entered_in_pounds_is_hauled_per_ton_on_its_converted_tons(client, yard):
    _catalog(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    ticket = _ticket(client, yard, quantity="44300", unit="lb", hauler=True)
    assert (ticket["tonnage"], ticket["unit"]) == ("22.150", "ton")
    assert (ticket["haul_billing"], ticket["haul_cost"]) == ("per_ton", "88.60")  # 22.15 t x $4.00
    [line] = _invoice(client, yard, [_tons("80001", quantity="22.150", amount="88.60")])["lines"]
    assert (line["result"], line["supported"], line["difference"]) == ("matched", "88.60", "0.00")


def test_a_yards_delivery_on_a_tons_line_supports_nothing_and_asks_for_a_rate(client, yard):
    _catalog(client, price="32.00", unit="cy")
    _haul_rate(client, yard, "per_ton", "4.00")
    ticket = _ticket(client, yard, quantity="12.00", unit="cy", hauler=True)
    assert ticket["haul_billing"] == "unpriced"
    invoice = _invoice(client, yard, [_tons("80001", quantity="12.000", amount="48.00")])
    [line] = invoice["lines"]
    # Only "needs review" problems: the line can't be checked, so nothing is paid and no dollars are guessed.
    assert line["result"] == "not_approved" and line["supported"] in (None, "0.00")
    items = _items(client, "HV-1")
    assert [(i["category"], i["material"]) for i in items] == [("needs_review", "Hauling per ton")]
    assert "No agreed hauling rate" in items[0]["title"]


def test_changing_an_unapproved_ticket_between_units(client, yard):
    _catalog(client)
    ticket = _ticket(client, yard, quantity="12.00", unit="cy", approve=False, gross_weight="80000", tare_weight="35700",
                     weight_unit="lb")
    back = client.patch(f"/api/tickets/{ticket['id']}", json={"unit": "ton"}, headers={"If-Match": ticket["revision"]})
    assert back.status_code == 200, back.text
    back = back.json()
    # The quantity and the scale weights stay; only the unit changes, and the weights are now checked against tons.
    assert (back["tonnage"], back["unit"], back["gross_weight"], back["tare_weight"], back["weight_unit"]) == \
        ("12.000", "ton", "80000.000", "35700.000", "lb")
    assert (back["source_quantity"], back["source_unit"]) == (None, None)
    assert any(f.startswith("Weights: gross minus tare is 22.150 t") for f in back["flags"])
    cleared = client.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": None}).json()
    refused = client.patch(f"/api/tickets/{ticket['id']}", json={"unit": "lb"}, headers={"If-Match": cleared["revision"]})
    assert refused.status_code == 422
    assert cleared["unit"] == "ton" and cleared["tonnage"] is None


def test_the_same_ticket_on_two_lines_of_one_invoice_is_billed_again_on_the_second(client, yard):
    _catalog(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _ticket(client, yard, number="80001", quantity="25.00", hauler=True)
    invoice = _invoice(client, yard, [_tons("80001", quantity="25.000", amount="100.00"),
                                      _tons("80001", quantity="25.000", amount="100.00")])
    first, second = invoice["lines"]
    assert (first["result"], first["supported"]) == ("matched", "100.00")
    assert (second["result"], second["supported"], second["difference"]) == ("repeat", "0.00", "100.00")
    items = _items(client, "HV-1")
    assert [(i["category"], i["amount"]) for i in items] == [("duplicate", "100.00")]
    assert "Billed again" in items[0]["title"]


def test_billing_one_of_two_days_leaves_the_other_unbilled_without_a_finding(client, yard):
    _catalog(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    _ticket(client, yard, number="80001", quantity="25.00", hauler=True)
    _ticket(client, yard, number="80002", quantity="20.00", hauler=True, delivery_date="2026-11-04")
    invoice = _invoice(client, yard, [_tons(work_date="2026-11-03", quantity="25.000", amount="100.00")])
    [line] = invoice["lines"]
    assert (line["result"], line["supported"]) == ("matched", "100.00")
    assert (invoice["exceptions"], invoice["unbilled_in_period"]) == (0, 1)
    unbilled = client.get("/api/exceptions", params={"category": "not_invoiced"}).json()["items"]
    assert any("ticket 80002" in i["title"] for i in unbilled)
    assert not any("ticket 80001" in i["title"] for i in unbilled)


def test_per_ton_cents_rounded_per_ticket_sum_to_an_exact_match(client, yard):
    _catalog(client)
    _haul_rate(client, yard, "per_ton", "4.25")
    # 22.3 t x 4.25 = 94.775 -> 94.78; 18.1 t x 4.25 = 76.925 -> 76.93; 21.7 t x 4.25 = 92.225 -> 92.23 (half up).
    costs = [_ticket(client, yard, number=n, quantity=q, hauler=True)["haul_cost"]
             for n, q in (("80001", "22.3"), ("80002", "18.1"), ("80003", "21.7"))]
    assert costs == ["94.78", "76.93", "92.23"]
    # The line's amount is the per-ticket sum (263.94), a cent above 62.1 t x 4.25 = 263.925 -> 263.93.
    [line] = _invoice(client, yard, [_tons("80001 80002 80003", quantity="62.100", rate="4.2500", amount="263.94")])["lines"]
    assert (line["supported"], line["difference"]) == ("263.94", "0.00")
    assert line["result"] == "matched"
    assert _items(client, "HV-1") == []


def test_an_hours_line_for_a_truck_whose_only_shift_is_on_another_project(client, yard):
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")                              # T-12, this project
    _approve(client, _shift(client, yard, truck=1, project_id=yard["other"]), work_hours="9", standby_hours="0")  # T-14, other
    [line] = _invoice(client, yard, [{"kind": "hours", "truck_identifier": "T-14", "work_date": "2026-11-03", "hours": "9",
                                      "rate": "110", "amount": "990.00"}])["lines"]
    assert (line["supported"], line["difference"]) == ("0.00", "990.00")
    # This project's own T-12 shift is left unbilled (not_invoiced); the T-14 line is the one problem with dollars.
    billed = [(i["category"], i["amount"]) for i in _items(client, "HV-1") if i["category"] != "not_invoiced"]
    assert billed == [("wrong_project", "990.00")]
    assert any(c == "wrong_project" for c, _ in _findings(client, "HV-1"))
