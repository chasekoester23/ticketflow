"""What the first synthetic measurement found in TicketFlow's intake. Strict xfails:
each documents current behaviour, and fails loudly (XPASS) once it changes, so
the entry in docs/engineering/synthetic-tickets.md gets updated with the fix.

The tickets are fictional and drawn by the synthetic generator; the code under
test (render, split, slot detection, upload) is TicketFlow's own.
"""
import io

import pytest

from app.config import settings
from app.services import splitter
from synthetic import geometry as g
from synthetic.records import generate_records
from synthetic.render import render_ticket
from synthetic.scan import PAGE_SIZE, SCAN_DPI, Placement, compose_page, write_pdf
from synthetic.presets import PRESETS
from synthetic.templates import TEMPLATES


def _clean_single_page_pdf(tmp_path, supplier="ridgeback") -> bytes:
    """One neatly written fictional ticket, squarely on a clean 1-up scan."""
    rec = generate_records(1, 1, suppliers=[supplier])[0]
    ticket = render_ticket(rec, {"style": "neat", "caps": False}, 1).image
    page, _, _ = compose_page([Placement("X", ticket, {}, False)], "single", PRESETS["easy"]["scan"], 1, "finding",
                              photometric=False)
    buf = io.BytesIO()
    page.save(buf, format="JPEG", quality=92)
    path = tmp_path / "single.pdf"
    write_pdf(path, [buf.getvalue()])
    return path.read_bytes()


def test_the_ink_is_there_at_full_resolution(tmp_path):
    """Not an xfail: the ticket has plenty of ink, so B1 below is the detector's doing."""
    page = splitter.render_pdf_pages(_clean_single_page_pdf(tmp_path), dpi=settings.render_dpi)[0]
    grey = page.convert("L")
    ink = sum(grey.histogram()[:140]) / (grey.width * grey.height)
    assert ink > 3 * 0.002  # more than three times the detector's own "content" threshold


@pytest.mark.xfail(strict=True, reason=(
    "B1: assess_slot shrinks the whole crop to 200x200 px before counting dark pixels. On a 1-up page "
    "(1700x2200 px) a 0.5 mm pen stroke averages to light grey, so a clean, clearly written ticket reads "
    "as 'faint' (thumbnail ink ~0.0014 vs threshold 0.002; ~0.0096 at full resolution)."))
def test_a_clean_single_ticket_page_is_detected_as_content(tmp_path):
    page = splitter.render_pdf_pages(_clean_single_page_pdf(tmp_path), dpi=settings.render_dpi)[0]
    crop = splitter.split_page(page, "single")[0]
    assert splitter.assess_slot(crop.image).detection == "content"


@pytest.mark.xfail(strict=True, reason=(
    "B1 consequence: with every slot 'faint', process_file finds no content and fails the file with "
    "'No ticket content detected in this file ... upload a corrected document', for a good scan. "
    "Nothing is lost (the manifest lets a person mark the slot), but the message is wrong and every "
    "1-up ticket costs a click and a retry."))
def test_a_clean_single_ticket_upload_is_not_reported_as_empty(client, tmp_path):
    data = _clean_single_page_pdf(tmp_path)
    resp = client.post("/api/batches", data={"layout": "single"}, files=[("files", ("one.pdf", data, "application/pdf"))])
    assert resp.status_code == 200
    batch = client.get(f"/api/batches/{resp.json()['id']}").json()
    assert batch["files"][0]["status"] != "failed", batch["files"][0]["error"]


def _grid_crop(position: int):
    """TicketFlow's crop for a 2x2 slot, in synthetic page px (150 dpi)."""
    scale = settings.render_dpi / SCAN_DPI
    w, h = round(PAGE_SIZE[0] * scale), round(PAGE_SIZE[1] * scale)
    return tuple(v / scale for v in splitter.grid_boxes(w, h, 2, 2)[position])


@pytest.mark.xfail(strict=True, reason=(
    "L1 (known limitation F6, now measured): the 2x2 cut trims 1% of the page from every quadrant edge, "
    "leaving 4.08 in of width. A 4.13 in wide ticket laid squarely in the middle of its quadrant can't fit."))
def test_a_centred_quarter_page_ticket_fits_its_2x2_crop():
    w, h = TEMPLATES["copperleaf_grid"]["size"]
    cx, cy = PAGE_SIZE[0] / 4, PAGE_SIZE[1] / 4
    quad = g.box_quad((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
    assert g.inside_fraction(quad, _grid_crop(0)) == 1.0


@pytest.mark.xfail(strict=True, reason=(
    "L2 (known limitation F6, now measured): a ticket taller than a quadrant, laid against the top of the "
    "glass in a 2x2 batch, has its Net Tons box cut off by the fixed cut; the crop can't hold it."))
def test_a_long_ticket_keeps_its_net_tons_in_a_2x2_crop():
    t = TEMPLATES["northfork_long"]
    w, h = t["size"]
    box = next(f["box"] for f in t["fields"] if f["field"] == "net_tons")
    left, top = PAGE_SIZE[0] / 4 - w / 2, 30  # as the generator lays long tickets: against the glass edge
    quad = g.box_quad((box[0] + left, box[1] + top, box[2] + left, box[3] + top))
    assert g.inside_fraction(quad, _grid_crop(0)) == 1.0
