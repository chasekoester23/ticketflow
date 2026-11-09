"""Put drawn tickets on scanner pages and degrade them like a scanner would.

Every geometric step is an explicit affine matrix, applied both to the pixels
and to the bookkeeping (ticket corners and field boxes), so each slot's record
says exactly where its ticket ended up, how far it was turned, and how much
of it is still on the page.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageFilter

from . import geometry as g
from .util import np_rng, r2, rng

SCAN_DPI = 150
PAGE_SIZE = (1275, 1650)  # US letter at 150 dpi


@dataclass
class Placement:
    ticket_id: str
    image: Image.Image
    field_boxes: dict  # field -> [x0, y0, x1, y1] in ticket px
    landscape: bool


def _slot_centre(layout: str, position: int) -> tuple[float, float]:
    w, h = PAGE_SIZE
    if layout == "single":
        return w / 2, h / 2
    row, col = divmod(position, 2)
    return (col + 0.5) * w / 2, (row + 0.5) * h / 2


def _placement_matrix(tw, th, centre, angle) -> g.Matrix:
    """Ticket px -> page px: turn about the ticket's centre, then move it to `centre`."""
    m = g.compose(g.rotate(angle), g.translate(-tw / 2, -th / 2))
    return g.compose(g.translate(*centre), m)


def _warp(image: Image.Image, matrix: g.Matrix, size, fill, resample=Image.BICUBIC) -> Image.Image:
    return image.transform(size, Image.AFFINE, data=g.invert(matrix), resample=resample, fillcolor=fill)


def _clip_shift(quad, page, r) -> tuple[float, float]:
    """A shift that pushes a placed ticket partly off the nearest page edge."""
    x0, y0, x1, y1 = g.bounds(quad)
    w, h = page
    gaps = {"left": x0, "right": w - x1, "top": y0, "bottom": h - y1}
    edge = min(gaps, key=gaps.get)
    over = r.uniform(0.10, 0.28) * ((x1 - x0) if edge in ("left", "right") else (y1 - y0))
    push = gaps[edge] + over
    return {"left": (-push, 0), "right": (push, 0), "top": (0, -push), "bottom": (0, push)}[edge]


def compose_page(slots: list[Placement | None], layout: str, scan: dict, seed, page_key: str, *,
                 photometric: bool = True):
    """Returns (page image, slot records). `scan` holds the difficulty's scan settings."""
    r = rng("scan", seed, page_key)
    w, h = PAGE_SIZE
    paper = r.randrange(238, 256)
    page = Image.new("RGB", PAGE_SIZE, (paper, paper, paper - r.randrange(0, 4)))
    records = [{"position": p, "ticket_id": None} for p, s in enumerate(slots) if s is None]
    traits = []  # fixed per ticket across placement attempts
    for position, slot in enumerate(slots):
        if slot is None:
            continue
        base = r.choice([90.0, -90.0]) if layout == "grid_2x2" and slot.landscape else 0.0  # on its side to fit
        upside = r.random() < scan["upside_down"]
        traits.append((position, slot, base + (180.0 if upside else 0.0), upside, r.random() < scan["clip"]))
    # The whole sheet sits crooked and off-centre on the glass.
    shift = scan["page_shift"] * SCAN_DPI
    page_moves = (r.uniform(-shift, shift), r.uniform(-shift, shift), r.gauss(0, scan["page_rotation_sd"]),
                  r.uniform(-scan["shear"], scan["shear"]))
    inner = (3, 3, w - 3, h - 3)
    # Tickets never lie on top of each other, and never leave the page unless the
    # plan says so: either would hide fields the labels say are readable. If
    # jitter breaks that, the page is laid out again more calmly; the last try
    # sits every ticket square in its slot on a straight sheet.
    for calm in (1.0, 0.75, 0.5, 0.25, 0.0):
        tx, ty, turn, skew = page_moves
        page_m = g.compose(g.translate(tx * calm, ty * calm),
                           g.about(g.compose(g.rotate(turn * calm), g.shear(skew * calm)), w / 2, h / 2))
        placed, quads = [], []
        for position, slot, base, upside, clip in traits:
            tw, th = slot.image.size
            cx, cy = _slot_centre(layout, position)
            if layout == "grid_2x2" and th > h / 2 - 20 and abs(base % 180) < 1:
                # A ticket taller than a quadrant: people lay it against the glass edge,
                # and leave the slot next to it empty (page_plan does). It crosses the cut.
                cy = 30 + th / 2 if position < 2 else h - 30 - th / 2
            sd = scan["offset_sd"] * SCAN_DPI * calm
            for attempt in range(12):
                damp = 1 - attempt / 12
                dx, dy = (damp * max(-2.5 * sd, min(2.5 * sd, r.gauss(0, sd))) for _ in range(2))
                angle = base + damp * calm * r.gauss(0, scan["rotation_sd"])
                m = _placement_matrix(tw, th, (cx + dx, cy + dy), angle)
                if clip:
                    sx, sy = _clip_shift([g.apply(m, p) for p in g.box_quad((0, 0, tw, th))], PAGE_SIZE, r)
                    m = g.compose(g.translate(sx, sy), m)
                quad = [g.apply(g.compose(page_m, m), p) for p in g.box_quad((0, 0, tw, th))]
                before = [g.apply(m, p) for p in g.box_quad((0, 0, tw, th))]  # on the glass, before the sheet moves
                on_page = clip or (g.inside_fraction(quad, inner) >= 0.99999
                                   and g.inside_fraction(before, inner) >= 0.99999)
                if on_page and all(g.separated(quad, other, gap=6) for other in quads):
                    break
            else:
                break  # this ticket can't go anywhere: lay the page out again, calmer
            quads.append(quad)
            placed.append((position, slot, m, angle, clip, upside))
        if len(placed) == len(traits):
            break
    else:
        raise RuntimeError(f"{page_key}: can't place these tickets on the page without covering or cutting one")
    for position, slot, m, angle, clip, upside in placed:
        tw, th = slot.image.size
        layer = _warp(slot.image, m, PAGE_SIZE, (0, 0, 0))
        mask = _warp(Image.new("L", (tw, th), 255), m, PAGE_SIZE, 0, Image.BILINEAR)
        page.paste(layer, (0, 0), mask)
    lid = r.randrange(228, 250)
    page = _warp(page, page_m, PAGE_SIZE, (lid, lid, lid))
    page_box = (0, 0, w, h)
    for position, slot, m, angle, clip, upside in placed:
        full = g.compose(page_m, m)
        tw, th = slot.image.size
        quad = [g.apply(full, p) for p in g.box_quad((0, 0, tw, th))]
        visible = g.visible_fraction(g.box_quad((0, 0, tw, th)), m, page_m, page_box)
        fields = {}
        for name, box in slot.field_boxes.items():
            fq = [g.apply(full, p) for p in g.box_quad(box)]
            fields[name] = {"quad": [[r2(x), r2(y)] for x, y in fq],
                            "visible_fraction": round(g.visible_fraction(g.box_quad(box), m, page_m, page_box), 4)}
        a, _, _, d, _, _ = full
        records.append({
            "position": position, "ticket_id": slot.ticket_id,
            "quad": [[r2(x), r2(y)] for x, y in quad],
            "bbox": [r2(v) for v in g.bounds(quad)],
            "bbox_on_page": [r2(v) for v in g.bounds(g.visible_part(g.box_quad((0, 0, tw, th)), m, page_m, page_box))]
            if visible > 0 else None,
            "rotation_deg": r2(float(np.degrees(np.arctan2(-d, a)))),
            "placed_rotation_deg": r2(angle), "upside_down": upside,
            "visible_fraction": round(visible, 4), "fully_visible": visible >= 0.999,
            "intentionally_clipped": clip, "matrix": [round(v, 6) for v in full],
            "placement_matrix": [round(v, 6) for v in m], "fields": fields,
        })
    records.sort(key=lambda s: s["position"])
    info = {"page_matrix": [round(v, 6) for v in page_m]}
    if photometric:
        page, effects = degrade(page, scan, seed, page_key)
        info.update(effects)
    return page, records, info


def degrade(page: Image.Image, scan: dict, seed, page_key: str):
    """Blur, contrast and faintness, uneven light, noise, grey or colour, JPEG."""
    r = rng("degrade", seed, page_key)
    nr = np_rng("noise", seed, page_key)
    blur = r.uniform(*scan["blur"])
    if blur > 0.05:
        page = page.filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(page, dtype=np.float32)
    contrast = r.uniform(*scan["contrast"])
    faint = r.random() < scan["faint"]
    if faint:
        contrast *= 0.55
    a = 255 - (255 - a) * contrast
    h, w = a.shape[:2]
    gradient = r.uniform(0, scan["gradient"])
    if gradient > 0:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        theta = r.uniform(0, 2 * np.pi)
        ramp = (np.cos(theta) * xx / w + np.sin(theta) * yy / h)
        ramp = (ramp - ramp.min()) / max(1e-6, ramp.max() - ramp.min())
        a = a * (1 - gradient * ramp)[..., None]
    sigma = r.uniform(*scan["noise"])
    if sigma > 0:
        a = a + nr.normal(0, sigma, size=a.shape[:2]).astype(np.float32)[..., None]
    a = np.clip(a, 0, 255).astype(np.uint8)
    out = Image.fromarray(a, "RGB")
    grey = r.random() >= scan["colour"]
    if grey:
        out = out.convert("L")
    quality = r.randrange(*scan["jpeg"])
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=quality, optimize=False)
    jpeg = buf.getvalue()
    out = Image.open(io.BytesIO(jpeg))
    out.load()
    return out, {"blur": round(blur, 3), "contrast": round(contrast, 3), "faint_scan": faint,
                 "gradient": round(gradient, 3), "noise_sigma": round(sigma, 2), "greyscale": grey,
                 "jpeg_quality": quality, "_jpeg": jpeg}


def write_pdf(path, jpegs: list[bytes], size=PAGE_SIZE, dpi=SCAN_DPI) -> None:
    """A multi-page PDF of the scanned pages, like a scanner's output. Deterministic bytes."""
    doc = fitz.open()
    width, height = size[0] * 72 / dpi, size[1] * 72 / dpi
    for jpeg in jpegs:
        page = doc.new_page(width=width, height=height)
        page.insert_image(page.rect, stream=jpeg)
    doc.set_metadata({"producer": "TicketFlow synthetic generator", "creator": "TicketFlow synthetic generator",
                      "title": "SYNTHETIC test scan", "creationDate": "", "modDate": ""})
    data = doc.tobytes(garbage=3, deflate=True, no_new_id=True)
    doc.close()
    with open(path, "wb") as fh:
        fh.write(data)
