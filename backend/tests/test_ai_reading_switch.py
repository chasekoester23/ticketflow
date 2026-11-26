"""AI_READING_ENABLED=false: a workspace (a public demo, say) where nothing can spend on
an AI reader, whatever keys the server happens to hold. Synthetic scans only."""
import pytest

from app.config import settings
from app.services import extraction, jobqueue

from .sample_data import make_scan_pdf
from .test_durable_jobs import _db, _pending_upload


@pytest.fixture
def reading_off(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "a-key-that-must-never-be-used")
    monkeypatch.setattr(settings, "anthropic_api_key", "a-key-that-must-never-be-used")
    monkeypatch.setattr(settings, "ai_reading_enabled", False)

    def no_provider(*_args, **_kwargs):
        raise AssertionError("A reader was built while automatic reading is off")
    from app.services import benchmark_providers
    monkeypatch.setattr(benchmark_providers, "GeminiBenchmarkExtractor", no_provider)
    monkeypatch.setattr(extraction, "AnthropicExtractor", no_provider)


def test_an_upload_to_be_read_is_refused_before_anything_is_stored(client, reading_off):
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "extract"},
                           files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
    assert response.status_code == 409 and "turned off" in response.text
    assert client.get("/api/batches").json() == []


def test_entry_by_hand_still_works(client, reading_off):
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual"},
                           files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
    assert response.status_code == 200, response.text


def test_the_app_is_told_reading_is_off_even_with_a_key_present(client, reading_off):
    status = client.get("/api/status").json()
    assert status["extraction_configured"] is False and status["reading_disabled"] is True
    assert "a-key" not in str(status)


def test_the_reader_is_never_built_with_reading_off(reading_off):
    with pytest.raises(extraction.ExtractionError, match="turned off"):
        extraction.get_extractor()


def test_a_batch_queued_before_reading_was_turned_off_fails_visibly_without_a_call(client, monkeypatch):
    batch_id = _pending_upload(client, monkeypatch)  # queued while reading was on
    monkeypatch.setattr(settings, "ai_reading_enabled", False)
    with _db() as db:
        claimed = jobqueue.claim_next(db)
        jobqueue.execute_claimed(db, *claimed, extractor_factory=extraction.get_extractor)
    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "failed" and "turned off" in batch["files"][0]["error"]
    assert client.get(f"/api/batches/{batch_id}/tickets").json() == []
