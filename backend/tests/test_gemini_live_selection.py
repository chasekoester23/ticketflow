"""Offline integration coverage for Gemini as TicketFlow's primary extractor.

Fake client/test data are NEVER evidence of real handwriting accuracy.
"""
from decimal import Decimal

from app.config import settings
from app.routers import api as api_module
from app.services import benchmark_providers, extraction
from app.services.extraction import ExtractionError
from .conftest import FixtureExtractor
from .sample_data import make_scan_pdf


def _upload(client, mode):
    return client.post(
        "/api/batches",
        data={"layout": "grid_2x2", "intake_mode": mode},
        files=[("files", ("synthetic.pdf", make_scan_pdf(), "application/pdf"))],
    )


def test_gemini_is_default_provider_and_api_status_tracks_its_key(client, monkeypatch):
    monkeypatch.setattr(settings, "extraction_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "unused-legacy-test-key")

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json() == {
        "extraction_configured": False, "model": settings.gemini_model,
    }

    monkeypatch.setattr(settings, "gemini_api_key", "fictional-test-key")
    assert client.get("/api/status").json()["extraction_configured"] is True
    assert "fictional-test-key" not in str(client.get("/api/status").json())


def test_gemini_selected_by_durable_worker_without_hidden_anthropic_calls(client, monkeypatch):
    monkeypatch.setattr(settings, "extraction_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "fictional-test-key")
    called = []

    def fake_gemini(*, model):
        called.append(model)
        return FixtureExtractor()

    monkeypatch.setattr(benchmark_providers, "GeminiBenchmarkExtractor", fake_gemini)
    monkeypatch.setattr(api_module, "get_extractor", extraction.get_extractor)
    response = _upload(client, "extract")
    assert response.status_code == 200
    batch_id = response.json()["id"]

    tickets = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert len(tickets) == 4
    assert called == [settings.gemini_model]
    assert all(t["status"] == "needs_review" for t in tickets)
    assert all(not t["tonnage_confirmed"] for t in tickets)
    assert Decimal(str(client.get("/api/dashboard").json()["total_tons"])) == 0


def test_missing_gemini_key_marks_ai_batch_failed_but_manual_remains_available(client, monkeypatch):
    monkeypatch.setattr(settings, "extraction_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(api_module, "get_extractor", extraction.get_extractor)

    failed = _upload(client, "extract")
    assert failed.status_code == 200
    batch = client.get(f"/api/batches/{failed.json()['id']}").json()
    assert batch["status"] == "failed"
    assert "GEMINI_API_KEY" in batch["files"][0]["error"]
    assert client.get(f"/api/batches/{batch['id']}/tickets").json() == []

    manual = _upload(client, "manual")
    assert manual.status_code == 200
    manual_batch = client.get(f"/api/batches/{manual.json()['id']}").json()
    assert manual_batch["status"] == "ready_for_review"
    tickets = client.get(f"/api/batches/{manual_batch['id']}/tickets").json()
    assert len(tickets) == 4
    assert all(t["status"] == "needs_review" and t["tonnage"] is None for t in tickets)


def test_unknown_provider_cannot_silently_fallback_to_another_paid_vendor(monkeypatch):
    monkeypatch.setattr(settings, "extraction_provider", "typo")
    try:
        extraction.get_extractor()
    except ExtractionError as exc:
        assert "EXTRACTION_PROVIDER" in str(exc)
    else:
        raise AssertionError("Invalid provider must fail, not call a fallback")
