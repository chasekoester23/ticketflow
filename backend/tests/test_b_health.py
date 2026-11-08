"""Readiness for a load balancer, and the two newer System health checks."""
import shutil

from sqlalchemy import create_engine, select

from app import main as main_module
from app.auth import current_user
from app.config import settings
from app.main import app
from app.models import Batch, Organization, ProcessingJob, UploadedFile, User
from app.services import storage

from .sample_data import make_scan_pdf
from .test_query_budgets import _session

STATES = {"ok", "off", "attention", "problem"}


def _check(client, name):
    status = client.get("/api/system/status").json()
    assert all(set(check) == {"name", "state", "message", "action"} and check["state"] in STATES for check in status["checks"])
    return next(check for check in status["checks"] if check["name"] == name)


# ------------------------------------------------------------------ /health/ready

def test_ready_when_the_database_answers(client):
    app.dependency_overrides.pop(current_user, None)  # Public: a load balancer has no account.
    response = client.get("/health/ready")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"


def test_not_ready_when_the_database_doesnt_answer_and_says_nothing_else(client, monkeypatch, tmp_path):
    broken = create_engine(f"sqlite:///{tmp_path / 'no-such-folder' / 'x.db'}")
    monkeypatch.setattr(main_module, "engine", broken)
    response = client.get("/health/ready")
    assert response.status_code == 503 and response.json() == {"ok": False}
    assert "no-such-folder" not in response.text and "sqlite" not in response.text.lower()
    assert response.headers["retry-after"] == "5"


def test_plain_health_still_answers_without_the_database(client, monkeypatch, tmp_path):
    """Liveness and readiness are different questions: a restart wouldn't fix the database."""
    monkeypatch.setattr(main_module, "engine", create_engine(f"sqlite:///{tmp_path / 'absent' / 'x.db'}"))
    assert client.get("/health").status_code == 200


def test_ready_still_refuses_an_unexpected_host(client):
    assert client.get("/health/ready", headers={"Host": "unapproved.example"}).status_code == 400


# ------------------------------------------------------------------ Failed jobs

def _failed_upload(client, organization_id=1) -> int:
    with _session() as db:
        batch = Batch(organization_id=organization_id, layout="single", status="failed", intake_mode="manual")
        db.add(batch)
        db.flush()
        db.add(ProcessingJob(batch_id=batch.id, status="failed", attempts=3, error="set aside"))
        db.commit()
        return batch.id


def test_no_failed_jobs_is_ok(client):
    assert _check(client, "Failed jobs") == {"name": "Failed jobs", "state": "ok", "message": "No uploads are waiting on a person.", "action": None}


def test_a_failed_job_needs_attention_and_says_what_to_do(client):
    _failed_upload(client)
    check = _check(client, "Failed jobs")
    assert check["state"] == "attention" and "1 upload" in check["message"]
    assert "retry" in check["action"].lower() and "by hand" in check["action"]


def test_a_failed_job_retried_successfully_no_longer_counts(client):
    batch_id = _failed_upload(client)
    with _session() as db:
        db.get(Batch, batch_id).status = "ready_for_review"
        db.commit()
    assert _check(client, "Failed jobs")["state"] == "ok"


def test_failed_jobs_are_counted_per_company(client):
    with _session() as db:
        db.add(Organization(id=2, name="Other"))
        db.commit()
    _failed_upload(client, organization_id=2)
    assert _check(client, "Failed jobs")["state"] == "ok"


# ------------------------------------------------------------------ Stored documents

def _upload(client) -> int:
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual"},
                           files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_no_documents_yet_is_fine(client):
    assert _check(client, "Stored documents")["state"] == "ok"


def test_documents_that_are_there_are_ok(client):
    _upload(client)
    check = _check(client, "Stored documents")
    assert check["state"] == "ok" and "checked" in check["message"]


def test_a_missing_document_is_a_problem_with_a_way_forward(client):
    batch_id = _upload(client)
    with _session() as db:
        file = db.scalar(select(UploadedFile).where(UploadedFile.batch_id == batch_id))
        storage.current().delete(file.storage_path)
        internal = file.storage_path
    check = _check(client, "Stored documents")
    assert check["state"] == "problem" and "missing" in check["message"]
    assert "check_documents.py" in check["action"]
    assert internal not in str(check) and str(settings.storage_dir) not in str(check)


def test_a_missing_storage_folder_is_a_problem(client, monkeypatch, tmp_path):
    _upload(client)
    moved = tmp_path / "elsewhere"
    monkeypatch.setattr(settings, "storage_dir", moved)
    check = _check(client, "Stored documents")
    assert check["state"] == "problem" and "storage folder" in check["message"]
    assert not moved.exists() or not any(moved.iterdir())


def test_another_companys_missing_documents_arent_reported(client):
    with _session() as db:
        db.add(Organization(id=2, name="Other"))
        db.flush()
        db.add(User(id=2, organization_id=2, username="other-owner", password_hash="x", role="owner"))
        db.flush()
        batch = Batch(organization_id=2, layout="single", status="ready_for_review", intake_mode="manual")
        db.add(batch)
        db.flush()
        db.add(UploadedFile(batch_id=batch.id, original_name="theirs.pdf", storage_path="uploads/999/gone.pdf", sha256="0" * 64, status="ready_for_review"))
        db.commit()
    assert _check(client, "Stored documents")["state"] == "ok"


def test_the_overall_health_follows_the_new_checks(client):
    batch_id = _upload(client)
    assert client.get("/api/system/status").json()["healthy"] in (True, False)  # Other checks vary by machine.
    shutil.rmtree(settings.storage_dir / "uploads" / str(batch_id))
    assert client.get("/api/system/status").json()["healthy"] is False
