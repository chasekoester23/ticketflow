"""Prelaunch guardrails for private construction documents and exports.

Tests use fictional scans and never invoke real AI providers.
"""
import io
import stat
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pymupdf
from openpyxl import load_workbook
from PIL import Image

from app.config import settings
from app.services.export import build_workbook
from .sample_data import make_scan_pdf


def upload(client, filename: str, data: bytes, *, layout="grid_2x2"):
    return client.post(
        "/api/batches",
        data={"layout": layout, "intake_mode": "manual"},
        files=[("files", (filename, data, "application/octet-stream"))],
    )


def test_unexpected_host_rejected_but_local_development_still_works(client):
    assert client.get("/health").status_code == 200
    denied = client.get("/health", headers={"Host": "unapproved.ticketflow.example"})
    assert denied.status_code == 400
    assert client.get("/api/records", headers={"Host": "unapproved.ticketflow.example"}).status_code == 400
    allowed = client.get("/api/records")
    assert allowed.status_code == 200
    assert allowed.headers["x-frame-options"] == "DENY"
    assert allowed.headers["referrer-policy"] == "no-referrer"


def test_excess_file_count_is_rejected_before_saving(client, monkeypatch):
    monkeypatch.setattr(settings, "max_batch_files", 1)
    pdf = make_scan_pdf()
    result = client.post(
        "/api/batches",
        data={"layout": "grid_2x2", "intake_mode": "manual"},
        files=[
            ("files", ("one.pdf", pdf, "application/pdf")),
            ("files", ("two.pdf", pdf, "application/pdf")),
        ],
    )
    assert result.status_code == 413
    assert client.get("/api/batches").json() == []
    assert not (settings.storage_dir / "uploads").exists()


def test_excess_total_batch_bytes_is_rejected_atomically(client, monkeypatch):
    monkeypatch.setattr(settings, "max_batch_total_bytes", 1024)
    result = upload(client, "scan.pdf", make_scan_pdf())
    assert result.status_code == 413
    assert client.get("/api/batches").json() == []
    assert not (settings.storage_dir / "uploads").exists()


def test_pdf_page_limit_fails_visibly_without_creating_tickets(client, monkeypatch):
    monkeypatch.setattr(settings, "max_pdf_pages", 2)
    with pymupdf.open() as document:
        for _ in range(3):
            document.new_page(width=612, height=792)
        pdf = document.tobytes()
    result = upload(client, "long-scan.pdf", pdf)
    assert result.status_code == 200
    bid = result.json()["id"]
    batch = client.get(f"/api/batches/{bid}").json()
    assert batch["status"] == "failed"
    assert "per-file limit is 2" in batch["files"][0]["error"]
    assert client.get(f"/api/batches/{bid}/tickets").json() == []


def test_oversized_pdf_dimensions_rejected_before_rasterization(client):
    with pymupdf.open() as document:
        document.new_page(width=10000, height=10000)
        pdf = document.tobytes()
    result = upload(client, "enormous-page.pdf", pdf)
    assert result.status_code == 200
    bid = result.json()["id"]
    batch = client.get(f"/api/batches/{bid}").json()
    assert batch["status"] == "failed"
    assert "too large to render safely" in batch["files"][0]["error"]
    assert client.get(f"/api/batches/{bid}/tickets").json() == []


def test_compressed_image_dimensions_rejected_before_decode(client, monkeypatch):
    monkeypatch.setattr(settings, "max_image_pixels", 2000)
    image = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(image, format="PNG")
    result = upload(client, "large.png", image.getvalue(), layout="single")
    assert result.status_code == 200
    bid = result.json()["id"]
    batch = client.get(f"/api/batches/{bid}").json()
    assert batch["status"] == "failed"
    assert "Image dimensions are too large" in batch["files"][0]["error"]


def test_new_scans_and_crops_are_private_on_disk(client):
    result = upload(client, "sample.pdf", make_scan_pdf())
    assert result.status_code == 200
    bid = result.json()["id"]
    batch = client.get(f"/api/batches/{bid}").json()
    assert batch["status"] == "ready_for_review"
    scans_dir = settings.storage_dir / "uploads" / str(bid)
    crops_dir = settings.storage_dir / "crops" / str(bid)
    scans = list(scans_dir.glob("*.pdf"))
    crops = list(crops_dir.glob("*.png"))
    assert len(scans) == 1
    assert len(crops) == 4
    for folder in (scans_dir, crops_dir):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for file in scans + crops:
        assert stat.S_IMODE(file.stat().st_mode) == 0o600


def test_excel_export_neutralizes_formulas_but_keeps_numeric_values():
    ticket = SimpleNamespace(
        id=123,
        ticket_number="=1+1",
        delivery_date=date(2026, 10, 15),
        material_type="=HYPERLINK(\"https://example.test\",\"click\")",
        tonnage=Decimal("1.500"),
        supplier="+SUM(2,3)",
        job_location="  @SUM(1,1)",
        status="verified",
        tonnage_confirmed=True,
        file=SimpleNamespace(original_name="=WEBSERVICE(\"https://example.test\").pdf"),
        source_page=1,
        position=0,
        verified_by_user_id=9,
        verified_at=datetime(2026, 10, 16, 15, 4),
    )
    # A person's display name is user-controlled text too.
    workbook = load_workbook(io.BytesIO(build_workbook(
        [ticket], {123: Decimal("2.00")}, {9: "=cmd|' /C calc'!A0"},
    )))
    sheet = workbook["Delivery Records"]
    for letter in ("A", "C", "E", "F", "J", "K"):
        cell = sheet[f"{letter}2"]
        assert cell.data_type != "f", (letter, cell.value)
        assert cell.value.startswith("'"), (letter, cell.value)
    assert sheet["D2"].value == 1.5
    assert sheet["G2"].value == 2
    assert sheet["H2"].value == 3
    summary = workbook["Summary"]
    for row in summary.iter_rows():
        for cell in row:
            assert cell.data_type != "f"
