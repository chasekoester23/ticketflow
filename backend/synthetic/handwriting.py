"""Procedural, stroke-based handwriting, drawn with Pillow.

Every glyph is a polyline from glyphs.py, bent by a style: jitter, slant,
baseline drift, size wobble, stroke width, pressure (ink darkness) and letter
spacing. The canvas records exactly which glyphs it drew for each field, and
where the ink landed. That log, not the plan, is what verification checks.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

from .glyphs import GLYPHS, base_char, unsupported

SS = 2  # draw at twice the size, then downsample: smoother strokes


class RenderError(RuntimeError):
    """The renderer couldn't draw what was asked inside the space it had."""


@dataclass(frozen=True)
class Style:
    name: str
    jitter: float  # point noise, in glyph units (cap height = 10)
    slant: float  # shear: x += slant * y
    slant_var: float
    drift: float  # baseline random walk per character, glyph units
    size_var: float  # relative size wobble per character
    width: float  # stroke width in px at a 22 px cap height (2.8 px at 150 dpi is a 0.5 mm pen)
    width_var: float
    darkness: tuple[float, float]  # ink pressure range, 0 = paper, 1 = full ink
    spacing: float  # gap between letters, glyph units
    rotation: float  # sd of the line's angle, degrees
    overshoot: float  # stroke ends running past, glyph units
    legibility: str


STYLES = {
    "neat": Style("neat", 0.12, 0.08, 0.02, 0.06, 0.04, 2.8, 0.08, (0.88, 1.0), 1.3, 0.6, 0.0, "clear"),
    "rushed": Style("rushed", 0.30, 0.26, 0.05, 0.22, 0.10, 2.5, 0.15, (0.72, 0.95), 0.6, 1.8, 0.6, "clear"),
    "messy": Style("messy", 0.50, 0.14, 0.12, 0.35, 0.18, 3.2, 0.25, (0.65, 1.0), 0.9, 2.8, 0.4, "degraded"),
    "faint": Style("faint", 0.18, 0.12, 0.04, 0.12, 0.06, 2.2, 0.15, (0.22, 0.42), 1.1, 1.0, 0.2, "degraded"),
    "difficult": Style("difficult", 0.80, 0.30, 0.20, 0.55, 0.25, 3.0, 0.35, (0.40, 0.90), 0.75, 4.0, 0.8, "poor"),
}


def _catmull_rom(points: list[tuple[float, float]], steps: int = 6) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    pts = [points[0], *points, points[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for s in range(steps):
            t = s / steps
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                                    + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3) for k in (0, 1)))
    out.append(points[-1])
    return out


def _mix(paper: tuple, ink: tuple, amount: float) -> tuple:
    return tuple(round(p + (i - p) * amount) for p, i in zip(paper, ink))


@dataclass
class Segment:
    role: str  # value | correction | struck | strike | smudge | printed | form
    glyphs: list[str] = field(default_factory=list)
    text: str = ""
    ink: list[float] | None = None  # [x0, y0, x1, y1] in ticket px

    def grow(self, x0, y0, x1, y1) -> None:
        if self.ink is None:
            self.ink = [x0, y0, x1, y1]
        else:
            self.ink = [min(self.ink[0], x0), min(self.ink[1], y0), max(self.ink[2], x1), max(self.ink[3], y1)]

    def as_dict(self) -> dict:
        return {"role": self.role, "glyphs": list(self.glyphs), "text": self.text,
                "ink": [round(v, 1) for v in self.ink] if self.ink else None}


class Canvas:
    """A ticket being drawn. Coordinates are ticket pixels; drawing happens at SS×."""

    def __init__(self, width: int, height: int, paper: tuple[int, int, int]):
        self.width, self.height, self.paper = width, height, paper
        self.image = Image.new("RGB", (width * SS, height * SS), paper)
        self.draw = ImageDraw.Draw(self.image)
        self.log: dict[str, list[Segment]] = {}
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}

    def segment(self, field_name: str, role: str) -> Segment:
        seg = Segment(role)
        self.log.setdefault(field_name, []).append(seg)
        return seg

    def font(self, size: float):
        key = max(6, round(size * SS))
        if key not in self._fonts:
            self._fonts[key] = ImageFont.load_default(size=key)  # Pillow's bundled Aileron Regular (CC0)
        return self._fonts[key]

    # -- primitives -------------------------------------------------------
    def stroke(self, points, width: float, color, seg: Segment | None = None) -> None:
        if not points:
            return
        scaled = [(x * SS, y * SS) for x, y in points]
        w = max(1, round(width * SS))
        if len(scaled) == 1:
            x, y = scaled[0]
            r = w / 2
            self.draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
        else:
            self.draw.line(scaled, fill=color, width=w, joint="curve")
            r = w / 2
            for x, y in (scaled[0], scaled[-1]):  # round caps
                self.draw.ellipse([x - r + 0.5, y - r + 0.5, x + r - 0.5, y + r - 0.5], fill=color)
        if seg is not None:
            h = width / 2 + 0.5
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            seg.grow(min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h)

    def rect(self, box, width: float = 1.5, color=(40, 40, 40), fill=None) -> None:
        x0, y0, x1, y1 = (v * SS for v in box)
        self.draw.rectangle([x0, y0, x1, y1], outline=color, width=max(1, round(width * SS)), fill=fill)

    def line(self, points, width: float = 1.2, color=(40, 40, 40), dash: float | None = None) -> None:
        if dash is None:
            self.draw.line([(x * SS, y * SS) for x, y in points], fill=color, width=max(1, round(width * SS)))
            return
        (x0, y0), (x1, y1) = points
        length = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(length // (dash * 2)))
        for i in range(n):
            a, b = (2 * i) / (2 * n), (2 * i + 1) / (2 * n)
            self.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)], width, color)

    def print_text(self, field_name: str | None, text: str, x: float, y: float, size: float, color=(30, 30, 30),
                   *, anchor: str = "la", role: str = "printed", bold: bool = False) -> Segment:
        """Printed form text in Pillow's built-in font. Logged like handwriting."""
        font = self.font(size)
        seg = self.segment(field_name or "_form", role if field_name else "form")
        stroke = max(1, round(size * SS / 30)) if bold else 0
        self.draw.text((x * SS, y * SS), text, font=font, fill=color, anchor=anchor, stroke_width=stroke,
                       stroke_fill=color)
        x0, y0, x1, y1 = self.draw.textbbox((x * SS, y * SS), text, font=font, anchor=anchor, stroke_width=stroke)
        seg.glyphs.extend(text)
        seg.text = text
        if x1 > x0:
            seg.grow(x0 / SS, y0 / SS, x1 / SS, y1 / SS)
        return seg

    def text_width(self, text: str, size: float) -> float:
        x0, _, x1, _ = self.draw.textbbox((0, 0), text, font=self.font(size))
        return (x1 - x0) / SS

    def finish(self) -> Image.Image:
        return self.image.resize((self.width, self.height), Image.LANCZOS)


# -- handwriting ------------------------------------------------------------
def _plan_glyphs(keys: list[str], style: Style, r: random.Random, scale: float):
    """Glyph strokes in ticket px relative to the line start, before rotation."""
    placed, x, drift = [], 0.0, 0.0
    slant = style.slant + r.gauss(0, style.slant_var)
    for key in keys:
        advance, strokes = GLYPHS[key]
        size = 1 + r.gauss(0, style.size_var)
        size = min(1.35, max(0.7, size))
        drift = max(-1.5, min(1.5, drift + r.gauss(0, style.drift)))
        glyph = []
        for kind, pts in strokes:
            pts = [(px + r.gauss(0, style.jitter), py + r.gauss(0, style.jitter)) for px, py in pts]
            if kind == "s":
                pts = _catmull_rom(pts)
            if kind != "d" and style.overshoot and len(pts) >= 2 and r.random() < 0.5:
                (ax, ay), (bx, by) = pts[-2], pts[-1]
                d = math.hypot(bx - ax, by - ay) or 1
                k = r.uniform(0, style.overshoot) / d
                pts = [*pts, (bx + (bx - ax) * k, by + (by - ay) * k)]
            out = [((x + (px + slant * py) * size) * scale, -(py * size + drift) * scale) for px, py in pts]
            glyph.append((kind, out))
        placed.append((key, glyph))
        x += (advance * size + style.spacing + r.gauss(0, style.jitter * 0.5))
    return placed, x * scale


def hand_text(canvas: Canvas, field_name: str, role: str, text: str, box, style: Style, r: random.Random, *,
              ink=(20, 40, 120), ambiguous: dict[int, str] | None = None, cap: float | None = None,
              align: str = "left") -> Segment:
    """Write `text` inside `box` (x0, y0, x1, y1). Shrinks to fit; refuses to
    draw outside the box. `ambiguous` maps a character index to its variant key."""
    if unsupported(text):
        raise RenderError(f"No glyph for {sorted(unsupported(text))} in {text!r}")
    keys = list(text)
    for index, variant in (ambiguous or {}).items():
        if variant not in GLYPHS or base_char(variant) != text[index]:
            raise RenderError(f"Variant {variant} doesn't draw {text[index]!r}")
        keys[index] = variant
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    cap = cap or min(26.0, bh * 0.55)
    state = r.getstate()
    for attempt in range(10):
        r.setstate(state)  # every attempt draws the same jitter, just smaller
        scale = cap / 10 * (0.88 ** attempt)
        placed, _ = _plan_glyphs(keys, style, r, scale)
        angle = math.radians(r.gauss(0, style.rotation))
        lead_pick, drop_pick = r.random(), r.random()
        ca, sa = math.cos(angle), math.sin(angle)
        rel = [(key, [(kind, [(px * ca - py * sa, px * sa + py * ca) for px, py in pts]) for kind, pts in glyph])
               for key, glyph in placed]
        xs = [p[0] for _, gl in rel for _, pts in gl for p in pts]
        ys = [p[1] for _, gl in rel for _, pts in gl for p in pts]
        width_base = style.width * (cap / 22) * (0.88 ** attempt)
        margin = width_base * (1 + style.width_var) / 2 + 1.5
        if not xs:  # only spaces
            xs, ys = [0.0], [0.0]
        room_x = bw - 2 * margin - (max(xs) - min(xs))
        room_y = bh - 2 * margin - (max(ys) - min(ys))
        if room_x < 0 or room_y < 0:
            continue
        lead = room_x / 2 if align == "center" else lead_pick * min(room_x, 14.0)
        start_x = x0 + margin + lead - min(xs)
        # Sit on the line near the bottom of the box, as people do, but inside it.
        base_y = y1 - margin - max(ys) - drop_pick * min(room_y, bh * 0.12)
        base_y = max(base_y, y0 + margin - min(ys))
        glyphs = [(key, [(kind, [(start_x + px, base_y + py) for px, py in pts]) for kind, pts in gl])
                  for key, gl in rel]
        break
    else:
        raise RenderError(f"{field_name}: {text!r} doesn't fit in {box}")
    seg = canvas.segment(field_name, role)
    seg.text = text
    for key, strokes in glyphs:
        seg.glyphs.append(key)  # logged as it is drawn, one entry per glyph (a space draws nothing)
        for kind, pts in strokes:
            darkness = r.uniform(*style.darkness)
            width = width_base * (1 + r.uniform(-style.width_var, style.width_var))
            canvas.stroke(pts if kind != "d" else pts[:1], width * (1.3 if kind == "d" else 1),
                          _mix(canvas.paper, ink, darkness), seg)
    return seg


def strike(canvas: Canvas, field_name: str, target: Segment, r: random.Random, ink, kind: str) -> Segment:
    """Cross out what `target` drew: one line, two lines or a scribble."""
    seg = canvas.segment(field_name, "strike")
    x0, y0, x1, y1 = target.ink
    mid, h = (y0 + y1) / 2, (y1 - y0)
    color = _mix(canvas.paper, ink, 0.95)
    if kind == "scribble":
        pts, x, up = [], x0 - 1, True
        while x < x1 + 1:
            pts.append((x, mid + (h * 0.28 if up else -h * 0.28) + r.uniform(-1, 1)))
            x += r.uniform(3, 6)
            up = not up
        canvas.stroke(pts, 1.8, color, seg)
    else:
        offsets = [0.0] if kind == "line" else [-h * 0.12, h * 0.12]
        for off in offsets:
            canvas.stroke([(x0 - 2, mid + off + r.uniform(-1, 1)), (x1 + 2, mid + off + r.uniform(-1.5, 1.5))],
                          2.0, color, seg)
    return seg


def smudge(canvas: Canvas, field_name: str, target: Segment, r: random.Random, ink) -> Segment:
    """Scribble heavily over a value until it can't be read (the label says so)."""
    seg = canvas.segment(field_name, "smudge")
    x0, y0, x1, y1 = target.ink
    color = _mix(canvas.paper, ink, 0.9)
    for _ in range(int((x1 - x0) // 3) + 6):
        a = (r.uniform(x0, x1), r.uniform(y0, y1))
        b = (min(x1, max(x0, a[0] + r.uniform(-14, 14))), min(y1, max(y0, a[1] + r.uniform(-8, 8))))
        canvas.stroke([a, b], r.uniform(2.5, 4.5), color, seg)
    return seg
