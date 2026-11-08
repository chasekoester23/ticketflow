"""Things going wrong for real: killed processes, damaged files, a database that stops
answering. Each must recover safely or say what's wrong, and must never silently lose
a record or report success it didn't have. Approved tickets must survive all of it.

Fictional data only. Passing says nothing about a real disk, network or database host.
"""
from __future__ import annotations

import os
import stat
import threading
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base, engine_options, normalized_url
from app.db import get_db
from app.main import app
from app.models import Batch, InvoiceDocument, ProcessingJob, ScanSlot, Ticket, UploadedFile

from .conftest import TEST_DATABASE_URL, _reset_postgres
from .fixtures_stack import Stack, build_stack, scan_batch, wait_until
from .sample_data import TICKETS, make_page, make_pdf
from .sample_invoices import letterhead_supplier
from .test_concurrency import race  # noqa: F401  (a fixture: real file database, real threads)
from .test_query_budgets import _seed, _session

postgres_only = pytest.mark.skipif(not TEST_DATABASE_URL, reason="Needs PostgreSQL's own connection handling")
sqlite_only = pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="SQLite's single-writer file lock")
not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


@pytest.fixture()
def stack(tmp_path, postgres_engine):
    running = build_stack(tmp_path, postgres_engine, TEST_DATABASE_URL, _reset_postgres)
    running.start_server()
    try:
        yield running
    finally:
        running.stop_everything()
        if postgres_engine is None:
            running.engine.dispose()


def _job(stack: Stack, batch_id: int) -> ProcessingJob:
    with stack.Session() as db:
        return db.scalar(select(ProcessingJob).where(ProcessingJob.batch_id == batch_id).order_by(ProcessingJob.id.desc()))


def _all_crops_are_readable(stack: Stack, batch_id: int) -> list[str]:
    bad = []
    with stack.Session() as db:
        for path, sha in db.execute(select(ScanSlot.crop_path, ScanSlot.crop_sha256).where(ScanSlot.batch_id == batch_id)):
            try:
                with Image.open(path) as image:
                    image.load()
            except OSError:
                bad.append(Path(path).name)
    return bad


# ------------------------------------------------------------ kill -9 of the worker

@pytest.mark.parametrize("moment", ["after_claim", "mid_file", "between_files", "before_finish"])
def test_a_worker_killed_mid_batch_finishes_after_restart_with_nothing_lost_or_doubled(stack, moment):
    """Three files, four tickets each. Killed for real, then a new worker takes over."""
    client = stack.signed_in()
    done_earlier = scan_batch(client, 1)
    stack.start_worker().pid  # An ordinary worker does some real work first...
    stack.wait_for_batch(client, done_earlier, "ready_for_review")
    approved = stack.batch_tickets(client, done_earlier)[0]
    stack.approve(client, approved["id"])
    os.kill(stack.workers[-1].pid, 9)
    stack.workers[-1].wait(10)
    before = stack.verified()
    assert len(before) == 1

    batch_id = scan_batch(client, 3)
    worker = stack.start_worker(pause_at=moment)
    wait_until(stack.paused, seconds=120, what=f"the worker to reach {moment}")
    assert _job(stack, batch_id).status == "running"
    os.kill(worker.pid, 9)
    assert worker.wait(10) == -9
    stack.clear_pause()
    # Nothing has happened behind anyone's back: the batch still says it isn't done.
    assert client.get(f"/api/batches/{batch_id}").json()["status"] != "failed"

    stack.start_worker()
    # The batch can read as done a moment before the job records that it is.
    wait_until(lambda: _job(stack, batch_id).status == "finished", seconds=90, what="the job to finish")
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    tickets = stack.batch_tickets(client, batch_id)
    assert len(tickets) == 12, "every slot of every file has exactly one record"
    assert len({(t["file_id"], t["source_page"], t["position"]) for t in tickets}) == 12
    with stack.Session() as db:
        assert db.scalar(select(func.count()).select_from(ScanSlot).where(ScanSlot.batch_id == batch_id)) == 12
        assert {f.status for f in db.scalars(select(UploadedFile).where(UploadedFile.batch_id == batch_id))} == {"ready_for_review"}
    job = _job(stack, batch_id)
    assert (job.status, job.attempts) == ("finished", 2)
    assert _all_crops_are_readable(stack, batch_id) == []
    assert stack.verified() == before
    assert stack.approvals_on_record(approved["id"]) == 1


@pytest.mark.xfail(strict=True, reason="BUG: record_slot keeps a crop file that a killed worker left half written, so the ticket's image is broken for good and its hash isn't the crop's")
def test_a_worker_killed_while_writing_a_crop_doesnt_leave_a_broken_image(stack):
    """write_private_png writes straight to the final name, and record_slot trusts any
    file that exists. Writing to a temporary name and renaming would close this."""
    client = stack.signed_in()
    batch_id = scan_batch(client, 3)
    worker = stack.start_worker(pause_at="mid_crop_write")
    wait_until(stack.paused, seconds=120, what="the worker to be writing a crop")
    os.kill(worker.pid, 9)
    worker.wait(10)
    stack.start_worker()
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    assert len(stack.batch_tickets(client, batch_id)) == 12
    assert _all_crops_are_readable(stack, batch_id) == []


def test_a_killed_worker_reads_again_only_what_it_never_saved(stack):
    """AI reads cost money, and a reviewer's work on a saved file must never be redone."""
    client = stack.signed_in()
    batch_id = scan_batch(client, 3, intake="extract")
    worker = stack.start_worker(pause_at="between_files")
    wait_until(stack.paused, seconds=120, what="the first file to be saved")
    log = stack.work / "reads.log"
    assert log.read_text().count("read") == 4
    os.kill(worker.pid, 9)
    worker.wait(10)
    stack.clear_pause()
    # A person corrects a record from the finished file while the worker is down.
    first = stack.batch_tickets(client, batch_id)[0]
    assert first["tonnage"] == "20.000"
    approved = stack.approve(client, first["id"], tonnage="31.250")
    before = stack.verified()
    stack.start_worker(stand_in_reader=True)
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    assert log.read_text().count("read") == 12, "four reads for each of three files, none repeated"
    assert len(stack.batch_tickets(client, batch_id)) == 12
    assert stack.verified() == before and Decimal(approved["tonnage"]) == Decimal("31.250")


def test_a_second_worker_refuses_to_start_beside_a_live_one(stack):
    client = stack.signed_in()
    batch_id = scan_batch(client, 1)
    first = stack.start_worker(pause_at="after_claim")
    wait_until(stack.paused, seconds=60, what="the first worker to claim the job")
    second = stack.start_worker()
    assert second.wait(30) == 2
    assert _job(stack, batch_id).status == "running", "the second worker must not reclaim a job someone holds"
    os.kill(first.pid, 9)
    first.wait(10)


# ------------------------------------------------------------ the server restarting mid-review

def test_a_server_killed_mid_review_keeps_what_was_saved_and_the_session(stack):
    client = stack.signed_in()
    batch_id = scan_batch(client, 1)
    stack.start_worker()
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    first, second, *_ = stack.batch_tickets(client, batch_id)
    stack.approve(client, first["id"])
    edited = client.patch(f"/api/tickets/{second['id']}", json={"tonnage": "22.500", "ticket_number": "SAVED"}).json()
    stale_revision = second["revision"]
    before = stack.verified()

    stack.kill_server()
    with pytest.raises(httpx.TransportError):
        client.get("/api/dashboard")
    stack.start_server()
    client = stack.reconnect(client)

    assert client.get("/api/auth/me").json()["username"] == "stack-owner", "signing in again isn't needed"
    now = {t["id"]: t for t in stack.batch_tickets(client, batch_id)}
    assert now[second["id"]]["ticket_number"] == "SAVED" and now[second["id"]]["tonnage"] == "22.500"
    assert now[second["id"]]["revision"] == edited["revision"]
    # The page someone still had open is out of date, and that is still refused.
    refused = client.post(f"/api/tickets/{second['id']}/verify", headers={"If-Match": stale_revision})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ticket_changed"
    assert stack.verified() == before
    assert stack.approvals_on_record(first["id"]) == 1


def test_approvals_in_flight_when_the_server_dies_are_all_or_nothing(stack):
    """Whatever instant the kill lands, an approval either fully happened or didn't."""
    client = stack.signed_in()
    batch_id = scan_batch(client, 3)
    stack.start_worker()
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    tickets = stack.batch_tickets(client, batch_id)
    prepared = [client.patch(f"/api/tickets/{t['id']}", json=dict(
        ticket_number=f"N{t['id']}", delivery_date="2026-09-15", material_type="Sand", tonnage="20.000",
        supplier="Example Aggregates", job_location="Lot")).json() for t in tickets]
    gone = threading.Event()

    def approve_until_gone():
        for ticket in prepared:
            if gone.is_set():
                return
            try:
                client.post(f"/api/tickets/{ticket['id']}/verify")
            except httpx.TransportError:
                return

    # Only one copy of each scan may be approved, so some of these are refused. That's fine.
    runner = threading.Thread(target=approve_until_gone)
    runner.start()
    wait_until(lambda: stack.verified(), seconds=60, what="the first approval")
    stack.kill_server()
    gone.set()
    runner.join(30)
    stack.start_server()
    client = stack.reconnect(client)
    with stack.Session() as db:
        for ticket in db.scalars(select(Ticket).where(Ticket.batch_id == batch_id)):
            if ticket.status == "verified":
                assert ticket.tonnage_confirmed and ticket.price_snapshot_state == "priced"
                assert ticket.applied_unit_price == Decimal("10.0000") and ticket.approved_cost == Decimal("200.00")
                assert ticket.verified_by_user_id == 1 and stack.approvals_on_record(ticket.id) == 1
            else:
                assert ticket.status == "needs_review" and not ticket.tonnage_confirmed
                assert ticket.approved_cost is None and ticket.applied_unit_price is None
                assert stack.approvals_on_record(ticket.id) == 0
    dashboard = client.get("/api/dashboard").json()
    assert Decimal(dashboard["total_tons"]) == Decimal("20.000") * len(stack.verified())


# ------------------------------------------------------------ duplicate uploads

def test_the_same_scan_and_invoice_uploaded_at_once_cant_double_count(stack):
    client = stack.signed_in()
    pdf = make_pdf([make_page(TICKETS)])
    results: list[httpx.Response] = []

    def upload():
        results.append(client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": "1"},
                                   files=[("files", ("scan.pdf", pdf, "application/pdf"))]))

    threads = [threading.Thread(target=upload) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert [r.status_code for r in results] == [200, 200], "kept, for a person to decide about"
    stack.start_worker()
    for result in results:
        stack.wait_for_batch(client, result.json()["id"], "ready_for_review")
    first, second = (stack.batch_tickets(client, r.json()["id"])[0] for r in results)
    assert "identical" in " ".join(first["duplicates"]).lower() or first["duplicates"], "the copy is pointed out"
    stack.approve(client, first["id"], ticket_number="A1")
    values = client.patch(f"/api/tickets/{second['id']}", json=dict(
        ticket_number="A2", delivery_date="2026-09-15", material_type="Sand", tonnage="20.000",
        supplier="Example Aggregates", job_location="Lot")).json()
    refused = client.post(f"/api/tickets/{second['id']}/verify", headers={"If-Match": values["revision"]})
    assert refused.status_code == 409, refused.text
    assert Decimal(client.get("/api/dashboard").json()["total_tons"]) == Decimal("20.000")

    invoice = letterhead_supplier()
    answers: list[int] = []

    def upload_invoice():
        answers.append(client.post("/api/invoice-documents", files={"file": ("same.pdf", invoice, "application/pdf")}).status_code)

    threads = [threading.Thread(target=upload_invoice) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert sorted(answers) == [201, 409, 409, 409]
    with stack.Session() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceDocument)) == 1


@pytest.mark.xfail(strict=True, reason="BUG: upload_document writes the file before db.flush(), and a simultaneous duplicate fails at that flush, outside the try that deletes the file, so each refused copy stays in storage")
def test_refused_simultaneous_invoice_copies_leave_no_file_behind(race, monkeypatch):
    """Both requests are held until each has passed the duplicate check and stored its
    file, so the race happens every time, on either database."""
    from app.config import settings
    from app.routers import invoice_documents
    invoice = letterhead_supplier()

    def send():
        return race.client.post("/api/invoice-documents", files={"file": ("same.pdf", invoice, "application/pdf")})

    with race.meeting_at(monkeypatch, invoice_documents.invoice_extraction, "get_invoice_extractor"):
        results = race.run(send, send)
    assert sorted(r.status_code for r in results) == [201, 409]
    files = [p for p in (settings.storage_dir / "invoices").rglob("*") if p.is_file()]
    assert len(files) == 1, f"{len(files)} stored copies of one kept document"


# ------------------------------------------------------------ damaged stored files

def _approved_scan(client) -> tuple[dict, dict]:
    """One approved ticket from a real manual upload, with its neighbour unapproved."""
    project = client.post("/api/projects", json={"code": "F1", "name": "Fictional"}).json()["id"]
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(project)},
                        files=[("files", ("scan.pdf", make_pdf([make_page(TICKETS)]), "application/pdf"))]).json()["id"]
    first, second, *_ = client.get(f"/api/batches/{batch}/tickets").json()
    values = dict(ticket_number="D1", delivery_date="2026-09-15", material_type="Sand", tonnage="20.000", supplier="S", job_location="L")
    assert client.patch(f"/api/tickets/{first['id']}", json=values).status_code == 200
    approved = client.post(f"/api/tickets/{first['id']}/verify").json()
    assert approved["status"] == "verified"
    return approved, second


def _stored_paths(batch_id: int) -> tuple[Path, list[Path]]:
    with _session() as db:
        upload = Path(db.scalar(select(UploadedFile.storage_path).where(UploadedFile.batch_id == batch_id)))
        crops = [Path(p) for p in db.scalars(select(ScanSlot.crop_path).where(ScanSlot.batch_id == batch_id))]
    return upload, crops


def _totals(client) -> tuple[str, int]:
    dashboard = client.get("/api/dashboard").json()
    return dashboard["total_tons"], dashboard["verified"]


def test_a_deleted_scan_image_says_so_and_changes_no_record(client):
    approved, _ = _approved_scan(client)
    totals = _totals(client)
    _, crops = _stored_paths(approved["batch_id"])
    with _session() as db:
        Path(db.get(Ticket, approved["id"]).crop_path).unlink()
    response = client.get(f"/api/tickets/{approved['id']}/image")
    assert response.status_code == 404 and "missing" in response.json()["detail"]
    assert str(settings.storage_dir) not in response.text
    assert _totals(client) == totals
    assert client.get(f"/api/batches/{approved['batch_id']}/tickets").status_code == 200
    assert client.get("/api/records/export.xlsx").status_code == 200
    assert client.get(f"/api/batches/{approved['batch_id']}/manifest").status_code == 200
    assert len(crops) == 4


def test_a_deleted_original_scan_gives_a_clear_page_error_and_a_retry_that_fails_visibly(client):
    approved, second = _approved_scan(client)
    upload, _ = _stored_paths(approved["batch_id"])
    upload.unlink()
    totals = _totals(client)
    with _session() as db:
        file_id = db.scalar(select(UploadedFile.id).where(UploadedFile.batch_id == approved["batch_id"]))
    page = client.get(f"/api/files/{file_id}/pages/1/image")
    assert page.status_code == 404 and str(upload) not in page.text
    rebuilt = client.post(f"/api/batches/{approved['batch_id']}/manifest/rebuild")
    assert rebuilt.status_code == 409 and "could not be read" in rebuilt.json()["detail"]
    assert _totals(client) == totals
    assert client.get(f"/api/tickets/{second['id']}/image").status_code == 200, "crops don't depend on the original"


@pytest.mark.xfail(strict=True, reason="BUG: pipeline.process_file stores str(OSError) as the file's error, and the API returns it, so people see the server's full storage path")
def test_a_retry_of_a_deleted_scan_doesnt_show_the_server_path(client):
    approved, _ = _approved_scan(client)
    upload, _ = _stored_paths(approved["batch_id"])
    upload.unlink()
    with _session() as db:
        file = db.scalar(select(UploadedFile).where(UploadedFile.batch_id == approved["batch_id"]))
        batch = db.get(Batch, approved["batch_id"])
        file.status, batch.status = "failed", "failed"
        db.commit()
    assert client.post(f"/api/batches/{approved['batch_id']}/retry").status_code == 200
    batch = client.get(f"/api/batches/{approved['batch_id']}").json()
    assert batch["status"] == "failed"
    assert batch["files"][0]["error"]
    assert str(settings.storage_dir) not in batch["files"][0]["error"]


def test_a_corrupted_original_fails_visibly_on_retry_and_keeps_approved_tickets(client):
    approved, second = _approved_scan(client)
    totals = _totals(client)
    upload, _ = _stored_paths(approved["batch_id"])
    upload.write_bytes(b"%PDF-1.4 this file was damaged on disk")
    with _session() as db:
        file = db.scalar(select(UploadedFile).where(UploadedFile.batch_id == approved["batch_id"]))
        batch = db.get(Batch, approved["batch_id"])
        file.status, batch.status = "failed", "failed"
        db.commit()
    assert client.post(f"/api/batches/{approved['batch_id']}/retry").status_code == 200
    batch = client.get(f"/api/batches/{approved['batch_id']}").json()
    assert batch["status"] == "failed" and batch["files"][0]["error"]
    tickets = {t["id"]: t for t in client.get(f"/api/batches/{approved['batch_id']}/tickets").json()}
    assert len(tickets) == 4, "nothing was erased"
    assert tickets[approved["id"]]["status"] == "verified" and tickets[approved["id"]]["cost"] == approved["cost"]
    assert tickets[second["id"]]["status"] == "needs_review"
    assert _totals(client) == totals


@pytest.mark.xfail(strict=True, reason="BUG: rotate_slot doesn't handle a crop that won't decode, so a damaged image gives a bare 500 instead of the 409 a missing one gets")
def test_rotating_a_damaged_crop_gets_a_clear_answer(client):
    _approved, second = _approved_scan(client)
    with _session() as db:
        slot = db.scalar(select(ScanSlot).where(ScanSlot.file_id == second["file_id"], ScanSlot.position == second["position"]))
        Path(slot.crop_path).write_bytes(b"not an image")
        slot_id = slot.id
    response = client.post(f"/api/scan-slots/{slot_id}/rotate", json={"rotation": 90})
    assert response.status_code == 409, response.status_code


def test_a_damaged_invoice_original_says_so_and_its_lines_still_work(client):
    project = client.post("/api/projects", json={"code": "F2", "name": "Fictional"}).json()["id"]
    document = client.post("/api/invoice-documents", data={"project_id": str(project)},
                           files={"file": ("i.pdf", letterhead_supplier(), "application/pdf")}).json()
    with _session() as db:
        path = Path(db.get(InvoiceDocument, document["id"]).storage_path)
    path.write_bytes(b"damaged")
    page = client.get(f"/api/invoice-documents/{document['id']}/pages/1/image")
    assert page.status_code == 404 and str(path) not in page.text
    path.unlink()
    original = client.get(f"/api/invoice-documents/{document['id']}/original")
    assert original.status_code == 404 and "missing" in original.json()["detail"]
    line = document["lines"][0]
    assert client.get(f"/api/invoice-documents/{document['id']}").json()["lines"][0]["billed_subtotal"] == line["billed_subtotal"]


# ------------------------------------------------------------ truncated PDFs

def _cut(data: bytes, fraction: float) -> bytes:
    return data[: int(len(data) * fraction)]


def _long_invoice() -> bytes:
    from . import fixtures_invoices as fx
    buffer, pdf = fx._canvas()
    xs, head = (50, 120, 200, 330, 400, 490), ("Date", "Ticket No", "Description", "Tons", "Rate", "Amount")
    fx._text(pdf, [(50, 750, "Invoice #: TRUNC-1"), (300, 750, "Vendor: Ridgeline Aggregates")])
    for page in range(3):
        fx._table(pdf, 700, xs, head, [(f"10/0{page + 1}/2026", str(9000 + page * 10 + n), "Sand", "10.000", "15.0000", "150.00") for n in range(5)])
        pdf.showPage()
    return fx._finish(buffer, pdf)


@pytest.mark.parametrize("fraction", [0.2, 0.4])
def test_an_invoice_cut_off_early_is_refused_cleanly(client, fraction):
    response = client.post("/api/invoice-documents", files={"file": ("cut.pdf", _cut(_long_invoice(), fraction), "application/pdf")})
    assert response.status_code == 400 and "can't be opened" in response.json()["detail"]
    assert client.get("/api/invoice-documents").json() == []


@pytest.mark.xfail(strict=True, reason="BUG: a truncated invoice PDF that MuPDF can repair is drafted from whatever survived, with no warning, so rows silently vanish from the bill")
@pytest.mark.parametrize("fraction", [0.6, 0.8])
def test_an_invoice_cut_off_late_isnt_drafted_as_if_it_were_whole(client, fraction):
    """15 rows on paper. PyMuPDF's is_repaired says the file was damaged; nothing uses it."""
    response = client.post("/api/invoice-documents", files={"file": ("cut.pdf", _cut(_long_invoice(), fraction), "application/pdf")})
    if response.status_code == 400:
        return
    document = response.json()
    lines = document["lines"]
    whole = sum(Decimal(line["billed_quantity"]) for line in lines) == Decimal("150.000")
    assert whole or document["status"] != "needs_review" or document["message"], (document["status"], [line["billed_quantity"] for line in lines])


def _scan_of_two_pages() -> bytes:
    return make_pdf([make_page(TICKETS), make_page(TICKETS)])


_SILENT_LOSS = pytest.mark.xfail(strict=True, reason=(
    "BUG: a scan PDF cut off after its first page opens as 'repaired', page 2 renders blank, its slots read as "
    "empty, and the batch is ready for review with 4 of 8 tickets and no warning. Nothing checks doc.is_repaired"))


@pytest.mark.parametrize("fraction", [0.01, 0.3, 0.5, pytest.param(0.6, marks=_SILENT_LOSS),
                                      pytest.param(0.8, marks=_SILENT_LOSS), pytest.param(0.95, marks=_SILENT_LOSS)])
def test_a_cut_off_scan_never_passes_for_a_complete_one(client, fraction):
    """Eight tickets on paper. Refused, failed, or with its missing slots held for a person.
    Showing fewer tickets with nothing to say so is what must never happen."""
    project = client.post("/api/projects", json={"code": "T1", "name": "Fictional"}).json()["id"]
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(project)},
                           files=[("files", ("cut.pdf", _cut(_scan_of_two_pages(), fraction), "application/pdf"))])
    if response.status_code == 400:
        return
    assert response.status_code == 200, response.text
    batch = client.get(f"/api/batches/{response.json()['id']}").json()
    if batch["status"] == "failed":
        assert batch["files"][0]["error"]
        return
    manifest = client.get(f"/api/batches/{batch['id']}/manifest").json()
    tickets = client.get(f"/api/batches/{batch['id']}/tickets").json()
    assert len(tickets) == 8, (fraction, batch["status"], len(tickets), {k: v for k, v in manifest.items() if isinstance(v, (int, str))})


# ------------------------------------------------------------ conflicting updates

def test_two_people_saving_one_ticket_cant_silently_overwrite(stack):
    one, two = stack.signed_in(), stack.signed_in()
    batch_id = scan_batch(one, 1)
    stack.start_worker()
    stack.wait_for_batch(one, batch_id, "ready_for_review")
    ticket = stack.batch_tickets(one, batch_id)[0]
    first = one.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": "21.000"}, headers={"If-Match": ticket["revision"]})
    second = two.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": "99.000"}, headers={"If-Match": ticket["revision"]})
    assert first.status_code == 200 and second.status_code == 409
    assert second.json()["detail"]["code"] == "ticket_changed"
    assert stack.batch_tickets(one, batch_id)[0]["tonnage"] == "21.000"
    history = one.get(f"/api/tickets/{ticket['id']}/audit").json()
    assert [event["new"] for event in history if event["field"] == "tonnage"] == ["21.000"]


# ------------------------------------------------------------ failing part way through a request

def _tables(session) -> dict[str, list]:
    """Every row of every table, for proving nothing changed at all."""
    return {table.name: sorted(str(tuple(row)) for row in session.execute(table.select()))
            for table in Base.metadata.sorted_tables}


class FailAtWrite:
    """Fails the request's Nth write, or its Nth commit. Everything before it really ran.
    A failed commit is rolled back in the database first, as a real one would be."""

    def __init__(self, engine, nth: int | None, on_commit: int | None = None):
        self.engine, self.nth, self.on_commit = engine, nth, on_commit
        self.writes = self.commits = 0
        self.fired = False

    def _write(self, _conn, _cursor, statement, *_rest):
        if statement.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE"):
            self.writes += 1
            if self.nth == self.writes:
                self.fired = True
                raise OperationalError("injected", {}, Exception("connection lost"))

    def _commit(self, conn):
        self.commits += 1
        if self.on_commit == self.commits:
            self.fired = True
            conn.connection.rollback()
            raise OperationalError("injected", {}, Exception("connection lost"))

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._write)
        event.listen(self.engine, "commit", self._commit)
        return self

    def __exit__(self, *_):
        event.remove(self.engine, "before_cursor_execute", self._write)
        event.remove(self.engine, "commit", self._commit)


def _world(client) -> dict:
    """A company with a bit of everything, for an action to fail partway through."""
    _seed(6)
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    material = client.post("/api/materials", json={"name": "Sand"}).json()["id"]
    client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "10"})
    assert client.post("/api/tickets/1/verify").status_code == 200
    assert client.post("/api/tickets/2/verify").status_code == 200
    line = client.post("/api/invoice-comparisons", json={
        "project_id": 1, "invoice_number": "I-1", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-02", "billed_tons": "15.01", "billed_unit_price": "10", "billed_subtotal": "150.10",
        "ticket_list": "10000\t15.00"}).json()
    document = client.post("/api/invoice-documents", data={"project_id": "1"},
                           files={"file": ("i.pdf", letterhead_supplier(), "application/pdf")}).json()
    return {"supplier": supplier, "material": material, "line": line, "document": document}


NEW_LINE = {"project_id": 1, "invoice_number": "I-2", "supplier_name": "Example Aggregates", "material_name": "Sand",
            "date_from": "2026-09-20", "date_to": "2026-09-30", "billed_tons": "5", "billed_unit_price": "10", "billed_subtotal": "50",
            "ticket_list": "10019\t5\n10020\t5\n10021\t5"}
ACTIONS = {
    "edit_ticket": lambda c, w: c.patch("/api/tickets/3", json={"tonnage": "19.5", "supplier": "Other"}),
    "approve_ticket": lambda c, w: c.post("/api/tickets/3/verify"),
    "withdraw_approval": lambda c, w: c.patch("/api/tickets/1", json={"tonnage": "19.5"}),
    "flag_ticket": lambda c, w: c.post("/api/tickets/3/flag", json={"note": "check"}),
    "skip_ticket": lambda c, w: c.post("/api/tickets/3/skip"),
    "bulk_edit": lambda c, w: c.post("/api/tickets/bulk-edit", json={"ticket_ids": [3, 4, 5], "field": "job_location", "value": "Elsewhere"}),
    "rename": lambda c, w: c.post("/api/records/rename", json={"field": "supplier", "from_value": "Example Aggregates", "to_value": "Example Aggregates"}),
    "new_comparison": lambda c, w: c.post("/api/invoice-comparisons", json=NEW_LINE),
    "correct_comparison": lambda c, w: c.put(f"/api/invoice-comparisons/{w['line']['id']}", json={
        **NEW_LINE, "invoice_number": "I-1", "date_from": "2026-09-01", "date_to": "2026-09-02", "ticket_list": "10000\t16"}),
    "link_tickets": lambda c, w: c.post(f"/api/invoice-comparisons/{w['line']['id']}/ticket-links", json={"ticket_ids": [1]}),
    "edit_draft": lambda c, w: c.patch(f"/api/invoice-documents/{w['document']['id']}/lines/{w['document']['lines'][0]['id']}", json={"billed_quantity": "7", "supplier_name": "X"}),
    "confirm_draft": lambda c, w: c.post(f"/api/invoice-documents/{w['document']['id']}/lines/{w['document']['lines'][0]['id']}/confirm"),
    "void_document": lambda c, w: c.post(f"/api/invoice-documents/{w['document']['id']}/void"),
    "set_rate": lambda c, w: c.put("/api/rates", json={"supplier_id": w["supplier"], "material_id": w["material"], "unit_price": "11"}),
    "rate_sheet": lambda c, w: c.post("/api/rates/import", json={"text": "Example Aggregates,Sand,12\nNew Supplier,New Stone,30", "apply": True}),
    "remove_supplier": lambda c, w: c.delete(f"/api/suppliers/{w['supplier']}"),
    "new_team_member": lambda c, w: c.post("/api/team/members", json={"username": "new-person", "role": "clerk"}),
}


def _prepare(client, world) -> None:
    """A confirmable draft: the document was uploaded without everything it needs."""
    draft = world["document"]["lines"][0]
    response = client.patch(f"/api/invoice-documents/{world['document']['id']}/lines/{draft['id']}", json={
        "supplier_name": "Example Aggregates", "material_name": "Sand", "date_from": "2026-09-10", "date_to": "2026-09-15",
        "unit": "ton", "project_id": 1})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("name", list(ACTIONS))
def test_a_failure_at_any_write_leaves_nothing_half_saved(client, name):
    """Runs the action once for each write it makes, failing at that write, then at the
    commit. Every time: a clean 503, and every row of every table exactly as it was."""
    world = _world(client)
    _prepare(client, world)
    action = ACTIONS[name]
    engine = _session().get_bind()
    with _session() as db:
        before = _tables(db)
    attempts = 0
    nth = 1
    while True:
        with FailAtWrite(engine, nth) as failing:
            response = action(client, world)
        if not failing.fired:
            break  # It made fewer writes than this, so every write has been tried.
        attempts += 1
        assert response.status_code == 503, (name, nth, response.status_code, response.text)
        assert "Nothing was saved" in response.json()["detail"], response.text
        with _session() as db:
            assert _tables(db) == before, (name, nth)
        nth += 1
    if response.status_code < 400:
        # That last call really saved, with nothing failing. Put it back for the commit check.
        with _session() as db:
            assert _tables(db) != before or name == "rename"
    assert attempts >= 1 or name == "rename", f"{name} made no writes, so this tested nothing"


@pytest.mark.parametrize("name", list(ACTIONS))
def test_a_failed_commit_leaves_nothing_half_saved(client, name):
    """A request that commits twice could leave its first half saved. Each commit is
    failed in turn; a failure at any of them must leave every table as it was."""
    world = _world(client)
    _prepare(client, world)
    engine = _session().get_bind()
    with _session() as db:
        before = _tables(db)
    nth = 1
    while True:
        with FailAtWrite(engine, None, on_commit=nth) as failing:
            response = ACTIONS[name](client, world)
        if not failing.fired:
            break
        assert response.status_code == 503, (name, nth, response.status_code, response.text)
        with _session() as db:
            assert _tables(db) == before, (name, nth)
        nth += 1
    assert nth > 1 or name == "rename", f"{name} never committed, so this tested nothing"


@pytest.mark.xfail(strict=True, reason="BUG: upload_document only cleans up its stored file on IntegrityError, so a database failure at commit leaves an orphan copy of the invoice in storage")
def test_a_failed_invoice_upload_leaves_no_stored_file(client):
    engine = _session().get_bind()
    with FailAtWrite(engine, None, on_commit=1):
        response = client.post("/api/invoice-documents", files={"file": ("i.pdf", letterhead_supplier(), "application/pdf")})
    assert response.status_code == 503
    assert [p.name for p in (settings.storage_dir / "invoices").rglob("*") if p.is_file()] == []


def test_a_failed_scan_upload_leaves_no_file_and_no_batch(client):
    project = client.post("/api/projects", json={"code": "U1", "name": "Fictional"}).json()["id"]
    engine = _session().get_bind()
    with FailAtWrite(engine, None, on_commit=1):
        response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(project)},
                               files=[("files", ("scan.pdf", make_pdf([make_page(TICKETS)]), "application/pdf"))])
    assert response.status_code == 503
    assert client.get("/api/batches").json() == []
    uploads = settings.storage_dir / "uploads"
    assert not uploads.exists() or [p for p in uploads.rglob("*") if p.is_file()] == []


# ------------------------------------------------------------ the database going away

def _hold_everything(client: httpx.Client, batch_id: int) -> list[httpx.Response]:
    results: list = []

    def read():
        results.append(client.get("/api/dashboard"))

    def write():
        results.append(client.post("/api/projects", json={"code": "DOWN", "name": "Made while the database was down"}))

    threads = [threading.Thread(target=read), threading.Thread(target=write)]
    [t.start() for t in threads]
    [t.join(70) for t in threads]
    return results


@sqlite_only
def test_a_locked_sqlite_database_gets_a_clean_503_then_recovers(stack):
    """A real lock, held by another process's connection, longer than the 30 second wait."""
    client = stack.signed_in()
    batch_id = scan_batch(client, 1)
    stack.start_worker()
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    ticket = stack.batch_tickets(client, batch_id)[0]
    stack.approve(client, ticket["id"])
    before = stack.verified()
    lock = create_engine(normalized_url(stack.url), connect_args={"timeout": 1})
    connection = lock.connect()
    connection.exec_driver_sql("BEGIN EXCLUSIVE")
    try:
        results = _hold_everything(client, batch_id)
    finally:
        connection.exec_driver_sql("ROLLBACK")
        connection.close()
        lock.dispose()
    assert [r.status_code for r in results] == [503, 503], [r.text for r in results]
    for response in results:
        assert "Nothing was saved" in response.json()["detail"] and response.headers["Retry-After"] == "5"
    assert client.get("/api/dashboard").status_code == 200
    assert "DOWN" not in {p["code"] for p in client.get("/api/projects").json()}
    assert stack.verified() == before


def _maintenance_engine():
    base, _, name = TEST_DATABASE_URL.rpartition("/")
    return create_engine(normalized_url(f"{base}/postgres"), isolation_level="AUTOCOMMIT"), name


@postgres_only
def test_postgres_refusing_connections_gets_a_clean_503_then_recovers(stack):
    client = stack.signed_in()
    batch_id = scan_batch(client, 1)
    stack.start_worker()
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    stack.approve(client, stack.batch_tickets(client, batch_id)[0]["id"])
    before = stack.verified()
    os.kill(stack.workers[-1].pid, 9)
    stack.workers[-1].wait(10)
    admin, name = _maintenance_engine()
    with admin.connect() as connection:
        connection.execute(text(f"ALTER DATABASE {name} ALLOW_CONNECTIONS false"))
        connection.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}'"))
    try:
        results = _hold_everything(client, batch_id)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f"ALTER DATABASE {name} ALLOW_CONNECTIONS true"))
        admin.dispose()
    assert [r.status_code for r in results] == [503, 503], [r.text for r in results]
    for response in results:
        assert "Nothing was saved" in response.json()["detail"]
    assert client.get("/api/dashboard").status_code == 200, "a dropped connection is replaced"
    assert "DOWN" not in {p["code"] for p in client.get("/api/projects").json()}
    assert stack.verified() == before


@postgres_only
def test_postgres_connection_dying_mid_request_gets_a_clean_503(stack):
    """The request is really inside its transaction, waiting on a row, when its connection dies."""
    client = stack.signed_in()
    batch_id = scan_batch(client, 1)
    stack.start_worker()
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    ticket = stack.batch_tickets(client, batch_id)[0]
    admin, name = _maintenance_engine()
    holder = stack.engine.connect()
    transaction = holder.begin()
    holder.execute(text(f"SELECT id FROM tickets WHERE id = {ticket['id']} FOR UPDATE"))
    results: list[httpx.Response] = []
    thread = threading.Thread(target=lambda: results.append(client.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": "77"})))
    thread.start()
    waiting = "SELECT pid FROM pg_stat_activity WHERE datname = :name AND wait_event_type = 'Lock'"
    with admin.connect() as connection:
        pid = wait_until(lambda: connection.execute(text(waiting), {"name": name}).scalar(), seconds=30, what="the request to wait on the row")
        connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    thread.join(30)
    transaction.rollback()
    holder.close()
    admin.dispose()
    assert results[0].status_code == 503, (results[0].status_code, results[0].text)
    assert "Nothing was saved" in results[0].json()["detail"]
    assert stack.batch_tickets(client, batch_id)[0]["tonnage"] != "77.000"
    assert client.get("/api/dashboard").status_code == 200


@pytest.mark.xfail(strict=True, reason="BUG: main.py maps OperationalError to 503, but SQLAlchemy's pool TimeoutError isn't one, so running out of connections under load gives a generic 500")
def test_running_out_of_database_connections_gets_a_503_not_a_500(tmp_path, monkeypatch, postgres_engine):
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import QueuePool

    from app.auth import current_user
    from app.models import Organization, User
    url = TEST_DATABASE_URL or f"sqlite:///{tmp_path / 'pool.db'}"
    if postgres_engine is not None:
        _reset_postgres(postgres_engine)
    options = {"connect_args": {"check_same_thread": False}} if not TEST_DATABASE_URL else {}
    engine = create_engine(normalized_url(url), poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.2, **options)
    if postgres_engine is None:
        Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as db:
        db.add(Organization(id=1, name="Fictional"))
        db.commit()

    def override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override
    app.dependency_overrides[__import__("app.auth", fromlist=["current_user"]).current_user] = lambda: User(
        id=1, organization_id=1, username="u", password_hash="x", active=True, role="owner", must_change_password=False)
    holder = engine.connect()
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/api/projects")
    finally:
        holder.close()
        engine.dispose()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
    assert response.status_code == 503, response.status_code


# ------------------------------------------------------------ storage that won't take writes

@not_root
def test_a_read_only_storage_folder_refuses_uploads_cleanly_and_recovers(stack):
    client = stack.signed_in()
    done = scan_batch(client, 1)
    stack.start_worker()
    stack.wait_for_batch(client, done, "ready_for_review")
    stack.approve(client, stack.batch_tickets(client, done)[0]["id"])
    before = stack.verified()
    batches = client.get("/api/batches").json()
    uploads, invoices = stack.storage / "uploads", stack.storage / "invoices"
    invoices.mkdir(exist_ok=True)
    original = stat.S_IMODE(uploads.stat().st_mode)
    uploads.chmod(0o500)
    invoices.chmod(0o500)
    try:
        pdf = make_pdf([make_page(TICKETS)])
        scan = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": "1"},
                           files=[("files", ("scan.pdf", pdf, "application/pdf"))])
        invoice = client.post("/api/invoice-documents", files={"file": ("i.pdf", letterhead_supplier(), "application/pdf")})
    finally:
        uploads.chmod(original)
        invoices.chmod(original)
    for response in (scan, invoice):
        assert response.status_code >= 500, response.text
        assert "nothing further was changed" in response.json()["detail"]
        assert str(stack.storage) not in response.text
    assert client.get("/api/batches").json() == batches, "no empty batch is left behind"
    assert client.get("/api/invoice-documents").json() == []
    assert stack.verified() == before
    assert client.post("/api/invoice-documents", files={"file": ("i.pdf", letterhead_supplier(), "application/pdf")}).status_code == 201


@not_root
def test_a_worker_that_cant_write_crops_fails_visibly_and_a_retry_works(stack):
    client = stack.signed_in()
    crops = stack.storage / "crops"
    crops.mkdir()
    batch_id = scan_batch(client, 1)
    crops.chmod(0o500)
    try:
        stack.start_worker()
        batch = stack.wait_for_batch(client, batch_id, "failed")
    finally:
        crops.chmod(0o700)
    assert batch["files"][0]["status"] == "failed" and "retry" in batch["files"][0]["error"].lower()
    assert str(stack.storage) not in batch["files"][0]["error"]
    assert stack.batch_tickets(client, batch_id) == []
    assert client.post(f"/api/batches/{batch_id}/retry").status_code == 200
    stack.wait_for_batch(client, batch_id, "ready_for_review")
    assert len(stack.batch_tickets(client, batch_id)) == 4


# The upload itself stays in memory (under 1 MB), so the first write to disk past the
# limit is TicketFlow's own. Bigger, and the temporary upload file would hit it first.
DISK_LIMIT = 700 * 1024


def _big_pdf() -> bytes:
    """A real PDF padded past the limit below, with its signature intact."""
    return letterhead_supplier() + b"\n%" + b"x" * (900 * 1024)


def test_a_full_disk_refuses_an_upload_cleanly(tmp_path, postgres_engine):
    """The kernel really refuses the write, past a size limit on the server process."""
    stack = build_stack(tmp_path, postgres_engine, TEST_DATABASE_URL, _reset_postgres)
    stack.start_server(largest_file_bytes=DISK_LIMIT)
    try:
        client = stack.signed_in()
        scan = client.post("/api/batches", data={"layout": "single", "intake_mode": "manual", "project_id": "1"},
                           files=[("files", ("big.pdf", _big_pdf(), "application/pdf"))])
        assert scan.status_code >= 500, scan.text
        assert "nothing further was changed" in scan.json()["detail"] and str(stack.storage) not in scan.text
        assert client.get("/api/batches").json() == [], "no batch for a file that wasn't kept"
        uploads = stack.storage / "uploads"
        assert not uploads.exists() or [p for p in uploads.rglob("*") if p.is_file()] == []
        invoice = client.post("/api/invoice-documents", files={"file": ("big.pdf", _big_pdf(), "application/pdf")})
        assert invoice.status_code >= 500, invoice.text
        assert client.get("/api/invoice-documents").json() == []
        assert client.get("/api/dashboard").status_code == 200, "everything else keeps working"
        assert client.post("/api/invoice-documents", files={"file": ("small.pdf", letterhead_supplier(), "application/pdf")}).status_code == 201
    finally:
        stack.stop_everything()
        if postgres_engine is None:
            stack.engine.dispose()


@pytest.mark.xfail(strict=True, reason="BUG: upload_document doesn't remove a partly written file when the write fails, so a full disk leaves half an invoice in storage, unreferenced and using the space that ran out")
def test_a_full_disk_leaves_no_half_written_invoice(tmp_path, postgres_engine):
    stack = build_stack(tmp_path, postgres_engine, TEST_DATABASE_URL, _reset_postgres)
    stack.start_server(largest_file_bytes=DISK_LIMIT)
    try:
        client = stack.signed_in()
        client.post("/api/invoice-documents", files={"file": ("big.pdf", _big_pdf(), "application/pdf")})
        invoices = stack.storage / "invoices"
        assert not invoices.exists() or [p.stat().st_size for p in invoices.rglob("*") if p.is_file()] == []
    finally:
        stack.stop_everything()
        if postgres_engine is None:
            stack.engine.dispose()
