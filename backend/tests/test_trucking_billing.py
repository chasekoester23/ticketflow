"""Hauling billed per ton or per load, freight included in a delivered price, and the
rules that keep a delivery's hauling from being paid twice (docs/engineering/trucking-billing.md).
Fictional data."""
import pytest
import sqlalchemy

from app.models import Organization, Hauler, Ticket

from .sample_data import TICKETS, make_scan_pdf
from .test_query_budgets import _session
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)


def _material(client, freight=None, price="20.00"):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    material = client.post("/api/materials", json={"name": "No. 57 Stone"}).json()["id"]
    body = {"supplier_id": supplier, "material_id": material, "unit_price": price}
    if freight:
        body["freight"] = freight
    assert client.put("/api/rates", json=body).status_code == 200
    return supplier, material


def _haul_rate(client, yard, method, rate, **extra):
    response = client.post("/api/hauling-rates", json={"hauler_id": yard["hauler"], "billing_method": method, "unit_rate": rate, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _delivery(client, yard, *, tons="25.00", hauler=True, number="70001", approve=True):
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(yard["project"])},
                        files=[("files", ("d.pdf", make_scan_pdf([{**TICKETS[0], "ticket_number": number}]), "application/pdf"))]).json()
    ticket = client.get(f"/api/batches/{batch['id']}/tickets").json()[0]
    body = {"ticket_number": number, "delivery_date": "2026-11-03", "supplier": "Example Aggregates", "material_type": "No. 57 Stone",
            "tonnage": tons, **({"hauler_id": yard["hauler"], "truck_number": "T-12"} if hauler else {})}
    edited = client.patch(f"/api/tickets/{ticket['id']}", json=body)
    assert edited.status_code == 200, edited.text
    edited = edited.json()
    if approve:
        verified = client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]})
        assert verified.status_code == 200, verified.text
        return verified.json()
    return edited


def _costs(client, yard):
    dashboard = client.get("/api/dashboard", params={"project_id": yard["project"]}).json()
    directory = next(r for r in client.get("/api/projects/directory").json() if r["id"] == yard["project"])
    report = client.get("/api/reports/summary", params={"project_id": yard["project"]}).json()
    return {"material": {dashboard["total_cost"], directory["material_cost"], report["costs"]["material"]},
            "hauling": {dashboard["hauling"]["approved_cost"], directory["trucking_cost"], report["costs"]["trucking"]},
            "total": {dashboard["hauling"]["approved_with_material"], directory["verified_cost"], report["costs"]["total"]}}


def test_per_ton_hauling_is_tons_times_rate_locked_at_approval_and_counted_once_everywhere(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    ticket = _delivery(client, yard)
    assert (ticket["haul_billing"], ticket["haul_unit_rate"], ticket["haul_cost"]) == ("per_ton", "4.0000", "100.00")
    assert _costs(client, yard) == {"material": {"500.00"}, "hauling": {"100.00"}, "total": {"600.00"}}
    report = client.get("/api/reports/summary", params={"project_id": yard["project"]}).json()
    assert {m["method"]: m["cost"] for m in report["trucking"]["billing_methods"]} == {"hourly": "0.00", "per_ton": "100.00"}
    [hauler] = report["trucking"]["by_hauler"]
    assert (hauler["cost"], hauler["deliveries"], hauler["per_hour"]) == ("100.00", 1, None)  # no hours, so no $/hour


def test_per_load_hauling_is_one_rate_per_delivery(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_load", "85.00")
    first, second = _delivery(client, yard, number="70001"), _delivery(client, yard, number="70002", tons="18.00")
    assert (first["haul_cost"], second["haul_cost"]) == ("85.00", "85.00")
    assert _costs(client, yard)["hauling"] == {"170.00"}


def test_a_delivered_price_with_freight_included_is_never_charged_hauling_again(client, yard):
    _material(client, freight="included", price="24.00")
    _haul_rate(client, yard, "per_ton", "4.00")
    ticket = _delivery(client, yard)
    assert (ticket["haul_billing"], ticket["haul_cost"]) == ("freight_included", None)
    assert _costs(client, yard) == {"material": {"600.00"}, "hauling": {"0.00"}, "total": {"600.00"}}


def test_a_delivery_carried_on_an_hourly_shift_is_paid_by_the_shift_only(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    pending = _delivery(client, yard, approve=False)
    shift = _shift(client, yard)
    linked = client.put(f"/api/truck-shifts/{shift['id']}/deliveries", json={"ticket_ids": [pending["id"]]})
    assert linked.status_code == 200, linked.text
    ticket = client.post(f"/api/tickets/{pending['id']}/verify").json()
    assert (ticket["haul_billing"], ticket["haul_cost"]) == ("hourly", None)
    _approve(client, linked.json(), work_hours="9", standby_hours="0")
    assert _costs(client, yard) == {"material": {"500.00"}, "hauling": {"990.00"}, "total": {"1490.00"}}


def test_a_delivery_already_charged_per_ton_cant_also_go_on_an_hourly_shift(client, yard):
    _material(client)
    _haul_rate(client, yard, "per_ton", "4.00")
    ticket = _delivery(client, yard)
    shift = _shift(client, yard)
    refused = client.put(f"/api/truck-shifts/{shift['id']}/deliveries", json={"ticket_ids": [ticket["id"]]})
    assert refused.status_code == 409 and "already charged per ton" in refused.text


def test_a_hauler_without_a_rate_is_missing_hauling_not_free_hauling(client, yard):
    _material(client)
    ticket = _delivery(client, yard)
    assert (ticket["haul_billing"], ticket["haul_cost"]) == ("unpriced", None)
    report = client.get("/api/reports/summary", params={"project_id": yard["project"]}).json()
    assert report["costs"]["hauling_rate_missing"] == 1 and report["costs"]["complete"] is False
    assert client.get("/api/dashboard", params={"project_id": yard["project"]}).json()["hauling"]["rate_missing"] == 1


def test_per_ton_and_per_load_rates_never_price_an_hourly_shift(client, yard):
    _haul_rate(client, yard, "per_ton", "4.00", project_id=yard["project"])
    shift = _shift(client, yard)
    approved = _approve(client, shift, work_hours="9", standby_hours="0").json()
    assert approved["cost"] == "990.00"  # the company's hourly agreement ($110/h), as before


def test_a_later_rate_change_never_changes_an_approved_hauling_charge(client, yard):
    _material(client)
    rate = _haul_rate(client, yard, "per_ton", "4.00")
    ticket = _delivery(client, yard)
    client.delete(f"/api/hauling-rates/{rate['id']}")
    _haul_rate(client, yard, "per_ton", "6.50")
    again = next(t for t in client.get(f"/api/batches/{ticket['batch_id']}/tickets").json() if t["id"] == ticket["id"])
    assert (again["haul_unit_rate"], again["haul_cost"]) == ("4.0000", "100.00")
    assert _costs(client, yard)["hauling"] == {"100.00"}
    with _session() as db:
        row = db.get(Ticket, ticket["id"])
        row.haul_cost = 1
        with pytest.raises(sqlalchemy.exc.DatabaseError):
            db.commit()  # the database refuses it too
        db.rollback()


def test_withdrawing_the_approval_clears_the_charge_and_the_next_approval_uses_todays_rate(client, yard):
    _material(client)
    rate = _haul_rate(client, yard, "per_ton", "4.00")
    ticket = _delivery(client, yard)
    flagged = client.post(f"/api/tickets/{ticket['id']}/flag", json={"note": "Check the hauler"}).json()
    assert flagged["haul_billing"] is None and flagged["haul_cost"] is None
    client.delete(f"/api/hauling-rates/{rate['id']}")
    _haul_rate(client, yard, "per_ton", "5.00")
    again = client.post(f"/api/tickets/{ticket['id']}/verify").json()
    assert (again["haul_unit_rate"], again["haul_cost"]) == ("5.0000", "125.00")


def test_rates_only_take_the_fields_of_their_billing_method(client, yard):
    bad = [{"billing_method": "per_ton", "unit_rate": "4", "hourly_rate": "110"},
           {"billing_method": "per_ton", "unit_rate": "4", "minimum_hours": "4"},
           {"billing_method": "per_load"},
           {"billing_method": "hourly", "unit_rate": "4"},
           {"billing_method": "per_mile", "unit_rate": "4"}]
    for body in bad:
        assert client.post("/api/hauling-rates", json={"hauler_id": yard["hauler"], **body}).status_code == 422, body


def test_another_companys_hauler_cant_be_put_on_a_ticket(client, yard):
    with _session() as db:
        other = Organization(name="Fictional Other Co")
        db.add(other)
        db.flush()
        theirs = Hauler(organization_id=other.id, name="Their Trucking", name_key="their trucking")
        db.add(theirs)
        db.commit()
        theirs_id = theirs.id
    ticket = _delivery(client, yard, hauler=False, approve=False)
    assert client.patch(f"/api/tickets/{ticket['id']}", json={"hauler_id": theirs_id}).status_code == 404


def test_resaving_a_price_without_a_freight_choice_keeps_the_freight_setting(client, yard):
    supplier, material = _material(client, freight="included", price="24.00")
    assert client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "25.00"}).status_code == 200
    [rate] = client.get("/api/rates").json()
    assert (rate["unit_price"], rate["freight"]) == ("25.0000", "included")
    history = client.get("/api/pricing/history").json()
    assert not any(e["action"] == "freight_changed" for e in history)
