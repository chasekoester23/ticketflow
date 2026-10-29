"""A delivery batch must be complete, visible, and recoverable.

These tests use synthetic scans and a stand-in extractor, not real handwriting.
"""
from pathlib import Path

from app.config import settings
from app.routers import api as api_module
from app.services import pipeline
from .sample_data import make_scan_pdf


def _post(client, files):
    return client.post(
        "/api/batches",
        data={"layout": "grid_2x2"},
        files=[("files", (name, data, mimetype)) for name, data, mimetype in files],
    )


def test_mixed_invalid_upload_is_atomic_and_names_every_rejected_file(client):
    good = make_scan_pdf()
    response = _post(client, [
        ("good.pdf", good, "application/pdf"),
        ("notes.txt", b"unrelated", "text/plain"),
        ("empty.pdf", b"", "application/pdf"),
        ("fake.jpg", b"not a jpg", "image/jpeg"),
    ])
    assert response.status_code == 400
    message = response.json()["detail"]
    assert "no files were processed" in message.lower()
    for filename in ("notes.txt", "empty.pdf", "fake.jpg"):
        assert filename in message
    assert "good.pdf" not in message  # good PDF is not itself invalid
    assert client.get("/api/batches").json() == []
    assert not (settings.storage_dir / "uploads").exists()


def test_valid_multi_file_upload_keeps_every_file_and_ticket(client):
    response = _post(client, [
        ("scan-a.pdf", make_scan_pdf(), "application/pdf"),
        ("scan-b.pdf", make_scan_pdf(), "application/pdf"),
    ])
    assert response.status_code == 200
    bid = response.json()["id"]
    batch = client.get(f"/api/batches/{bid}").json()
    assert batch["status"] == "ready_for_review"
    assert {f["original_name"] for f in batch["files"]} == {"scan-a.pdf", "scan-b.pdf"}
    assert all(f["status"] == "ready_for_review" for f in batch["files"])
    assert len(client.get(f"/api/batches/{bid}/tickets").json()) == 8


def test_retry_requires_failure_and_cannot_reprocess_successful_batch(client):
    upload = _post(client, [("ok.pdf", make_scan_pdf(), "application/pdf")])
    bid = upload.json()["id"]
    before = client.get(f"/api/batches/{bid}/tickets").json()
    retry = client.post(f"/api/batches/{bid}/retry")
    assert retry.status_code == 409
    assert "Only failed batches" in retry.json()["detail"]
    after = client.get(f"/api/batches/{bid}/tickets").json()
    assert [t["id"] for t in after] == [t["id"] for t in before]


def test_processing_error_on_one_file_preserves_other_files(client, monkeypatch):
    original = pipeline.process_file
    call_count = 0

    def fail_first(db, file, layout, extractor):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("synthetic renderer crash")
        return original(db, file, layout, extractor)

    monkeypatch.setattr(pipeline, "process_file", fail_first)
    result = _post(client, [
        ("bad-one.pdf", make_scan_pdf(), "application/pdf"),
        ("good-one.pdf", make_scan_pdf(), "application/pdf"),
    ])
    assert result.status_code == 200
    bid = result.json()["id"]
    batch = client.get(f"/api/batches/{bid}").json()
    assert batch["status"] == "failed"
    assert [f["status"] for f in batch["files"]] == ["failed", "ready_for_review"]
    assert "retry" in batch["files"][0]["error"].lower()
    good_tickets = client.get(f"/api/batches/{bid}/tickets").json()
    assert len(good_tickets) == 4
    kept_ids = {t["id"] for t in good_tickets}

    monkeypatch.setattr(pipeline, "process_file", original)
    retry = client.post(f"/api/batches/{bid}/retry")
    assert retry.status_code == 200
    batch_after = client.get(f"/api/batches/{bid}").json()
    assert batch_after["status"] == "ready_for_review"
    after = client.get(f"/api/batches/{bid}/tickets").json()
    assert len(after) == 8
    assert kept_ids.issubset({t["id"] for t in after})
    assert all(t["status"] == "needs_review" for t in after)
    assert client.post(f"/api/batches/{bid}/retry").status_code == 409


def test_unexpected_batch_crash_is_visible_and_retryable(client, monkeypatch):
    original = pipeline.process_batch

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic process-wide crash")

    monkeypatch.setattr(pipeline, "process_batch", crash)
    upload = _post(client, [("scan.pdf", make_scan_pdf(), "application/pdf")])
    assert upload.status_code == 200
    bid = upload.json()["id"]
    batch = client.get(f"/api/batches/{bid}").json()
    assert batch["status"] == "failed"
    assert batch["files"][0]["status"] == "failed"
    assert "retry" in batch["files"][0]["error"].lower()

    monkeypatch.setattr(pipeline, "process_batch", original)
    assert client.post(f"/api/batches/{bid}/retry").status_code == 200
    assert client.get(f"/api/batches/{bid}").json()["status"] == "ready_for_review"
    assert len(client.get(f"/api/batches/{bid}/tickets").json()) == 4
