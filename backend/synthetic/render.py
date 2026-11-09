"""Draw one fictional ticket from its record, template and rendering plan.

Returns the image, a label per field, and the canvas log of what was drawn.
The label says what the ticket MEANS (true_value), what the writer put on paper
(written_value, struck_value) and what can still be read (visible_value).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from PIL import Image, ImageFilter

from .handwriting import STYLES, Canvas, RenderError, hand_text, smudge, strike, _mix
from .templates import ALIASES, TEMPLATES
from .util import rng

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
STAMP_RED = (176, 32, 44)
FOOTER = "SYNTHETIC TEST DOCUMENT - fictional supplier - not a real ticket"
HAND_INKS = ("hand", "check")


def format_date(iso: str, fmt: str) -> str:
    d = date.fromisoformat(iso)
    return {
        "M/D/YY": f"{d.month}/{d.day}/{d.year % 100:02d}",
        "MM/DD/YY": f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}",
        "M-D-YY": f"{d.month}-{d.day}-{d.year % 100:02d}",
        "M/D/YYYY": f"{d.month}/{d.day}/{d.year}",
        "MM/DD/YYYY": f"{d.month:02d}/{d.day:02d}/{d.year}",
        "Mon D YYYY": f"{MONTHS[d.month - 1]} {d.day} {d.year}",
    }[fmt]


def _thousands(value: str) -> str:
    return f"{int(value):,}" if value.isdigit() else value


def written_value(record: dict, spec: dict, plan: dict) -> str | None:
    """The text the form shows for a field, before any effect."""
    key = ALIASES.get(spec["field"], spec["field"])
    value = record.get(key)
    if value is None:
        return None
    hand = spec["ink"] in HAND_INKS
    if key == "delivery_date":
        return format_date(value, spec.get("fmt") or "M/D/YY")
    if key == "net_tons" and hand and plan.get("trim_tons"):
        text = value.rstrip("0").rstrip(".") if "." in value else value
        return text
    if key in ("gross", "tare", "net_weight") and not hand and record.get("weight_unit") == "lb":
        return _thousands(value)
    if hand and plan.get("caps") and key not in ("ticket_number",):
        return value.upper()
    return value


def true_value(record: dict, spec: dict):
    return record.get(ALIASES.get(spec["field"], spec["field"]))


@dataclass
class TicketRender:
    image: Image.Image
    labels: dict
    log: dict


def _print_fit(canvas: Canvas, text: str, box, size: float) -> float:
    x0, y0, x1, y1 = box
    size = min(size, (y1 - y0) * 0.7)
    while size > 7 and canvas.text_width(text, size) > (x1 - x0) - 8:
        size -= 1
    return size


def _draw_form(canvas: Canvas, record: dict, t: dict) -> None:
    w, h = t["size"]
    head = t["header"]
    sup = next(f for f in t["fields"] if f["field"] == "supplier")
    sx0, sy0, sx1, sy1 = sup["box"]
    canvas.print_text(None, f"{record['supplier_town']}  -  Ph {record['supplier_phone']}", sx0, sy1 + 4, 12,
                      (70, 70, 70))
    canvas.print_text(None, head["title"], w - 22, sy1 + 22, 14, (40, 40, 40), anchor="ra")
    if head.get("rule"):
        canvas.line([(16, sy1 + 42), (w - 16, sy1 + 42)], 2.2)
    if head.get("box"):
        canvas.rect([12, 12, w - 12, sy1 + 42], 1.6)
    canvas.rect([4, 4, w - 4, h - 4], 1.2, (120, 120, 120))
    for f in t["fields"]:
        if f.get("label_xy"):
            x, y = f["label_xy"]
            canvas.print_text(None, f["label"], x, y, f["label_size"], (35, 35, 40), anchor=f.get("label_anchor", "la"))
        if f["deco"] == "line":
            x0, _, x1, y1 = f["deco_box"]
            canvas.line([(x0, y1 - 4), (x1, y1 - 4)], 1.3, (60, 60, 70))
        elif f["deco"] == "cell":
            canvas.rect(f["deco_box"], 1.6, (45, 45, 55))
    if t.get("perforation_y"):
        y = t["perforation_y"]
        canvas.line([(8, y), (w - 8, y)], 1.2, (120, 120, 120), dash=5)
        for x in range(14, w - 8, 18):
            canvas.draw.ellipse([(x - 2) * 2, (y + 6 - 2) * 2, (x + 2) * 2, (y + 6 + 2) * 2], fill=(200, 200, 196))
        canvas.print_text(None, "- - detach stub - -", w / 2, y + 12, 10, (110, 110, 110), anchor="ma")
    canvas.print_text(None, FOOTER, w / 2, t["footer_y"], 10, (120, 120, 120), anchor="ma")


def _check_field(canvas, spec, chosen, style, r, ink, labels_entry):
    x0, y0, x1, y1 = spec["box"]
    options = spec["options"]
    cols, rows = 2, (len(options) + 1) // 2
    cw, ch = (x1 - x0) / cols, (y1 - y0) / rows
    hit = None
    for i, name in enumerate(options):
        cx, cy = x0 + (i % cols) * cw + 8, y0 + (i // cols) * ch + ch / 2
        canvas.rect([cx, cy - 9, cx + 18, cy + 9], 1.3, (60, 60, 70))
        canvas.print_text(None, name, cx + 26, cy + 5, 13, (40, 40, 45), anchor="ls")
        if name == chosen:
            hit = (cx, cy)
    if chosen is None:
        return None
    if hit is None:
        raise RenderError(f"{spec['field']}: {chosen!r} isn't an option on this form")
    seg = canvas.segment(spec["field"], "check")
    seg.text, seg.glyphs = chosen, ["check"]
    cx, cy = hit
    j = style.jitter * 2
    pts = [(cx + 2 + r.gauss(0, j), cy + r.gauss(0, j)), (cx + 8 + r.gauss(0, j), cy + 7 + r.gauss(0, j)),
           (cx + 22 + r.gauss(0, j), cy - 12 + r.gauss(0, j))]
    canvas.stroke(pts, 2.4, _mix(canvas.paper, ink, r.uniform(*style.darkness)), seg)
    return seg


def render_ticket(record: dict, plan: dict, seed: int) -> TicketRender:
    t = TEMPLATES[record["template"]]
    w, h = t["size"]
    canvas = Canvas(w, h, tuple(t["paper"]))
    style = STYLES[plan["style"]]
    ink = tuple(t["ink"])
    r = rng("draw", seed, record["ticket_id"])
    _draw_form(canvas, record, t)
    labels = {}
    for spec in t["fields"]:
        name = spec["field"]
        tv = true_value(record, spec)
        text = written_value(record, spec, plan)
        hand = spec["ink"] in HAND_INKS
        omitted = name in plan.get("omit", ())  # optional field nobody filled in
        missing = name in plan.get("missing", ())  # a field that matters, left blank on purpose
        cross = plan.get("crossout", {}).get(name)
        amb = {int(k): v for k, v in plan.get("ambiguous", {}).get(name, {}).items()}
        smudged = name in plan.get("smudge", ())
        entry = {
            "field": name, "ink": spec["ink"], "label_text": spec["label"], "box": spec["box"],
            "true_value": tv, "written_value": None, "visible_value": None, "struck_value": None,
            "crossed_out": False, "intentionally_missing": bool(missing), "omitted_optional": bool(omitted),
            "smudged": False, "ambiguous_chars": [], "legibility": "missing", "ink_bbox": None,
        }
        labels[name] = entry
        if text is None or omitted or missing:
            if tv is None:
                entry["legibility"] = "not_applicable"
            continue
        entry["written_value"] = text
        if spec["ink"] == "check":
            seg = _check_field(canvas, spec, tv, style, r, ink, entry)
            entry.update(visible_value=tv, legibility=style.legibility, ink_bbox=seg.ink if seg else None)
            continue
        if not hand:
            color = STAMP_RED if spec["ink"] == "stamp" else (28, 28, 34)
            size = _print_fit(canvas, text, spec["box"], 22 if spec["ink"] == "stamp" else 17)
            x0, y0, x1, y1 = spec["box"]
            seg = canvas.print_text(name, text, x0 + 4, y1 - max(5, (y1 - y0) * 0.22), size, color, anchor="ls",
                                    role="value", bold=spec["ink"] == "stamp" or name == "supplier")
            entry.update(visible_value=text, legibility="printed", ink_bbox=seg.ink)
            continue
        x0, y0, x1, y1 = spec["box"]
        if cross:
            if (y1 - y0) >= 56:  # a tall cell: wrong value low, correction above it
                mid = (y0 + y1) / 2
                wrong_box, fix_box = [x0, mid - 2, x1, y1], [x0, y0, x1, mid + 2]
            else:  # a line: wrong value, then the correction beside it
                split = x0 + (x1 - x0) * 0.46
                wrong_box, fix_box = [x0, y0, split, y1], [split + 4, y0, x1, y1]
            wrong = hand_text(canvas, name, "struck", cross["wrong"], wrong_box, style, r, ink=ink)
            strike(canvas, name, wrong, r, ink, cross["kind"])
            seg = hand_text(canvas, name, "correction", text, fix_box, style, r, ink=ink, ambiguous=amb)
            entry.update(crossed_out=True, struck_value=cross["wrong"])
        else:
            seg = hand_text(canvas, name, "value", text, spec["box"], style, r, ink=ink, ambiguous=amb)
        entry["ink_bbox"] = seg.ink
        entry["ambiguous_chars"] = [{"index": i, "char": text[i], "confusable_with": v.split("~")[1]}
                                    for i, v in sorted(amb.items())]
        if smudged:
            smudge(canvas, name, seg, r, ink)
            entry.update(smudged=True, legibility="illegible")
        else:
            entry["visible_value"] = text
            entry["legibility"] = "ambiguous" if amb else style.legibility
    image = canvas.finish()
    if t.get("carbon"):
        image = image.filter(ImageFilter.GaussianBlur(0.7))  # carbon copies are soft
    for entry in labels.values():
        if entry["ink_bbox"]:
            entry["ink_bbox"] = [round(v, 1) for v in entry["ink_bbox"]]
    log = {name: [s.as_dict() for s in segs] for name, segs in canvas.log.items()}
    return TicketRender(image, labels, log)


def wrong_value(record: dict, spec: dict, plan: dict, r) -> str:
    """A plausible slip of the pen for a crossed-out value: never the right one."""
    text = written_value(record, spec, plan)
    digits = [i for i, c in enumerate(text) if c.isdigit()]
    if digits:
        for _ in range(20):
            i = r.choice(digits)
            new = text[:i] + str((int(text[i]) + r.choice([1, 2, 7, 9])) % 10) + text[i + 1:]
            if new != text:
                return new
    words = text.split()
    return " ".join(words[:-1] + [words[-1][::-1]]) if len(words) > 1 else text[::-1] + "X"

