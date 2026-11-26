"""Ticket fields from a digital PDF's own text layer: no AI call, no network.

A ticket a supplier's system printed straight to PDF carries its words as text. When
every field is printed beside its label ("Ticket No", "Net Tons", "Gross", "Tare",
"Truck", "Job") and nothing is in doubt, the ticket is read from that text and the
reader is never called, so it isn't counted against the monthly read limit.

Conservative on purpose, as the invoice text reader is (invoice_extraction.py):
- Only the words inside the crop count, matched by labels. Nothing is inferred from
  position or from a number with no label.
- A field printed twice with different values, a quantity whose unit isn't stated, a
  net weight that disagrees with the tons, or any required field missing means "not
  sure": the configured reader reads the crop exactly as it would have.
- A scanned page has no text layer, so this never applies to it. A scan that carries
  a scanner's OCR text (an image under invisible text), and text hidden from the eye
  (invisible, transparent or white), are also left to the reader: the text a person
  can't see must not become the record.
- Units are as printed. Pounds stay pounds here; the pipeline converts them by the
  same rule as a person's entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

import pymupdf as fitz

from . import splitter, units
from .pricing import norm
from .validation import ExtractedTicket, parse_amount, parse_date, quantity_unit

SOURCE = "pdf_text"
# An image covering this much of a crop makes it a scan, whatever text sits on it.
_IMAGE_SHARE = 0.3
_ROW_TOLERANCE = 3.0  # points: words this close vertically are on one printed line
_SEGMENT_GAP = 18.0  # points: a wider gap separates a label's column from another's


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def middle(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2


@dataclass
class PageText:
    """One page's words in PDF points, and what the render's pixels are per point."""
    zoom: float
    words: list[Word] = field(default_factory=list)
    images: list[tuple[float, float, float, float]] = field(default_factory=list)
    hidden: list[tuple[float, float, float, float]] = field(default_factory=list)


def read_pages(data: bytes, *, dpi: int, max_page_pixels: int, max_pages: int = 20) -> list[PageText | None] | None:
    """Each page's text layer, or None for a page this can't place exactly (a rotated
    page). None for the whole file when it can't be opened or has no text at all."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return None
    try:
        if doc.needs_pass or doc.page_count == 0 or doc.page_count > max_pages:
            return None
        pages: list[PageText | None] = []
        for page in doc:
            words = [Word(*w[:5]) for w in page.get_text("words") if str(w[4]).strip()]
            if not words or page.rotation:
                pages.append(None)
                continue
            hidden = [tuple(span["bbox"]) for span in page.get_texttrace() if _hidden(span)]
            images = [tuple(info["bbox"]) for info in page.get_image_info()]
            pages.append(PageText(splitter.page_zoom(page, dpi, max_page_pixels), words, images, hidden))
        return pages if any(pages) else None
    finally:
        doc.close()


def _hidden(span: dict) -> bool:
    """Text a reader of the printed page can't see: invisible (OCR layers), transparent or white."""
    if span.get("type") == 3 or (span.get("opacity") is not None and span["opacity"] < 0.1):
        return True
    color = tuple(span.get("color") or ())
    white = (len(color) in (1, 3) and all(c >= 0.9 for c in color)) or (len(color) == 4 and all(c <= 0.1 for c in color))
    return white


def _overlap(a, b) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return width * height if width > 0 and height > 0 else 0.0


def words_in(page: PageText, crop_box) -> list[Word] | None:
    """The words whose middle is inside the crop (rendered pixels), or None when the
    crop is a scan or holds hidden text, so the text can't be trusted to be the page."""
    left, top, right, bottom = (float(v) / page.zoom for v in crop_box)
    box = (left, top, right, bottom)
    area = (right - left) * (bottom - top)
    if area <= 0 or any(_overlap(image, box) >= _IMAGE_SHARE * area for image in page.images):
        return None
    if any(_overlap(hidden, box) > 0 for hidden in page.hidden):
        return None
    return [w for w in page.words if left <= w.middle[0] < right and top <= w.middle[1] < bottom]


def _rows(words: list[Word]) -> list[list[Word]]:
    rows: list[tuple[float, list[Word]]] = []
    for word in sorted(words, key=lambda w: (w.middle[1], w.x0)):
        for middle, row in rows:
            if abs(middle - word.middle[1]) < _ROW_TOLERANCE:
                row.append(word)
                break
        else:
            rows.append((word.middle[1], [word]))
    return [sorted(row, key=lambda w: w.x0) for _, row in rows]


def _segments(row: list[Word]) -> list[str]:
    groups: list[list[Word]] = []
    for word in row:
        if groups and word.x0 - groups[-1][-1].x1 < _SEGMENT_GAP:
            groups[-1].append(word)
        else:
            groups.append([word])
    return [" ".join(w.text for w in group) for group in groups]


# field, label pattern, unit the label itself states. The longest label that matches
# wins, so "Net Tons" is a quantity in tons and "Job No" is a job number, not a location.
_NO = r"\s*(?:no\.?|number|num\.?|#)"
_LABELS: list[tuple[str, str, str | None]] = [
    ("ticket_number", rf"(?:scale\s+|delivery\s+|load\s+)?ticket(?:{_NO})?|tkt(?:{_NO})?", None),
    ("delivery_date", r"(?:delivery\s+|ship\s+|load\s+)?date", None),
    ("supplier", r"supplier|vendor|quarry|sold\s+by", None),
    ("material_type", r"material|product", None),
    ("quantity", r"net\s+tons?|tons?\s+net|tonnage|tons", "ton"),
    ("quantity", r"net\s+(?:lbs?|pounds)", "lb"),
    ("quantity", r"cubic\s+yards|net\s+(?:cy|yards)", "cy"),
    ("quantity", r"quantity|qty", None),
    ("unit", r"unit|uom|u/m", None),
    ("net_weight", r"net(?:\s+(?:weight|wt\.?))?", None),
    ("gross_weight", r"gross(?:\s+(?:weight|wt\.?))?", None),
    ("tare_weight", r"tare(?:\s+(?:weight|wt\.?))?", None),
    ("truck_number", rf"truck(?:{_NO})?|vehicle(?:{_NO})?", None),
    ("job_number", rf"(?:job|project|p\.?o\.?){_NO}", None),
    ("job_location", r"job\s+location|delivery\s+location|deliver\s+to|ship\s+to|destination|job\s*site|job|project", None),
    ("hours", r"(?:total\s+)?hours|hrs", None),
    ("loads", r"loads", None),
]
_COMPILED = [(name, re.compile(rf"^(?P<label>{pattern})(?![a-z0-9])\s*(?:\((?P<paren>[a-z.]+)\))?\s*[:#]?\s*(?P<value>.*)$",
                               re.IGNORECASE), implied)
             for name, pattern, implied in _LABELS]


def _label(segment: str) -> tuple[str, str | None, str] | None:
    """(field, unit the label states, value after it) for a segment that starts with a label."""
    best = None
    for name, pattern, implied in _COMPILED:
        match = pattern.match(segment.strip())
        if match and (best is None or len(match.group("label")) > best[0]):
            stated = implied
            if match.group("paren"):  # "Gross (lb)", "Quantity (CY)"
                stated = quantity_unit(match.group("paren")) or implied
            best = (len(match.group("label")), name, stated, match.group("value").strip())
    return best[1:] if best else None


class _Doubt(Exception):
    """Something in the text could be read two ways: the reader reads this crop instead."""


@dataclass
class _Found:
    values: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)

    def add(self, name: str, value: str, unit: str | None) -> None:
        if value:
            self.values.setdefault(name, []).append((" ".join(value.split()), unit))

    def one(self, name: str) -> str | None:
        seen = {value for value, _ in self.values.get(name, ())}
        if len(seen) > 1:
            raise _Doubt(f"{name} is printed twice with different values")
        return seen.pop() if seen else None


def _labelled(words: list[Word]) -> _Found:
    found = _Found()
    for row in _rows(words):
        segments = _segments(row)
        i = 0
        while i < len(segments):
            labelled = _label(segments[i])
            if labelled is None:
                i += 1
                continue
            name, unit, value = labelled
            if not value and i + 1 < len(segments):
                # The label's value printed in the next column. Taken even if it reads like a
                # label ("Job:  Project A"); a wrong pairing fails a check below, never guesses.
                value = segments[i + 1]
                i += 1
            found.add(name, value, unit)
            i += 1
    return found


_TICKET_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/-]{0,49}$")


def _amount(value: str | None, label_unit: str | None) -> tuple[Decimal | None, str | None]:
    if value is None:
        return None, None
    amount, printed = parse_amount(value)
    if amount is None:
        raise _Doubt("a quantity or weight isn't a plain number")
    if printed and label_unit and printed != label_unit:
        raise _Doubt("a quantity's label and figure name different units")
    return amount, printed or label_unit


def _one_amount(found: _Found, name: str, unit_hint: str | None = None) -> tuple[Decimal | None, str | None]:
    readings = {_amount(value, unit or unit_hint) for value, unit in found.values.get(name, ())}
    if len(readings) > 1:
        raise _Doubt(f"{name} is printed more than once with different figures")
    return readings.pop() if readings else (None, None)


def _letterhead(words: list[Word], supplier_names: tuple[str, ...]) -> str | None:
    """With no supplier label: the one catalog supplier whose whole name is a line of the ticket."""
    lines = {norm(" ".join(w.text for w in row)) for row in _rows(words)}
    named = {name for name in supplier_names if norm(name) and norm(name) in lines}
    return named.pop() if len(named) == 1 else None


def parse_ticket(words: list[Word], supplier_names: tuple[str, ...] = ()) -> tuple[ExtractedTicket, dict] | None:
    """The ticket, when its text gives every required field without doubt; otherwise None."""
    try:
        return _parse(words, supplier_names)
    except _Doubt:
        return None


def _parse(words: list[Word], supplier_names: tuple[str, ...]) -> tuple[ExtractedTicket, dict] | None:
    found = _labelled(words)
    number = found.one("ticket_number")
    date_text = found.one("delivery_date")
    delivered = parse_date(date_text)
    supplier = found.one("supplier") or _letterhead(words, supplier_names)
    material = found.one("material_type")
    job_location = found.one("job_location")

    # The quantity: one figure in one stated unit, from a quantity label.
    unit_text = found.one("unit")
    stated_unit = quantity_unit(unit_text) if unit_text else None
    if unit_text and stated_unit is None:
        raise _Doubt("the unit printed isn't one Quarvera knows")
    quantity, unit = _one_amount(found, "quantity", stated_unit)
    net, net_unit = _one_amount(found, "net_weight")
    if quantity is None and net is not None:
        quantity, unit = net, net_unit  # "Net: 49,000 lb" is the quantity when nothing else is
    elif quantity is not None and net is not None:
        if net_unit is None or unit not in units.WEIGHT_UNITS or net_unit not in units.WEIGHT_UNITS:
            if (net, net_unit) != (quantity, unit):
                raise _Doubt("a net figure is printed beside the quantity")
        elif abs(units.to_tons(net, net_unit) - units.to_tons(quantity, unit)) > units.NET_TOLERANCE_TONS:
            raise _Doubt("the net weight disagrees with the quantity")

    if not (number and _TICKET_NUMBER.match(number) and any(c.isdigit() for c in number)):
        return None
    if not (delivered and supplier and material and job_location and quantity is not None and unit):
        return None
    if quantity <= 0:
        return None

    gross, gross_unit = _one_amount(found, "gross_weight")
    tare, tare_unit = _one_amount(found, "tare_weight")
    weight_units = {u for u in (gross_unit, tare_unit) if u}
    if len(weight_units) > 1:
        raise _Doubt("gross and tare are printed in different units")
    # Weights with no unit printed by them or the net weight stay as read, never checked or saved.
    weight_unit = weight_units.pop() if weight_units else (net_unit if net_unit in units.WEIGHT_UNITS else None)
    truck = found.one("truck_number")
    job_number = found.one("job_number")

    raw = {
        "ticket_number": number, "delivery_date": delivered.isoformat(), "material_type": material,
        "tonnage": str(quantity), "unit": unit, "supplier": supplier, "job_location": job_location,
        "job_number": job_number[:100] if job_number else None, "hours": found.one("hours"), "loads": found.one("loads"),
        "gross_weight": str(gross) if gross is not None else None,
        "tare_weight": str(tare) if tare is not None else None,
        "weight_unit": weight_unit if (gross is not None or tare is not None) else None,
        "truck_number": truck[:50] if truck else None,
        "_ticketflow": {"reader": SOURCE, "words": len(words)},
    }
    extracted = ExtractedTicket.model_validate({k: raw.get(k) for k in ExtractedTicket.model_fields})
    return extracted, raw


def read_crop(page: PageText | None, crop_box, supplier_names: tuple[str, ...] = ()) -> tuple[ExtractedTicket, dict] | None:
    """The ticket printed in this crop of a digital PDF page, or None to use the reader."""
    if page is None:
        return None
    words = words_in(page, crop_box)
    if not words:
        return None
    return parse_ticket(words, supplier_names)
