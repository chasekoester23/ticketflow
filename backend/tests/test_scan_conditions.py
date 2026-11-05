"""A ticket must never be classified as empty, whatever the scanner did to it.

Reading a slot as "content" or "faint" is safe, because a person sees it either
way. Calling a real ticket "empty" is the failure that could lose a delivery.
Fictional images only; real scanners may differ.
"""
import pytest

from app.services.splitter import assess_slot

from . import scan_conditions as conditions
from .sample_data import TICKETS, make_faint_ticket_image, make_page, make_pdf, make_ticket_image


@pytest.mark.parametrize("name", conditions.STYLES)
def test_a_ticket_is_never_called_empty_under_any_condition(name):
    image = conditions.STYLES[name](make_ticket_image(TICKETS[0]).resize((680, 880)))
    assert assess_slot(image).detection != "empty"


@pytest.mark.parametrize("name", conditions.STYLES)
def test_even_a_faint_ticket_is_never_called_empty(name):
    image = conditions.STYLES[name](make_faint_ticket_image().resize((680, 880)))
    assert assess_slot(image).detection != "empty"


def test_clean_blank_paper_is_empty():
    assert assess_slot(conditions.blank_white()).detection == "empty"


def test_faint_marks_on_grey_paper_arent_missed():
    """Found by this suite: the old check only looked for marks darker than a
    fixed level, so low-contrast paper hid a faint ticket and it read as empty."""
    from PIL import ImageDraw
    image = conditions.Image.new("RGB", (680, 880), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    for y in range(100, 700, 60):
        draw.rectangle([60, y, 620, y + 8], fill=(224, 224, 224))
    assert assess_slot(image).detection == "faint"


def test_tinted_blank_paper_with_nothing_on_it_is_still_empty():
    assert assess_slot(conditions.Image.new("RGB", (680, 880), (250, 240, 190))).detection == "empty"


@pytest.mark.parametrize("blank", [conditions.blank_scanner_noise, conditions.blank_with_edge_shadow, conditions.blank_grey_page])
def test_blank_paper_with_scanner_artifacts_goes_to_a_person_or_stays_empty(blank):
    """Any result is safe here. The point is this never crashes, and a false
    alarm costs one click rather than a lost delivery."""
    assert assess_slot(blank()).detection in ("content", "faint", "empty")


def test_every_condition_survives_the_whole_pipeline_with_nothing_lost(client):
    """Four slots per page of odd scans: every slot is kept, with a record or a question."""
    names = list(conditions.STYLES)
    pages = []
    for start in range(0, len(names), 4):
        slots = [conditions.STYLES[name](make_ticket_image(TICKETS[0])) for name in names[start:start + 4]]
        page = make_page([None] * 4)
        width, height = page.size[0] // 2, page.size[1] // 2
        for index, slot in enumerate(slots):
            page.paste(slot.resize((width - 80, height - 80)), ((index % 2) * width + 40, (index // 2) * height + 40))
        pages.append(page)
    project = client.post("/api/projects", json={"code": "S", "name": "Fictional"}).json()
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": project["id"]},
                           files=[("files", ("odd.pdf", make_pdf(pages), "application/pdf"))])
    assert response.status_code == 200, response.text
    manifest = client.get(f"/api/batches/{response.json()['id']}/manifest").json()
    assert manifest["counts"]["slots"] == 12
    slots = [slot for file in manifest["files"] for page in file["pages"] for slot in page["slots"]]
    assert all(slot["state"] != "empty" for slot in slots)
    assert manifest["counts"]["records"] + manifest["counts"]["suspected_blank"] == 12
