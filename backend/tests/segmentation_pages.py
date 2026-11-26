"""Synthetic pages for measuring auto layout (ticket segmentation). Entirely made up.

Each page knows where its tickets are, so detection can be scored: a ticket is
"found" when one region holds at least 90% of it and no other ticket's
majority, "merged" when the region holding it also holds another ticket, and
"missed" otherwise. Regions holding no ticket are "extra".

These are SYNTHETIC pages drawn with PIL from tests/sample_data.py tickets.
They measure the algorithm on known geometry, never real-world accuracy.

Run the measurement (from backend/):  python -m tests.segmentation_pages
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .sample_data import TICKETS, make_faint_ticket_image, make_scan_page, make_ticket_image

PAGE = (1700, 2200)  # letter at 200 dpi, the size uploads render to


@dataclass
class Page:
    image: Image.Image
    tickets: list[tuple[int, int, int, int]]  # true bounding box of each ticket, page px
    marks: list[tuple[int, int, int, int]] = None  # non-ticket marks that must still be kept
    angles: list[float] = None  # counter-clockwise tilt each ticket was placed at


def ticket(i: int, *, border: bool = True, size=(850, 1100)) -> Image.Image:
    image = make_ticket_image(TICKETS[i % len(TICKETS)], size=size)
    if not border:
        ImageDraw.Draw(image).rectangle([10, 10, size[0] - 10, size[1] - 10], outline="white", width=4)
    return image


def low_contrast(image: Image.Image, level: int = 165) -> Image.Image:
    """Ink no darker than ``level``: a worn ribbon or a light carbon copy."""
    return image.point(lambda v: level + (v * (255 - level)) // 255)


def place(page: Image.Image, tile: Image.Image, cell, rng: random.Random, *, fill=(0.6, 0.88),
          angle: float = 0.0, inset: int = 45, paper_visible: bool = False) -> tuple[int, int, int, int]:
    """Paste ``tile`` at a random size and offset inside ``cell``; returns its true box: the
    whole sheet when the paper shows against the background, else the printed part."""
    x0, y0, x1, y1 = cell
    room_w, room_h = x1 - x0 - 2 * inset, y1 - y0 - 2 * inset
    ratio = tile.width / tile.height
    for _ in range(50):
        f = rng.uniform(*fill)
        tw = int(min(room_w, room_h * ratio) * f)
        th = int(tw / ratio)
        rot = tile.resize((tw, th)).convert("RGBA").rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        if rot.width <= room_w and rot.height <= room_h:
            break
    left = x0 + inset + rng.randint(0, room_w - rot.width)
    top = y0 + inset + rng.randint(0, room_h - rot.height)
    page.paste(rot, (left, top), rot)
    shown = np.asarray(rot.split()[-1]) > 0
    if not paper_visible:  # white paper on a white scan: only the print can be seen
        shown &= np.asarray(rot.convert("L")) < 245
    ys, xs = np.nonzero(shown)
    # Only the part on the page: a sheet may hang over the edge of the photo.
    return (max(0, left + int(xs.min())), max(0, top + int(ys.min())),
            min(page.width, left + int(xs.max()) + 1), min(page.height, top + int(ys.max()) + 1))


def cells(count: int, size=PAGE) -> list[tuple[int, int, int, int]]:
    w, h = size
    if count == 1:
        return [(0, 0, w, h)]
    if count == 2:
        return [(0, 0, w, h // 2), (0, h // 2, w, h)]
    if count == 3:  # two small side by side, one wide below
        return [(0, 0, w // 2, h * 2 // 5), (w // 2, 0, w, h * 2 // 5), (0, h * 2 // 5, w, h)]
    if count == 4:
        return [(c * w // 2, r * h // 2, (c + 1) * w // 2, (r + 1) * h // 2) for r in range(2) for c in range(2)]
    if count == 5:
        return [(c * w // 2, r * h // 3, (c + 1) * w // 2, (r + 1) * h // 3) for r in range(3) for c in range(2)][:5]
    if count == 6:
        return [(c * w // 2, r * h // 3, (c + 1) * w // 2, (r + 1) * h // 3) for r in range(3) for c in range(2)]
    raise ValueError(count)


def add_noise(image: Image.Image, rng: random.Random, sigma: float = 14, salt: float = 0.004) -> Image.Image:
    generator = np.random.default_rng(rng.randint(0, 2**31))
    a = np.asarray(image.convert("RGB")).astype(np.int16)
    a = a + generator.normal(0, sigma, a.shape[:2])[..., None].astype(np.int16)
    speck = generator.random(a.shape[:2])
    a[speck < salt / 2] = 0
    a[speck > 1 - salt / 2] = 255
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def tickets_page(count: int, rng: random.Random, *, border=True, angle_range=(0.0, 0.0), contrast=None,
                 faint=False, fill=(0.6, 0.88), background=None, inset=45) -> Page:
    page = Image.new("RGB", PAGE, background or "white")
    boxes, angles = [], []
    for i, cell in enumerate(cells(count)):
        tile = make_faint_ticket_image() if faint else ticket(rng.randint(0, 3), border=border)
        if contrast:
            tile = low_contrast(tile, contrast)
        lo, hi = angle_range
        angle = rng.choice((-1, 1)) * rng.uniform(lo, hi) if hi else 0.0
        boxes.append(place(page, tile, cell, rng, fill=fill, angle=angle, inset=inset))
        angles.append(angle)
    return Page(page, boxes, [], angles)


def photo_page(count: int, rng: random.Random, *, inset: int = 30, fill=(0.6, 0.88)) -> Page:
    """Tickets photographed on a grey table: uneven light, tilt, blur and sensor noise."""
    w, h = PAGE
    yy, xx = np.mgrid[0:h, 0:w]
    light = 125 + 30 * (xx / w) - 20 * (yy / h)
    table = np.stack([light, light * 0.97, light * 0.92], axis=-1).clip(0, 255).astype(np.uint8)
    page = Image.fromarray(table)
    angles = [rng.choice((-1, 1)) * rng.uniform(2, 8) for _ in range(count)]
    boxes = [place(page, ticket(rng.randint(0, 3), border=rng.random() < 0.5), cell, rng,
                   angle=angle, paper_visible=True, inset=inset, fill=fill) for cell, angle in zip(cells(count), angles)]
    page = page.filter(ImageFilter.GaussianBlur(1.2))
    return Page(add_noise(page, rng, sigma=6, salt=0.0), boxes, [], angles)


def stray_page(rng: random.Random) -> Page:
    """Two tickets on the left and a handwritten-style note in the right margin, on neither."""
    page = Image.new("RGB", PAGE, "white")
    boxes = [place(page, ticket(rng.randint(0, 3)), cell, rng) for cell in ((0, 0, 1100, 1100), (0, 1100, 1100, 2200))]
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=40)
    top = rng.randint(300, 1800)
    draw.text((1200, top), "call yard", fill=(30, 30, 90), font=font)
    draw.text((1200, top + 60), "re: load 4", fill=(30, 30, 90), font=font)
    left, upper, _, _ = draw.textbbox((1200, top), "call yard", font=font)
    _, _, right, lower = draw.textbbox((1200, top + 60), "re: load 4", font=font)
    return Page(page, boxes, [(left, upper, right, lower)])


def grid_page() -> Page:
    """The 2x2 page every other test uses (tests/sample_data.make_scan_page)."""
    w, h = PAGE[0] // 2, PAGE[1] // 2
    boxes = [((i % 2) * w + 40, (i // 2) * h + 40, (i % 2) * w + 40 + w - 80, (i // 2) * h + 40 + h - 80)
             for i in range(4)]
    return Page(make_scan_page(TICKETS), boxes, [])


def scenarios() -> dict[str, list[Page]]:
    rng = random.Random(20261125)
    out: dict[str, list[Page]] = {}
    out["1 per page, any size and place"] = [tickets_page(1, rng, fill=(0.35, 0.95)) for _ in range(5)]
    out["2 per page"] = [tickets_page(2, rng) for _ in range(5)]
    out["3 per page, irregular sizes"] = [tickets_page(3, rng, fill=(0.45, 0.95)) for _ in range(5)]
    out["4 per page, 2x2 test fixture"] = [grid_page()]
    out["4 per page, irregular"] = [tickets_page(4, rng, fill=(0.45, 0.95)) for _ in range(5)]
    out["5 per page"] = [tickets_page(5, rng) for _ in range(3)]
    out["6 per page"] = [tickets_page(6, rng) for _ in range(5)]
    out["rotated 3-8 degrees, 4 per page"] = [tickets_page(4, rng, angle_range=(3, 8)) for _ in range(5)]
    out["rotated 3-8 degrees, 6 per page"] = [tickets_page(6, rng, angle_range=(3, 8)) for _ in range(5)]
    out["no printed border, 4 per page"] = [tickets_page(4, rng, border=False) for _ in range(5)]
    out["low contrast (ink no darker than 165)"] = [tickets_page(4, rng, contrast=165) for _ in range(5)]
    out["faint print (ink 205)"] = [tickets_page(4, rng, faint=True) for _ in range(3)]
    noisy = []
    for _ in range(5):
        p = tickets_page(4, rng, angle_range=(0, 5))
        noisy.append(Page(add_noise(p.image, rng), p.tickets, [], p.angles))
    out["scanner noise (sigma 14, 0.4% specks), tilt up to 5"] = noisy
    out["no printed border, 1 large ticket"] = [tickets_page(1, rng, border=False, fill=(0.8, 0.97)) for _ in range(5)]
    out["no printed border, 6 per page"] = [tickets_page(6, rng, border=False) for _ in range(5)]
    out["close together (gaps from 0.2 in), 6 per page"] = [tickets_page(6, rng, fill=(0.9, 0.99), inset=20)
                                                            for _ in range(5)]
    out["no border and close together (gaps from 0.2 in), 4 per page"] = [
        tickets_page(4, rng, border=False, fill=(0.9, 0.99), inset=20) for _ in range(5)]
    stress = []
    for _ in range(5):
        p = tickets_page(6, rng, angle_range=(3, 8), contrast=150)
        stress.append(Page(add_noise(p.image, rng), p.tickets, [], p.angles))
    out["6 per page: tilt 3-8, low contrast and noise together"] = stress
    out["photo-like, grey table, 6 per page"] = [photo_page(6, rng) for _ in range(5)]
    out["photo-like, 4 sheets touching or overlapping"] = [photo_page(4, rng, inset=-40, fill=(0.85, 0.95))
                                                           for _ in range(5)]
    out["photo-like, grey table, 3 per page"] = [photo_page(3, rng) for _ in range(5)]
    out["photo-like, grey table, 4 per page"] = [photo_page(4, rng) for _ in range(5)]
    out["2 tickets plus a margin note"] = [stray_page(rng) for _ in range(3)]
    out["blank page"] = [Page(Image.new("RGB", PAGE, "white"), [], [])]
    return out


def score(page: Page, boxes, reviews=None) -> dict[str, int]:
    """For one page: tickets found alone, merged with another, or split (no single region
    holds 90% of it); how many merged or split tickets had a review note on a region holding
    part of them; tickets whose print is not kept (under 99% of its dark pixels inside the
    regions put together); extra regions; and margin marks kept."""
    reviews = reviews or [None] * len(boxes)
    def inter(a, b):
        return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))

    def area(a):
        return (a[2] - a[0]) * (a[3] - a[1])

    found = merged = missed = flagged = lost = 0
    used = set()
    covered = np.zeros((page.image.height, page.image.width), bool)
    for b in boxes:
        covered[b[1]:b[3], b[0]:b[2]] = True
    gray = np.asarray(page.image.convert("L"))
    for t in page.tickets:
        window = gray[t[1]:t[3], t[0]:t[2]]
        dark = window < np.median(window) - 40  # the ticket's print, against its own paper
        if dark.any() and covered[t[1]:t[3], t[0]:t[2]][dark].mean() < 0.99:
            lost += 1
        holders = [i for i, b in enumerate(boxes) if inter(t, b) >= 0.9 * area(t)]
        touching = [i for i, b in enumerate(boxes) if inter(t, b) > 0.05 * area(t)]
        if not holders:
            missed += 1
            flagged += any(reviews[i] for i in touching)
            continue
        used.update(holders)
        shared = any(inter(o, boxes[i]) >= 0.5 * area(o) for i in holders for o in page.tickets if o is not t)
        if shared:
            merged += 1
            flagged += any(reviews[i] for i in holders)
        else:
            found += 1
    marks_kept = sum(1 for m in (page.marks or []) if any(inter(m, b) >= 0.9 * area(m) for b in boxes))
    for m in page.marks or []:
        used.update(i for i, b in enumerate(boxes) if inter(m, b) >= 0.9 * area(m))
    extra = sum(1 for i in range(len(boxes)) if i not in used)
    if not page.tickets and len(boxes) == 1:
        extra = 0  # a blank page is one region on record, as with every layout
    return {"present": len(page.tickets), "found": found, "merged": merged, "missed": missed,
            "flagged": flagged, "lost": lost, "extra": extra, "marks": len(page.marks or []), "marks_kept": marks_kept}


def tilt_errors(page: Page, regions) -> list[float]:
    """For each tilted ticket (at least 1 degree), how far its region's straightening is off, in degrees."""
    errors = []
    for t, angle in zip(page.tickets, page.angles or []):
        if abs(angle) < 1:
            continue
        holder = max(regions, key=lambda r: max(0, min(t[2], r.box[2]) - max(t[0], r.box[0]))
                     * max(0, min(t[3], r.box[3]) - max(t[1], r.box[1])))
        errors.append(abs(holder.angle + angle))
    return errors


def measure() -> list[dict]:
    import time

    from app.services import segmentation

    rows = []
    for name, pages in scenarios().items():
        total = {"scenario": name, "pages": len(pages), "present": 0, "found": 0, "merged": 0, "missed": 0,
                 "flagged": 0, "lost": 0, "extra": 0, "marks": 0, "marks_kept": 0, "whole_page": 0, "review": 0, "tilts": [], "seconds": 0.0}
        for page in pages:
            started = time.perf_counter()
            result = segmentation.segment_page(page.image)
            total["seconds"] += time.perf_counter() - started
            for key, value in score(page, [r.box for r in result.regions], [r.review for r in result.regions]).items():
                total[key] += value
            total["whole_page"] += any(r.kind == "page" for r in result.regions) and bool(page.tickets)
            total["review"] += sum(1 for r in result.regions if r.review)
            total["tilts"] += tilt_errors(page, result.regions)
        rows.append(total)
    return rows


if __name__ == "__main__":
    rows = measure()
    print("| Scenario (synthetic) | Pages | Tickets | Found alone | Recall | Merged | Split | Merged/split with a note "
          "| Content lost | Extra regions | Whole-page fallbacks | Review notes | Tilt error, mean / max (tickets) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        recall = f"{r['found'] / r['present']:.0%}" if r["present"] else "n/a"
        tilt = (f"{sum(r['tilts']) / len(r['tilts']):.1f} / {max(r['tilts']):.1f} deg ({len(r['tilts'])})"
                if r["tilts"] else "-")
        print(f"| {r['scenario']} | {r['pages']} | {r['present']} | {r['found']} | {recall} | {r['merged']} | "
              f"{r['missed']} | {r['flagged']} of {r['merged'] + r['missed']} | {r['lost']} | {r['extra']} | "
              f"{r['whole_page']} | {r['review']} | {tilt} |")
    for r in rows:
        if r["marks"]:
            print(f"{r['scenario']}: {r['marks_kept']} of {r['marks']} margin notes kept in a region")
    pages = sum(r["pages"] for r in rows)
    print(f"{pages} pages, {sum(r['seconds'] for r in rows) / pages * 1000:.0f} ms per page on average")

