"""A hauler's invoice through the API, against approved shifts, into the exceptions
center; and the original documents kept with shifts and invoices. Fictional data."""
import io
from decimal import Decimal

import pytest
from PIL import Image

from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def billed(client, yard):
    """Two approved nine-hour shifts at $110 (T-12 and T-14 on Nov 3), one more unbilled (T-14, Nov 4)."""
    for truck, day in ((0, "03"), (1, "03"), (1, "04")):
        shift = _shift(client, yard, truck=truck, start_at=f"2026-11-{day}T06:30", end_at=f"2026-11-{day}T16:00")
        assert _approve(client, shift, work_hours="9", standby_hours="0").status_code == 200
    invoice = {"hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": "FH-1001", "period_from": "2026-11-01",
               "period_to": "2026-11-30", "lines": [
                   {"kind": "hours", "truck_identifier": "T-12", "work_date": "2026-11-03", "hours": "9", "rate": "110", "amount": "990.00"},
                   {"kind": "hours", "truck_identifier": "t 14", "work_date": "2026-11-03", "hours": "12", "rate": "115", "amount": "1380.00"},
                   {"kind": "hours", "truck_identifier": "T-12", "work_date": "2026-11-03", "hours": "9", "rate": "110", "amount": "990.00"},
                   {"kind": "fee", "amount": "75.00", "description": "Fuel surcharge"},
                   {"kind": "hours", "truck_identifier": "T-99", "work_date": "2026-11-05", "hours": "8", "rate": "110", "amount": "880.00"}]}
    response = client.post("/api/hauling-invoices", json=invoice)
    assert response.status_code == 201, response.text
    return response.json(), invoice


def test_each_line_is_checked_against_its_approved_shift(client, yard, billed):
    invoice, _ = billed
    assert [line["result"] for line in invoice["lines"]] == ["matched", "differs", "repeat", "fee", "no_shift"]
    assert [line["difference"] for line in invoice["lines"]] == ["0.00", "390.00", "990.00", "75.00", "880.00"]
    assert invoice["billed"] == "4315.00" and invoice["supported"] == "1980.00" and invoice["difference"] == "2335.00"


def test_the_problems_land_in_the_exceptions_center_counted_once(client, yard, billed):
    data = client.get("/api/exceptions").json()
    mine = {i["key"]: i for i in data["items"] if i["material"] == "Hourly trucking"}
    by_category = {}
    for item in mine.values():
        by_category.setdefault(item["category"], []).append(item["amount"])
    assert by_category == {"hours": ["345.00"], "rate": ["45.00"], "duplicate": ["990.00"], "unsupported_fee": ["75.00"],
                           "missing_shift": ["880.00"], "not_invoiced": [None]}
    # 345 + 45 for the over-billed shift (its own $390), 990 repeat, 75 fee, 880 with no shift.
    assert Decimal(data["summary"]["potential_overcharge"]) == Decimal("2335.00")
    rate = next(i for i in mine.values() if i["category"] == "rate")
    assert rate["shift_id"] and rate["hauling_invoice_id"] and rate["line_billed"] == "1380.00" and rate["line_approved"] == "990.00"
    settled = client.post("/api/exceptions/review", headers={"If-Match": rate["revision"]},
                          json={"key": rate["key"], "status": "disputed", "note": "Sent to the hauler with the time sheet."})
    assert settled.status_code == 200, settled.text


def test_a_corrected_invoice_clears_what_it_fixed(client, yard, billed):
    invoice, body = billed
    body["lines"] = [body["lines"][0], {**body["lines"][1], "hours": "9", "rate": "110", "amount": "990.00"}]
    assert client.put(f"/api/hauling-invoices/{invoice['id']}", json=body).status_code == 428
    corrected = client.put(f"/api/hauling-invoices/{invoice['id']}", json=body, headers={"If-Match": invoice["revision"]})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["difference"] == "0.00"
    keys = {i["category"] for i in client.get("/api/exceptions").json()["items"] if i["material"] == "Hourly trucking"}
    assert keys == {"not_invoiced"}  # only Nov 4's unbilled shift is left
    assert client.post("/api/hauling-invoices", json=body).status_code == 409  # same number again


def test_original_documents_are_kept_served_only_to_the_company_and_typed(client, yard, billed):
    invoice, _ = billed
    pdf = b"%PDF-1.4 fictional hauling invoice"
    attached = client.post(f"/api/hauling-invoices/{invoice['id']}/document", files={"file": ("FH-1001.pdf", pdf, "application/pdf")})
    assert attached.status_code == 200 and attached.json()["document"] == {"name": "FH-1001.pdf"}
    original = client.get(f"/api/hauling-invoices/{invoice['id']}/document")
    assert original.status_code == 200 and original.content == pdf
    shift = client.get("/api/truck-shifts").json()["shifts"][0]
    sheet = client.post(f"/api/truck-shifts/{shift['id']}/timesheet", files={"file": ("sheet.png", _png(), "image/png")})
    assert sheet.status_code == 200 and sheet.json()["timesheet"] == {"name": "sheet.png"}
    assert client.get(f"/api/truck-shifts/{shift['id']}/timesheet").content[:4] == b"\x89PNG"
    refused = client.post(f"/api/truck-shifts/{shift['id']}/timesheet", files={"file": ("x.txt", b"hello", "text/plain")})
    assert refused.status_code == 400
    history = [e["field"] for e in client.get(f"/api/truck-shifts/{shift['id']}/history").json()]
    assert "timesheet" in history


def test_only_managers_enter_hauling_invoices(client, yard):
    from app.auth import current_user
    from app.main import app
    from app.models import User
    app.dependency_overrides[current_user] = lambda: User(id=1, organization_id=1, username="t", display_name="T", password_hash="x",
                                                          active=True, role="clerk", must_change_password=False)
    response = client.post("/api/hauling-invoices", json={"hauler_id": yard["hauler"], "invoice_number": "X", "period_from": "2026-11-01",
                                                           "period_to": "2026-11-30", "lines": [{"kind": "fee", "amount": "1.00"}]})
    assert response.status_code == 403


def test_an_invoice_for_one_project_doesnt_answer_for_another_projects_shifts(client, yard):
    on_p1 = _approve(client, _shift(client, yard), work_hours="9", standby_hours="0").json()
    on_p2 = _approve(client, _shift(client, yard, truck=1, project_id=yard["other"]), work_hours="9", standby_hours="0").json()
    created = client.post("/api/hauling-invoices", json={
        "hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": "P1-NOV", "period_from": "2026-11-01",
        "period_to": "2026-11-30", "lines": [{"kind": "hours", "truck_identifier": "T-12", "work_date": "2026-11-03", "hours": "9",
                                              "rate": "110", "amount": "990.00"}]})
    assert created.status_code == 201, created.text
    invoice = created.json()
    assert invoice["difference"] == "0.00" and invoice["exceptions"] == 0  # P2's shift isn't this P1 invoice's omission
    register = client.get("/api/invoice-comparisons/register").json()["invoices"]
    assert [(r["invoice_number"], r["status"]) for r in register] == [("P1-NOV", "matched")]
    assert on_p1["status"] == on_p2["status"] == "approved"
