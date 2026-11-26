"""Document intake baseline: upload, worker processing, duplicates and failure recovery.

Usage (from backend/):
    python scripts/benchmark_intake.py                                  # 10, 50, 100, 300 tickets
    python scripts/benchmark_intake.py --tickets 10 50 --reader-ms 800 --json intake.json

Every scenario runs in its own process, on its own new SQLite database and document
folder in a temporary directory (DATABASE_URL and STORAGE_DIR are set before the app is
imported). It drives the real API in-process (FastAPI's TestClient, as seed_demo.py
does) and the real worker loop (scripts/run_worker.py, run(once=True)) with the real
pipeline: PDF rendering, 2x2 splitting, scan manifest, validation, usage rows.

Only the AI reader is replaced, by a deterministic STAND-IN that makes no network call.
It is pre-told what each crop's fictional ticket says (keyed by the crop's pixels), so
it reads perfectly, and it waits --reader-ms per call (default 0). Real reader latency,
accuracy and cost are therefore NOT measured here: no paid calls are made.

Tickets are fictional, drawn by tests/sample_data.py, four to a page (grid_2x2), in one
PDF. Times depend on the machine and its load; compare runs on the same machine.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PER_PAGE = 4
LETTER_PAGE = (1700, 2200)  # 8.5 x 11 in at 200 dpi: what tests/sample_data.make_page draws
SUPPLIER = "Example Aggregates"
MATERIALS = ("No. 57 Stone", "Base Rock", "Concrete Sand")
POLL_SECONDS = 0.02  # how often the benchmark looks for newly reviewable tickets


# ================================================================ child: one scenario

def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)  # bytes on macOS, KB on Linux


def _truth(n: int, offset: int) -> dict:
    """Fictional ticket values. No two of one run share a number or a load (date, material, tons)
    unless they are deliberately the same ticket, so every duplicate warning is a real one."""
    m = n + offset
    return {"ticket_number": f"{700000 + m}", "delivery_date": f"2026-10-{1 + m % 28:02d}",
            "material_type": MATERIALS[m % len(MATERIALS)], "tonnage": f"{18 + (m * 37) % 900 / 100:.2f}",
            "supplier": SUPPLIER, "job_location": "Fictional Bench Site"}


class Harness:
    """One throwaway workspace: database, documents, one company, its owner, a client."""

    def __init__(self, work: Path, reader_ms: float):
        self.work = work
        self.reader_ms = reader_ms
        sys.path.insert(0, str(BACKEND))
        sys.path.insert(0, str(BACKEND / "scripts"))
        from alembic import command
        from alembic.config import Config
        command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")

        from fastapi.testclient import TestClient

        from app.auth import current_user, password_hash
        from app.db import SessionLocal
        from app.main import app
        from app.models import Organization, User

        with SessionLocal() as db:
            company = Organization(name="Fictional Intake Bench Co")
            db.add(company)
            db.flush()
            owner = User(organization_id=company.id, username="bench-owner", display_name="Bench Owner",
                         password_hash=password_hash("not-a-real-password"), role="owner", active=True)
            db.add(owner)
            db.commit()
            owner_id, company_id = owner.id, company.id
        app.dependency_overrides[current_user] = lambda: User(
            id=owner_id, organization_id=company_id, username="bench-owner", display_name="Bench Owner",
            password_hash="x", active=True, role="owner", must_change_password=False)
        self.app = app
        self.client = TestClient(app)
        self.client.__enter__()
        self.client.headers.update({"X-TicketFlow-Request": "1"})
        self.api_calls = 0
        self.truth_by_crop: dict[str, dict] = {}
        self.reader = StandIn(self)

    def close(self) -> None:
        self.client.__exit__(None, None, None)
        self.app.dependency_overrides.clear()

    # ---------------------------------------------------------------- documents

    def make_pdf(self, rows: list[dict | None]) -> bytes:
        data = build_pdf(rows)
        learn(self.truth_by_crop, data, rows)
        return data

    def prepared_pdfs(self, parts: list[tuple[str, int, int]]) -> list[tuple[str, bytes]]:
        """Draw the PDFs in a separate process, so drawing them doesn't count towards this
        process's peak memory. parts: (file name, first ticket, ticket count)."""
        out = self.work / "documents"
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--prepare",
                               json.dumps({"out": str(out), "parts": parts})], cwd=BACKEND, capture_output=True, text=True)
        if done.returncode:
            raise SystemExit("Preparing the documents failed: " + done.stderr[-1500:])
        self.truth_by_crop.update(json.loads((out / "truth.json").read_text()))
        return [(name, (out / name).read_bytes()) for name, _first, _count in parts]

    # ---------------------------------------------------------------- API

    def call(self, method: str, path: str, **kwargs):
        self.api_calls += 1
        return self.client.request(method, path, **kwargs)

    def upload(self, files: list[tuple[str, bytes]], expected: int | None = None) -> tuple[object, float]:
        data = {"layout": "grid_2x2", "intake_mode": "extract"}
        if expected is not None:
            data["expected_ticket_count"] = str(expected)
        started = time.perf_counter()
        response = self.call("POST", "/api/batches", data=data,
                             files=[("files", (name, body, "application/pdf")) for name, body in files])
        return response, time.perf_counter() - started

    def tickets(self, batch_id: int) -> list[dict]:
        response = self.call("GET", f"/api/batches/{batch_id}/tickets")
        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------------------- the worker

    def process(self, batch_id: int) -> dict:
        """Run the real worker once, in a thread, watching the database for reviewable tickets."""
        import run_worker
        from sqlalchemy import func, select
        from sqlalchemy.exc import OperationalError

        from app.db import SessionLocal
        from app.models import Batch, Ticket

        run_worker.get_extractor = lambda: self.reader
        before = self.reader.calls
        self.reader.first_call = None
        outcome: dict = {}
        started = time.perf_counter()

        def work():
            outcome["rc"] = run_worker.run(once=True, poll_seconds=0.25)

        thread = threading.Thread(target=work, name="bench-worker")
        thread.start()
        first_ready = batch_done = None
        while True:
            alive = thread.is_alive()
            try:
                with SessionLocal() as db:
                    ready = db.scalar(select(func.count()).select_from(Ticket).where(
                        Ticket.batch_id == batch_id, Ticket.status == "needs_review"))
                    status = db.scalar(select(Batch.status).where(Batch.id == batch_id))
            except OperationalError:  # the worker holds SQLite's write lock this instant
                ready, status = 0, "processing"
            now = time.perf_counter() - started
            if ready and first_ready is None:
                first_ready = now
            if status not in ("uploaded", "processing") and batch_done is None:
                batch_done = now
            if not alive:
                break
            time.sleep(POLL_SECONDS)
        thread.join()
        return {"worker_exit": outcome.get("rc"), "worker_seconds": round(time.perf_counter() - started, 3),
                "first_ticket_reviewable_seconds": None if first_ready is None else round(first_ready, 3),
                "batch_complete_seconds": None if batch_done is None else round(batch_done, 3),
                "first_reader_call_seconds": None if self.reader.first_call is None else round(self.reader.first_call - started, 3),
                "reader_calls": self.reader.calls - before}

    def crop_hashes(self, batch_id: int) -> dict:
        """Not in the API's ticket view, so read from the database: {(page, position): crop sha256}."""
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import Ticket
        with SessionLocal() as db:
            return {(p, q): d for p, q, d in db.execute(select(Ticket.source_page, Ticket.position, Ticket.crop_sha256)
                                                         .where(Ticket.batch_id == batch_id))}

    def states(self, batch_id: int) -> dict:
        from collections import Counter

        from sqlalchemy import func, select

        from app.db import SessionLocal
        from app.models import Batch, ExtractionUsage, ScanSlot, Ticket, UploadedFile
        with SessionLocal() as db:
            return {
                "batch": db.scalar(select(Batch.status).where(Batch.id == batch_id)),
                "files": dict(Counter(db.scalars(select(UploadedFile.status).where(UploadedFile.batch_id == batch_id)))),
                "file_errors": sorted({e for e in db.scalars(select(UploadedFile.error).where(UploadedFile.batch_id == batch_id)) if e}),
                "tickets": dict(Counter(db.scalars(select(Ticket.status).where(Ticket.batch_id == batch_id)))),
                "scan_slots": db.scalar(select(func.count()).select_from(ScanSlot).where(ScanSlot.batch_id == batch_id)),
                "usage_rows": db.scalar(select(func.count()).select_from(ExtractionUsage).where(ExtractionUsage.batch_id == batch_id)),
            }


def build_pdf(rows: list[dict | None]) -> bytes:
    from tests.sample_data import make_page, make_pdf
    return make_pdf([make_page(rows[i:i + PER_PAGE]) for i in range(0, len(rows), PER_PAGE)])


def learn(truth_by_crop: dict, data: bytes, rows: list[dict | None]) -> None:
    """Tell the stand-in what each crop of this PDF says, rendered and split exactly as the
    worker does, one page at a time."""
    import fitz

    from app.config import settings
    from app.services import splitter
    doc = fitz.open(stream=data, filetype="pdf")
    for page_index, page in enumerate(doc):
        image = splitter.render_page(page, settings.render_dpi, settings.max_rendered_page_pixels)
        for crop in splitter.split_page(image, "grid_2x2"):
            n = page_index * PER_PAGE + crop.position
            if n < len(rows) and rows[n] is not None:
                truth_by_crop[crop_key(crop.image)] = rows[n]


def prepare(spec: dict) -> None:
    sys.path.insert(0, str(BACKEND))
    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)
    truth: dict = {}
    for name, first, count in spec["parts"]:
        rows = [_truth(n, 0) for n in range(first, first + count)]
        data = build_pdf(rows)
        learn(truth, data, rows)
        (out / name).write_bytes(data)
    (out / "truth.json").write_text(json.dumps(truth))


def _footprint() -> dict | None:
    """macOS's physical footprint of this process, now and at its peak, which (unlike RSS)
    counts memory the system has compressed. None elsewhere."""
    if sys.platform != "darwin":
        return None
    try:
        text = subprocess.run(["vmmap", "--summary", str(os.getpid())], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = {}
    for line in text.splitlines():
        for label, key in (("Physical footprint (peak):", "peak_mb"), ("Physical footprint:", "now_mb")):
            if line.strip().startswith(label):
                value = line.split(":", 1)[1].strip()
                number, unit = float(value[:-1]), value[-1].upper()
                found[key] = round(number * {"K": 1 / 1024, "M": 1, "G": 1024}.get(unit, 1 / 2**20), 1)
                break
    return found or None


def crop_key(image) -> str:
    import hashlib
    return hashlib.sha256(f"{image.size}{image.mode}".encode() + image.tobytes()).hexdigest()


class StandIn:
    """Deterministic stand-in for the AI reader. No network, no cost, no tokens reported."""

    model = "stand-in-reader (no AI)"

    def __init__(self, harness: Harness):
        self.harness = harness
        self.lock = threading.Lock()
        self.calls = 0
        self.first_call: float | None = None
        self.fail: set[str] = set()  # crop keys to fail
        self.fail_always = False
        self.seen: dict[str, int] = {}
        self.unknown = 0

    def extract(self, image):
        from app.services.extraction import ExtractionError
        from app.services.validation import ExtractedTicket
        key = crop_key(image)
        with self.lock:
            self.calls += 1
            if self.first_call is None:
                self.first_call = time.perf_counter()
            self.seen[key] = self.seen.get(key, 0) + 1
            attempt = self.seen[key]
        if self.harness.reader_ms:
            time.sleep(self.harness.reader_ms / 1000)
        if key in self.fail and (self.fail_always or attempt == 1):
            if self.fail_always:
                raise ExtractionError("Stand-in reader: simulated failure that a retry doesn't fix (benchmark).")
            raise ExtractionError("Stand-in reader: simulated temporary failure (benchmark).", kind="temporary")
        raw = self.harness.truth_by_crop.get(key)
        if raw is None:
            with self.lock:
                self.unknown += 1
            raw = {"ticket_number": None, "delivery_date": None, "material_type": None, "tonnage": None,
                   "supplier": None, "job_location": None}
        raw = dict(raw)
        return ExtractedTicket.model_validate(raw), {**raw, "_ticketflow": {
            "model": self.model, "attempts": 1, "seconds": self.harness.reader_ms / 1000}}


# ---------------------------------------------------------------- scenarios

def scenario_intake(h: Harness, tickets: int, files: int, tracemalloc_on: bool) -> dict:
    """N tickets, 4 per page, in `files` PDFs (1 = one PDF), one upload."""
    per_file = -(-tickets // files)
    per_file = -(-per_file // PER_PAGE) * PER_PAGE  # whole pages per file
    parts = [(f"bench-{tickets}-part{k + 1}.pdf", k * per_file, min(per_file, tickets - k * per_file))
             for k in range(files) if k * per_file < tickets]
    documents = h.prepared_pdfs(parts)
    pages = -(-tickets // PER_PAGE)
    idle_rss = _peak_rss_mb()
    idle_footprint = _footprint()
    if tracemalloc_on:
        import tracemalloc
        tracemalloc.start()
    response, upload_seconds = h.upload(documents, expected=tickets)
    if response.status_code != 200:
        return {"upload_status": response.status_code, "upload_detail": response.text[:300]}
    batch_id = response.json()["id"]
    run = h.process(batch_id)
    traced = None
    if tracemalloc_on:
        import tracemalloc
        traced = round(tracemalloc.get_traced_memory()[1] / (1024 * 1024), 1)
        tracemalloc.stop()
    listed_started = time.perf_counter()
    listed = h.tickets(batch_id)
    list_seconds = time.perf_counter() - listed_started
    states = h.states(batch_id)
    calls = run["reader_calls"]
    return {
        "tickets": tickets, "pages": pages, "pdf_files": len(documents),
        "upload_bytes": sum(len(body) for _name, body in documents),
        "upload_status": response.status_code, "upload_seconds": round(upload_seconds, 3), **run,
        "reader_calls_per_ticket": round(calls / tickets, 3), "reader_calls_per_page": round(calls / pages, 3),
        "unknown_crops": h.reader.unknown,
        "tickets_listed": len(listed), "ticket_list_seconds": round(list_seconds, 3),
        "tickets_with_duplicate_warnings": sum(1 for t in listed if t.get("duplicates")),
        "states": states, "rss_before_upload_mb": idle_rss, "peak_rss_mb": _peak_rss_mb(), "tracemalloc_peak_mb": traced,
        "footprint_before_upload": idle_footprint, "footprint_after": _footprint(),
    }


def scenario_duplicates(h: Harness, tickets: int) -> dict:
    """The identical file uploaded again; one page of a file inside a different batch; the same
    file twice in one upload. Each case starts from its own original, whose first ticket is approved."""
    import fitz
    out: dict = {"tickets_per_upload": tickets}

    def verify(ticket_id: int) -> dict:
        r = h.call("POST", f"/api/tickets/{ticket_id}/verify")
        detail = r.json().get("detail") if r.status_code != 200 else None
        return {"status": r.status_code, "detail": detail if isinstance(detail, (str, type(None))) else detail.get("message", str(detail))}

    def original(offset: int, name: str):
        pdf = h.make_pdf([_truth(n, offset) for n in range(tickets)])
        response, _ = h.upload([(name, pdf)])
        batch_id = response.json()["id"]
        h.process(batch_id)
        listed = sorted(h.tickets(batch_id), key=lambda t: (t["source_page"], t["position"]))
        return pdf, batch_id, listed, verify(listed[0]["id"])

    def report(first: int, a: list[dict], batch_id: int, run: dict, repeat_of: dict[tuple, dict]) -> dict:
        listed = h.tickets(batch_id)
        first_hashes = set(h.crop_hashes(first).values())
        same_crop = [k for k, digest in h.crop_hashes(batch_id).items() if digest in first_hashes]
        flagged = [t for t in listed if t.get("duplicates")]
        result = {"tickets_created": len(listed), "reader_calls": run["reader_calls"], "states": h.states(batch_id),
                  "tickets_with_identical_crop_to_original": len(same_crop),
                  "tickets_with_duplicate_warnings": len(flagged),
                  "example_warning": (flagged[0]["duplicates"][0] if flagged else None),
                  "ticket_flags_mentioning_duplicate": sum(1 for t in listed if any("uplicate" in f for f in t.get("flags") or []))}
        pairs = [(repeat_of.get((t["source_page"], t["position"])), t) for t in listed]
        copy_of_approved = next((t for o, t in pairs if o and o["id"] == a[0]["id"]), None)
        copy_of_unapproved = next((t for o, t in pairs if o and o["id"] != a[0]["id"]), None)
        if copy_of_approved:
            result["approve_copy_of_approved_ticket"] = verify(copy_of_approved["id"])
        if copy_of_unapproved:
            result["approve_copy_of_unapproved_ticket"] = verify(copy_of_unapproved["id"])
            result["then_approve_its_original"] = verify(repeat_of[(copy_of_unapproved["source_page"], copy_of_unapproved["position"])]["id"])
        return result

    # 1. The identical file, a second time, as a new upload.
    pdf, first, a, approved = original(0, "delivery-scans.pdf")
    response, upload_seconds = h.upload([("delivery-scans.pdf", pdf)])
    entry = {"original_tickets": len(a), "approve_original_first_ticket": approved, "upload_status": response.status_code,
             "upload_response_keys": sorted(response.json()) if response.status_code == 200 else response.text[:300]}
    if response.status_code == 200:
        second = response.json()["id"]
        entry.update(report(first, a, second, h.process(second), {(t["source_page"], t["position"]): t for t in a}))
    out["identical_file_again"] = entry

    # 2. Page 1 of an original again, as page 2 of a different PDF in a different batch.
    pdf, first, a, approved = original(10_000, "week-1-scans.pdf")
    mixed = fitz.open(stream=h.make_pdf([_truth(n, 50_000) for n in range(PER_PAGE)]), filetype="pdf")
    mixed.insert_pdf(fitz.open(stream=pdf, filetype="pdf"), from_page=0, to_page=0)
    response, _ = h.upload([("week-2-scans.pdf", mixed.tobytes())])
    entry = {"original_tickets": len(a), "approve_original_first_ticket": approved, "upload_status": response.status_code}
    if response.status_code == 200:
        third = response.json()["id"]
        entry.update(report(first, a, third, h.process(third), {(2, t["position"]): t for t in a if t["source_page"] == 1}))
    out["one_page_inside_another_batch"] = entry

    # 3. The same file twice in one upload request.
    pdf = h.make_pdf([_truth(n, 20_000) for n in range(tickets)])
    response, _ = h.upload([("delivery-scans.pdf", pdf), ("delivery-scans (1).pdf", pdf)])
    entry = {"upload_status": response.status_code}
    if response.status_code == 200:
        fourth = response.json()["id"]
        run = h.process(fourth)
        listed = h.tickets(fourth)
        entry.update({"tickets_created": len(listed), "reader_calls": run["reader_calls"], "states": h.states(fourth),
                      "tickets_with_duplicate_warnings": sum(1 for t in listed if t.get("duplicates"))})
    out["same_file_twice_in_one_upload"] = entry
    return out


def scenario_failures(h: Harness, tickets: int, fail_every: int) -> dict:
    """Every fail_every-th ticket's read fails: first temporarily (a retry succeeds), then for good."""
    out: dict = {"tickets": tickets, "failing_reads": len(range(0, tickets, fail_every))}
    for mode, offset in (("temporary", 0), ("permanent", 100_000)):
        rows = [_truth(n, offset) for n in range(tickets)]
        data = h.make_pdf(rows)
        h.reader.fail = {k for k, row in h.truth_by_crop.items() if row in rows[::fail_every]}
        h.reader.fail_always = mode == "permanent"
        calls_before = h.api_calls
        response, _ = h.upload([("failing-scans.pdf", data)], expected=tickets)
        batch_id = response.json()["id"]
        run = h.process(batch_id)
        after_first = h.states(batch_id)
        listed = h.tickets(batch_id)
        failed = [t for t in listed if t["status"] == "failed"]
        section = {"after_processing": {**after_first, "reader_calls": run["reader_calls"],
                                        "reviewable_while_failed": sum(1 for t in listed if t["status"] == "needs_review"),
                                        "failed_ticket_flags": sorted({f for t in failed for f in t.get("flags") or []}),
                                        "failed_ticket_error": failed[0].get("error") if failed else None}}
        recovery_calls = 0
        retry = h.call("POST", f"/api/batches/{batch_id}/retry")
        recovery_calls += 1
        section["retry"] = {"status": retry.status_code}
        if retry.status_code == 200:
            again = h.process(batch_id)
            section["retry"].update({**h.states(batch_id), "reader_calls": again["reader_calls"],
                                     "worker_seconds": again["worker_seconds"]})
        still = [t for t in h.tickets(batch_id) if t["status"] == "failed"]
        if still:
            # What is left is entered by hand: one edit per ticket (the values from the paper).
            truth = {(t["source_page"], t["position"]): rows[(t["source_page"] - 1) * PER_PAGE + t["position"]] for t in still}
            for t in still:
                row = truth[(t["source_page"], t["position"])]
                h.call("PATCH", f"/api/tickets/{t['id']}", json={k: row[k] for k in (
                    "ticket_number", "delivery_date", "material_type", "tonnage", "supplier", "job_location")})
                recovery_calls += 1
            section["entered_by_hand"] = {**h.states(batch_id), "edits": len(still)}
        section["api_calls_to_recover"] = recovery_calls
        section["api_calls_total_including_upload_and_listing"] = h.api_calls - calls_before
        out[mode] = section
    h.reader.fail = set()
    return out


def child(spec: dict) -> dict:
    work = Path(spec["work"])
    os.environ.update({
        "DATABASE_URL": f"sqlite:///{work / 'bench.db'}", "STORAGE_DIR": str(work / "storage"),
        "AI_READING_ENABLED": "true", "EXTRACTION_PROVIDER": "gemini",
        "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "",
        "RATE_LIMIT_UPLOADS_PER_MINUTE": "0", "RATE_LIMIT_REPORTS_PER_MINUTE": "0", "RATE_LIMIT_EXPORTS_PER_MINUTE": "0",
        "REVIEW_TIMING_ENABLED": "false",
        **{k: str(v) for k, v in spec.get("env", {}).items()},
    })
    assert os.environ["DATABASE_URL"].removeprefix("sqlite:///").startswith(str(work)), "benchmark database must be in its temp dir"
    harness = Harness(work, spec["reader_ms"])
    try:
        started = time.perf_counter()
        if spec["scenario"] == "intake":
            result = scenario_intake(harness, spec["tickets"], spec.get("files", 1), spec.get("tracemalloc", False))
        elif spec["scenario"] == "duplicates":
            result = scenario_duplicates(harness, spec["tickets"])
        elif spec["scenario"] == "failures":
            result = scenario_failures(harness, spec["tickets"], spec["fail_every"])
        else:
            raise SystemExit(f"unknown scenario {spec['scenario']}")
        result["scenario_seconds"] = round(time.perf_counter() - started, 2)
        from app.config import settings
        result["settings"] = {"extraction_concurrency": settings.extraction_concurrency, "max_pdf_pages": settings.max_pdf_pages,
                              "max_total_pdf_pixels": settings.max_total_pdf_pixels, "render_dpi": settings.render_dpi,
                              "reader_ms": spec["reader_ms"]}
        return result
    finally:
        harness.close()


# ================================================================ parent: run, collect, print

def run_child(spec: dict, keep: bool) -> dict:
    work = Path(tempfile.mkdtemp(prefix="quarvera-intake-bench-"))
    spec = {**spec, "work": str(work)}
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "STORAGE_DIR", "TEST_DATABASE_URL")}
    try:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", json.dumps(spec)], cwd=BACKEND,
                              env=env, capture_output=True, text=True, timeout=1800)
        lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
        if done.returncode or not lines:
            return {"error": f"exit {done.returncode}", "stderr_tail": done.stderr[-1500:]}
        return json.loads(lines[-1].removeprefix("RESULT "))
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def _machine() -> dict:
    def sysctl(name):
        try:
            return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count(),
            "hw.ncpu": sysctl("hw.ncpu"), "hw.model": sysctl("hw.model"), "cpu": sysctl("machdep.cpu.brand_string"),
            "memory_gb": round(int(sysctl("hw.memsize") or 0) / 2**30, 1) or None,
            "load_average_before": [round(x, 2) for x in os.getloadavg()]}


def _commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=BACKEND, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def _footprints(r: dict) -> str:
    before, after = r.get("footprint_before_upload") or {}, r.get("footprint_after") or {}
    return f"{_fmt(before.get('now_mb'))} -> {_fmt(after.get('peak_mb'))}"


def print_tables(report: dict) -> None:
    print("\nINTAKE (stand-in reader, %s ms per call; real reader latency NOT measured)" % report["reader_ms"])
    head = ("config", "tickets", "pages", "files", "MB", "upload s", "1st read s", "1st reviewable s", "complete s",
            "reads/ticket", "reads/page", "batch", "tickets by status", "peak RSS MB", "footprint MB idle -> peak")
    print(" | ".join(head))
    for row in report["intake"]:
        r = row["result"]
        if "error" in r or "tickets" not in r:
            print(f"{row['config']} | {row['tickets']} | failed to run: {r.get('error') or r.get('upload_detail')}")
            continue
        print(" | ".join(map(_fmt, (row["config"], r["tickets"], r["pages"], r["pdf_files"], round(r["upload_bytes"] / 2**20, 1),
                                    r["upload_seconds"], r["first_reader_call_seconds"], r["first_ticket_reviewable_seconds"],
                                    r["batch_complete_seconds"], r["reader_calls_per_ticket"], r["reader_calls_per_page"],
                                    r["states"]["batch"], r["states"]["tickets"], r["peak_rss_mb"],
                                    _footprints(r)))))
        if r["states"]["file_errors"]:
            print(f"    file error: {r['states']['file_errors'][0]}")
    if report.get("memory"):
        m = report["memory"]
        print(f"\nMEMORY, {m.get('tickets')} tickets one PDF (raised limits), tracemalloc on: "
              f"peak RSS {m.get('peak_rss_mb')} MB (idle {m.get('rss_before_upload_mb')} MB), footprint {_footprints(m)} MB, "
              f"tracemalloc peak {m.get('tracemalloc_peak_mb')} MB (Python + numpy only; PDF renderer and Pillow pixels not traced)")
    if report.get("duplicates"):
        d = report["duplicates"]
        print("\nDUPLICATES")
        for key in ("identical_file_again", "one_page_inside_another_batch", "same_file_twice_in_one_upload"):
            r = d.get(key, {})
            print(f"  {key}: upload {r.get('upload_status')}, tickets created {r.get('tickets_created')}, reader calls "
                  f"{r.get('reader_calls')}, with duplicate warnings {r.get('tickets_with_duplicate_warnings')}, "
                  f"approve copy of approved -> {(r.get('approve_copy_of_approved_ticket') or {}).get('status')}, "
                  f"approve copy of unapproved -> {(r.get('approve_copy_of_unapproved_ticket') or {}).get('status')}, "
                  f"then its original -> {(r.get('then_approve_its_original') or {}).get('status')}")
    if report.get("failures"):
        f = report["failures"]
        print(f"\nFAILURE RECOVERY ({f.get('failing_reads')} of {f.get('tickets')} reads fail)")
        for mode in ("temporary", "permanent"):
            s = f.get(mode, {})
            a = s.get("after_processing", {})
            print(f"  {mode}: after processing batch={a.get('batch')} files={a.get('files')} tickets={a.get('tickets')}; "
                  f"retry {s.get('retry', {}).get('status')} -> batch={s.get('retry', {}).get('batch')} "
                  f"tickets={s.get('retry', {}).get('tickets')} reads={s.get('retry', {}).get('reader_calls')}; "
                  f"API calls to recover: {s.get('api_calls_to_recover')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickets", type=int, nargs="+", default=[10, 50, 100, 300])
    parser.add_argument("--reader-ms", type=float, default=0, help="Stand-in reader wait per call, ms (default 0)")
    parser.add_argument("--fail-every", type=int, default=5, help="Failure scenario: every Nth ticket's read fails")
    parser.add_argument("--failure-tickets", type=int, default=40)
    parser.add_argument("--duplicate-tickets", type=int, default=8)
    parser.add_argument("--skip", nargs="*", default=[], choices=["raised", "split", "memory", "duplicates", "failures"])
    parser.add_argument("--json", type=Path)
    parser.add_argument("--keep", action="store_true", help="Keep each scenario's temp folder")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--prepare", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.prepare:
        prepare(json.loads(args.prepare))
        return 0
    if args.child:
        result = child(json.loads(args.child))
        print("RESULT " + json.dumps(result, default=str))
        return 0

    from_settings = {"max_pages": 20, "max_pixels": 70_000_000}  # app/config.py defaults at this commit
    page_pixels = LETTER_PAGE[0] * LETTER_PAGE[1]
    fits = min(from_settings["max_pages"], from_settings["max_pixels"] // page_pixels)  # letter pages one PDF may hold
    report = {"measured_at": time.strftime("%Y-%m-%d %H:%M %Z"), "commit": _commit(), "machine": _machine(),
              "reader_ms": args.reader_ms,
              "reader_note": "Deterministic stand-in reader, no network and no AI. Real reader latency, accuracy and cost are NOT measured.",
              "default_limit_letter_pages_per_pdf": fits, "intake": []}
    base = {"reader_ms": args.reader_ms}
    for n in args.tickets:
        pages = -(-n // PER_PAGE)
        print(f"intake {n} tickets ...", file=sys.stderr)
        report["intake"].append({"config": "one PDF, default limits", "tickets": n,
                                 "result": run_child({**base, "scenario": "intake", "tickets": n}, args.keep)})
        if pages > fits:
            if "raised" not in args.skip:
                env = {"MAX_PDF_PAGES": pages, "MAX_TOTAL_PDF_PIXELS": pages * page_pixels + 1}
                report["intake"].append({"config": "one PDF, limits raised (not default)", "tickets": n,
                                         "result": run_child({**base, "scenario": "intake", "tickets": n, "env": env}, args.keep)})
            if "split" not in args.skip:
                report["intake"].append({"config": f"split into PDFs of <= {fits} pages, default limits", "tickets": n,
                                         "result": run_child({**base, "scenario": "intake", "tickets": n,
                                                              "files": -(-pages // fits)}, args.keep)})
    largest = max(args.tickets)
    if "memory" not in args.skip:
        pages = -(-largest // PER_PAGE)
        print(f"memory {largest} tickets ...", file=sys.stderr)
        env = {"MAX_PDF_PAGES": max(pages, 20), "MAX_TOTAL_PDF_PIXELS": max(pages * page_pixels + 1, 70_000_000)}
        report["memory"] = run_child({**base, "scenario": "intake", "tickets": largest, "env": env, "tracemalloc": True}, args.keep)
    if "duplicates" not in args.skip:
        print("duplicates ...", file=sys.stderr)
        report["duplicates"] = run_child({**base, "scenario": "duplicates", "tickets": args.duplicate_tickets}, args.keep)
    if "failures" not in args.skip:
        print("failures ...", file=sys.stderr)
        report["failures"] = run_child({**base, "scenario": "failures", "tickets": args.failure_tickets,
                                        "fail_every": args.fail_every}, args.keep)
    report["load_average_after"] = [round(x, 2) for x in os.getloadavg()]
    print_tables(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
