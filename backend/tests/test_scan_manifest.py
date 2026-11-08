"""Scan completeness: every physical slot is on record and reconcilable.

Entirely fictional scans; the extractor is a test fixture, so no AI calls.
"""
import io
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.auth import current_user
from app.config import settings
from app.main import app
from app.models import Batch, Organization, ScanSlot, Ticket, UploadedFile, User
from app.routers import api as api_module
from app.services import pipeline, splitter
from app.services.extraction import ExtractionError
from .conftest import FixtureExtractor
from .migration_helpers import alembic_config, head_revision
from .sample_data import TICKETS, make_page, make_pdf

A, B, C, D = TICKETS


def _upload(client, pdf, *, mode="manual", layout="grid_2x2", **data):
    response = client.post(
        "/api/batches", data={"layout": layout, "intake_mode": mode, **data},
        files=[("files", ("fictional.pdf", pdf, "application/pdf"))],
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _manifest(client, batch):
    response = client.get(f"/api/batches/{batch}/manifest")
    assert response.status_code == 200, response.text
    return response.json()


def _slots(manifest):
    return [s for f in manifest["files"] for p in f["pages"] for s in p["slots"]]


def _tickets(client, batch):
    return client.get(f"/api/batches/{batch}/tickets").json()


def _as_role(role):
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="test-owner", password_hash="fixture",
        active=True, role=role, must_change_password=False)


# ---------------------------------------------------------------- detection

def test_faint_fixture_is_exactly_what_the_old_test_would_have_lost():
    crops = splitter.split_page(make_page(["faint", None, A, None]), "grid_2x2")
    assert splitter.is_blank(crops[0].image)  # the old code discarded this
    assert splitter.assess_slot(crops[0].image).detection == "faint"
    assert splitter.assess_slot(crops[1].image).detection == "empty"
    assert splitter.assess_slot(crops[2].image).detection == "content"


def test_normal_four_up_has_four_ticket_slots_and_full_geometry(client):
    batch = _upload(client, make_pdf([make_page([A, B, C, D])]), expected_ticket_count="4")
    manifest = _manifest(client, batch)
    assert manifest["counts"]["pages"] == 1 and manifest["counts"]["slots"] == 4
    assert manifest["counts"]["records"] == 4
    assert manifest["status"] == "count_matches" and manifest["blockers"] == []
    tickets = {t["position"]: t for t in _tickets(client, batch)}
    assert manifest["files"][0]["sha256"]
    for slot in _slots(manifest):
        assert slot["state"] == "ticket" and slot["ticket_id"] == tickets[slot["position"]]["id"]
        assert len(slot["crop_box"]) == 4 and len(slot["crop_sha256"]) == 64


def test_without_a_count_a_finished_batch_never_claims_every_ticket_was_captured(client):
    batch = _upload(client, make_pdf([make_page([A, B, C, D])]))
    assert _manifest(client, batch)["status"] == "count_not_confirmed"


def test_three_up_with_a_truly_empty_slot_is_visible_and_kept(client):
    batch = _upload(client, make_pdf([make_page([A, B, None, D])]))
    manifest = _manifest(client, batch)
    assert manifest["counts"]["records"] == 3 and manifest["counts"]["empty"] == 1
    empty = next(s for s in _slots(manifest) if s["position"] == 2)
    assert empty["state"] == "empty" and empty["ticket_id"] is None
    image = client.get(f"/api/scan-slots/{empty['id']}/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    # Empty isn't a blocker on its own; a count mismatch is what catches a problem.
    assert manifest["status"] == "count_not_confirmed"
    assert client.put(f"/api/batches/{batch}/expected-count",
                      json={"expected_ticket_count": 4}).json()["status"] == "needs_attention"


def test_faint_slot_blocks_until_a_person_marks_it_and_neighbours_are_untouched(client):
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]))
    tickets = _tickets(client, batch)
    assert len(tickets) == 1
    assert client.patch(f"/api/tickets/{tickets[0]['id']}", json=dict(A, supplier="Corrected Supplier")).status_code == 200
    assert client.post(f"/api/tickets/{tickets[0]['id']}/verify").status_code == 200
    before = next(t for t in _tickets(client, batch))

    manifest = _manifest(client, batch)
    assert manifest["status"] == "needs_attention"
    assert any("look faint" in b for b in manifest["blockers"])
    faint = next(s for s in _slots(manifest) if s["state"] == "suspected_blank")
    after = client.post(f"/api/scan-slots/{faint['id']}/mark-ticket").json()
    assert after["counts"]["records"] == 2 and after["counts"]["suspected_blank"] == 0
    tickets = _tickets(client, batch)
    created = next(t for t in tickets if t["position"] == 0)
    # A real, unapproved record with no invented values.
    assert created["status"] == "needs_review" and created["tonnage"] is None
    assert created["ticket_number"] is None and not created["tonnage_confirmed"]
    assert next(t for t in tickets if t["id"] == before["id"]) == before
    audit = client.get(f"/api/tickets/{created['id']}/audit").json()
    assert audit[0]["field"] == "created_from_scan_slot" and audit[0]["actor"] == "Test Owner"
    # Totals still only count the approved ticket.
    assert Decimal(str(client.get("/api/dashboard").json()["total_tons"])) == Decimal(str(A["tonnage"]))


def test_marking_twice_creates_exactly_one_ticket(client):
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]))
    faint = next(s for s in _slots(_manifest(client, batch)) if s["state"] == "suspected_blank")
    for _ in range(3):
        assert client.post(f"/api/scan-slots/{faint['id']}/mark-ticket").status_code == 200
    assert len(_tickets(client, batch)) == 2
    events = client.get(f"/api/batches/{batch}/scan-events").json()
    assert [e["action"] for e in events] == ["marked_ticket"]


def test_confirm_blank_is_a_named_reversible_manager_decision(client):
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]))
    faint = next(s for s in _slots(_manifest(client, batch)) if s["state"] == "suspected_blank")

    _as_role("clerk")
    assert client.post(f"/api/scan-slots/{faint['id']}/confirm-blank").status_code == 403
    _as_role("owner")
    done = client.post(f"/api/scan-slots/{faint['id']}/confirm-blank").json()
    assert done["counts"]["suspected_blank"] == 0 and done["counts"]["confirmed_blank"] == 1
    assert done["status"] == "count_not_confirmed"
    # Changing your mind is possible, and history shows both.
    again = client.post(f"/api/scan-slots/{faint['id']}/mark-ticket").json()
    assert again["counts"]["confirmed_blank"] == 0 and again["counts"]["records"] == 2
    actions = [e["action"] for e in client.get(f"/api/batches/{batch}/scan-events").json()]
    assert actions == ["confirmed_blank", "marked_ticket"]
    # A slot with a ticket must go through Skip, which keeps ticket history.
    assert client.post(f"/api/scan-slots/{faint['id']}/confirm-blank").status_code == 409


def test_content_slot_cannot_be_declared_blank(client):
    batch = _upload(client, make_pdf([make_page([A, None, None, None])]))
    slot = _slots(_manifest(client, batch))[0]
    assert client.post(f"/api/scan-slots/{slot['id']}/confirm-blank").status_code == 409


def test_all_blank_page_fails_visibly_then_people_can_resolve_it(client):
    batch = _upload(client, make_pdf([make_page(["faint", None, None, None])]))
    meta = client.get(f"/api/batches/{batch}").json()
    assert meta["status"] == "failed" and "No ticket content" in meta["files"][0]["error"]
    manifest = _manifest(client, batch)
    assert manifest["counts"]["slots"] == 4  # nothing discarded
    assert manifest["status"] == "needs_attention"

    for slot in _slots(manifest):
        assert client.post(f"/api/scan-slots/{slot['id']}/confirm-blank").status_code == 200
    settled = client.get(f"/api/batches/{batch}").json()
    assert settled["status"] == "ready_for_review" and settled["files"][0]["error"] is None
    assert _manifest(client, batch)["counts"]["records"] == 0


def test_marking_in_ai_batch_makes_no_ai_call_and_retry_reads_only_that_slot(client, monkeypatch):
    fixture = FixtureExtractor([A, B])
    monkeypatch.setattr(api_module, "get_extractor", lambda: fixture)
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]), mode="extract")
    assert fixture.calls == 1
    original = _tickets(client, batch)[0]
    assert client.post(f"/api/tickets/{original['id']}/verify").status_code == 200
    faint = next(s for s in _slots(_manifest(client, batch)) if s["state"] == "suspected_blank")

    result = client.post(f"/api/scan-slots/{faint['id']}/mark-ticket").json()
    assert fixture.calls == 1  # no AI call from marking
    assert result["counts"]["failed_tickets"] == 1 and result["status"] == "needs_attention"
    assert client.get(f"/api/batches/{batch}").json()["status"] == "failed"

    assert client.post(f"/api/batches/{batch}/retry").status_code == 200
    assert fixture.calls == 2  # only the marked slot was read
    tickets = {t["position"]: t for t in _tickets(client, batch)}
    assert tickets[0]["status"] == "needs_review" and tickets[0]["ticket_number"] == B["ticket_number"]
    assert tickets[1]["status"] == "verified"
    assert client.get(f"/api/batches/{batch}").json()["status"] == "ready_for_review"


def test_entering_an_unread_ticket_by_hand_clears_the_failure_without_retry(client, monkeypatch):
    monkeypatch.setattr(api_module, "get_extractor", lambda: FixtureExtractor([A]))
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]), mode="extract")
    faint = next(s for s in _slots(_manifest(client, batch)) if s["state"] == "suspected_blank")
    client.post(f"/api/scan-slots/{faint['id']}/mark-ticket")
    created = next(t for t in _tickets(client, batch) if t["position"] == 0)
    assert client.patch(f"/api/tickets/{created['id']}", json={"ticket_number": "55120"}).status_code == 200
    assert client.get(f"/api/batches/{batch}").json()["status"] == "ready_for_review"


# ---------------------------------------------------------------- rotation

def test_rotation_is_non_destructive_and_keeps_the_duplicate_guard(client, tmp_path):
    page = make_page([A, None, None, None])
    batch = _upload(client, make_pdf([page]), mode="manual")
    ticket = _tickets(client, batch)[0]
    slot = _slots(_manifest(client, batch))[0]
    width, height = Image.open(_as_bytes(client.get(f"/api/tickets/{ticket['id']}/image"))).size

    assert client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 45}).status_code == 422
    assert client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 90}).status_code == 200
    assert Image.open(_as_bytes(client.get(f"/api/tickets/{ticket['id']}/image"))).size == (height, width)
    audit = client.get(f"/api/tickets/{ticket['id']}/audit").json()
    assert ("scan_rotation", "0", "90") in {(e["field"], e["old"], e["new"]) for e in audit}
    with api_module.SessionLocal() as db:
        row = db.scalar(select(ScanSlot).where(ScanSlot.id == slot["id"]))
        assert (settings.storage_dir / row.crop_path).is_file()  # the original stays
        stored = db.get(Ticket, ticket["id"])
        assert stored.crop_sha256 == slot["crop_sha256"]  # identity of the unrotated crop

    # Rotating back restores the original image.
    client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 0})
    assert Image.open(_as_bytes(client.get(f"/api/tickets/{ticket['id']}/image"))).size == (width, height)

    # An identical scan, rotated, still cannot be approved twice.
    client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 90})
    assert client.patch(f"/api/tickets/{ticket['id']}", json=A).status_code == 200
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    copy_batch = _upload(client, make_pdf([page]), mode="manual")
    copy_ticket = _tickets(client, copy_batch)[0]
    assert client.patch(f"/api/tickets/{copy_ticket['id']}", json=A).status_code == 200
    copy_slot = _slots(_manifest(client, copy_batch))[0]
    client.post(f"/api/scan-slots/{copy_slot['id']}/rotate", json={"rotation": 180})
    assert client.post(f"/api/tickets/{copy_ticket['id']}/verify").status_code == 409


def _as_bytes(response):
    assert response.status_code == 200, response.text
    return io.BytesIO(response.content)


def test_approved_ticket_image_cannot_change_until_approval_is_withdrawn(client):
    batch = _upload(client, make_pdf([make_page([A, None, None, None])]))
    ticket = _tickets(client, batch)[0]
    assert client.patch(f"/api/tickets/{ticket['id']}", json=A).status_code == 200
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    slot = _slots(_manifest(client, batch))[0]
    refused = client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 90})
    assert refused.status_code == 409 and "approved" in refused.json()["detail"]
    assert _tickets(client, batch)[0]["status"] == "verified"

    # Withdrawing is explicit and audited; only then does the image change.
    assert client.post(f"/api/tickets/{ticket['id']}/flag").status_code == 200
    assert client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 90}).status_code == 200
    fields = [e["field"] for e in client.get(f"/api/tickets/{ticket['id']}/audit").json()]
    assert fields.index("status") < fields.index("scan_rotation")


def test_retry_of_rotated_failed_ticket_reads_the_rotated_image(client, monkeypatch):
    class Failing(FixtureExtractor):
        def __init__(self):
            super().__init__([A])
            self.sizes = []
            self.fail = True

        def extract(self, image):
            self.sizes.append(image.size)
            if self.fail:
                raise ExtractionError("provider unavailable")
            return super().extract(image)

    fixture = Failing()
    monkeypatch.setattr(api_module, "get_extractor", lambda: fixture)
    batch = _upload(client, make_pdf([make_page([A, None, None, None])]), mode="extract")
    slot = next(s for s in _slots(_manifest(client, batch)) if s["position"] == 0)
    w, h = fixture.sizes[0]
    assert client.post(f"/api/scan-slots/{slot['id']}/rotate", json={"rotation": 90}).status_code == 200
    fixture.fail = False
    assert client.post(f"/api/batches/{batch}/retry").status_code == 200
    assert fixture.sizes[-1] == (h, w)
    ticket = _tickets(client, batch)[0]
    assert Image.open(_as_bytes(client.get(f"/api/tickets/{ticket['id']}/image"))).size == (h, w)


# ---------------------------------------------------------------- reliability

def test_multipage_slots_and_page_previews(client):
    batch = _upload(client, make_pdf([make_page([A, B, None, None]), make_page([C, None, D, None])]))
    manifest = _manifest(client, batch)
    assert manifest["counts"]["pages"] == 2 and manifest["counts"]["slots"] == 8
    assert manifest["counts"]["records"] == 4 and manifest["counts"]["empty"] == 4
    file_id = manifest["files"][0]["id"]
    for page in (1, 2):
        preview = client.get(f"/api/files/{file_id}/pages/{page}/image")
        assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/files/{file_id}/pages/3/image").status_code == 404
    assert client.get(f"/api/files/{file_id}/pages/0/image").status_code == 404


def test_corrupt_pdf_is_reported_as_failed_not_as_zero_tickets(client):
    batch = _upload(client, b"%PDF-1.4 this is not really a pdf")
    manifest = _manifest(client, batch)
    assert manifest["status"] == "needs_attention"
    assert manifest["counts"]["failed_files"] == 1 and manifest["counts"]["slots"] == 0
    assert manifest["files"][0]["legacy"] is False
    assert any("did not finish processing" in b for b in manifest["blockers"])


def test_repeated_and_interrupted_processing_does_not_duplicate(client, monkeypatch):
    class FailsOnce(FixtureExtractor):
        def extract(self, image):
            if self.calls == 1 and not getattr(self, "tripped", False):
                self.tripped = True
                raise ExtractionError("temporary provider error")
            return super().extract(image)

    fixture = FailsOnce([A, B, C, D])
    monkeypatch.setattr(api_module, "get_extractor", lambda: fixture)
    batch = _upload(client, make_pdf([make_page([A, B, C, D])]), mode="extract")
    assert client.get(f"/api/batches/{batch}").json()["status"] == "failed"
    first = {t["position"]: t["id"] for t in _tickets(client, batch)}
    assert len(first) == 4
    verified = first[0]
    assert client.post(f"/api/tickets/{verified}/verify").status_code == 200

    assert client.post(f"/api/batches/{batch}/retry").status_code == 200
    assert client.get(f"/api/batches/{batch}").json()["status"] == "ready_for_review"
    second = _tickets(client, batch)
    assert {t["position"]: t["id"] for t in second} == first
    assert next(t for t in second if t["id"] == verified)["status"] == "verified"
    with api_module.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ScanSlot)) == 4
        assert db.scalar(select(func.count()).select_from(Ticket)) == 4


def test_decisions_survive_reprocessing(client):
    batch = _upload(client, make_pdf([make_page(["faint", None, None, None])]))
    manifest = _manifest(client, batch)
    faint = next(s for s in _slots(manifest) if s["state"] == "suspected_blank")
    client.post(f"/api/scan-slots/{faint['id']}/rotate", json={"rotation": 180})
    client.post(f"/api/scan-slots/{faint['id']}/mark-ticket")
    with api_module.SessionLocal() as db:
        file = db.scalar(select(UploadedFile).where(UploadedFile.batch_id == batch))
        pipeline.process_file(db, file, "grid_2x2", None, manual=True)
    after = next(s for s in _slots(_manifest(client, batch)) if s["id"] == faint["id"])
    assert after["decision"] == "real_ticket" and after["rotation"] == 180 and after["state"] == "ticket"
    assert len(_tickets(client, batch)) == 1


def test_actions_are_refused_while_the_batch_is_processing(client, monkeypatch):
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]))
    slot = next(s for s in _slots(_manifest(client, batch)) if s["state"] == "suspected_blank")
    with api_module.SessionLocal() as db:
        db.get(Batch, batch).status = "processing"
        db.commit()
    assert _manifest(client, batch)["status"] == "processing"
    for path, body in (("mark-ticket", None), ("confirm-blank", None), ("rotate", {"rotation": 90})):
        assert client.post(f"/api/scan-slots/{slot['id']}/{path}", json=body).status_code == 409


def test_missing_stored_images_return_404_not_a_crash(client):
    batch = _upload(client, make_pdf([make_page([A, None, None, None])]))
    ticket = _tickets(client, batch)[0]
    with api_module.SessionLocal() as db:
        (settings.storage_dir / db.get(Ticket, ticket["id"]).crop_path).unlink()
    assert client.get(f"/api/tickets/{ticket['id']}/image").status_code == 404
    slot = _slots(_manifest(client, batch))[0]
    assert client.get(f"/api/scan-slots/{slot['id']}/image").status_code == 404


def test_older_batch_gets_slots_without_any_ticket_changing(client):
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]))
    ticket = _tickets(client, batch)[0]
    assert client.patch(f"/api/tickets/{ticket['id']}", json=dict(A, supplier="Hand Corrected")).status_code == 200
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    before = _tickets(client, batch)
    with api_module.SessionLocal() as db:  # as if processed before the manifest existed
        for slot in db.scalars(select(ScanSlot)):
            db.delete(slot)
        db.commit()
    legacy = _manifest(client, batch)
    assert legacy["counts"]["legacy_files"] == 1 and legacy["status"] == "needs_attention"
    assert any("older file" in b for b in legacy["blockers"])

    built = client.post(f"/api/batches/{batch}/manifest/rebuild").json()
    assert built["counts"]["legacy_files"] == 0 and built["counts"]["slots"] == 4
    assert built["counts"]["suspected_blank"] == 1  # the lost faint ticket is now visible
    assert _tickets(client, batch) == before
    # Repeating is harmless.
    assert client.post(f"/api/batches/{batch}/manifest/rebuild").json()["counts"]["slots"] == 4


# ---------------------------------------------------------------- counts

@pytest.mark.parametrize("value", [-1, 100001, "many"])
def test_expected_count_is_validated(client, value):
    batch = _upload(client, make_pdf([make_page([A, None, None, None])]))
    assert client.put(f"/api/batches/{batch}/expected-count",
                      json={"expected_ticket_count": value}).status_code == 422


def test_expected_count_reconciles_and_is_audited(client):
    batch = _upload(client, make_pdf([make_page([A, B, None, None])]), expected_ticket_count="3")
    manifest = _manifest(client, batch)
    assert manifest["status"] == "needs_attention"
    assert any("expected 3" in b and "2 are on record" in b for b in manifest["blockers"])
    fixed = client.put(f"/api/batches/{batch}/expected-count", json={"expected_ticket_count": 2}).json()
    assert fixed["status"] == "count_matches"
    [event] = client.get(f"/api/batches/{batch}/scan-events").json()
    assert (event["action"], event["old"], event["new"], event["actor"]) == ("expected_count", "3", "2", "Test Owner")

    # A skipped scan is not a ticket: the count no longer matches.
    ticket = _tickets(client, batch)[0]
    assert client.post(f"/api/tickets/{ticket['id']}/skip").status_code == 200
    skipped = _manifest(client, batch)
    assert skipped["counts"]["records"] == 1 and skipped["counts"]["skipped"] == 1
    assert skipped["status"] == "needs_attention"


# ---------------------------------------------------------------- invoices

def _invoice(client, project):
    return client.post("/api/invoice-comparisons", json=dict(
        project_id=project, invoice_number="EA-2026-1107", supplier_name="Example Aggregates",
        material_name=A["material_type"], date_from="2026-10-01", date_to="2026-10-31",
        billed_tons=A["tonnage"], billed_unit_price="30", billed_subtotal="735.00")).json()


def _priced_project(client):
    project = client.post("/api/projects", json={"code": "24-115", "name": "Fictional Road Job"}).json()["id"]
    supplier = client.post("/api/suppliers", json={"name": A["supplier"]}).json()
    material = client.post("/api/materials", json={"name": A["material_type"]}).json()
    client.put("/api/rates", json={"supplier_id": supplier["id"], "material_id": material["id"], "unit_price": "30"})
    return project


def test_faint_slot_keeps_an_invoice_from_reading_as_a_match(client):
    project = _priced_project(client)
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]), project_id=str(project))
    ticket = _tickets(client, batch)[0]
    assert client.patch(f"/api/tickets/{ticket['id']}", json=A).status_code == 200
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    invoice = _invoice(client, project)
    assert invoice["status"] == "incomplete"
    assert any("faint scan slot" in w for w in invoice["warnings"])

    faint = next(s for s in _slots(_manifest(client, batch)) if s["state"] == "suspected_blank")
    client.post(f"/api/scan-slots/{faint['id']}/confirm-blank")
    cleared = client.get(f"/api/invoice-comparisons/{invoice['id']}").json()
    assert cleared["status"] == "matches" and cleared["warnings"] == []


def test_failed_file_and_count_mismatch_keep_an_invoice_incomplete(client):
    project = _priced_project(client)
    batch = _upload(client, make_pdf([make_page([A, None, None, None])]), project_id=str(project),
                    expected_ticket_count="2")
    ticket = _tickets(client, batch)[0]
    assert client.patch(f"/api/tickets/{ticket['id']}", json=A).status_code == 200
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    invoice = _invoice(client, project)
    assert invoice["status"] == "incomplete"
    assert any("ticket count people expected" in w for w in invoice["warnings"])

    client.put(f"/api/batches/{batch}/expected-count", json={"expected_ticket_count": 1})
    assert client.get(f"/api/invoice-comparisons/{invoice['id']}").json()["status"] == "matches"

    _upload(client, b"%PDF-1.4 broken", project_id=str(project))
    broken = client.get(f"/api/invoice-comparisons/{invoice['id']}").json()
    assert broken["status"] == "incomplete"
    assert any("did not finish processing" in w for w in broken["warnings"])


def test_intake_check_query_count_does_not_grow_with_batches(client):
    from sqlalchemy import event

    from app.services import scan_manifest

    project = _priced_project(client)

    def queries() -> int:
        engine = api_module.SessionLocal.kw["bind"]
        statements = []

        def counter(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", counter)
        try:
            with api_module.SessionLocal() as db:
                scan_manifest.project_intake_issues(db, 1, project)
        finally:
            event.remove(engine, "before_cursor_execute", counter)
        return len(statements)

    _upload(client, make_pdf([make_page(["faint", A, None, None])]), project_id=str(project), expected_ticket_count="2")
    few = queries()
    for number in range(5):
        _upload(client, make_pdf([make_page(["faint", dict(A, ticket_number=str(70000 + number)), None, None])]),
                project_id=str(project), expected_ticket_count="2")
    assert queries() == few


# ---------------------------------------------------------------- isolation

def test_another_company_cannot_see_or_change_any_scan_data(client):
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]))
    manifest = _manifest(client, batch)
    slot = next(s for s in _slots(manifest) if s["state"] == "suspected_blank")
    file_id = manifest["files"][0]["id"]
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        db.commit()
    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=2, username="other", password_hash="fixture", active=True,
        role="owner", must_change_password=False)
    for url in (f"/api/batches/{batch}/manifest", f"/api/batches/{batch}/scan-events",
                f"/api/scan-slots/{slot['id']}/image", f"/api/files/{file_id}/pages/1/image"):
        assert client.get(url).status_code == 404, url
    for url, body in ((f"/api/scan-slots/{slot['id']}/mark-ticket", None),
                      (f"/api/scan-slots/{slot['id']}/confirm-blank", None),
                      (f"/api/scan-slots/{slot['id']}/rotate", {"rotation": 90}),
                      (f"/api/batches/{batch}/manifest/rebuild", None)):
        assert client.post(url, json=body).status_code == 404, url
    assert client.put(f"/api/batches/{batch}/expected-count",
                      json={"expected_ticket_count": 1}).status_code == 404


def test_viewer_can_look_but_not_change_scans(client):
    batch = _upload(client, make_pdf([make_page(["faint", A, None, None])]))
    slot = next(s for s in _slots(_manifest(client, batch)) if s["state"] == "suspected_blank")
    _as_role("viewer")
    assert _manifest(client, batch)["counts"]["slots"] == 4
    assert client.get(f"/api/scan-slots/{slot['id']}/image").status_code == 200
    for path, body in (("mark-ticket", None), ("confirm-blank", None), ("rotate", {"rotation": 90})):
        assert client.post(f"/api/scan-slots/{slot['id']}/{path}", json=body).status_code == 403
    assert client.put(f"/api/batches/{batch}/expected-count",
                      json={"expected_ticket_count": 1}).status_code == 403
    _as_role("clerk")
    assert client.post(f"/api/scan-slots/{slot['id']}/mark-ticket").status_code == 200


# ---------------------------------------------------------------- migration

def test_migration_adds_tables_and_keeps_existing_rows(tmp_path):
    import sqlite3
    from alembic import command

    path = tmp_path / "before-manifest.db"
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "20261104_09")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO batches(id,organization_id,layout,intake_mode,status,created_at) "
                     "VALUES(5,1,'grid_2x2','manual','ready_for_review',CURRENT_TIMESTAMP)")
        conn.commit()
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head_revision()
        assert conn.execute("SELECT id, status, expected_ticket_count FROM batches").fetchall() == [
            (5, "ready_for_review", None)]
        assert conn.execute("SELECT COUNT(*) FROM scan_slots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 0
