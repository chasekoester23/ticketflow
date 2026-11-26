"""Auto layout: finding the tickets on a page with deterministic image processing.

Entirely fictional, synthetic pages (tests/segmentation_pages.py); the extractor is
the test fixture, so no AI calls. These prove behaviour on known geometry, never
real-world accuracy (docs/engineering/segmentation.md).
"""
import io
import random

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.auth import current_user
from app.main import app
from app.models import AuditEvent, Batch, Organization, ScanEvent, ScanSlot, Ticket, UploadedFile, User
from app.routers import api as api_module
from app.services import pipeline, scan_manifest, segmentation, splitter
from app.services.extraction import ExtractionError
from . import segmentation_pages as pages
from .conftest import FixtureExtractor
from .sample_data import TICKETS, make_pdf, make_scan_page

A, B, C, D = TICKETS


def _upload(client, pdf, *, mode="extract", layout="auto"):
    response = client.post("/api/batches", data={"layout": layout, "intake_mode": mode},
                           files=[("files", ("fictional.pdf", pdf, "application/pdf"))])
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


def _boxes(client, batch):
    """Each ticket's crop box in page pixels, from the scan check (tickets don't return it)."""
    return {s["ticket_id"]: s["crop_box"] for s in _slots(_manifest(client, batch)) if s["ticket_id"]}


def _as_role(role, organization_id=1):
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=organization_id, username="test-owner", password_hash="fixture",
        active=True, role=role, must_change_password=False)


def _inside(inner, outer, slack=0):
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


# ---------------------------------------------------------------- detection (unit)

def test_the_2x2_fixture_page_gives_four_regions_in_reading_order():
    result = segmentation.segment_page(make_scan_page(TICKETS))
    assert result.confident and [r.kind for r in result.regions] == ["ticket"] * 4
    truth = pages.grid_page().tickets
    for region, ticket in zip(result.regions, truth):  # top-left, top-right, bottom-left, bottom-right
        assert _inside(ticket, region.box)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6])
def test_one_to_six_tickets_of_irregular_size_and_place_are_each_found_alone(count):
    rng = random.Random(count)
    for _ in range(3):
        page = pages.tickets_page(count, rng, fill=(0.45, 0.95))
        result = segmentation.segment_page(page.image)
        score = pages.score(page, [r.box for r in result.regions])
        assert score["found"] == count and score["extra"] == 0, score
        assert all(r.review is None for r in result.regions)


def test_a_tilted_ticket_is_straightened_and_the_page_is_not_touched():
    for tilt in (-8, -3.5, 3, 6.5):
        page = Image.new("RGB", pages.PAGE, "white")
        pages.place(page, pages.ticket(0), (0, 0, *pages.PAGE), random.Random(1), angle=tilt, fill=(0.6, 0.6))
        before = page.tobytes()
        region = segmentation.segment_page(page).regions[0]
        assert region.angle == pytest.approx(-tilt, abs=0.3)
        crop = segmentation.crop_region(page, region)
        assert page.tobytes() == before  # the original page is never modified
        dark = (np.asarray(crop.convert("L")) < 128).astype(np.uint8)
        residual = cv2.minAreaRect(cv2.findNonZero(dark))[2]
        assert abs((residual + 45) % 90 - 45) < 0.5


def test_a_straight_ticket_is_not_rotated():
    region = segmentation.segment_page(make_scan_page(TICKETS[:1])).regions[0]
    assert region.angle == 0.0


def test_a_blank_page_is_one_whole_page_region_for_the_blank_checks():
    result = segmentation.segment_page(Image.new("RGB", pages.PAGE, "white"))
    assert [(r.kind, r.box, r.review) for r in result.regions] == [("page", (0, 0, *pages.PAGE), None)]


def test_marks_outside_every_ticket_become_their_own_region_for_review():
    page = pages.stray_page(random.Random(3))
    result = segmentation.segment_page(page.image)
    strays = [r for r in result.regions if r.kind == "stray"]
    assert len(strays) == 1 and strays[0].review == segmentation.STRAY_REVIEW
    assert _inside(page.marks[0], strays[0].box)
    assert result.regions[-1].kind == "stray"  # tickets first, so their positions are stable
    assert pages.score(page, [r.box for r in result.regions])["found"] == 2


def test_too_many_areas_falls_back_to_the_whole_page_for_review():
    page = Image.new("RGB", pages.PAGE, "white")
    draw = ImageDraw.Draw(page)
    for row in range(4):
        for col in range(4):
            x, y = 60 + col * 410, 60 + row * 530
            draw.rectangle([x, y, x + 330, y + 440], outline="black", width=4)
            draw.text((x + 30, y + 40), "BOX", fill="black")
    result = segmentation.segment_page(page)
    assert [(r.kind, r.review) for r in result.regions] == [("page", segmentation.TOO_MANY_REVIEW)]
    assert not result.confident


def test_print_that_forms_no_ticket_falls_back_to_the_whole_page():
    """Scattered short marks all over the page: no ticket-sized area. Whole page, reviewed."""
    page = Image.new("RGB", pages.PAGE, "white")
    draw = ImageDraw.Draw(page)
    rng = random.Random(9)
    for _ in range(8):
        x, y = rng.randint(50, 1500), rng.randint(50, 2000)
        draw.line([x, y, x + 60, y], fill="black", width=4)
    result = segmentation.segment_page(page)
    assert len(result.regions) == 1 and result.regions[0].kind == "page"
    assert result.regions[0].review == segmentation.WHOLE_PAGE_REVIEW


def test_no_print_is_ever_left_outside_the_regions_and_every_merge_or_split_is_flagged():
    """The safety property, over every synthetic scenario (about 100 pages)."""
    for name, scenario in pages.scenarios().items():
        for page in scenario:
            result = segmentation.segment_page(page.image)
            assert result.regions, name
            score = pages.score(page, [r.box for r in result.regions], [r.review for r in result.regions])
            assert score["lost"] == 0, (name, score)
            assert score["flagged"] == score["merged"] + score["missed"], (name, score)
            assert score["marks_kept"] == score["marks"], (name, score)


def test_split_page_auto_returns_crops_with_page_boxes_and_notes():
    page = pages.stray_page(random.Random(4))
    crops = splitter.split_page(page.image, "auto")
    assert [c.position for c in crops] == list(range(len(crops)))
    for crop in crops:
        assert _inside(crop.box, (0, 0, *pages.PAGE))
        assert crop.image.size == (crop.box[2] - crop.box[0], crop.box[3] - crop.box[1])
    assert [c.review for c in crops].count(segmentation.STRAY_REVIEW) == 1
    with pytest.raises(ValueError):
        splitter.split_page(page.image, "grid_3x3")


def test_segmentation_is_deterministic():
    page = pages.tickets_page(6, random.Random(7), angle_range=(3, 8))
    assert segmentation.segment_page(page.image) == segmentation.segment_page(page.image)


def test_clamp_box():
    assert splitter.clamp_box([-5, 10.4, 50, 9999], 100, 200) == (0, 10, 50, 200)
    assert splitter.clamp_box([60, 80, 10, 20], 100, 200) == (10, 20, 60, 80)
    assert splitter.clamp_box([150, 10, 160, 20], 100, 200) is None


# ---------------------------------------------------------------- through the API

def _three_ticket_pdf():
    page = pages.tickets_page(3, random.Random(11), fill=(0.5, 0.9), angle_range=(3, 6))
    return page, make_pdf([page.image])


def test_auto_upload_end_to_end_reads_each_found_ticket(client, monkeypatch):
    fixture = FixtureExtractor([A, B, C])
    monkeypatch.setattr(api_module, "get_extractor", lambda: fixture)
    page, pdf = _three_ticket_pdf()
    batch = _upload(client, pdf)
    assert client.get(f"/api/batches/{batch}").json()["layout"] == "auto"
    tickets = _tickets(client, batch)
    assert [t["ticket_number"] for t in tickets] == [A["ticket_number"], B["ticket_number"], C["ticket_number"]]
    assert all(t["status"] == "needs_review" for t in tickets) and fixture.calls == 3
    # Crop boxes are page pixels, so the scan viewer shows each one on its page.
    boxes = _boxes(client, batch)
    for ticket, truth in zip(tickets, page.tickets):
        assert _inside(truth, boxes[ticket["id"]], slack=4)
    manifest = _manifest(client, batch)
    assert manifest["layout"] == "auto" and manifest["counts"]["slots"] == 3 and manifest["counts"]["records"] == 3
    # The crop read was straightened, so it is not simply its box cut from the page.
    image = Image.open(io.BytesIO(client.get(f"/api/tickets/{tickets[0]['id']}/image").content))
    box = boxes[tickets[0]["id"]]
    assert image.size != (box[2] - box[0], box[3] - box[1])


def test_a_review_note_reaches_the_ticket_and_survives_a_retry(client, monkeypatch):
    class FailsTheNote(FixtureExtractor):
        fail = True

        def extract(self, image):
            if self.fail and image.width < 700 and image.height < 300:  # the margin note's crop
                raise ExtractionError("provider unavailable")
            return super().extract(image)

    fixture = FailsTheNote([A, B, C])
    monkeypatch.setattr(api_module, "get_extractor", lambda: fixture)
    page = pages.stray_page(random.Random(3))
    batch = _upload(client, make_pdf([page.image]))
    tickets = _tickets(client, batch)
    assert [t["status"] for t in tickets] == ["needs_review", "needs_review", "failed"]
    with api_module.SessionLocal() as db:
        events = db.scalars(select(ScanEvent).where(ScanEvent.action == pipeline.LAYOUT_REVIEW_EVENT)).all()
        assert [e.new_value for e in events] == ["stray"] and events[0].actor_user_id is None
        boxes = [s.crop_box for s in db.scalars(select(ScanSlot).order_by(ScanSlot.position))]

    # A retry keeps the regions found the first time, even if detection would now differ.
    monkeypatch.setattr(segmentation, "segment_page", lambda image: pytest.fail("pages on record are not re-detected"))
    fixture.fail = False
    assert client.post(f"/api/batches/{batch}/retry").status_code == 200
    tickets = _tickets(client, batch)
    assert [t["status"] for t in tickets] == ["needs_review"] * 3
    assert segmentation.STRAY_REVIEW in tickets[2]["flags"]
    assert list(_boxes(client, batch).values()) == boxes


def test_manual_intake_auto_layout_puts_notes_on_blank_records(client):
    page = pages.stray_page(random.Random(3))
    batch = _upload(client, make_pdf([page.image]), mode="manual")
    tickets = _tickets(client, batch)
    assert len(tickets) == 3 and all(t["ticket_number"] is None for t in tickets)
    assert segmentation.STRAY_REVIEW in tickets[2]["flags"]
    assert all(segmentation.STRAY_REVIEW not in t["flags"] for t in tickets[:2])


def test_a_blank_page_in_auto_layout_is_reported_not_silently_empty(client):
    batch = _upload(client, make_pdf([Image.new("RGB", pages.PAGE, "white")]), mode="manual")
    manifest = _manifest(client, batch)
    assert manifest["counts"]["slots"] == 1 and manifest["counts"]["records"] == 0
    assert manifest["files"][0]["status"] == "failed"
    assert manifest["files"][0]["error"] == scan_manifest.NO_CONTENT_ERROR


def test_unknown_layout_is_refused(client):
    response = client.post("/api/batches", data={"layout": "grid_3x3", "intake_mode": "manual"},
                           files=[("files", ("f.pdf", make_pdf([make_scan_page(TICKETS)]), "application/pdf"))])
    assert response.status_code == 422 and "auto" in response.json()["detail"]


# ---------------------------------------------------------------- adding a missed region

def _file_id(client, batch):
    return _manifest(client, batch)["files"][0]["id"]


def test_a_person_adds_a_region_the_layout_missed(client):
    batch = _upload(client, make_pdf([make_scan_page(TICKETS)]), mode="manual", layout="grid_2x2")
    file_id = _file_id(client, batch)
    box = [100, 120, 700, 900]
    response = client.post(f"/api/files/{file_id}/pages/1/regions", json={"crop_box": box})
    assert response.status_code == 201, response.text
    ticket = response.json()
    assert ticket["status"] == "needs_review" and ticket["position"] == 4
    assert _boxes(client, batch)[ticket["id"]] == box
    assert ticket["ticket_number"] is None and ticket["tonnage"] is None  # nothing invented
    image = Image.open(io.BytesIO(client.get(f"/api/tickets/{ticket['id']}/image").content))
    assert image.size == (600, 780)
    manifest = _manifest(client, batch)
    added = [s for s in _slots(manifest) if s["position"] == 4]
    assert len(added) == 1 and added[0]["decision"] == "real_ticket" and added[0]["ticket_id"] == ticket["id"]
    assert manifest["counts"]["records"] == 5 and manifest["counts"]["slots"] == 5
    with api_module.SessionLocal() as db:
        audit = db.scalars(select(AuditEvent).where(AuditEvent.ticket_id == ticket["id"])).all()
        assert [(a.field, a.actor_user_id) for a in audit] == [("created_from_region", 1)]
        assert db.scalar(select(ScanEvent.action).where(ScanEvent.slot_id == added[0]["id"])) == "added_region"
    # The same box twice is refused, so a double click makes one ticket.
    again = client.post(f"/api/files/{file_id}/pages/1/regions", json={"crop_box": [101, 121, 701, 899]})
    assert again.status_code == 409
    assert len(_tickets(client, batch)) == 5
    # Removing a wrong region is skipping its ticket, which keeps its history.
    assert client.post(f"/api/tickets/{ticket['id']}/skip").status_code == 200
    assert _manifest(client, batch)["counts"]["records"] == 4


def test_adding_a_region_recovers_a_file_with_no_content_detected(client):
    faint = Image.new("RGB", pages.PAGE, "white")
    ImageDraw.Draw(faint).text((300, 300), "barely there", fill=(250, 250, 250))
    batch = _upload(client, make_pdf([faint]), mode="manual")
    manifest = _manifest(client, batch)
    assert manifest["files"][0]["status"] == "failed"
    response = client.post(f"/api/files/{manifest['files'][0]['id']}/pages/1/regions",
                           json={"crop_box": [200, 200, 900, 600]})
    assert response.status_code == 201, response.text
    manifest = _manifest(client, batch)
    assert manifest["files"][0]["status"] == "ready_for_review" and manifest["counts"]["records"] == 1


def test_adding_a_region_is_checked(client):
    batch = _upload(client, make_pdf([make_scan_page(TICKETS)]), mode="manual", layout="grid_2x2")
    file_id = _file_id(client, batch)
    url = f"/api/files/{file_id}/pages/1/regions"
    assert client.post(url, json={"crop_box": [10, 10, 25, 400]}).status_code == 422  # too small
    assert client.post(url, json={"crop_box": [5000, 5000, 6000, 6000]}).status_code == 422  # off the page
    assert client.post(url, json={"crop_box": [1, 2, 3]}).status_code == 422
    assert client.post(f"/api/files/{file_id}/pages/2/regions", json={"crop_box": [0, 0, 100, 100]}).status_code == 404
    assert client.post(f"/api/files/{file_id + 99}/pages/1/regions", json={"crop_box": [0, 0, 100, 100]}).status_code == 404
    with api_module.SessionLocal() as db:
        db.get(Batch, batch).status = "processing"
        db.commit()
    assert client.post(url, json={"crop_box": [0, 0, 300, 300]}).status_code == 409
    with api_module.SessionLocal() as db:
        db.get(Batch, batch).status = "ready_for_review"
        db.commit()
    try:
        _as_role("viewer")
        assert client.post(url, json={"crop_box": [0, 0, 300, 300]}).status_code == 403
        _as_role("clerk")
        assert client.post(url, json={"crop_box": [0, 0, 300, 300]}).status_code == 201
    finally:
        _as_role("owner")
    assert len(_tickets(client, batch)) == 5


def test_another_companys_file_cannot_get_a_region(client):
    batch = _upload(client, make_pdf([make_scan_page(TICKETS)]), mode="manual", layout="grid_2x2")
    file_id = _file_id(client, batch)
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Company"))
        db.commit()
    try:
        _as_role("owner", organization_id=2)
        response = client.post(f"/api/files/{file_id}/pages/1/regions", json={"crop_box": [0, 0, 300, 300]})
        assert response.status_code == 404
    finally:
        _as_role("owner")
    with api_module.SessionLocal() as db:
        assert db.scalar(select(UploadedFile).where(UploadedFile.id == file_id)) is not None
        assert len(db.scalars(select(Ticket).where(Ticket.file_id == file_id)).all()) == 4
