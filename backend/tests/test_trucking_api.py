"""Hourly trucking through the API: entering, approving, locking, allocating, reopening,
and linking deliveries without charging per delivery. Fictional data."""
from decimal import Decimal

import pytest
import sqlalchemy

from app.models import ShiftAllocation, TruckShift
from app.routers import api as api_module

from .sample_data import TICKETS, make_scan_pdf


@pytest.fixture()
def yard(client):
    project = client.post("/api/projects", json={"code": "P1", "name": "Fictional Sitework"}).json()["id"]
    other = client.post("/api/projects", json={"code": "P2", "name": "Fictional Paving"}).json()["id"]
    hauler = client.post("/api/haulers", json={"name": "Fictional Hauling LLC"}).json()["id"]
    trucks = [client.post("/api/trucks", json={"hauler_id": hauler, "identifier": n, "equipment_type": "quad_axle"}).json()["id"]
              for n in ("T-12", "T-14")]
    assert client.post("/api/hauling-rates", json={"hauler_id": hauler, "hourly_rate": "110.00"}).status_code == 201
    return {"project": project, "other": other, "hauler": hauler, "trucks": trucks}


def _shift(client, yard, truck=0, **kw):
    body = {"project_id": yard["project"], "truck_id": yard["trucks"][truck], "start_at": "2026-11-03T06:30",
            "end_at": "2026-11-03T16:00", "unpaid_break_minutes": 30, "field_signoff": "J. Foreman", **kw}
    response = client.post("/api/truck-shifts", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _approve(client, shift, **body):
    return client.post(f"/api/truck-shifts/{shift['id']}/approve", json=body, headers={"If-Match": shift["revision"]})


def test_the_brief_example_material_and_hauling_are_each_verified_and_add_up(client, yard):
    # 220 t of aggregate at $18/t from the quarry: four approved deliveries of 55 t.
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()["id"]
    client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "18.00"})
    papers = [{**TICKETS[0], "ticket_number": f"Q{n}"} for n in range(4)]
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(yard["project"])},
                        files=[("files", ("q.pdf", make_scan_pdf(papers), "application/pdf"))]).json()
    deliveries = []
    for n, ticket in enumerate(client.get(f"/api/batches/{batch['id']}/tickets").json()):
        edited = client.patch(f"/api/tickets/{ticket['id']}", json={"ticket_number": f"Q{n}", "tonnage": "55.00", "supplier": "Example Aggregates",
                                                                   "material_type": "No. 53 Crushed Stone"}).json()
        assert client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]}).status_code == 200
        deliveries.append(edited["id"])
    # Two separately hired trucks, nine approved hours each at $110/h, two loads each.
    hauling = Decimal(0)
    for n in range(2):
        shift = _shift(client, yard, truck=n, loads=2)
        assert shift["on_site_hours"] == "9.00" and shift["cost"] == "990.00" and not shift["cost_is_locked"]
        linked = client.put(f"/api/truck-shifts/{shift['id']}/deliveries", json={"ticket_ids": deliveries[2 * n:2 * n + 2]})
        assert linked.status_code == 200 and linked.json()["cost"] == "990.00"  # linking two loads adds no charge
        approved = _approve(client, linked.json(), work_hours="9", standby_hours="0")
        assert approved.status_code == 200, approved.text
        view = approved.json()
        assert view["cost"] == "990.00" and view["cost_is_locked"] and view["deliveries"]["tons"] == "110.000"
        assert view["deliveries"]["cost_per_ton"] == "9.00"
        hauling += Decimal(view["cost"])
    material_cost = Decimal(client.get("/api/dashboard", params={"project_id": yard["project"]}).json()["total_cost"])
    assert (material_cost, hauling, material_cost + hauling) == (Decimal("3960.00"), Decimal("1980.00"), Decimal("5940.00"))
    summary = client.get("/api/truck-shifts", params={"project_id": yard["project"]}).json()["summary"]
    assert summary["approved_cost"] == "1980.00" and summary["approved_hours"] == "18.00" and summary["loads"] == 4


def test_a_night_shift_across_midnight_with_standby_and_a_minimum(client, yard):
    client.post("/api/hauling-rates", json={"hauler_id": yard["hauler"], "project_id": yard["project"], "hourly_rate": "120.00",
                                            "standby_rate": "90.00", "minimum_hours": "8"})
    shift = _shift(client, yard, start_at="2026-11-03T22:00", end_at="2026-11-04T04:30", unpaid_break_minutes=0, standby_hours="1.50")
    assert shift["work_date"] == "2026-11-03" and shift["on_site_hours"] == "6.50" and shift["work_hours"] == "5.00"
    assert [(line["label"], line["amount"]) for line in shift["cost_lines"]] == [
        ("Working hours", "600.00"), ("Standby", "135.00"), ("Minimum charge (8.00 h)", "180.00")]
    approved = _approve(client, shift, work_hours="5.00", standby_hours="1.50").json()
    assert approved["cost"] == "915.00" and approved["approved"]["billable_hours"] == "8.00"
    assert approved["approved"]["hourly_rate"] == "120.0000"  # the project agreement, not the company's $110


def test_approval_needs_a_revision_real_hours_and_a_rate_that_says_how_standby_is_paid(client, yard):
    shift = _shift(client, yard, standby_hours="1.00")
    assert client.post(f"/api/truck-shifts/{shift['id']}/approve", json={"work_hours": "8", "standby_hours": "1"}).status_code == 428
    assert _approve(client, {**shift, "revision": "99"}, work_hours="8", standby_hours="1").status_code == 409
    too_many = _approve(client, shift, work_hours="9", standby_hours="1")
    assert too_many.status_code == 422 and "more than the time on site" in too_many.text
    no_standby_rate = _approve(client, shift, work_hours="8", standby_hours="1")
    assert no_standby_rate.status_code == 422 and "standby" in no_standby_rate.text
    assert any("standby" in flag for flag in shift["flags"])  # said before anyone clicks


def test_the_time_sheet_claim_and_missing_signoff_are_flagged_not_used(client, yard):
    shift = _shift(client, yard, claimed_hours="10.00", field_signoff=None)
    assert shift["cost"] == "990.00"  # priced from the sheet's times, not the claim
    assert any("claims 10.00 h" in flag for flag in shift["flags"])
    assert any("sign-off" in flag for flag in shift["flags"])


def test_allocation_across_projects_adds_up_to_the_cent(client, yard):
    shift = _shift(client, yard)
    wrong = _approve(client, shift, work_hours="9", standby_hours="0",
                     allocations=[{"project_id": yard["project"], "hours": "5"}, {"project_id": yard["other"], "hours": "3"}])
    assert wrong.status_code == 422 and "add up to 8" in wrong.text
    split = _approve(client, shift, work_hours="9", standby_hours="0", allocations=[
        {"project_id": yard["project"], "cost_code": "31-2300", "hours": "6"}, {"project_id": yard["other"], "hours": "3"}])
    assert split.status_code == 200, split.text
    with api_module.SessionLocal() as db:
        parts = db.query(ShiftAllocation).order_by(ShiftAllocation.id).all()
    assert [(p.cost_code, p.hours, p.cost) for p in parts] == [("31-2300", Decimal("6.00"), Decimal("660.00")),
                                                               (None, Decimal("3.00"), Decimal("330.00"))]


def test_an_approved_shift_is_locked_until_a_manager_reopens_it_with_a_reason(client, yard):
    shift = _approve(client, _shift(client, yard), work_hours="9", standby_hours="0").json()
    edit = client.put(f"/api/truck-shifts/{shift['id']}", headers={"If-Match": shift["revision"]}, json={
        "project_id": yard["project"], "truck_id": yard["trucks"][0], "start_at": "2026-11-03T06:00", "end_at": "2026-11-03T16:00"})
    assert edit.status_code == 409
    with api_module.SessionLocal() as db:
        db.get(TruckShift, shift["id"]).approved_cost = Decimal("1.00")
        with pytest.raises(sqlalchemy.exc.DatabaseError):
            db.commit()
        db.rollback()
        db.delete(db.query(ShiftAllocation).first())
        with pytest.raises(sqlalchemy.exc.DatabaseError):
            db.commit()
    assert client.post(f"/api/truck-shifts/{shift['id']}/reopen", json={"reason": ""}, headers={"If-Match": shift["revision"]}).status_code == 422
    reopened = client.post(f"/api/truck-shifts/{shift['id']}/reopen", json={"reason": "Driver left at 3:00, sheet corrected"},
                           headers={"If-Match": shift["revision"]})
    assert reopened.status_code == 200 and reopened.json()["status"] == "needs_review" and not reopened.json()["cost_is_locked"]
    history = [e["field"] for e in client.get(f"/api/truck-shifts/{shift['id']}/history").json()]
    assert history == ["created", "status", "approved", "status", "reopened"]


def test_a_delivery_belongs_to_one_shift(client, yard):
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(yard["project"])},
                        files=[("files", ("s.pdf", make_scan_pdf(), "application/pdf"))]).json()
    ticket = client.get(f"/api/batches/{batch['id']}/tickets").json()[0]["id"]
    first, second = _shift(client, yard, truck=0), _shift(client, yard, truck=1)
    assert client.put(f"/api/truck-shifts/{first['id']}/deliveries", json={"ticket_ids": [ticket]}).status_code == 200
    taken = client.put(f"/api/truck-shifts/{second['id']}/deliveries", json={"ticket_ids": [ticket]})
    assert taken.status_code == 409
    assert client.put(f"/api/truck-shifts/{first['id']}/deliveries", json={"ticket_ids": [999999]}).status_code == 404
    released = client.put(f"/api/truck-shifts/{first['id']}/deliveries", json={"ticket_ids": []}).json()
    assert released["deliveries"]["count"] == 0  # cost per ton is never shown without linked tons
    assert released["deliveries"]["cost_per_ton"] is None


def test_a_clerk_enters_shifts_and_only_a_manager_approves(client, yard):
    from app.auth import current_user
    from app.main import app
    from app.models import User
    app.dependency_overrides[current_user] = lambda: User(id=1, organization_id=1, username="test-owner", display_name="Test Owner",
                                                          password_hash="fixture", active=True, role="clerk", must_change_password=False)
    shift = _shift(client, yard)
    assert _approve(client, shift, work_hours="9", standby_hours="0").status_code == 403
    assert client.post("/api/hauling-rates", json={"hauler_id": yard["hauler"], "hourly_rate": "1"}).status_code == 403


@pytest.mark.parametrize("start, end, unpaid, words", [
    ("2026-11-03T16:00", "2026-11-03T06:00", 0, "ends before it starts"),
    ("2026-11-03T06:00", "2026-11-04T07:00", 0, "longer than 24 hours"),
])
def test_impossible_time_sheets_are_refused_with_the_reason(client, yard, start, end, unpaid, words):
    response = client.post("/api/truck-shifts", json={"project_id": yard["project"], "truck_id": yard["trucks"][0],
                                                      "start_at": start, "end_at": end, "unpaid_break_minutes": unpaid})
    assert response.status_code == 422 and words in response.text


def test_the_dashboard_counts_approved_hauling_per_project_through_allocations(client, yard):
    split = _approve(client, _shift(client, yard), work_hours="9", standby_hours="0", allocations=[
        {"project_id": yard["project"], "hours": "6"}, {"project_id": yard["other"], "hours": "3"}])
    assert split.status_code == 200
    _shift(client, yard, truck=1)  # waiting for approval: not a cost yet
    everything = client.get("/api/dashboard").json()["hauling"]
    assert everything == {"approved_cost": "990.00", "approved_hours": "9.00", "approved_with_material": "990.00",
                          "per_unit_cost": "0.00", "rate_missing": 0, "awaiting_approval": 1, "disputed": 0}
    p1 = client.get("/api/dashboard", params={"project_id": yard["project"]}).json()["hauling"]
    p2 = client.get("/api/dashboard", params={"project_id": yard["other"]}).json()["hauling"]
    assert (p1["approved_cost"], p2["approved_cost"]) == ("660.00", "330.00")  # the shift counted once, in its parts
    assert client.get("/api/dashboard", params={"project_id": yard["project"]}).json()["total_cost"] == "0.00"  # material unchanged


def test_an_approved_shifts_deliveries_are_locked_with_it(client, yard):
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(yard["project"])},
                        files=[("files", ("s.pdf", make_scan_pdf(), "application/pdf"))]).json()
    first, second = [t["id"] for t in client.get(f"/api/batches/{batch['id']}/tickets").json()[:2]]
    shift = _shift(client, yard)
    linked = client.put(f"/api/truck-shifts/{shift['id']}/deliveries", json={"ticket_ids": [first]}).json()
    approved = _approve(client, linked, work_hours="9", standby_hours="0").json()
    for change in ([first, second], []):  # neither adding nor removing after approval
        assert client.put(f"/api/truck-shifts/{shift['id']}/deliveries", json={"ticket_ids": change}).status_code == 409
    again = next(s for s in client.get("/api/truck-shifts").json()["shifts"] if s["id"] == shift["id"])
    assert again["deliveries"]["ticket_ids"] == [first] and again["cost"] == approved["cost"]


def test_quarter_hour_billing_rounds_the_time_and_approval_takes_whole_steps(client, yard):
    client.post("/api/hauling-rates", json={"hauler_id": yard["hauler"], "project_id": yard["project"], "hourly_rate": "100.00",
                                            "billing_increment": "0.25", "increment_rounding": "up"})
    shift = _shift(client, yard, start_at="2026-11-03T07:00", end_at="2026-11-03T14:50", unpaid_break_minutes=0)  # 7 h 50 m
    assert (shift["on_site_hours"], shift["billed_time_hours"], shift["work_hours"], shift["cost"]) == ("7.83", "8.00", "8.00", "800.00")
    assert any("0.25 h steps" in flag for flag in shift["flags"])
    assert _approve(client, shift, work_hours="7.90", standby_hours="0").status_code == 422  # not a whole step
    assert _approve(client, shift, work_hours="8.25", standby_hours="0").status_code == 422  # more than the rounded time
    approved = _approve(client, shift, work_hours="8.00", standby_hours="0").json()
    assert approved["cost"] == "800.00" and approved["approved"]["billing_increment"] == "0.25"
    half = client.post("/api/hauling-rates", json={"hauler_id": yard["hauler"], "hourly_rate": "1", "billing_increment": "0.25"})
    assert half.status_code == 422  # an increment needs its rounding rule


def test_a_dispute_is_raised_and_settled_with_reasons_and_blocks_approval_meanwhile(client, yard):
    shift = _shift(client, yard, claimed_hours="11.00")
    raised = client.post(f"/api/truck-shifts/{shift['id']}/dispute", json={"status": "disputed", "reason": "Hauler bills 11 h; sheet shows 9"},
                         headers={"If-Match": shift["revision"]})
    assert raised.status_code == 200 and raised.json()["status"] == "disputed"
    disputed = raised.json()
    assert _approve(client, disputed, work_hours="9", standby_hours="0").status_code == 409
    settled = client.post(f"/api/truck-shifts/{shift['id']}/dispute", json={"status": "needs_review", "reason": "Hauler accepted 9 h"},
                          headers={"If-Match": disputed["revision"]}).json()
    assert _approve(client, settled, work_hours="9", standby_hours="0").status_code == 200
    fields = [e["field"] for e in client.get(f"/api/truck-shifts/{shift['id']}/history").json()]
    assert fields[1:6] == ["status", "dispute", "status", "dispute_settled", "status"]
