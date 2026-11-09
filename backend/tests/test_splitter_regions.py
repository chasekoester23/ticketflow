"""Slot detection at the crop's own resolution (B1) and 2x2 crops that grow to
the ticket (L1, L2), with the rules that keep both safe:

- a real ticket is never called "empty", and no slot reads quieter than the
  old thumbnail test said;
- a blank slot never becomes a ticket, even with a neighbour's overhang in it;
- a crop never holds less than the fixed cut, and growing one never brings in
  another ticket without a check for a person;
- stored geometry is kept on retry, and a crop check survives edits but
  doesn't block approval.

Fictional images only (synthetic generator and drawn fixtures).
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw

from app.config import settings
from app.routers.api import _refresh_flags
from app.services import splitter
from app.services.scan_manifest import recorded_crop
from synthetic import geometry as g

from . import scan_conditions as conditions
from .sample_data import make_faint_ticket_image, make_ticket_image, TICKETS

MINI = Path(__file__).resolve().parents[1] / "eval_data" / "synthetic" / "benchmark-mini"
RANK = {"empty": 0, "faint": 1, "content": 2}
ONE_UP = (1700, 2200)  # letter at 200 dpi


def _one_up(ticket: Image.Image) -> Image.Image:
    page = Image.new("RGB", ONE_UP, "white")
    page.paste(ticket.resize((1400, 1812)), (150, 194))
    return page


# --- B1: 1-up pages -----------------------------------------------------------

def test_a_clean_1up_ticket_is_content():
    assert splitter.assess_slot(_one_up(make_ticket_image(TICKETS[0]))).detection == "content"


def test_a_faint_but_real_1up_ticket_goes_to_a_person_not_empty():
    assert splitter.assess_slot(_one_up(make_faint_ticket_image())).detection in ("faint", "content")


@pytest.mark.parametrize("blank", [
    lambda: Image.new("RGB", ONE_UP, "white"),
    lambda: Image.new("RGB", ONE_UP, (250, 240, 190)),
    lambda: conditions.blank_scanner_noise(ONE_UP),
    lambda: conditions.blank_grey_page(ONE_UP),
])
def test_a_blank_1up_page_is_never_a_ticket(blank):
    assert splitter.assess_slot(blank()).detection != "content"


def test_a_dark_scanner_edge_on_a_blank_page_reads_as_it_always_did():
    """Known and unchanged: a solid dark band down the edge of a blank 1-up page
    was "content" for the old thumbnail test too, and stays so (the thumbnail's
    vote is kept so nothing reads quieter). Open: see splitter-results.md."""
    image = conditions.blank_with_edge_shadow(ONE_UP)
    assert splitter.assess_slot(image).detection == splitter._thumbnail_reading(image, 0.002, 0.0015)


def test_clean_blank_1up_paper_is_empty():
    assert splitter.assess_slot(Image.new("RGB", ONE_UP, "white")).detection == "empty"


@pytest.mark.parametrize("name", conditions.STYLES)
@pytest.mark.parametrize("size", [(680, 880), ONE_UP])
def test_no_slot_reads_quieter_than_the_old_thumbnail_test(name, size):
    image = conditions.STYLES[name](make_ticket_image(TICKETS[0]).resize(size))
    old = splitter._thumbnail_reading(image, 0.002, 0.0015)
    assert RANK[splitter.assess_slot(image).detection] >= RANK[old]


def test_ink_no_longer_depends_on_the_crop_size():
    """The same ticket on a quarter-size and a full-size crop: the old test
    averaged the bigger one's strokes away."""
    ticket = make_ticket_image(TICKETS[0])
    small = splitter.assess_slot(ticket.resize((850, 1100)))
    large = splitter.assess_slot(ticket.resize(ONE_UP))
    assert small.detection == large.detection == "content"
    assert 0.25 < large.ink / small.ink < 4


# --- L1/L2: adaptive 2x2 crops -------------------------------------------------

def test_a_blank_2x2_page_keeps_the_fixed_cut_and_has_no_checks():
    crops = splitter.split_page(Image.new("RGB", ONE_UP, "white"), "grid_2x2")
    assert [c.box for c in crops] == splitter.grid_boxes(*ONE_UP, 2, 2)
    assert not any(c.checks for c in crops)
    assert all(splitter.assess_crop(c).detection == "empty" for c in crops)


def test_split_is_deterministic_and_the_fixed_cut_is_still_available():
    page = Image.new("RGB", ONE_UP, "white")
    ImageDraw.Draw(page).rectangle([30, 30, 820, 1300], outline="black", width=3)
    a, b = splitter.split_page(page, "grid_2x2"), splitter.split_page(page, "grid_2x2")
    assert [c.box for c in a] == [c.box for c in b]
    assert [c.box for c in splitter.split_page(page, "grid_2x2", adaptive=False)] == splitter.grid_boxes(*ONE_UP, 2, 2)


def test_an_implausibly_big_region_falls_back_to_the_fixed_cut_with_a_check():
    """One outline over most of the page is not one ticket we can trust."""
    page = Image.new("RGB", ONE_UP, "white")
    ImageDraw.Draw(page).rectangle([60, 60, 1500, 1900], outline="black", width=4)
    crops = splitter.split_page(page, "grid_2x2")
    owner = next(c for c in crops if c.owns_ticket)
    assert owner.box == owner.fixed_box
    assert splitter.CHECK_CONTINUES in owner.checks


def test_two_tickets_in_one_slot_ask_for_a_check():
    page = Image.new("RGB", ONE_UP, "white")
    draw = ImageDraw.Draw(page)
    for top in (60, 600):
        draw.rectangle([60, top, 700, top + 400], outline="black", width=3)
        for y in range(top + 60, top + 380, 50):
            draw.line([90, y, 650, y], fill="black", width=2)
    assert splitter.CHECK_TWO in splitter.split_page(page, "grid_2x2")[0].checks


def _mini_grid_pages():
    pages = json.loads((MINI / "pages.json").read_text())["pages"]
    scale = settings.render_dpi / json.loads((MINI / "pages.json").read_text())["scan_dpi"]
    by_file: dict[str, list] = {}
    for p in pages:
        if p["layout"] == "grid_2x2":
            by_file.setdefault(p["file"], []).append(p)
    for name, metas in sorted(by_file.items()):
        doc = fitz.open(MINI / "scans" / name)
        for meta in metas:
            pix = doc[meta["page"] - 1].get_pixmap(dpi=settings.render_dpi, alpha=False)
            yield Image.frombytes("RGB", (pix.width, pix.height), pix.samples), meta, scale


@pytest.mark.skipif(not (MINI / "pages.json").exists(), reason="benchmark-mini not present")
def test_benchmark_mini_crops_only_grow_never_make_phantoms_and_never_swallow_silently():
    """Every 2x2 page of the committed synthetic set, against its true geometry."""
    checked = 0
    for image, meta, scale in _mini_grid_pages():
        quads = {s["position"]: [tuple(p) for p in s["quad"]] for s in meta["slots"] if s["ticket_id"]}
        for crop in splitter.split_page(image, "grid_2x2"):
            box = tuple(v / scale for v in crop.box)
            fixed = tuple(v / scale for v in crop.fixed_box)
            where = (meta["file"], meta["page"], crop.position)
            assert fixed[0] >= box[0] and fixed[1] >= box[1] and fixed[2] <= box[2] and fixed[3] <= box[3], where
            if crop.position in quads:
                assert g.inside_fraction(quads[crop.position], box) >= g.inside_fraction(quads[crop.position], fixed)
            else:
                assert splitter.assess_crop(crop).detection != "content", where  # no phantom ticket
            added = sum(g.area(g.clip_to_box(q, box)) - g.area(g.clip_to_box(q, fixed))
                        for p, q in quads.items() if p != crop.position)
            if added > 0.02 * (box[2] - box[0]) * (box[3] - box[1]):
                assert crop.checks, where  # took in part of another ticket: a person must look
            checked += 1
    assert checked >= 80


# --- Stored geometry and crop-check flags --------------------------------------

def test_a_slot_on_record_keeps_its_stored_geometry_on_retry():
    page = Image.new("RGB", ONE_UP, "white")
    crop = splitter.Crop(0, (0, 0, 900, 1200), page.crop((0, 0, 900, 1200)), fixed_box=(17, 22, 833, 1078),
                         owns_ticket=True, checks=(splitter.CHECK_CONTINUES,))
    stored = SimpleNamespace(crop_box=[17, 22, 833, 1078])
    kept = recorded_crop({(1, 0): stored}, 1, crop, page)
    assert kept.box == (17, 22, 833, 1078) and kept.image.size == (816, 1056)
    assert kept.checks == ()  # the checks were about geometry that isn't used
    assert recorded_crop({}, 1, crop, page) is crop


def test_a_crop_check_survives_edits():
    t = SimpleNamespace(ticket_number="1", delivery_date=None, material_type=None, tonnage=None, supplier=None,
                        job_location=None, flags=["Date missing", splitter.CHECK_OVERLAP])
    _refresh_flags(t)
    assert splitter.CHECK_OVERLAP in t.flags


def _page_pdf(name: str, page: int) -> bytes:
    src = fitz.open(MINI / "scans" / name)
    out = fitz.open()
    out.insert_pdf(src, from_page=page - 1, to_page=page - 1)
    return out.tobytes()


@pytest.mark.skipif(not (MINI / "scans").exists(), reason="benchmark-mini not present")
def test_a_flagged_crop_reaches_the_reviewer_and_doesnt_block_approval(client):
    """benchmark-mini hard page 7: a long ticket whose crop crosses the quarter line."""
    project = client.post("/api/projects", json={"code": "SYN", "name": "Fictional"}).json()
    resp = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": project["id"]},
                       files=[("files", ("long.pdf", _page_pdf("benchmark-mini-hard-4up-01.pdf", 7), "application/pdf"))])
    assert resp.status_code == 200, resp.text
    tickets = client.get(f"/api/batches/{resp.json()['id']}/tickets").json()
    flagged = [t for t in tickets if any(f.startswith(splitter.CROP_CHECK_PREFIX) for f in t["flags"])]
    assert flagged, [t["flags"] for t in tickets]
    t = flagged[0]
    edited = client.patch(f"/api/tickets/{t['id']}", json={
        "ticket_number": "F-1", "delivery_date": "2026-01-05", "material_type": "Fictional fill",
        "tonnage": "10.000", "supplier": "Fictional Supply", "job_location": "Fictional site"})
    assert edited.status_code == 200
    assert any(f.startswith(splitter.CROP_CHECK_PREFIX) for f in edited.json()["flags"])
    verified = client.post(f"/api/tickets/{t['id']}/verify")
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "verified"
