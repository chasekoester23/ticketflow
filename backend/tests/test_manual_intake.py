"""Manual ticket intake requires zero AI calls and no invented ticket fields.

All scans in these tests are synthetic printed-style examples. These tests
validate processing, review, tenant isolation and pricing but NOT handwriting.
"""
from decimal import Decimal

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.models import Batch, Ticket, ProcessingJob
from app.routers import api as api_module
from app.services import jobqueue
from .sample_data import make_scan_pdf, TICKETS


def _post(client, layout="grid_2x2", mode="manual"):
    return client.post(
        "/api/batches",
        data={"layout": layout, "intake_mode": mode},
        files=[("files", ("synthetic.pdf", make_scan_pdf(), "application/pdf"))],
    )


def test_manual_intake_works_without_model_key_and_never_fabricates_data(client, monkeypatch):
    def unavailable_model():
        raise AssertionError("Manual intake must not initialize an AI provider")
    monkeypatch.setattr(api_module, "get_extractor", unavailable_model)

    result = _post(client)
    assert result.status_code == 200, result.text
    batch = result.json()
    assert batch["intake_mode"] == "manual"
    # The API response is constructed before TestClient's queued fixture
    # worker completes; fetch persisted status in a separate request.
    assert client.get(f"/api/batches/{batch['id']}").json()["status"] == "ready_for_review"
    assert client.get(f"/api/batches/{batch['id']}").json()["files"][0]["status"] == "ready_for_review"

    tickets = client.get(f"/api/batches/{batch['id']}/tickets").json()
    assert len(tickets) == 4
    for ticket in tickets:
        assert ticket["status"] == "needs_review"
        assert ticket["tonnage_confirmed"] is False
        assert ticket["price_snapshot_state"] == "pending"
        assert ticket["cost"] is None
        assert ticket["unit_price_override"] is None
        assert ticket["flags"]
        for field in ("ticket_number", "delivery_date", "material_type",
                      "tonnage", "supplier", "job_location"):
            assert ticket[field] is None
        assert client.get(f"/api/tickets/{ticket['id']}/image").status_code == 200

    with api_module.SessionLocal() as db:
        db_tickets = db.scalars(select(Ticket).where(Ticket.batch_id == batch["id"])).all()
        assert len(db_tickets) == 4
        assert all(t.raw_extraction is None for t in db_tickets)
        jobs = db.scalars(select(ProcessingJob).where(ProcessingJob.batch_id == batch["id"])).all()
        assert len(jobs) == 1 and jobs[0].status == "finished"


def test_manual_ticket_needs_six_real_values_before_approval(client):
    uploaded = _post(client)
    assert uploaded.status_code == 200
    bid = uploaded.json()["id"]
    tid = client.get(f"/api/batches/{bid}/tickets").json()[0]["id"]

    rejected = client.post(f"/api/tickets/{tid}/verify")
    assert rejected.status_code == 422
    assert "Ticket number" in rejected.json()["detail"]

    raw = TICKETS[0]
    saved = client.patch(f"/api/tickets/{tid}", json={
        "ticket_number": raw["ticket_number"],
        "delivery_date": raw["delivery_date"],
        "material_type": raw["material_type"],
        "tonnage": raw["tonnage"],
        "supplier": raw["supplier"],
        "job_location": raw["job_location"],
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "needs_review"
    assert saved.json()["tonnage_confirmed"] is False

    before = client.get("/api/dashboard").json()
    assert Decimal(str(before["total_tons"])) == Decimal("0")
    approved = client.post(f"/api/tickets/{tid}/verify")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "verified"
    assert approved.json()["tonnage_confirmed"] is True
    assert approved.json()["tonnage"] == "24.500"
    assert Decimal(str(client.get("/api/dashboard").json()["total_tons"])) == Decimal("24.500")
    assert client.get(f"/api/tickets/{tid}/audit").json()
    assert client.get(f"/api/batches/{bid}/export.xlsx").status_code == 200


def test_manual_intake_rejects_unknown_mode_atomically(client):
    response = _post(client, mode="not-real")
    assert response.status_code == 422
    assert client.get("/api/batches").json() == []
    with api_module.SessionLocal() as db:
        assert db.scalars(select(ProcessingJob)).all() == []


def test_manual_mode_is_queued_and_resumable_without_worker_or_model(client, monkeypatch):
    monkeypatch.setattr(api_module, "_notify_worker", lambda *_: None)
    result = _post(client)
    assert result.status_code == 200
    bid = result.json()["id"]
    assert result.json()["status"] == "uploaded"
    assert client.get(f"/api/batches/{bid}/tickets").json() == []

    with api_module.SessionLocal() as db:
        claimed = jobqueue.claim_next(db)
        assert claimed is not None
        jobqueue.execute_claimed(
            db, *claimed, extractor_factory=lambda: (_ for _ in ()).throw(
                AssertionError("Never call AI while processing manual batches")
            )
        )

    after = client.get(f"/api/batches/{bid}").json()
    assert after["status"] == "ready_for_review"
    assert len(client.get(f"/api/batches/{bid}/tickets").json()) == 4

    # Process an interrupted job again; manually approved records are not
    # overwritten and no duplicate tickets may be created.
    first_id = client.get(f"/api/batches/{bid}/tickets").json()[0]["id"]
    client.patch(f"/api/tickets/{first_id}", json={"ticket_number": "MANUAL-42"})
    before = client.get(f"/api/batches/{bid}/tickets").json()
    with api_module.SessionLocal() as db:
        batch = db.get(Batch, bid)
        batch.status = "processing"
        db.commit()
        jobqueue.enqueue(db, bid, only_failed=True)
        db.commit()
        claimed = jobqueue.claim_next(db)
        assert claimed is not None
        jobqueue.execute_claimed(db, *claimed, extractor_factory=lambda: None)
    after = client.get(f"/api/batches/{bid}/tickets").json()
    assert [t["id"] for t in before] == [t["id"] for t in after]
    assert after[0]["ticket_number"] == "MANUAL-42"


def test_manual_intake_schema_migration_preserves_original_batch_state(tmp_path):
    import sqlite3
    from pathlib import Path

    dbpath = tmp_path / "legacy-batches.db"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dbpath}")
    command.upgrade(cfg, "20261029_04")
    with sqlite3.connect(dbpath) as db:
        # Earlier migrations may create a fresh table via *current*
        # metadata. Drop the future column to model a genuine older install.
        db.execute("ALTER TABLE batches DROP COLUMN intake_mode")
        db.execute(
            "INSERT INTO batches(id,organization_id,layout,status,created_at) "
            "VALUES (12,1,'single','ready_for_review',CURRENT_TIMESTAMP)"
        )
        db.commit()
    command.upgrade(cfg, "head")
    with sqlite3.connect(dbpath) as db:
        assert db.execute("SELECT intake_mode, status FROM batches WHERE id=12").fetchone() == ("extract", "ready_for_review")
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261029_06"


def test_fictional_demo_generator_is_watermarked_and_wont_overwrite(tmp_path):
    from pathlib import Path
    import pytest
    import pymupdf
    from scripts.make_demo_tickets import make_demo_pdf
    from PIL import Image

    output = tmp_path / "demo.pdf"
    assert make_demo_pdf(output, "grid_2x2") == output
    assert output.read_bytes().startswith(b"%PDF")
    with pymupdf.open(output) as doc:
        assert doc.page_count == 1
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(0.25, 0.25))
        assert pix.width > 100 and pix.height > 100
    with pytest.raises(FileExistsError):
        make_demo_pdf(output, "grid_2x2")
    with pytest.raises(ValueError):
        make_demo_pdf(tmp_path / "not-a-pdf.png", "single")
