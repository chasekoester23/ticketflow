"""Stored documents: one way in and out, and they survive moving to a new location.

The disaster this prevents: a database holding absolute paths, restored somewhere
new, can no longer find a single scan.
"""
import io
import os
import re
import shutil
import stat
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select, update

from app.config import settings
from app.models import InvoiceDocument, ScanSlot, Ticket, UploadedFile
from app.routers import api as api_module
from app.services import jobqueue, storage

from .conftest import FixtureExtractor
from .sample_data import make_invoice_pdf, make_scan_pdf

SECTIONS = ("uploads", "crops", "invoices")


def _db():
    return api_module.SessionLocal()


def _upload(client, *, intake_mode="extract") -> int:
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": intake_mode},
                           files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _upload_invoice(client) -> int:
    response = client.post("/api/invoice-documents", files={"file": ("invoice.pdf", make_invoice_pdf(), "application/pdf")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _every_stored_reference(db) -> list[str]:
    return ([f.storage_path for f in db.scalars(select(UploadedFile))] + [t.crop_path for t in db.scalars(select(Ticket))]
            + [s.crop_path for s in db.scalars(select(ScanSlot))] + [d.storage_path for d in db.scalars(select(InvoiceDocument))])


def _first_slot(client, batch_id) -> dict:
    return client.get(f"/api/batches/{batch_id}/manifest").json()["files"][0]["pages"][0]["slots"][0]


def _reads(client, batch_id, document_id) -> dict[str, int]:
    """Every endpoint that reads a stored document."""
    ticket = client.get(f"/api/batches/{batch_id}/tickets").json()[0]
    file_id = client.get(f"/api/batches/{batch_id}").json()["files"][0]["id"]
    urls = {"ticket image": f"/api/tickets/{ticket['id']}/image", "slot image": f"/api/scan-slots/{_first_slot(client, batch_id)['id']}/image",
            "page image": f"/api/files/{file_id}/pages/1/image", "invoice original": f"/api/invoice-documents/{document_id}/original",
            "invoice page": f"/api/invoice-documents/{document_id}/pages/1/image"}
    return {name: client.get(url).status_code for name, url in urls.items()}


ALL_OPEN = {"ticket image": 200, "slot image": 200, "page image": 200, "invoice original": 200, "invoice page": 200}


def _make_paths_absolute(db, prefix: Path) -> None:
    """Rows as older versions wrote them: the full path, including an old location."""
    for model, column in ((UploadedFile, "storage_path"), (Ticket, "crop_path"), (ScanSlot, "crop_path"), (InvoiceDocument, "storage_path")):
        for row in db.scalars(select(model)):
            db.execute(update(model).where(model.id == row.id).values({column: str(prefix / getattr(row, column))}))
    db.commit()


# ------------------------------------------------------------------ what gets stored

def test_new_rows_store_paths_relative_to_storage(client):
    batch_id = _upload(client)
    _upload_invoice(client)
    client.post(f"/api/scan-slots/{_first_slot(client, batch_id)['id']}/rotate", json={"rotation": 90})
    with _db() as db:
        references = _every_stored_reference(db)
    assert len(references) >= 10
    for reference in references:
        assert not os.path.isabs(reference), reference
        assert reference.split("/")[0] in SECTIONS
        assert str(settings.storage_dir) not in reference
        assert (settings.storage_dir / reference).is_file()


def test_a_marked_ticket_also_gets_a_relative_path(client):
    """A slot a person marks as a ticket creates a row that didn't come through the worker."""
    batch_id = _upload(client, intake_mode="manual")
    with _db() as db:
        ticket = db.scalars(select(Ticket).where(Ticket.batch_id == batch_id)).first()
        slot = db.scalar(select(ScanSlot).where(ScanSlot.file_id == ticket.file_id, ScanSlot.position == ticket.position))
        slot_id = slot.id
        db.delete(ticket)
        db.commit()
    assert client.post(f"/api/scan-slots/{slot_id}/mark-ticket").status_code == 200
    with _db() as db:
        recreated = db.scalars(select(Ticket).where(Ticket.batch_id == batch_id).order_by(Ticket.id.desc())).first()
        assert not os.path.isabs(recreated.crop_path)


def test_no_response_ever_contains_a_stored_path(client):
    batch_id = _upload(client)
    document_id = _upload_invoice(client)
    with _db() as db:
        file = db.scalars(select(UploadedFile).where(UploadedFile.batch_id == batch_id)).first()
        internal_name = Path(file.storage_path).name
    for url in (f"/api/batches/{batch_id}", f"/api/batches/{batch_id}/tickets", f"/api/batches/{batch_id}/manifest", f"/api/invoice-documents/{document_id}"):
        text = client.get(url).text
        assert internal_name not in text and "uploads/" not in text and "crops/" not in text


# ------------------------------------------------------------------ surviving a restore

def test_documents_open_after_the_whole_storage_folder_moves(client, tmp_path, monkeypatch):
    batch_id = _upload(client)
    document_id = _upload_invoice(client)
    assert _reads(client, batch_id, document_id) == ALL_OPEN
    moved = tmp_path.parent / f"{tmp_path.name}-restored"
    moved.mkdir()
    for section in SECTIONS:
        shutil.move(str(settings.storage_dir / section), str(moved / section))
    monkeypatch.setattr(settings, "storage_dir", moved)
    try:
        assert _reads(client, batch_id, document_id) == ALL_OPEN
    finally:
        shutil.rmtree(moved)


def test_a_legacy_absolute_path_is_found_when_its_old_location_no_longer_exists(client):
    """The real disaster-recovery bug: a backup from an older version, restored somewhere new."""
    batch_id = _upload(client)
    document_id = _upload_invoice(client)
    gone = Path("/nonexistent/old-machine/backend/storage")
    with _db() as db:
        _make_paths_absolute(db, gone)
    assert not gone.exists()
    assert _reads(client, batch_id, document_id) == ALL_OPEN


def test_a_legacy_absolute_path_inside_the_current_storage_still_works(client):
    batch_id = _upload(client)
    document_id = _upload_invoice(client)
    with _db() as db:
        _make_paths_absolute(db, settings.storage_dir)
    assert _reads(client, batch_id, document_id) == ALL_OPEN


def test_a_legacy_path_prefers_the_restored_copy_over_the_old_location(tmp_path):
    """Otherwise a restore could quietly keep reading the damaged original."""
    old, current = tmp_path / "old", tmp_path / "current"
    for root, content in ((old, b"the original location"), (current, b"the restored copy")):
        (root / "uploads" / "1").mkdir(parents=True)
        (root / "uploads" / "1" / "a.pdf").write_bytes(content)
    store = storage.LocalStorage(current)
    assert store.read(str(old / "uploads" / "1" / "a.pdf")) == b"the restored copy"


def test_a_legacy_path_still_resolves_where_it_was_if_nothing_is_under_current_storage(tmp_path):
    """STORAGE_DIR was changed without moving older files: they keep working as before."""
    old = tmp_path / "old"
    (old / "uploads" / "1").mkdir(parents=True)
    (old / "uploads" / "1" / "a.pdf").write_bytes(b"still here")
    store = storage.LocalStorage(tmp_path / "current")
    reference = str(old / "uploads" / "1" / "a.pdf")
    assert store.read(reference) == b"still here"
    # A check of a restored copy can ask for strictness, so it can't be fooled by this.
    assert store.locate(reference, inside_only=True) is None


def test_a_prefix_that_itself_contains_a_section_name_finds_the_right_file(tmp_path):
    store = storage.LocalStorage(tmp_path)
    (tmp_path / "uploads" / "1").mkdir(parents=True)
    (tmp_path / "uploads" / "1" / "a.pdf").write_bytes(b"right")
    assert store.read("/home/uploads/old/storage/uploads/1/a.pdf") == b"right"
    assert storage.key_for("/home/uploads/old/storage/uploads/1/a.pdf") == "uploads/1/a.pdf"


def test_processing_and_retrying_work_from_a_restored_legacy_database(client, monkeypatch):
    """The worker reads a stored upload too, so a queue restored mid-job has to work."""
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    batch_id = _upload(client)
    with _db() as db:
        _make_paths_absolute(db, Path("/nonexistent/old-machine/storage"))
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=FixtureExtractor)
    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "ready_for_review", batch
    tickets = client.get(f"/api/batches/{batch_id}/tickets").json()
    assert len(tickets) == 4
    assert client.get(f"/api/tickets/{tickets[0]['id']}/image").status_code == 200
    with _db() as db:
        assert all(not os.path.isabs(t.crop_path) for t in db.scalars(select(Ticket)))


def test_rotating_a_legacy_slot_writes_under_current_storage(client):
    batch_id = _upload(client)
    with _db() as db:
        _make_paths_absolute(db, Path("/nonexistent/old-machine/storage"))
    slot = _first_slot(client, batch_id)
    width, height = Image.open(io.BytesIO(client.get(f"/api/scan-slots/{slot['id']}/image").content)).size
    assert client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 90}).status_code == 200
    assert Image.open(io.BytesIO(client.get(f"/api/scan-slots/{slot['id']}/image").content)).size == (height, width)
    assert client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 0}).status_code == 200
    assert Image.open(io.BytesIO(client.get(f"/api/scan-slots/{slot['id']}/image").content)).size == (width, height)


def test_rebuilding_a_manifest_reads_a_legacy_path(client):
    batch_id = _upload(client, intake_mode="manual")
    with _db() as db:
        _make_paths_absolute(db, Path("/nonexistent/old-machine/storage"))
    assert client.post(f"/api/batches/{batch_id}/manifest/rebuild").status_code == 200


# ------------------------------------------------------------------ containment

@pytest.mark.parametrize("reference", ["../outside.pdf", "uploads/../../outside.pdf", "/etc/passwd", "/etc/uploads", "/private/outside.pdf", "", "."])
def test_a_reference_can_never_reach_outside_storage(tmp_path, reference):
    root = tmp_path / "storage"
    (root / "uploads").mkdir(parents=True)
    (tmp_path / "outside.pdf").write_bytes(b"private")
    store = storage.LocalStorage(root)
    assert store.locate(reference) is None
    assert store.exists(reference) is False
    assert store.serve(reference, "application/pdf") is None
    with pytest.raises(storage.DocumentMissing):
        store.read(reference)


def test_a_symlink_cant_lead_out_of_storage(tmp_path):
    root = tmp_path / "storage"
    (root / "uploads").mkdir(parents=True)
    (tmp_path / "outside.pdf").write_bytes(b"private")
    (root / "uploads" / "link.pdf").symlink_to(tmp_path / "outside.pdf")
    (root / "crops").symlink_to(tmp_path)
    store = storage.LocalStorage(root)
    assert store.locate("uploads/link.pdf") is None
    assert store.locate("crops/outside.pdf") is None


@pytest.mark.parametrize("key", ["../outside.pdf", "/etc/x", "uploads/../../x", "", "uploads/"])
def test_writing_outside_storage_is_refused(tmp_path, key):
    store = storage.LocalStorage(tmp_path / "storage")
    with pytest.raises(ValueError):
        store.save(key, b"x")
    assert not (tmp_path / "outside.pdf").exists()


def test_an_unknown_section_cant_be_written(tmp_path):
    """So every stored document is one backups, restores and the legacy lookup understand."""
    with pytest.raises(ValueError):
        storage.LocalStorage(tmp_path).save("elsewhere/x.pdf", b"x")


# ------------------------------------------------------------------ writing

def test_files_and_every_new_folder_are_private(tmp_path):
    root = tmp_path / "storage"
    store = storage.LocalStorage(root)
    assert store.save("uploads/7/a.pdf", b"x") == "uploads/7/a.pdf"
    assert stat.S_IMODE((root / "uploads" / "7" / "a.pdf").stat().st_mode) == 0o600
    for folder in (root, root / "uploads", root / "uploads" / "7"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_two_uploads_creating_one_new_folder_at_once_both_succeed(tmp_path, monkeypatch):
    """The first upload of a company's invoices, made twice at once, failed with a 500:
    both saw the folder missing, and the second to create it was refused."""
    store = storage.LocalStorage(tmp_path)
    real_exists = Path.exists

    def another_request_gets_there_first(self, *args, **kwargs):
        found = real_exists(self, *args, **kwargs)
        if self.name == "7" and not found:
            self.mkdir(parents=True)
        return found

    monkeypatch.setattr(Path, "exists", another_request_gets_there_first)
    assert store.save("invoices/7/a.pdf", b"x") == "invoices/7/a.pdf"
    monkeypatch.setattr(Path, "exists", real_exists)
    assert store.read("invoices/7/a.pdf") == b"x"


def test_saving_never_overwrites_unless_asked(tmp_path):
    store = storage.LocalStorage(tmp_path)
    store.save("uploads/1/a.pdf", b"first")
    with pytest.raises(FileExistsError):
        store.save("uploads/1/a.pdf", b"second")
    assert store.read("uploads/1/a.pdf") == b"first"
    store.save("uploads/1/a.pdf", b"second", replace=True)
    assert store.read("uploads/1/a.pdf") == b"second"


def test_a_failed_write_leaves_nothing_behind(tmp_path, monkeypatch):
    """A half-written crop would otherwise be found by the next run and kept for good."""
    store = storage.LocalStorage(tmp_path)

    def fails(_fd):
        raise OSError("disk full")

    monkeypatch.setattr(storage.os, "fsync", fails)
    with pytest.raises(OSError):
        store.save("crops/1/a.png", b"partial")
    assert not store.exists("crops/1/a.png")
    assert list((tmp_path / "crops" / "1").iterdir()) == []


def test_images_are_saved_as_private_pngs(tmp_path):
    store = storage.LocalStorage(tmp_path)
    store.save_png("crops/1/a.png", Image.new("RGB", (3, 2), "white"))
    assert Image.open(io.BytesIO(store.read("crops/1/a.png"))).size == (3, 2)
    assert stat.S_IMODE((tmp_path / "crops" / "1" / "a.png").stat().st_mode) == 0o600


def test_deleting_is_limited_to_storage(tmp_path):
    root = tmp_path / "storage"
    store = storage.LocalStorage(root)
    store.save("uploads/1/a.pdf", b"x")
    store.save("uploads/1/b.pdf", b"x")
    (tmp_path / "outside.pdf").write_bytes(b"private")
    store.delete("uploads/1/a.pdf")
    store.delete("uploads/1/never-there.pdf")
    store.delete("../outside.pdf")
    store.delete("/etc/passwd")
    assert not store.exists("uploads/1/a.pdf") and (tmp_path / "outside.pdf").exists()
    store.delete_prefix("uploads/1")
    assert not (root / "uploads" / "1").exists()
    for bad in ("..", "/", "", "uploads/../.."):
        with pytest.raises(ValueError):
            store.delete_prefix(bad)
    assert (tmp_path / "outside.pdf").exists() and root.exists()


def test_a_rejected_upload_leaves_no_files(client, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("database refused")

    monkeypatch.setattr(jobqueue, "enqueue", boom)
    response = client.post("/api/batches", data={"layout": "grid_2x2"},
                           files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
    assert response.status_code == 500
    assert not list((settings.storage_dir / "uploads").glob("*/*"))


# ------------------------------------------------------------------ when a file is gone

def test_a_missing_upload_fails_visibly_without_revealing_where_it_was(client, monkeypatch):
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    batch_id = _upload(client)
    with _db() as db:
        file = db.scalars(select(UploadedFile).where(UploadedFile.batch_id == batch_id)).first()
        storage.current().delete(file.storage_path)
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=FixtureExtractor)
    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "failed" and batch["files"][0]["status"] == "failed"
    message = batch["files"][0]["error"]
    assert "missing" in message
    assert str(settings.storage_dir) not in message and "uploads/" not in message


def test_a_file_that_cant_be_read_doesnt_reveal_its_path_either(client, monkeypatch):
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_args: None)
    batch_id = _upload(client)

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", str(settings.storage_dir / "uploads" / "x"))

    monkeypatch.setattr(storage.LocalStorage, "read", denied)
    with _db() as db:
        jobqueue.execute_claimed(db, *jobqueue.claim_next(db), extractor_factory=FixtureExtractor)
    file = client.get(f"/api/batches/{batch_id}").json()["files"][0]
    assert file["status"] == "failed" and str(settings.storage_dir) not in file["error"]


# ------------------------------------------------------------------ the single way in

APP = Path(__file__).resolve().parents[1] / "app"


def test_everything_goes_through_the_storage_service():
    """A new read that goes straight to the disk would bring the restore bug back."""
    offenders = []
    forbidden = re.compile(r"FileResponse|storage_dir\s*/|Path\([^)]*(storage_path|crop_path)|read_bytes\(|write_bytes\(|os\.open\(")
    # The worker's heartbeat file and the evaluation tool's own folder aren't documents.
    allowed = {"services/storage.py", "services/system_health.py", "services/evaluation.py"}
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP).as_posix()
        if relative in allowed:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if forbidden.search(line):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert offenders == []


def test_the_interface_stays_small_enough_for_another_driver():
    public = {name for name in vars(storage.LocalStorage) if not name.startswith("_")}
    assert public == {"locate", "exists", "read", "save", "save_png", "delete", "delete_prefix", "serve"}

