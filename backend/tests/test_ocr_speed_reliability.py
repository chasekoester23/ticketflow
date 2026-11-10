"""Reading several tickets at once, retrying Gemini's temporary failures, and never
losing or mixing up a result. Fictional data and stand-in readers only: no API calls."""
from __future__ import annotations

import hashlib
import io
import threading
import time
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.models import Batch, Ticket
from app.routers import api as api_module
from app.services import extraction_retry, jobqueue, pipeline, splitter
from app.services.benchmark_providers import GeminiBenchmarkExtractor, encode_image
from app.services.extraction import ExtractionError
from app.services.validation import ExtractedTicket

from .sample_data import TICKETS, make_scan_page
from .test_b_worker_reliability import _db


def _pdf(pages: list[list[dict]]) -> bytes:
    images = [make_scan_page(tickets) for tickets in pages]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", resolution=200, save_all=True, append_images=images[1:])
    return buffer.getvalue()


def _numbered(count: int) -> list[list[dict]]:
    """Fictional tickets 70001, 70002, ... four to a page."""
    tickets = [{**TICKETS[i % len(TICKETS)], "ticket_number": str(70001 + i)} for i in range(count)]
    return [tickets[i:i + 4] for i in range(0, count, 4)]


def _identities(pdf: bytes) -> dict[str, str]:
    """What each crop on the scan should be read as, keyed by its pixels."""
    found = {}
    pages = splitter.render_pdf_pages(pdf, dpi=settings.render_dpi, max_pages=20,
                                      max_page_pixels=settings.max_rendered_page_pixels,
                                      max_total_pixels=settings.max_total_pdf_pixels)
    number = 70001
    for page in pages:
        for crop in splitter.split_page(page, "grid_2x2"):
            found[hashlib.sha256(crop.image.tobytes()).hexdigest()] = str(number)
            number += 1
    return found


class PixelReader:
    """Reads a crop by recognising its pixels, so a result can only land on the right ticket.
    Takes a varying time per crop and records how many reads overlap."""

    def __init__(self, identities, *, delay=lambda n: 0.05, fail=()):
        self.identities, self.delay, self.fail = identities, delay, set(fail)
        self.lock = threading.Lock()
        self.active = self.peak = self.calls = 0
        self.order: list[str] = []
        self.threads: set[str] = set()

    def extract(self, image):
        number = self.identities[hashlib.sha256(image.tobytes()).hexdigest()]
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.current_thread().name)
        try:
            time.sleep(self.delay(int(number)))
            if number in self.fail:
                raise ExtractionError("Gemini request failed: a temporary problem on Gemini's side (503).", kind="temporary", status=503)
            raw = {**TICKETS[0], "ticket_number": number}
            return ExtractedTicket.model_validate(raw), dict(raw)
        finally:
            with self.lock:
                self.active -= 1
                self.order.append(number)


@pytest.fixture()
def concurrency(monkeypatch):
    def set_to(value):
        monkeypatch.setattr(settings, "extraction_concurrency", value)
    set_to(4)
    return set_to


def _upload(client, monkeypatch, pdf: bytes) -> int:
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    response = client.post("/api/batches", data={"layout": "grid_2x2"}, files=[("files", ("scan.pdf", pdf, "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _run(batch_id, reader):
    with _db() as db:
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=lambda: reader)


def _tickets(batch_id):
    with _db() as db:
        return list(db.scalars(select(Ticket).where(Ticket.batch_id == batch_id).order_by(Ticket.source_page, Ticket.position)))


# ------------------------------------------------------------------ reading in parallel

def test_twenty_tickets_are_read_four_at_a_time_and_each_result_lands_on_its_own_ticket(client, monkeypatch, concurrency):
    pdf = _pdf(_numbered(20))
    reader = PixelReader(_identities(pdf), delay=lambda n: 0.02 + (n % 5) * 0.03)
    batch_id = _upload(client, monkeypatch, pdf)
    started = time.perf_counter()
    _run(batch_id, reader)
    elapsed = time.perf_counter() - started
    tickets = _tickets(batch_id)
    assert [t.ticket_number for t in tickets] == [str(70001 + i) for i in range(20)]
    assert all(t.status == "needs_review" and t.tonnage_confirmed is False for t in tickets)
    assert reader.calls == 20 and 2 <= reader.peak <= 4
    assert "MainThread" not in reader.threads  # only the reader runs elsewhere; the database stays on this thread
    sequential = sum(0.02 + ((70001 + i) % 5) * 0.03 for i in range(20))
    assert elapsed < sequential * 0.75
    with _db() as db:
        assert db.get(Batch, batch_id).status == "ready_for_review"


def test_one_at_a_time_still_works_exactly_as_before(client, monkeypatch, concurrency):
    concurrency(1)
    pdf = _pdf(_numbered(8))
    reader = PixelReader(_identities(pdf))
    batch_id = _upload(client, monkeypatch, pdf)
    _run(batch_id, reader)
    assert reader.peak == 1 and reader.threads == {"MainThread"}
    assert [t.ticket_number for t in _tickets(batch_id)] == [str(70001 + i) for i in range(8)]


def test_results_are_saved_in_scan_order_even_when_they_finish_out_of_order(client, monkeypatch, concurrency):
    """So tickets only ever appear at the end of the list, and nobody is moved off theirs."""
    pdf = _pdf(_numbered(8))
    reader = PixelReader(_identities(pdf), delay=lambda n: (70009 - n) * 0.03)  # the first crop finishes last
    saved: list[str] = []
    real_apply = pipeline._Reader._apply

    def spy(self, job, result):
        real_apply(self, job, result)
        saved.append(job.ticket.ticket_number)

    monkeypatch.setattr(pipeline._Reader, "_apply", spy)
    batch_id = _upload(client, monkeypatch, pdf)
    _run(batch_id, reader)
    assert reader.order[:4] != sorted(reader.order[:4])  # they really did finish out of order
    assert saved == [str(70001 + i) for i in range(8)]


def test_a_ticket_is_reviewable_while_the_rest_are_still_being_read(client, monkeypatch, concurrency):
    pdf = _pdf(_numbered(8))
    release = threading.Event()
    reader = PixelReader(_identities(pdf), delay=lambda n: 0 if n == 70001 else (release.wait(10) and 0))
    batch_id = _upload(client, monkeypatch, pdf)
    worker = threading.Thread(target=_run, args=(batch_id, reader))
    worker.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not client.get(f"/api/batches/{batch_id}/tickets").json():
            time.sleep(0.05)
        rows = client.get(f"/api/batches/{batch_id}/tickets").json()
        batch = client.get(f"/api/batches/{batch_id}").json()
        assert [r["ticket_number"] for r in rows] == ["70001"] and rows[0]["status"] == "needs_review"
        assert batch["status"] == "processing"
        assert batch["progress"] == {"expected": 8, "read": 1, "failed": 0, "reading_again": 0, "files_waiting": 0}
    finally:
        release.set()
        worker.join(30)
    assert client.get(f"/api/batches/{batch_id}").json()["progress"] is None


def test_one_ticket_failing_doesnt_stop_the_others_and_a_retry_reads_only_that_one(client, monkeypatch, concurrency):
    pdf = _pdf(_numbered(8))
    identities = _identities(pdf)
    batch_id = _upload(client, monkeypatch, pdf)
    _run(batch_id, PixelReader(identities, fail={"70003"}))
    tickets = _tickets(batch_id)
    assert [t.status for t in tickets].count("failed") == 1
    failed = next(t for t in tickets if t.status == "failed")
    assert "temporary problem" in failed.error and failed.ticket_number is None
    kept = {t.id: t.ticket_number for t in tickets if t.status != "failed"}
    assert client.post(f"/api/batches/{batch_id}/retry").status_code == 200
    again = PixelReader(identities)
    _run(batch_id, again)
    assert again.calls == 1
    tickets = _tickets(batch_id)
    assert [t.ticket_number for t in tickets] == [str(70001 + i) for i in range(8)]
    assert all(t.status == "needs_review" for t in tickets)
    assert {t.id: t.ticket_number for t in tickets if t.id in kept} == kept  # the ones read the first time are untouched


def test_a_person_entering_a_failed_ticket_while_it_is_read_again_keeps_their_values(client, monkeypatch, concurrency):
    pdf = _pdf(_numbered(4))
    identities = _identities(pdf)
    batch_id = _upload(client, monkeypatch, pdf)
    _run(batch_id, PixelReader(identities, fail={"70002"}))
    failed = next(t for t in _tickets(batch_id) if t.status == "failed")
    assert client.post(f"/api/batches/{batch_id}/retry").status_code == 200

    class TypedMeanwhile(PixelReader):
        def extract(self, image):
            # A person enters the ticket by hand while Gemini is still reading it.
            with _db() as db:
                ticket = db.get(Ticket, failed.id)
                ticket.ticket_number, ticket.tonnage = "TYPED-BY-HAND", ticket.tonnage
                db.commit()
            return super().extract(image)

    _run(batch_id, TypedMeanwhile(identities))
    with _db() as db:
        assert db.get(Ticket, failed.id).ticket_number == "TYPED-BY-HAND"


def test_a_refused_key_stops_the_batch_instead_of_failing_every_ticket_slowly(client, monkeypatch, concurrency):
    concurrency(2)
    calls = []

    def refuse(**_request):
        calls.append(1)
        raise genai_errors.ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "private"}}, None)

    reader = GeminiBenchmarkExtractor(model="gemini-test", client=SimpleNamespace(models=SimpleNamespace(generate_content=refuse)),
                                      sleep=lambda _seconds: None)
    batch_id = _upload(client, monkeypatch, _pdf(_numbered(8)))
    _run(batch_id, reader)
    tickets = _tickets(batch_id)
    assert len(calls) <= 2
    assert all(t.status == "failed" and "API key" in t.error for t in tickets)
    assert "private" not in " ".join(t.error for t in tickets)


def test_a_manual_batch_never_starts_a_reader(client, monkeypatch, concurrency):
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual"},
                           files=[("files", ("scan.pdf", _pdf(_numbered(4)), "application/pdf"))])
    batch_id = response.json()["id"]

    def never():
        raise AssertionError("manual intake must not create a reader")

    with _db() as db:
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=never)
    assert [t.status for t in _tickets(batch_id)] == ["needs_review"] * 4


# ------------------------------------------------------------------ retrying Gemini

def _gemini(*outcomes, **kwargs):
    """A stand-in Gemini that answers with each outcome in turn: an exception or JSON text."""
    calls = []

    def generate_content(**request):
        calls.append(request)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome, usage_metadata=SimpleNamespace(prompt_token_count=1290, candidates_token_count=60, thoughts_token_count=400))

    waits = []
    reader = GeminiBenchmarkExtractor(model="gemini-test", client=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)),
                                      sleep=waits.append, rand=lambda: 1.0, **kwargs)
    return reader, calls, waits


def _answer():
    import json
    return json.dumps(TICKETS[0])


def _server(code, status="UNAVAILABLE"):
    return genai_errors.ServerError(code, {"error": {"code": code, "status": status, "message": "a private detail"}}, None)


def _client_error(code, status, details=None):
    body = {"error": {"code": code, "status": status, "message": "a private detail"}}
    if details:
        body["error"]["details"] = details
    return genai_errors.ClientError(code, body, None)


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_a_temporary_server_error_is_retried_and_the_ticket_is_read(code):
    reader, calls, waits = _gemini(_server(code), _server(code), _answer())
    ticket, raw = reader.extract(Image.new("RGB", (400, 400), "white"))
    assert ticket.ticket_number == TICKETS[0]["ticket_number"]
    assert len(calls) == 3 and raw["_ticketflow"]["attempts"] == 3
    assert waits == [1.0, 2.0]  # exponential: base, then double (rand fixed at the top of each range)


def test_retries_stop_at_the_limit_with_a_plain_message():
    reader, calls, _waits = _gemini(_server(503))
    with pytest.raises(ExtractionError) as failure:
        reader.extract(Image.new("RGB", (400, 400), "white"))
    assert len(calls) == settings.extraction_max_attempts
    assert failure.value.kind == "temporary" and failure.value.attempts == 3
    assert "temporary problem on Gemini's side (503)" in str(failure.value) and "Tried 3 times" in str(failure.value)
    assert "private detail" not in str(failure.value)


def test_a_rate_limit_waits_as_long_as_gemini_asks_within_a_cap():
    retry_info = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]
    clock = [0.0]
    reader, calls, waits = _gemini(_client_error(429, "RESOURCE_EXHAUSTED", retry_info), _answer())
    reader._clock = lambda: clock[0]
    reader._sleep = lambda seconds: (waits.append(seconds), clock.__setitem__(0, clock[0] + seconds))
    reader.extract(Image.new("RGB", (400, 400), "white"))
    assert len(calls) == 2 and waits == [7.0]


def test_a_rate_limit_slows_every_thread_not_just_the_one_that_hit_it():
    gate = extraction_retry.RateGate()
    gate.hold(5.0, now=100.0)
    assert gate.remaining(101.0) == pytest.approx(4.0)
    assert gate.remaining(106.0) == 0.0


@pytest.mark.parametrize("code,status,kind,words", [
    (400, "INVALID_ARGUMENT", "rejected", "rejected this request (400)"),
    (401, "UNAUTHENTICATED", "configuration", "refused the API key (401)"),
    (403, "PERMISSION_DENIED", "configuration", "refused the API key (403)"),
    (404, "NOT_FOUND", "configuration", "doesn't recognise the model 'gemini-test' (404)"),
])
def test_a_bad_request_or_key_is_never_retried(code, status, kind, words):
    reader, calls, waits = _gemini(_client_error(code, status), _answer())
    with pytest.raises(ExtractionError) as failure:
        reader.extract(Image.new("RGB", (400, 400), "white"))
    assert len(calls) == 1 and waits == []
    assert failure.value.kind == kind and words in str(failure.value) and "Not retried" in str(failure.value)


def test_after_a_refused_key_no_further_calls_are_made():
    reader, calls, _ = _gemini(_client_error(403, "PERMISSION_DENIED"), _answer())
    for _ in range(3):
        with pytest.raises(ExtractionError):
            reader.extract(Image.new("RGB", (400, 400), "white"))
    assert len(calls) == 1


def test_a_timeout_or_dropped_connection_is_retried():
    reader, calls, _ = _gemini(TimeoutError("private"), ConnectionError("private"), _answer())
    reader.extract(Image.new("RGB", (400, 400), "white"))
    assert len(calls) == 3


def test_a_malformed_answer_is_not_paid_for_twice():
    reader, calls, _ = _gemini("not json at all")
    with pytest.raises(ExtractionError, match="malformed JSON"):
        reader.extract(Image.new("RGB", (400, 400), "white"))
    assert len(calls) == 1


def test_the_jitter_keeps_parallel_retries_apart_and_never_exceeds_the_cap():
    waits = {round(extraction_retry.backoff_seconds(attempt, base=1.0, cap=20.0, rand=lambda r=r: r), 3)
             for attempt in (1, 2, 3, 6) for r in (0.0, 0.5, 1.0)}
    assert min(waits) == 0.5 and max(waits) == 20.0


def test_tokens_attempts_and_time_are_recorded_for_cost_tracking():
    reader, _calls, _ = _gemini(_answer())
    _ticket, raw = reader.extract(Image.new("RGB", (400, 400), "white"))
    meta = raw["_ticketflow"]
    assert (meta["prompt_tokens"], meta["output_tokens"], meta["thinking_tokens"], meta["attempts"]) == (1290, 60, 400, 1)
    assert set(raw) - {"_ticketflow"} == set(TICKETS[0])


# ------------------------------------------------------------------ the image Gemini receives

def test_lossless_webp_sends_exactly_the_same_pixels_as_png():
    page = make_scan_page(TICKETS)
    crop = splitter.split_page(page, "grid_2x2")[0].image
    png, png_type = encode_image(crop, fmt="png", max_side=1800)
    webp, webp_type = encode_image(crop, fmt="webp_lossless", max_side=1800)
    assert (png_type, webp_type) == ("image/png", "image/webp")
    assert Image.open(io.BytesIO(png)).convert("RGB").tobytes() == Image.open(io.BytesIO(webp)).convert("RGB").tobytes()


def test_the_image_setting_reaches_the_request(monkeypatch):
    monkeypatch.setattr(settings, "gemini_thinking_level", "low")
    reader, calls, _ = _gemini(_answer(), image_format="jpeg")
    reader.extract(Image.new("RGB", (2400, 1000), "white"))
    part = calls[0]["contents"][1].inline_data
    assert part.mime_type == "image/jpeg" and max(Image.open(io.BytesIO(part.data)).size) == 1800
    assert calls[0]["config"]["thinking_config"] == {"thinking_level": "low"}
