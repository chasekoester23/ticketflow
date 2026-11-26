"""Financial-correctness audit of hourly trucking and hauling invoices (2026-11-24):
each test is a question the audit asked, answered on fictional data."""
import json
from decimal import Decimal

from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)


def _invoice(client, yard, lines, number="FH-1"):
    response = client.post("/api/hauling-invoices", json={"hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": number,
                                                          "period_from": "2026-11-01", "period_to": "2026-11-30", "lines": lines})
    assert response.status_code == 201, response.text
    return response.json()


HOURS = {"kind": "hours", "truck_identifier": "T-12", "work_date": "2026-11-03", "hours": "9", "rate": "110", "amount": "990.00"}


def test_reconciliation_uses_the_locked_rate_so_a_later_agreement_change_changes_nothing(client, yard):
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    rates = client.get("/api/hauling-rates").json()
    client.delete(f"/api/hauling-rates/{rates[0]['id']}")
    client.post("/api/hauling-rates", json={"hauler_id": yard["hauler"], "hourly_rate": "135.00"})
    invoice = _invoice(client, yard, [HOURS])
    assert invoice["lines"][0]["result"] == "matched" and invoice["difference"] == "0.00"


def test_reopening_an_invoiced_shift_is_visible_on_the_invoice_and_its_approval_stays_in_history(client, yard):
    shift = _approve(client, _shift(client, yard), work_hours="9", standby_hours="0").json()
    _invoice(client, yard, [HOURS])
    reopened = client.post(f"/api/truck-shifts/{shift['id']}/reopen", json={"reason": "Driver's hours queried"},
                           headers={"If-Match": shift["revision"]}).json()
    line = client.get("/api/hauling-invoices").json()[0]["lines"][0]
    assert line["result"] == "not_approved"  # never silently "matched" against a reopened shift
    assert any(i["key"].endswith(":unapproved") for i in client.get("/api/exceptions").json()["items"])
    history = client.get(f"/api/truck-shifts/{shift['id']}/history").json()
    assert any(e["field"] == "approved" and "$990.00" in e["new"] for e in history)
    again = _approve(client, reopened, work_hours="9", standby_hours="0")
    assert again.status_code == 200 and client.get("/api/hauling-invoices").json()[0]["lines"][0]["result"] == "matched"


def test_increment_minimum_and_standby_together(client, yard):
    client.post("/api/hauling-rates", json={"hauler_id": yard["hauler"], "project_id": yard["project"], "hourly_rate": "100.00",
                                            "standby_rate": "80.00", "minimum_hours": "4", "billing_increment": "0.25", "increment_rounding": "up"})
    # 2 h 50 m on site, half an hour of it standby: 3.00 h billable time, 2.50 working, 0.50 standby, topped up to 4.
    shift = _shift(client, yard, start_at="2026-11-03T07:00", end_at="2026-11-03T09:50", unpaid_break_minutes=0, standby_hours="0.50")
    assert (shift["billed_time_hours"], shift["work_hours"]) == ("3.00", "2.50")
    assert [(line["label"], line["amount"]) for line in shift["cost_lines"]] == [
        ("Working hours", "250.00"), ("Standby", "40.00"), ("Minimum charge (4.00 h)", "100.00")]
    approved = _approve(client, shift, work_hours="2.50", standby_hours="0.50").json()
    assert approved["cost"] == "390.00" and approved["approved"]["billable_hours"] == "4.00"


def test_a_line_with_effects_in_both_directions_never_overstates_the_overcharge(client, yard):
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    # 10 h at $100 = $1,000 against 9 h at $110 = $990: an hour too many (+$100), a lower rate (-$90). Net +$10.
    _invoice(client, yard, [{**HOURS, "hours": "10", "rate": "100", "amount": "1000.00"}])
    data = client.get("/api/exceptions").json()
    effects = {i["category"]: i["amount"] for i in data["items"] if i["material"] == "Hourly trucking"}
    assert effects == {"hours": "100.00", "rate": "-90.00"}
    assert Decimal(data["summary"]["potential_overcharge"]) == Decimal("10.00")


def test_a_correction_keeps_the_invoice_as_first_billed_and_a_credit_lowers_the_bill_without_an_exception(client, yard):
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    invoice = _invoice(client, yard, [{**HOURS, "hours": "10", "amount": "1100.00"}])
    body = {"hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": "FH-1", "period_from": "2026-11-01",
            "period_to": "2026-11-30", "lines": [{**HOURS, "hours": "10", "amount": "1100.00"},
                                                 {"kind": "credit", "amount": "-110.00", "description": "Credit memo CM-5"}]}
    corrected = client.put(f"/api/hauling-invoices/{invoice['id']}", json=body, headers={"If-Match": invoice["revision"]}).json()
    assert corrected["billed"] == "990.00" and [line["result"] for line in corrected["lines"]] == ["differs", "credit"]
    history = client.get(f"/api/hauling-invoices/{invoice['id']}/history").json()
    first = json.loads(history[-1]["old"])
    assert history[-1]["field"] == "corrected" and first["billed"] == "1100.00" and first["lines"][0]["hours"] == "10.00"
    assert client.post("/api/hauling-invoices", json={**body, "invoice_number": "FH-2",
                                                      "lines": [{"kind": "credit", "amount": "5.00"}]}).status_code == 422
