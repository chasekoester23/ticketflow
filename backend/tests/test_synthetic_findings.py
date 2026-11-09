"""What the first synthetic measurement found in TicketFlow's intake, and the
fixes. B1, L1 and L2 were strict xfails on feature/d2-synthetic-tickets; each
now passes and guards its fix (see docs/engineering/splitter-results.md).

The tickets are fictional and drawn by the synthetic generator; the code under
test (render, split, slot detection, upload) is TicketFlow's own.
"""
import io

from app.config import settings
from app.services import splitter
from synthetic import geometry as g
from synthetic.records import generate_records
from synthetic.render import render_ticket
from synthetic.scan import PAGE_SIZE, SCAN_DPI, Placement, compose_page, write_pdf
from synthetic.presets import PRESETS

# A sheet laid perfectly square: no jitter, so geometry is exact.
STILL = {**PRESETS["easy"]["scan"], "rotation_sd": 0.0, "offset_sd": 0.0, "page_rotation_sd": 0.0,
         "page_shift": 0.0, "shear": 0.0, "clip": 0.0, "upside_down": 0.0}


def _pdf(tmp_path, page, name) -> bytes:
    buf = io.BytesIO()
    page.save(buf, format="JPEG", quality=92)
    path = tmp_path / name
    write_pdf(path, [buf.getvalue()])
    return path.read_bytes()


def _ticket(template: str, style: str = "neat") -> Placement:
    rec = next(r for r in generate_records(1, 40) if r["template"] == template)
    drawn = render_ticket(rec, {"style": style, "caps": False}, 1)
    return Placement(rec["ticket_id"], drawn.image, {k: f["box"] for k, f in drawn.labels.items()}, False)


def _clean_single_page_pdf(tmp_path, supplier="ridgeback") -> bytes:
    """One neatly written fictional ticket, squarely on a clean 1-up scan."""
    rec = generate_records(1, 1, suppliers=[supplier])[0]
    ticket = render_ticket(rec, {"style": "neat", "caps": False}, 1).image
    page, _, _ = compose_page([Placement("X", ticket, {}, False)], "single", PRESETS["easy"]["scan"], 1, "finding",
                              photometric=False)
    return _pdf(tmp_path, page, "single.pdf")


def _render(data: bytes):
    return splitter.render_pdf_pages(data, dpi=settings.render_dpi)[0]


def _in_page_px(crop_box):
    scale = settings.render_dpi / SCAN_DPI
    return tuple(v / scale for v in crop_box)


def test_the_ink_is_there_at_full_resolution(tmp_path):
    page = _render(_clean_single_page_pdf(tmp_path))
    grey = page.convert("L")
    ink = sum(grey.histogram()[:140]) / (grey.width * grey.height)
    assert ink > 3 * 0.002  # more than three times the detector's own "content" threshold


def test_b1_a_clean_single_ticket_page_is_detected_as_content(tmp_path):
    """B1 (fixed): the old 200x200 thumbnail averaged a 1-up page's pen strokes
    to light grey, so a clean ticket read as 'faint'."""
    crop = splitter.split_page(_render(_clean_single_page_pdf(tmp_path)), "single")[0]
    assert splitter.assess_slot(crop.image).detection == "content"


def test_b1_a_clean_single_ticket_upload_is_not_reported_as_empty(client, tmp_path):
    data = _clean_single_page_pdf(tmp_path)
    resp = client.post("/api/batches", data={"layout": "single"}, files=[("files", ("one.pdf", data, "application/pdf"))])
    assert resp.status_code == 200
    batch = client.get(f"/api/batches/{resp.json()['id']}").json()
    assert batch["files"][0]["status"] != "failed", batch["files"][0]["error"]


def _grid_page(tmp_path, placements, name):
    page, records, _ = compose_page(placements, "grid_2x2", STILL, 3, name, photometric=False)
    return _render(_pdf(tmp_path, page, name + ".pdf")), {r["position"]: r for r in records}


def test_l1_a_centred_quarter_page_ticket_fits_its_2x2_crop(tmp_path):
    """L1 (fixed): the fixed cut's 1% margin left 4.08 in, too narrow for a
    4.13 in ticket laid squarely in its quarter. The crop now grows to it."""
    page, records = _grid_page(tmp_path, [_ticket("copperleaf_grid")] * 4, "l1")
    crops = splitter.split_page(page, "grid_2x2")
    for crop in crops:
        quad = [tuple(p) for p in records[crop.position]["quad"]]
        assert g.inside_fraction(quad, _in_page_px(crop.box)) == 1.0, crop.position
        assert g.inside_fraction(quad, _in_page_px(crop.fixed_box)) < 1.0  # the old cut really did trim it
        assert splitter.assess_crop(crop).detection == "content"


def test_l2_a_long_ticket_keeps_its_net_tons_in_a_2x2_crop(tmp_path):
    """L2 (fixed): a ticket taller than a quarter, against the top of the glass,
    had its Net Tons box below the fixed cut. Its crop now reaches across the
    quarter line, and the empty slot below it is never taken for a ticket."""
    short = _ticket("copperleaf_grid")
    page, records = _grid_page(tmp_path, [_ticket("northfork_long"), short, None, short], "l2")
    crops = {c.position: c for c in splitter.split_page(page, "grid_2x2")}
    net = [tuple(p) for p in records[0]["fields"]["net_tons"]["quad"]]
    assert g.inside_fraction(net, _in_page_px(crops[0].fixed_box)) < 0.98  # what the fixed cut lost
    assert g.inside_fraction(net, _in_page_px(crops[0].box)) == 1.0
    assert g.inside_fraction([tuple(p) for p in records[0]["quad"]], _in_page_px(crops[0].box)) == 1.0
    assert crops[2].owns_ticket is False and crops[2].foreign_ticket
    assert splitter.assess_crop(crops[2]).detection != "content"  # no phantom from the overhang
