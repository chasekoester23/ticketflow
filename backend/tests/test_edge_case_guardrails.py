"""Fail-closed regressions for synthetic scans; NOT handwriting accuracy tests."""
from decimal import Decimal

import pymupdf
import pytest

from app.routers import api as api_module
from .conftest import FixtureExtractor
from .sample_data import TICKETS, make_scan_pdf


def _upload(client, pdf=None, *, layout="grid_2x2", mode="extract"):
    response = client.post(
        "/api/batches",
        data={"layout": layout, "intake_mode": mode},
        files=[("files", ("synthetic.pdf", pdf or make_scan_pdf(), "application/pdf"))],
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _blank_pdf() -> bytes:
    with pymupdf.open() as document:
        document.new_page(width=850, height=1100)
        return document.tobytes()


def test_second_approval_of_identical_scan_is_blocked_without_double_count(client):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()
    material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()
    assert client.put("/api/rates", json={
        "supplier_id": supplier["id"], "material_id": material["id"],
        "unit_price": "30",
    }).status_code == 200

    first_batch = _upload(client)
    first = client.get(f"/api/batches/{first_batch}/tickets").json()[0]
    second_batch = _upload(client)
    duplicate = client.get(f"/api/batches/{second_batch}/tickets").json()[0]
    assert any("Identical scanned image" in reason for reason in duplicate["duplicates"])

    # No actual duplicate has been counted until a user explicitly approves it.
    assert client.post(f"/api/tickets/{first['id']}/verify").status_code == 200
    refused = client.post(f"/api/tickets/{duplicate['id']}/verify")
    assert refused.status_code == 409
    assert "Identical scanned ticket" in refused.json()["detail"]
    assert client.get(f"/api/batches/{second_batch}/tickets").json()[0]["status"] == "needs_review"
    assert client.get(f"/api/batches/{second_batch}/tickets").json()[0]["cost"] is None

    totals = client.get("/api/dashboard").json()
    assert totals["verified"] == 1
    assert Decimal(str(totals["total_tons"])) == Decimal("24.500")
    assert Decimal(str(totals["total_cost"])) == Decimal("735.00")

    # Repeating a request against the ALREADY approved original stays idempotent.
    assert client.post(f"/api/tickets/{first['id']}/verify").status_code == 200
    assert client.post(f"/api/tickets/{duplicate['id']}/flag").json()["cost"] is None
    assert client.get("/api/dashboard").json()["verified"] == 1


def test_ambiguous_ocr_tonnage_requires_manual_correction(client, monkeypatch):
    bad_ocr = dict(TICKETS[0], tonnage="1O.5")  # letter O, not zero
    monkeypatch.setattr(api_module, "get_extractor", lambda: FixtureExtractor([bad_ocr]))
    batch_id = _upload(client, make_scan_pdf(TICKETS[:1]))
    ticket = client.get(f"/api/batches/{batch_id}/tickets").json()[0]

    assert ticket["tonnage"] is None  # must NOT be misread as 1.5
    assert "Tonnage unreadable" in ticket["flags"]
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 422
    assert Decimal(str(client.get("/api/dashboard").json()["total_tons"])) == 0

    corrected = client.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": "10.5"})
    assert corrected.status_code == 200
    assert corrected.json()["tonnage"] == "10.500"
    approved = client.post(f"/api/tickets/{ticket['id']}/verify")
    assert approved.status_code == 200


@pytest.mark.parametrize("field,unrepresentable", [
    ("tonnage", "9" * 100),
    ("tonnage", "999999999.9999"),
    ("unit_price_override", "100000000"),
    ("unit_price_override", "9" * 100),
])
def test_invalid_manual_numeric_patch_is_422_and_never_modifies_audit_or_totals(
    client, field, unrepresentable,
):
    bid = _upload(client)
    tid = client.get(f"/api/batches/{bid}/tickets").json()[0]["id"]
    before = client.get(f"/api/batches/{bid}/tickets").json()[0]
    audit_before = client.get(f"/api/tickets/{tid}/audit").json()

    result = client.patch(f"/api/tickets/{tid}", json={field: unrepresentable})
    assert result.status_code == 422, result.text
    after = client.get(f"/api/batches/{bid}/tickets").json()[0]
    assert after[field] == before[field]
    assert after["status"] == before["status"]
    assert client.get(f"/api/tickets/{tid}/audit").json() == audit_before
    assert Decimal(str(client.get("/api/dashboard").json()["total_cost"])) == 0


@pytest.mark.parametrize("mode,layout", [
    ("manual", "single"),
    ("manual", "grid_2x2"),
    ("extract", "grid_2x2"),
])
def test_blank_scan_is_visible_failed_file_not_empty_success(client, mode, layout):
    batch_id = _upload(client, _blank_pdf(), layout=layout, mode=mode)
    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "failed"
    assert batch["files"][0]["status"] == "failed"
    assert "No ticket content detected" in batch["files"][0]["error"]
    assert client.get(f"/api/batches/{batch_id}/tickets").json() == []
    assert Decimal(str(client.get("/api/dashboard").json()["total_tons"])) == 0


def test_blank_scan_failure_does_not_discard_other_file_in_same_batch(client):
    result = client.post(
        "/api/batches",
        data={"layout": "grid_2x2", "intake_mode": "manual"},
        files=[
            ("files", ("blank.pdf", _blank_pdf(), "application/pdf")),
            ("files", ("valid.pdf", make_scan_pdf(), "application/pdf")),
        ],
    )
    assert result.status_code == 200
    bid = result.json()["id"]
    batch = client.get(f"/api/batches/{bid}").json()
    assert batch["status"] == "failed"  # partial failure must be visible
    assert [item["status"] for item in batch["files"]] == ["failed", "ready_for_review"]
    assert len(client.get(f"/api/batches/{bid}/tickets").json()) == 4
