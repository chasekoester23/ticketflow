"""Contact sheets for eyeballing a synthetic dataset.

Pages: each scan with every ticket's true outline (green fully on the page,
orange deliberately clipped), blank slots crossed, and TicketFlow's fixed 2x2
cut lines dashed in red, so a ticket crossing a cut is plain to see.
Tickets: each drawn ticket with its field boxes and the labelled values.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

from . import STAMP

STAMP_TEXT = STAMP.replace("—", "-")  # the bundled font has no em dash
from .dataset import load
from .render import render_ticket

GREEN, ORANGE, RED, GREY = (20, 150, 60), (230, 120, 0), (210, 30, 30), (150, 150, 150)


def page_image(docs: dict, page: dict) -> Image.Image:
    root = docs["root"]
    if page.get("png") and (root / page["png"]).is_file():
        return Image.open(root / page["png"]).convert("RGB")
    dpi = docs["pages"]["scan_dpi"]
    with fitz.open(root / "scans" / page["file"]) as doc:
        pix = doc[page["page"] - 1].get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _font(size):
    return ImageFont.load_default(size=size)


def annotate_page(docs: dict, page: dict) -> Image.Image:
    img = page_image(docs, page)
    d = ImageDraw.Draw(img)
    w, h = img.size
    if page["layout"] == "grid_2x2":
        for x0, y0, x1, y1 in ((w / 2, 0, w / 2, h), (0, h / 2, w, h / 2)):
            for k in range(0, int(max(x1 - x0, y1 - y0)), 24):
                a = (x0 + (k if x1 > x0 else 0), y0 + (k if y1 > y0 else 0))
                b = (x0 + (min(k + 12, x1 - x0) if x1 > x0 else 0), y0 + (min(k + 12, y1 - y0) if y1 > y0 else 0))
                d.line([a, b], fill=RED, width=3)
    for slot in page["slots"]:
        if slot["ticket_id"] is None:
            cx = (slot["position"] % 2 + 0.5) * w / 2 if page["layout"] == "grid_2x2" else w / 2
            cy = (slot["position"] // 2 + 0.5) * h / 2 if page["layout"] == "grid_2x2" else h / 2
            d.line([(cx - 60, cy - 60), (cx + 60, cy + 60)], fill=GREY, width=5)
            d.line([(cx - 60, cy + 60), (cx + 60, cy - 60)], fill=GREY, width=5)
            continue
        colour = GREEN if slot["fully_visible"] else ORANGE
        quad = [tuple(p) for p in slot["quad"]]
        d.line(quad + [quad[0]], fill=colour, width=5)
        x0, y0 = slot["bbox"][0], slot["bbox"][1]
        label = f"{slot['ticket_id']}  {round(slot['visible_fraction'] * 100)}%  {slot['rotation_deg']:+.1f}°"
        tx, ty = max(4, min(w - 380, x0 + 8)), max(4, y0 + 8)
        d.rectangle([tx - 4, ty - 2, tx + 372, ty + 34], fill=(255, 255, 255))
        d.text((tx, ty), label, fill=colour, font=_font(30))
    return img


def contact_sheet(dataset: Path, out: Path, *, max_pages: int = 16, per_difficulty: int | None = None,
                  thumb_width: int = 420, columns: int = 4) -> Path:
    docs = load(dataset)
    pages = docs["pages"]["pages"]
    if per_difficulty:
        chosen, seen = [], {}
        for p in pages:
            if seen.get(p["difficulty"], 0) < per_difficulty:
                chosen.append(p)
                seen[p["difficulty"]] = seen.get(p["difficulty"], 0) + 1
        pages = chosen
    pages = pages[:max_pages]
    thumbs = []
    for p in pages:
        img = annotate_page(docs, p)
        img.thumbnail((thumb_width, thumb_width * 2))
        thumbs.append((p, img))
    th = max(i.height for _, i in thumbs)
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * (thumb_width + 12) + 12, 60 + rows * (th + 40)), (250, 250, 250))
    d = ImageDraw.Draw(sheet)
    d.text((12, 12), f"{docs['manifest']['name']}: {STAMP_TEXT}", fill=(160, 0, 0), font=_font(22))
    for i, (p, img) in enumerate(thumbs):
        x, y = 12 + (i % columns) * (thumb_width + 12), 60 + (i // columns) * (th + 40)
        d.text((x, y), f"{p['difficulty']} · {p['layout']} · {p['file'].split('-')[-1]} p{p['page']}",
               fill=(30, 30, 30), font=_font(16))
        sheet.paste(img, (x, y + 22))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    return out


def ticket_sheet(dataset: Path, out: Path, *, count: int = 8, columns: int = 4) -> Path:
    """Re-draws tickets (deterministic) and overlays field boxes and labels."""
    docs = load(dataset)
    records = {r["ticket_id"]: r for r in docs["records"]["records"]}
    tiles = []
    for t in docs["labels"]["tickets"][:count]:
        img = render_ticket(records[t["ticket_id"]], t["plan"], docs["manifest"]["seed"]).image.convert("RGB")
        d = ImageDraw.Draw(img)
        for name, f in t["fields"].items():
            colour = RED if f["visible_value"] is None else GREEN
            d.rectangle(f["box"], outline=colour, width=2)
            note = f"{name}={f['visible_value']!s}"
            if f["crossed_out"]:
                note += f" (x {f['struck_value']})"
            if f["ambiguous_chars"]:
                note += " ~" + "".join(a["char"] + a["confusable_with"] for a in f["ambiguous_chars"])
            d.text((f["box"][0] + 2, f["box"][1] - 1), note[:60], fill=colour, font=_font(11))
        d.text((8, img.height - 22), f"{t['ticket_id']} {t['difficulty']} {t['style']}", fill=RED, font=_font(14))
        img.thumbnail((420, 560))
        tiles.append(img)
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * 432 + 12, rows * 572 + 52), (250, 250, 250))
    ImageDraw.Draw(sheet).text((12, 12), STAMP_TEXT, fill=(160, 0, 0), font=_font(20))
    for i, img in enumerate(tiles):
        sheet.paste(img, (12 + (i % columns) * 432, 46 + (i // columns) * 572))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    return out
