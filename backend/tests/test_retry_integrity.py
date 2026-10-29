"""Regression tests: retry must not discard review work or audit history."""
from app.routers import api as api_module
from app.services.extraction import ExtractionError
from app.services.validation import ExtractedTicket
from .sample_data import TICKETS, make_scan_pdf


class FailOneExtraction:
    def __init__(self):
        self.calls = 0

    def extract(self, image):
        i = self.calls
        self.calls += 1
        if i == 1:
            raise ExtractionError("simulated provider failure")
        raw = TICKETS[i]
        return ExtractedTicket.model_validate(raw), dict(raw)


class SuccessfulExtraction:
    def __init__(self):
        self.calls = 0

    def extract(self, image):
        i = self.calls
        self.calls += 1
        raw = TICKETS[1]  # only the failed crop should be retried
        return ExtractedTicket.model_validate(raw), dict(raw)


def test_retry_preserves_verified_ticket_manual_edits_and_audit(client, monkeypatch):
    first = FailOneExtraction()
    monkeypatch.setattr(api_module, "get_extractor", lambda: first)
    upload = client.post(
        "/api/batches",
        data={"layout": "grid_2x2"},
        files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))],
    )
    assert upload.status_code == 200
    batch_id = upload.json()["id"]
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "failed"
    before = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert len(before) == 4
    assert before[1]["status"] == "failed"

    retained_id = before[0]["id"]
    corrected = client.patch(f"/api/tickets/{retained_id}", json={"tonnage": "26.25"})
    assert corrected.status_code == 200
    verified = client.post(f"/api/tickets/{retained_id}/verify")
    assert verified.status_code == 200
    audit_before = client.get(f"/api/tickets/{retained_id}/audit").json()
    assert len(audit_before) >= 2

    retry_extractor = SuccessfulExtraction()
    monkeypatch.setattr(api_module, "get_extractor", lambda: retry_extractor)
    response = client.post(f"/api/batches/{batch_id}/retry")
    assert response.status_code == 200
    after = client.get(f"/api/batches/{batch_id}/tickets").json()

    assert retry_extractor.calls == 1
    assert len(after) == 4
    assert [t["id"] for t in after] == [t["id"] for t in before]
    assert after[0]["status"] == "verified"
    assert after[0]["tonnage"] == "26.250"
    assert after[0]["tonnage_confirmed"] is True
    assert client.get(f"/api/tickets/{retained_id}/audit").json() == audit_before
    assert after[1]["status"] == "needs_review"
    assert after[1]["ticket_number"] == TICKETS[1]["ticket_number"]
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "ready_for_review"
