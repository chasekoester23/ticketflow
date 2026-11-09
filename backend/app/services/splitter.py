"""Render PDF pages and split a page image into individual ticket crops.

Pure functions, no database: easy to unit test.
Boxes are (left, top, right, bottom) in pixels of the rendered page image.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass

import cv2
import numpy as np
import pymupdf as fitz  # PyMuPDF
from PIL import Image

Box = tuple[int, int, int, int]

# Ticket flags that start with this ask a person to compare the record with the
# original scan, because its crop may be incomplete or show another ticket.
CROP_CHECK_PREFIX = "Crop check:"


class PdfError(Exception):
    """Raised for unreadable, corrupted, empty or password-protected PDFs."""


@dataclass
class Crop:
    position: int
    box: Box
    image: Image.Image
    # Grid pages only. The plain quarter-page cut this crop started from.
    fixed_box: Box | None = None
    # A ticket-sized region is centred in this slot (None: not measured, e.g. 1-up).
    owns_ticket: bool | None = None
    # Part of a ticket centred in another slot shows in this slot's plain cut.
    foreign_ticket: bool = False
    # Reasons a person should compare the record with the original scan.
    checks: tuple[str, ...] = ()


def render_pdf_pages(
    data: bytes,
    dpi: int = 200,
    *,
    max_pages: int = 20,
    max_page_pixels: int = 14_000_000,
    max_total_pixels: int = 70_000_000,
) -> list[Image.Image]:
    """Apply limits BEFORE rendering compressed PDF pages into memory."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PdfError("Could not open PDF. Check the document and try again.") from exc
    try:
        if doc.needs_pass:
            raise PdfError("PDF is password protected")
        if doc.page_count == 0:
            raise PdfError("PDF has no pages")
        if doc.page_count > max_pages:
            raise PdfError(
                f"PDF contains {doc.page_count} pages; the per-file limit is {max_pages}. "
                "Split this scan into smaller PDFs."
            )
        damaged = incomplete_reason(doc, data)
        if damaged:
            raise PdfError(damaged)
        pages: list[Image.Image] = []
        total_pixels = 0
        for page in doc:
            # FitZ page.rect describes the page at 72dpi. Reject huge page
            # dimensions before get_pixmap allocates the bitmap.
            width = math.ceil(page.rect.width * dpi / 72)
            height = math.ceil(page.rect.height * dpi / 72)
            estimate = width * height
            if width <= 0 or height <= 0 or estimate > max_page_pixels:
                raise PdfError("PDF page is too large to render safely. Rescan at a lower resolution.")
            if total_pixels + estimate > max_total_pixels:
                raise PdfError("PDF is too large to process safely. Split it into smaller files.")
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            actual = pix.width * pix.height
            if actual > max_page_pixels or total_pixels + actual > max_total_pixels:
                raise PdfError("PDF renders above the safe image limit. Rescan at a lower resolution.")
            pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            total_pixels += actual
        return pages
    except PdfError:
        raise
    except Exception as exc:
        # Never expose parser internals or filesystem paths in API errors.
        raise PdfError("Could not render PDF. Check the scan and try again.") from exc
    finally:
        doc.close()


def incomplete_reason(doc, data: bytes) -> str | None:
    """Why a PDF looks cut short, or None.

    MuPDF quietly rebuilds a damaged file and shows whatever survived, so a scan
    cut off mid-download would show half its tickets with nothing to say so.
    Only a rebuilt file is checked: it's refused when it has no end marker at all
    or a page has no content. A complete file with a slightly wrong index, which
    some scanners write, or with junk after its end, is rebuilt too, but every
    page is intact, so it passes.
    """
    if not doc.is_repaired:
        return None
    message = ("This PDF can't be opened completely. It looks cut off or damaged, so pages or "
               "tickets may be missing. Upload the original file again.")
    if b"%%EOF" not in data:
        return message
    for page in doc:
        if not page.get_contents():
            return message
    return None


def render_pdf_page(data: bytes, page_no: int, dpi: int = 100, *, max_page_pixels: int = 14_000_000) -> Image.Image:
    """Render one 1-based page for preview, under the same size limit."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PdfError("Could not open PDF.") from exc
    try:
        if doc.needs_pass or not 1 <= page_no <= doc.page_count:
            raise PdfError("That page is not available.")
        page = doc[page_no - 1]
        if math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72) > max_page_pixels:
            raise PdfError("PDF page is too large to preview.")
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("Could not render PDF page.") from exc
    finally:
        doc.close()


def load_image(data: bytes, *, max_pixels: int = 14_000_000) -> Image.Image:
    """Reject decompression bombs using dimensions before decoding image data."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise PdfError("Image dimensions are too large. Resize the scan before uploading.")
            img.load()
            return img.convert("RGB")
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("Could not read image. Check the file and try again.") from exc


def grid_boxes(width: int, height: int, rows: int, cols: int, margin: float = 0.01) -> list[Box]:
    """Equal grid cells, each shrunk by `margin` (fraction of the page size) to drop scanner edges and gaps."""
    mx, my = int(width * margin), int(height * margin)
    cw, ch = width / cols, height / rows
    boxes: list[Box] = []
    for r in range(rows):
        for c in range(cols):
            boxes.append((int(c * cw) + mx, int(r * ch) + my, int((c + 1) * cw) - mx, int((r + 1) * ch) - my))
    return boxes


def split_page(image: Image.Image, layout: str, margin: float = 0.01, *, adaptive: bool = True) -> list[Crop]:
    """Cut a page into one crop per slot. Deterministic: the same page always
    gives the same boxes. On a 2x2 page each slot starts from the fixed quarter
    cut and only ever grows to take in the ticket centred in it (see
    `adapt_grid`), so a crop never holds less of the page than the fixed cut."""
    w, h = image.size
    if layout == "single":
        return [Crop(0, (0, 0, w, h), image.crop((0, 0, w, h)))]
    if layout != "grid_2x2":
        raise ValueError(f"Unsupported layout: {layout}")
    boxes = grid_boxes(w, h, rows=2, cols=2, margin=margin)
    if not adaptive:
        return [Crop(i, b, image.crop(b), fixed_box=b) for i, b in enumerate(boxes)]
    return adapt_grid(image, boxes)


def crop_at(image: Image.Image, crop: Crop, box) -> Crop:
    """Re-cut a slot with stored geometry, e.g. one recorded by an earlier
    version, keeping what the page says about it but no crop checks (those
    were about the new geometry)."""
    box = tuple(int(v) for v in box)
    return Crop(crop.position, box, image.crop(box), fixed_box=crop.fixed_box, owns_ticket=crop.owns_ticket,
                foreign_ticket=crop.foreign_ticket)


# --- Ticket regions on a 2x2 page -------------------------------------------
# Measured on a working grid of about 50 cells per inch of a letter page.
_REGION_CONTRAST = 12     # grey levels below nearby paper, after averaging: a paper edge or rule
_REGION_MIN_CELLS = 10    # smaller clusters are scanner noise or specks
_TICKET_MIN_IN = 1.0      # a region at least this big both ways is ticket-sized
_PIECE_MIN_IN = 0.3       # smaller pieces never join a region
_MERGE_GAP_IN = 0.6       # pieces closer than this, and aligned, are one ticket
_PAD_IN = 0.08            # room left around a ticket's outermost mark
_MAX_TICKET = 1.40        # a region bigger than this many quarters is not one ticket we can trust
_MAX_REACH = _MAX_TICKET - 1  # so the furthest a crop may reach past its quarter, as a share of it
_OVERLAP_SHARE = 0.02     # extra share of a crop showing another slot's ticket that needs a look

CHECK_CONTINUES = (f"{CROP_CHECK_PREFIX} the ticket may continue past its crop. "
                   "Compare it with the original scan.")
CHECK_OVERLAP = (f"{CROP_CHECK_PREFIX} the crop may include part of another ticket. "
                 "Compare it with the original scan.")
CHECK_TWO = (f"{CROP_CHECK_PREFIX} this slot may hold more than one ticket. "
             "Compare it with the original scan.")


@dataclass(frozen=True)
class Region:
    box: Box                       # tight box of its strokes, page px
    centre: tuple[float, float]    # stroke-weighted centre, page px
    joined: bool = False           # two ticket-sized pieces joined across a quarter line


def _area(box) -> float:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _intersection(a, b) -> tuple:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def find_ticket_regions(image: Image.Image) -> list[Region]:
    """Ticket-sized clusters of marks on a page.

    The page is averaged down to about 50 cells per inch, which smooths scanner
    noise away while keeping a ticket's paper edge, printed rules and writing
    as faint but continuous lines; a black-hat then picks out anything darker
    than the paper next to it. Isolated specks are dropped, what's left is
    joined across one-cell breaks, pieces of one ticket whose edge broke are
    merged (`_merge_pieces`), and only regions at least an inch each way count,
    so a smudge, a lone word or a stray line never makes one. The outer 1% of
    the page (scanner edges and lid shadows) is ignored."""
    w, h = image.size
    cell = max(1, round(w / 425))
    sw, sh = w // cell, h // cell
    grey = np.asarray(image.convert("L"))[:sh * cell, :sw * cell]
    small = cv2.resize(grey, (sw, sh), interpolation=cv2.INTER_AREA)
    contrast = cv2.morphologyEx(small, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
    mask = (contrast >= _REGION_CONTRAST).astype(np.uint8)
    border = max(1, round(0.01 * sw))
    mask[:border, :] = mask[-border:, :] = 0
    mask[:, :border] = mask[:, -border:] = 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros(count, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= _REGION_MIN_CELLS
    mask = keep[labels].astype(np.uint8)
    per_inch = sw / 8.5
    joined = cv2.dilate(mask, np.ones((3, 3), np.uint8))  # bridge a broken line, not the gap between tickets
    count, labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    pieces = []  # [box in cells, ink cells, sum of x, sum of y, joined across a quarter line]
    for i in range(1, count):
        if max(stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]) < _PIECE_MIN_IN * per_inch:
            continue
        ys, xs = np.nonzero((labels == i) & (mask > 0))
        if len(xs):
            pieces.append([(int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                           len(xs), float(xs.sum()), float(ys.sum()), False])
    ticket_sized = lambda p: min(p[0][2] - p[0][0], p[0][3] - p[0][1]) >= _TICKET_MIN_IN * per_inch  # noqa: E731
    quarter = lambda p: int(p[2] / p[1] >= sw / 2) + 2 * int(p[3] / p[1] >= sh / 2)  # noqa: E731
    limits = (_MERGE_GAP_IN * per_inch, _MAX_TICKET * sw / 2, _MAX_TICKET * sh / 2)
    # First within each quarter. Then across a quarter line, but only when that
    # leaves neither quarter with another ticket-sized piece: a long ticket's
    # overhang joins it, while the broken-off edge of one ticket can't join the
    # neighbour next to it. Joining two ticket-sized pieces across a line might
    # still be two tickets, so the region says so and the slot gets a check.
    pieces = _merge_pieces(pieces, *limits, lambda a, b, rest: quarter(a) == quarter(b))

    def across(a, b, rest):
        if quarter(a) == quarter(b):
            return True
        return not any(ticket_sized(p) and quarter(p) in (quarter(a), quarter(b)) for p in rest)

    pieces = _merge_pieces(pieces, *limits, across, joins_two=lambda a, b: quarter(a) != quarter(b)
                           and ticket_sized(a) and ticket_sized(b))
    return [Region(tuple(v * cell for v in p[0]), ((p[2] / p[1] + 0.5) * cell, (p[3] / p[1] + 0.5) * cell), p[4])
            for p in pieces if ticket_sized(p)]


def _merge_pieces(pieces: list, max_gap: float, max_w: float, max_h: float, may_join, joins_two=None) -> list:
    """Join pieces of one ticket whose paper edge or rules broke apart: closest
    pair first, only when they sit side by side or one above the other (half
    overlapping the other way), close together, and the result is still no
    bigger than one ticket can be. Two tickets never fit that, so they stay apart."""
    while True:
        best = None
        for i in range(len(pieces)):
            for j in range(i + 1, len(pieces)):
                if not may_join(pieces[i], pieces[j], [p for k, p in enumerate(pieces) if k not in (i, j)]):
                    continue
                a, b = pieces[i][0], pieces[j][0]
                gap_x = max(a[0], b[0]) - min(a[2], b[2])
                gap_y = max(a[1], b[1]) - min(a[3], b[3])
                if gap_x > 0 and gap_y > 0:
                    continue  # diagonal neighbours
                if gap_x > 0 or gap_y > 0:
                    overlap = -gap_y if gap_x > 0 else -gap_x
                    narrow = min(a[3] - a[1], b[3] - b[1]) if gap_x > 0 else min(a[2] - a[0], b[2] - b[0])
                    if overlap < 0.5 * narrow:
                        continue
                gap = max(gap_x, gap_y, 0)
                box = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                if gap > max_gap or box[2] - box[0] > max_w or box[3] - box[1] > max_h:
                    continue
                if best is None or gap < best[0]:
                    best = (gap, i, j, box)
        if best is None:
            return pieces
        _, i, j, box = best
        a, b = pieces[i], pieces[j]
        two = a[4] or b[4] or bool(joins_two and joins_two(a, b))
        pieces[i] = [box, a[1] + b[1], a[2] + b[2], a[3] + b[3], two]
        del pieces[j]


def adapt_grid(image: Image.Image, fixed: list[Box]) -> list[Crop]:
    """Grow each 2x2 crop to the ticket centred in its quarter.

    Each ticket-sized region belongs to the quarter holding its centre. A crop
    is the fixed cut joined with its own regions plus a little padding, so it
    can reach past the 1% margin (L1) or across the quarter line (L2). It falls
    back to the fixed cut, with a check for a person, when the reach would be
    implausibly far or the region is too big to be one ticket; and it asks for
    a check whenever growing it brings in another slot's ticket, or two
    separate tickets seem to sit in one slot. Nothing is ever cut smaller."""
    w, h = image.size
    half_w, half_h = w / 2, h / 2
    regions = find_ticket_regions(image)
    quarter = lambda r: int(r.centre[0] >= half_w) + 2 * int(r.centre[1] >= half_h)  # noqa: E731
    own = {q: [r for r in regions if quarter(r) == q] for q in range(4)}
    pad = round(_PAD_IN * w / 8.5)
    crops = []
    for q, base in enumerate(fixed):
        cell = (int(q % 2 * half_w), int(q // 2 * half_h), int((q % 2 + 1) * half_w), int((q // 2 + 1) * half_h))
        others = [r for p, rs in own.items() if p != q for r in rs]
        foreign = any(_area(_intersection(base, r.box)) > 0.01 * _area(base) for r in others)
        checks: list[str] = []
        box = base
        if own[q]:
            rx0 = min(r.box[0] for r in own[q]) - pad
            ry0 = min(r.box[1] for r in own[q]) - pad
            rx1 = max(r.box[2] for r in own[q]) + pad
            ry1 = max(r.box[3] for r in own[q]) + pad
            reach = max((cell[0] - rx0) / half_w, (cell[1] - ry0) / half_h,
                        (rx1 - cell[2]) / half_w, (ry1 - cell[3]) / half_h)
            too_big = any((r.box[2] - r.box[0]) > _MAX_TICKET * half_w or (r.box[3] - r.box[1]) > _MAX_TICKET * half_h
                          for r in own[q])
            if reach > _MAX_REACH or too_big:
                checks.append(CHECK_CONTINUES)  # uncertain: keep the plain cut and ask
            else:
                box = (max(0, min(base[0], rx0)), max(0, min(base[1], ry0)),
                       min(w, max(base[2], rx1)), min(h, max(base[3], ry1)))
            if (len(own[q]) > 1 and not _nested(own[q])) or any(r.joined for r in own[q]):
                checks.append(CHECK_TWO)
        added = sum(_area(_intersection(box, r.box)) - _area(_intersection(base, r.box)) for r in others)
        if added > _OVERLAP_SHARE * _area(box):
            checks.append(CHECK_OVERLAP)
        crops.append(Crop(q, box, image.crop(box), fixed_box=base, owns_ticket=bool(own[q]),
                          foreign_ticket=foreign, checks=tuple(checks)))
    return crops


def _nested(regions: list[Region]) -> bool:
    """True when every region overlaps the largest one: pieces of one ticket."""
    big = max(regions, key=lambda r: _area(r.box))
    return all(r is big or _area(_intersection(r.box, big.box)) > 0 for r in regions)


def assess_crop(crop: Crop) -> SlotAssessment:
    """assess_slot, plus what the page as a whole says. On a 2x2 page, a slot
    with no ticket of its own whose cut shows part of a neighbour's ticket is
    at most "faint": a person decides, so a neighbour's overhang can never make
    a phantom ticket, and nothing is called empty that wasn't before."""
    plain_cut = None
    f, b = crop.fixed_box, crop.box
    if f and tuple(f) != tuple(b) and f[0] >= b[0] and f[1] >= b[1] and f[2] <= b[2] and f[3] <= b[3]:
        plain_cut = crop.image.crop((f[0] - b[0], f[1] - b[1], f[2] - b[0], f[3] - b[1]))
    assessment = assess_slot(crop.image, plain_cut=plain_cut)
    if assessment.detection == "content" and crop.owns_ticket is False and crop.foreign_ticket:
        return SlotAssessment(assessment.ink, "faint")
    return assessment


def is_blank(image: Image.Image) -> bool:
    """True unless the slot clearly holds a ticket, e.g. an empty quadrant when fewer than 4 tickets were scanned."""
    return assess_slot(image).detection != "content"


@dataclass(frozen=True)
class SlotAssessment:
    ink: float  # share of the slot covered by connected pen strokes clearly darker than the paper next to them
    detection: str  # content | faint | empty


# Stroke contrast (grey levels darker than the paper within a few pixels) for
# "clearly written" and for "a visible mark at all".
_INK_CONTRAST = 60
_MARK_CONTRAST = 20
_MAX_ASSESS_SIDE = 2400  # beyond this, shrink by a whole factor first (a phone photo, say)


def _grey(image: Image.Image) -> np.ndarray:
    grey = image.convert("L")
    factor = math.ceil(max(grey.size) / _MAX_ASSESS_SIDE)
    if factor > 1:
        grey = grey.reduce(factor)
    return np.asarray(grey)


def stroke_contrast(grey: np.ndarray) -> np.ndarray:
    """How much darker each pixel is than the brightest paper within a small
    window: a morphological black-hat. Pen strokes score high whatever the
    paper's tint or the light across the page; smooth shadows and gradients
    score low. A 3x3 median first removes single-pixel scanner speckle."""
    k = max(9, (min(grey.shape) // 60) | 1)
    clean = cv2.medianBlur(grey, 3)
    return cv2.morphologyEx(clean, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))


def _connected_share(mask: np.ndarray, min_area: int) -> float:
    """Share of the image covered by connected marks of at least min_area px, so
    leftover specks don't add up to a ticket."""
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    return float(areas[areas >= min_area].sum()) / mask.size


def _thumbnail_reading(image: Image.Image, ink_threshold: float, faint_threshold: float) -> str:
    """The original 200x200 thumbnail test, kept as a second opinion that can
    only raise a slot: whatever it called content or faint stays at least that,
    so no slot is quieter than before the stroke measure was added."""
    histogram = image.convert("L").resize((200, 200)).histogram()
    total = 200 * 200
    if sum(histogram[:140]) / total >= ink_threshold:
        return "content"
    marks = sum(histogram[:235]) / total
    background = max(range(256), key=histogram.__getitem__)
    against_paper = sum(histogram[:max(0, background - 8)]) / total
    return "faint" if max(marks, against_paper) >= faint_threshold else "empty"


def assess_slot(image: Image.Image, *, ink_threshold: float = 0.002,
                faint_threshold: float = 0.0015, plain_cut: Image.Image | None = None) -> SlotAssessment:
    """Classify one slot without discarding anything.

    Measured at the crop's own resolution (B1): the old test shrank every crop
    to 200x200 px first, so a 1-up page (1700x2200 px) averaged a 0.5 mm pen
    stroke to light grey and a clean ticket read as "faint". Now ink is the
    share of the slot covered by connected strokes clearly darker than the
    paper beside them, which doesn't depend on the crop's size.

    "content": clearly written strokes (or the old thumbnail test says so).
    "faint": visible marks too light or too few for that, so a person decides.
    "empty": effectively no marks by either measure. Thresholds are cautious
    and NOT calibrated on real scanners. `plain_cut` is the part of the image
    the old fixed cut held, when the crop has grown: the thumbnail test reads
    that, exactly as it did before.
    """
    grey = _grey(image)
    contrast = stroke_contrast(grey)
    min_area = max(12, (min(grey.shape) // 200) ** 2)
    ink = _connected_share(contrast >= _INK_CONTRAST, min_area)
    old = _thumbnail_reading(plain_cut or image, ink_threshold, faint_threshold)
    if ink >= ink_threshold or old == "content":
        return SlotAssessment(ink, "content")
    marks = _connected_share(contrast >= _MARK_CONTRAST, min_area)
    if marks >= faint_threshold or old == "faint":
        return SlotAssessment(ink, "faint")
    return SlotAssessment(ink, "empty")
