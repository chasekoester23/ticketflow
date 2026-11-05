"""Invoice PDF to reviewed draft to comparison. Fictional PDFs; no network or AI."""
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import func, select

from app.auth import current_user
from app.config import settings
from app.main import app
from app.models import InvoiceComparison, InvoiceDocument, Organization, User
from app.routers import api as api_module
from app.services.invoice_extraction import TextLayerExtractor, get_invoice_extractor, InvoiceExtractionError
from .migration_helpers import alembic_config, head_revision
from .sample_data import TICKETS, make_invoice_pdf, make_page, make_pdf

A, B, *_ = TICKETS
STONE_B = dict(B, material_type=A["material_type"])


def _project(client):
    return client.post("/api/projects", json={"code": "24-115", "name": "Fictional Road Job"}).json()["id"]


def _upload(client, pdf=None, project=None, name="invoice.pdf"):
    data = {"project_id": str(project)} if project else {}
    return client.post("/api/invoice-documents", data=data,
                       files={"file": (name, pdf or make_invoice_pdf(), "application/pdf")})


def _deliveries(client, project):
    supplier = client.post("/api/suppliers", json={"name": A["supplier"]}).json()
    material = client.post("/api/materials", json={"name": A["material_type"]}).json()
    client.put("/api/rates", json={"supplier_id": supplier["id"], "material_id": material["id"], "unit_price": "30"})
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(project)},
                        files=[("files", ("t.pdf", make_pdf([make_page([A, STONE_B, None, None])]), "application/pdf"))]).json()["id"]
    for ticket, values in zip(client.get(f"/api/batches/{batch}/tickets").json(), (A, STONE_B)):
        assert client.patch(f"/api/tickets/{ticket['id']}", json=values).status_code == 200
        assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200


def _as(role, organization=1, uid=1):
    app.dependency_overrides[current_user] = lambda: User(
        id=uid, organization_id=organization, username="u", password_hash="fixture", active=True,
        role=role, must_change_password=False)


def test_upload_drafts_lines_without_guessing_and_keeps_other_money_rows_visible(client):
    project = _project(client)
    response = _upload(client, project=project)
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "needs_review" and document["extractor"] == "text_layer"
    [line] = document["lines"]
    assert line["state"] == "pending_review" and line["page"] == 1
    assert line["invoice_number"] == "EA-2026-1107" and line["supplier_name"] == "Example Aggregates"
    assert line["material_name"] == "No. 53 Crushed Stone" and line["unit"] == "ton"
    assert Decimal(line["billed_quantity"]) == Decimal("46.6")
    assert Decimal(line["billed_subtotal"]) == Decimal("1398")  # the material line, not the invoice total
    assert line["date_from"] == "2026-10-01" and line["date_to"] == "2026-10-31"
    assert line["ticket_list"] == "10482\t24.50\n10483\t22.10"
    assert "No. 53 Crushed Stone" in line["source_text"]
    assert line["blockers"] == [] and line["warnings"] == []
    # Freight, tax and the total never join a material subtotal, and are shown.
    assert any("Freight" in r for r in document["unread_rows"])
    assert any("Sales tax" in r for r in document["unread_rows"])
    assert any("Invoice total" in r for r in document["unread_rows"])
    # Nothing counts until confirmed.
    assert client.get("/api/invoice-comparisons").json() == []


def test_confirmed_draft_gets_exactly_the_same_math_as_typed_entry(client):
    """Two jobs with equivalent deliveries: one invoice typed, one from a PDF."""
    project = _project(client)
    _deliveries(client, project)
    other = client.post("/api/projects", json={"code": "24-200", "name": "Second Fictional Job"}).json()["id"]
    other_a, other_b = dict(A, ticket_number="20482"), dict(STONE_B, ticket_number="20483")
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(other)},
                        files=[("files", ("t.pdf", make_pdf([make_page([other_a, other_b, None, None])]), "application/pdf"))]).json()["id"]
    for ticket, values in zip(client.get(f"/api/batches/{batch}/tickets").json(), (other_a, other_b)):
        assert client.patch(f"/api/tickets/{ticket['id']}", json=values).status_code == 200
        assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200

    typed = client.post("/api/invoice-comparisons", json=dict(
        project_id=other, invoice_number="TYPED-1", supplier_name="Example Aggregates",
        material_name="No. 53 Crushed Stone", date_from="2026-10-01", date_to="2026-10-31",
        billed_tons="46.6", billed_unit_price="30", billed_subtotal="1398.00",
        ticket_list="20482\t24.50\n20483\t22.10"))
    assert typed.status_code == 201, typed.text

    document = _upload(client, project=project).json()
    confirmed = client.post(f"/api/invoice-documents/{document['id']}/lines/{document['lines'][0]['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    from_pdf = client.get(f"/api/invoice-comparisons/{confirmed.json()['comparison_id']}").json()

    typed_saved = client.get(f"/api/invoice-comparisons/{typed.json()['id']}").json()
    assert from_pdf["status"] == "matches", from_pdf["warnings"]
    for field in ("status", "comparison_mode", "billed_tons", "billed_unit_price", "billed_subtotal", "billed_extension",
                  "invoice_arithmetic_difference", "verified_tons", "verified_subtotal", "quantity_difference",
                  "subtotal_difference", "verified_count", "pending_count", "unclassified_count", "unpriced_count",
                  "rate_mismatch_count", "warnings", "ticket_check_counts", "itemized_billed_tons", "date_from", "date_to"):
        assert from_pdf[field] == typed_saved[field], field
    # And the PDF path adds traceability back to the original.
    assert from_pdf["source_document_id"] == document["id"] and from_pdf["source_page"] == 1
    assert typed_saved["source_document_id"] is None


def test_confirm_requires_a_complete_line_in_tons(client):
    project = _project(client)
    document = _upload(client, make_invoice_pdf(period="sometime in October", tickets=(),
                                                rows=(("Sand", "20.000", "CY", "30.0000", "600.00"),)), project=project).json()
    line = document["lines"][0]
    assert line["unit"] == "cy" and Decimal(line["billed_quantity"]) == 20  # never converted
    assert "delivery date from is missing" in line["blockers"]
    url = f"/api/invoice-documents/{document['id']}/lines/{line['id']}"
    refused = client.post(url + "/confirm")
    assert refused.status_code == 422
    assert "tons only" in refused.json()["detail"] and "date" in refused.json()["detail"]
    assert client.get("/api/invoice-comparisons").json() == []

    # A person who reads the invoice can correct it, and each change is recorded.
    assert client.patch(url, json={"date_from": "2026-10-01", "date_to": "2026-10-31", "unit": "ton"}).status_code == 200
    assert client.post(url + "/confirm").status_code == 200
    actions = [e["action"] for e in client.get(f"/api/invoice-documents/{document['id']}/history").json()]
    assert actions[0] == "uploaded" and actions[-1] == "confirmed"
    assert set(actions[1:-1]) == {"edited:unit", "edited:date_from", "edited:date_to"}


def test_multiplication_mismatch_is_highlighted_and_reaches_the_comparison(client):
    project = _project(client)
    pdf = make_invoice_pdf(rows=(("No. 53 Crushed Stone", "46.600", "TON", "30.0000", "1,500.00"),))
    document = _upload(client, pdf, project=project).json()
    line = document["lines"][0]
    assert line["warnings"] == ["Quantity × rate is 1398.00, but the subtotal is 1500.00."]
    confirmed = client.post(f"/api/invoice-documents/{document['id']}/lines/{line['id']}/confirm").json()
    comparison = client.get(f"/api/invoice-comparisons/{confirmed['comparison_id']}").json()
    assert Decimal(comparison["invoice_arithmetic_difference"]) == Decimal("102")


def test_multipage_and_multiple_lines_keep_their_page_and_tickets_stay_unassigned(client):
    project = _project(client)
    pdf = make_invoice_pdf(rows=(("No. 53 Crushed Stone", "46.600", "TON", "30.0000", "1,398.00"),
                                 ("No. 8 Stone", "10.000", "TONS", "20.0000", "200.00")), pages=2)
    document = _upload(client, pdf, project=project).json()
    assert document["page_count"] == 2 and len(document["lines"]) == 2
    assert all(line["ticket_list"] == "" for line in document["lines"])
    assert any("could not be tied to one line" in r for r in document["unread_rows"])
    image = client.get(f"/api/invoice-documents/{document['id']}/pages/2/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert client.get(f"/api/invoice-documents/{document['id']}/pages/3/image").status_code == 404
    assert client.get(f"/api/invoice-documents/{document['id']}/original").content[:4] == b"%PDF"


@pytest.mark.parametrize("pdf, words", [
    (make_invoice_pdf(title="CREDIT MEMO"), "credit memo"),
    (make_invoice_pdf(rows=(("Sand", "-5.000", "TON", "30.0000", "-150.00"),)), "negative"),
])
def test_credits_are_visibly_blocked_and_nothing_is_drafted(client, pdf, words):
    document = _upload(client, pdf).json()
    assert document["status"] == "unsupported" and words in document["message"]
    assert document["lines"] == []


def test_scanned_pdf_is_flagged_and_can_still_be_entered_by_hand(client):
    project = _project(client)
    document = _upload(client, make_pdf([make_page([A, None, None, None])]), project=project).json()
    assert document["status"] == "unsupported" and "scanned" in document["message"]
    line = client.post(f"/api/invoice-documents/{document['id']}/lines?page=1")
    assert line.status_code == 201
    body = line.json()
    assert body["billed_quantity"] is None and body["invoice_number"] is None  # nothing invented
    assert len(body["blockers"]) >= 8
    assert client.post(f"/api/invoice-documents/{document['id']}/lines?page=9").status_code == 422


def test_duplicate_uploads_and_repeated_invoice_numbers_are_refused(client):
    project = _project(client)
    pdf = make_invoice_pdf()
    first = _upload(client, pdf, project=project).json()
    second = _upload(client, pdf, project=project, name="renamed.pdf")
    assert second.status_code == 409 and f"#{first['id']}" in second.json()["detail"]

    url = f"/api/invoice-documents/{first['id']}/lines/{first['lines'][0]['id']}/confirm"
    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 409  # one comparison, however many clicks
    with api_module.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceComparison)) == 1

    # A different file with the same invoice number, for different dates.
    later = _upload(client, make_invoice_pdf(period="11/01/2026 - 11/30/2026"), project=project).json()
    refused = client.post(f"/api/invoice-documents/{later['id']}/lines/{later['lines'][0]['id']}/confirm")
    assert refused.status_code == 409 and "already has a saved line" in refused.json()["detail"]
    assert client.get(f"/api/invoice-documents/{later['id']}").json()["lines"][0]["state"] == "pending_review"


def test_overlap_rule_still_applies_and_a_refused_confirm_changes_nothing(client):
    project = _project(client)
    assert client.post("/api/invoice-comparisons", json=dict(
        project_id=project, invoice_number="OTHER", supplier_name="Example Aggregates",
        material_name="No. 53 Crushed Stone", date_from="2026-10-15", date_to="2026-10-20",
        billed_tons="1", billed_unit_price="1", billed_subtotal="1")).status_code == 201
    document = _upload(client, project=project).json()
    refused = client.post(f"/api/invoice-documents/{document['id']}/lines/{document['lines'][0]['id']}/confirm")
    assert refused.status_code == 409
    line = client.get(f"/api/invoice-documents/{document['id']}").json()["lines"][0]
    assert line["state"] == "pending_review" and line["comparison_id"] is None
    assert len(client.get("/api/invoice-comparisons").json()) == 1


def test_invalid_ticket_list_on_a_draft_is_refused_at_confirm(client):
    project = _project(client)
    document = _upload(client, project=project).json()
    url = f"/api/invoice-documents/{document['id']}/lines/{document['lines'][0]['id']}"
    client.patch(url, json={"ticket_list": "10482\t-5"})
    refused = client.post(url + "/confirm")
    assert refused.status_code == 422 and "Ticket list needs fixes" in refused.json()["detail"]
    assert client.get("/api/invoice-comparisons").json() == []
    assert client.get(f"/api/invoice-documents/{document['id']}").json()["lines"][0]["state"] == "pending_review"


def test_confirmed_lines_are_locked_and_rejected_lines_can_not_be_confirmed(client):
    project = _project(client)
    document = _upload(client, make_invoice_pdf(rows=(("A Stone", "1.000", "TON", "2.0000", "2.00"),
                                                      ("B Stone", "1.000", "TON", "3.0000", "3.00"))), project=project).json()
    a, b = [f"/api/invoice-documents/{document['id']}/lines/{line['id']}" for line in document["lines"]]
    assert client.post(a + "/confirm").status_code == 200
    assert client.patch(a, json={"unit": "cy"}).status_code == 409
    assert client.post(a + "/reject").status_code == 409
    assert client.post(b + "/reject").status_code == 200
    assert client.post(b + "/confirm").status_code == 409
    assert client.post(f"/api/invoice-documents/{document['id']}/void").status_code == 409


def test_wrong_file_can_be_voided_keeping_file_and_history(client):
    pdf = make_invoice_pdf()
    document = _upload(client, pdf).json()
    voided = client.post(f"/api/invoice-documents/{document['id']}/void").json()
    assert voided["status"] == "voided" and voided["lines"][0]["state"] == "rejected"
    assert client.get(f"/api/invoice-documents/{document['id']}/original").status_code == 200
    assert client.post(f"/api/invoice-documents/{document['id']}/lines").status_code == 409
    # The same file can be uploaded again after voiding.
    assert _upload(client, pdf).status_code == 201


@pytest.mark.parametrize("payload, name", [
    (b"not a pdf", "invoice.pdf"),
    (b"%PDF-1.4 broken", "invoice.pdf"),
    (make_invoice_pdf(), "invoice.exe"),
])
def test_bad_uploads_store_nothing(client, payload, name):
    assert _upload(client, payload, name=name).status_code == 400
    assert client.get("/api/invoice-documents").json() == []
    assert not list(Path(settings.storage_dir).glob("invoices/**/*.pdf"))


def test_encrypted_and_oversized_page_counts_are_refused(client, monkeypatch):
    with pymupdf.open(stream=make_invoice_pdf(), filetype="pdf") as doc:
        locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="fictional", owner_pw="fictional")
    assert _upload(client, locked).status_code == 400
    monkeypatch.setattr(settings, "max_pdf_pages", 1)
    assert _upload(client, make_invoice_pdf(pages=2)).status_code == 413


def test_stored_file_is_private(client):
    document = _upload(client).json()
    with api_module.SessionLocal() as db:
        path = Path(db.get(InvoiceDocument, document["id"]).storage_path)
    assert oct(path.stat().st_mode)[-3:] == "600" and path.parent.stat().st_mode & 0o077 == 0
    assert str(path) not in str(document)


def test_extractor_is_explicit_with_no_fallback(client, monkeypatch):
    monkeypatch.setattr(settings, "invoice_extractor", "manual")
    assert get_invoice_extractor() is None
    document = _upload(client).json()
    assert document["extractor"] == "manual" and document["lines"] == []
    monkeypatch.setattr(settings, "invoice_extractor", "gemini")
    with pytest.raises(InvoiceExtractionError):
        get_invoice_extractor()
    failed = _upload(client, make_invoice_pdf(number="OTHER")).json()
    assert failed["status"] == "failed" and "not available" in failed["message"]
    assert failed["lines"] == []


def test_csv_formula_text_in_a_pdf_is_neutralized(client):
    project = _project(client)
    document = _upload(client, make_invoice_pdf(number="=1+1"), project=project).json()
    assert document["lines"][0]["invoice_number"] == "=1+1"  # kept as written
    confirmed = client.post(f"/api/invoice-documents/{document['id']}/lines/{document['lines'][0]['id']}/confirm").json()
    report = client.get(f"/api/invoice-comparisons/{confirmed['comparison_id']}/report.csv").text
    assert "'=1+1" in report and f"Document {document['id']}, page 1" in report


def test_edits_require_a_project_in_this_company_and_other_companies_see_nothing(client):
    project = _project(client)
    document = _upload(client, project=project).json()
    url = f"/api/invoice-documents/{document['id']}"
    line = f"{url}/lines/{document['lines'][0]['id']}"
    assert client.patch(line, json={"project_id": 9999}).status_code == 404
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        db.flush()
        db.add(User(id=2, organization_id=2, username="other", password_hash="fixture", role="owner"))
        db.commit()
    _as("owner", organization=2, uid=2)
    assert client.get("/api/invoice-documents").json() == []
    for path in (url, f"{url}/original", f"{url}/pages/1/image", f"{url}/history"):
        assert client.get(path).status_code == 404, path
    assert client.patch(line, json={"unit": "ton"}).status_code == 404
    for path in (f"{line}/confirm", f"{line}/reject", f"{url}/void", f"{url}/lines"):
        assert client.post(path).status_code == 404, path
    # The same file in another company is a different document.
    assert _upload(client, make_invoice_pdf()).status_code == 201


@pytest.mark.parametrize("role", ["clerk", "viewer"])
def test_only_managers_and_owners_change_documents(client, role):
    document = _upload(client).json()
    url = f"/api/invoice-documents/{document['id']}"
    line = f"{url}/lines/{document['lines'][0]['id']}"
    _as(role)
    assert client.get(url).status_code == 200
    assert _upload(client, make_invoice_pdf(number="X")).status_code == 403
    assert client.patch(line, json={"unit": "ton"}).status_code == 403
    for path in (f"{line}/confirm", f"{line}/reject", f"{url}/void", f"{url}/lines"):
        assert client.post(path).status_code == 403, path


def test_typed_entry_is_unchanged_and_has_no_source(client):
    project = _project(client)
    created = client.post("/api/invoice-comparisons", json=dict(
        project_id=project, invoice_number="TYPED", supplier_name="Example Aggregates",
        material_name="Sand", date_from="2026-10-01", date_to="2026-10-31",
        billed_tons="1", billed_unit_price="1", billed_subtotal="1")).json()
    assert created["source_document_id"] is None
    report = client.get(f"/api/invoice-comparisons/{created['id']}/report.csv").text
    assert "Entered by hand" in report


def test_text_reader_ignores_prose_and_does_not_accept_an_ambiguous_unit():
    result = TextLayerExtractor().extract(make_invoice_pdf(
        rows=(("Sand", "20.000", "T", "30.0000", "600.00"),), extras=(), tickets=()))
    assert result.lines == []  # "T" could mean anything
    assert any("Sand" in row for row in result.unread_rows)


def test_migration_is_additive(tmp_path):
    import sqlite3
    from alembic import command

    path = tmp_path / "before-documents.db"
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "20261105_11")
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision()
        assert conn.execute("SELECT COUNT(*) FROM invoice_documents").fetchone()[0] == 0
