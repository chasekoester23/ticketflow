"""Find the tickets on a scanned page with deterministic image processing (no AI).

Used by the "auto" layout, so a contractor can scan pages holding one to six
tickets, placed anywhere, of different sizes and slightly crooked, without
choosing a layout first.

How it works, on a copy of the page reduced to about 1000 px on its long side:
1. Ink: pixels clearly darker than their own neighbourhood (adaptive threshold),
   after a small median blur, so faint print and grey paper both count and
   scanner speckle does not.
2. On a dark background (a phone photo on a table), the bright paper itself is
   found too (Otsu), and each sheet of paper becomes a ticket.
3. A morphological close joins letters and lines into blobs. Ticket-sized blobs
   are tickets; small blobs (a lone line, a stamp, a note) join the ticket they
   touch or sit close to, or group with other small blobs into a ticket if
   together they are ticket-sized.
4. Overlapping tickets merge; they are sorted in reading order.

Nothing on the page is ever lost by this:
- every mark that belongs to no ticket becomes its own "stray" region (or is
  below a dust threshold), and the regions together must cover the page's ink;
- when detection is unsure (no ticket-sized area, too many areas, regions
  missing ink), the WHOLE PAGE becomes one region, marked for review.
A false negative costs a delivery; an extra region costs one click (skip it).

Each region can carry a deskew angle (from minAreaRect) for its crop. The page
image is never modified; crops are new images. Boxes are (left, top, right,
bottom) in pixels of the page image passed in, the same as every other crop.

The thresholds below were tuned on synthetic pages only (see
docs/engineering/segmentation.md), never on real scanner output.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

Box = tuple[int, int, int, int]

WORK_SIZE = 1000          # long side of the analysis copy, px
CLOSE_FRACTION = 0.012    # close kernel, fraction of the long side: joins letters and lines
GROUP_FRACTION = 0.03     # small blobs this close to a ticket join it
GROUP_CLOSE_FRACTION = 0.055  # other blobs at least this close to each other form one area,
LINE_GAPS = 3.5               # or up to this many typical line heights apart,
GROUP_MAX_FRACTION = 0.12     # but never further than this
FRAME_RECTANGULAR = 0.9   # outline area / min-area-rect area of a printed border
FRAME_HOLLOW = 0.6        # a border's blob covers at most this much of what it encloses
PAD_FRACTION = 0.01       # margin added around each region, fraction of the long side
MIN_TICKET_AREA = 0.012   # a ticket covers at least this fraction of the page (bounding box)
MIN_TICKET_SIDE = 0.06    # and each side is at least this fraction of the page's short side
MAX_ASPECT = 8.0
MIN_DENSITY = 0.01        # a ticket made of small blobs has at least this much ink in its box
MAX_STRAYS = 3            # more unexplained areas than this, or
MAX_STRAY_SHARE = 0.15    # this share of the ink outside tickets: use the whole page
NEAR_FACTOR = 1.5         # unbordered areas this many grouping gaps apart may be parts of one ticket
LARGE_FACTOR = 1.7        # an area without a border this much larger than the others may be two tickets
MAX_TICKETS = 12          # more areas than this is not a page of tickets: use the whole page
DUST_PIXELS = 6           # ink components this small (analysis px) are speckle
STRAY_MIN_INK = 0.00012   # ink outside tickets above this fraction of the page becomes a region
MIN_COVERAGE = 0.98       # regions must hold this much of the page's ink, or use the whole page
DESKEW_MIN, DESKEW_MAX = 0.7, 20.0  # degrees
RECTANGULAR = 0.85        # hull area / min-area-rect area needed to trust the angle

WHOLE_PAGE_REVIEW = ("Auto layout could not separate the tickets on this page, so this is the whole page. "
                     "If it shows more than one ticket, add a region for each.")
TOO_MANY_REVIEW = ("Auto layout found too many separate areas on this page, so this is the whole page. "
                   "If it shows more than one ticket, add a region for each.")
STRAY_REVIEW = "Marks found outside the tickets on this page. Check it; skip it if it is not a ticket."
LARGE_REVIEW = "This area is much larger than the other tickets on the page. Check it holds only one ticket."
OVERLAP_REVIEW = "This area overlaps another ticket on the page. Check both crops."
CONTAINER_REVIEW = ("This ticket was inside a larger printed box holding others. Check whether the "
                    "larger box is really one ticket.")
TOUCHING_REVIEW = "This sheet touches or overlaps another one. Check the crop holds one whole ticket."
TOUCHING_PAGE_REVIEW = ("The tickets in this photo touch or overlap, so this is the whole photo. "
                        "If it shows more than one ticket, add a region for each.")
NEAR_REVIEW = ("No printed border, and close to another area: check this crop holds one whole ticket, "
               "not part of one.")
# Short codes for the notes, for records with little room (scan events).
REVIEW_CODES = {
    WHOLE_PAGE_REVIEW: "whole_page", TOO_MANY_REVIEW: "too_many", STRAY_REVIEW: "stray",
    LARGE_REVIEW: "large", OVERLAP_REVIEW: "overlap", CONTAINER_REVIEW: "container",
    TOUCHING_REVIEW: "touching", TOUCHING_PAGE_REVIEW: "touching_page", NEAR_REVIEW: "near",
}
REVIEW_NOTES = {code: note for note, code in REVIEW_CODES.items()}


@dataclass(frozen=True)
class Region:
    box: Box                # padded, axis-aligned, page pixels
    kind: str               # ticket | stray | page
    angle: float = 0.0      # degrees counter-clockwise that deskew the crop; 0 = leave as is
    review: str | None = None  # why a person should check this region, or None


@dataclass(frozen=True)
class Segmentation:
    regions: list[Region]
    confident: bool
    reason: str
    ink_coverage: float     # fraction of the page's ink inside the regions (1.0 for a blank page)


@dataclass
class _Group:
    box: list[int]          # x0, y0, x1, y1 (exclusive), analysis px
    labels: list[int]
    paper: np.ndarray | None = None  # contour of a detected sheet of paper, analysis px
    kind: str = "ticket"
    framed: bool = True     # found by a printed border or a sheet of paper
    note: str | None = None  # a review note decided while finding it


def _odd(value: float) -> int:
    n = max(3, int(round(value)))
    return n if n % 2 else n + 1


def _gap(a, b) -> int:
    """Distance between two boxes; 0 when they touch or overlap."""
    dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return max(dx, dy)


def _union(a, b) -> list[int]:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _area(b) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _intersection(a, b) -> int:
    return _area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def _ticket_sized(box, w: int, h: int) -> bool:
    bw, bh = box[2] - box[0], box[3] - box[1]
    short = min(w, h)
    return (bw * bh >= MIN_TICKET_AREA * w * h and min(bw, bh) >= MIN_TICKET_SIDE * short
            and max(bw, bh) <= MAX_ASPECT * max(1, min(bw, bh)))


def ink_mask(gray: np.ndarray) -> tuple[np.ndarray, int]:
    """Clearly dark marks (255) on an analysis-size grey image, and the background level."""
    h, w = gray.shape
    blurred = cv2.medianBlur(gray, 3)
    edge = max(2, int(0.02 * min(w, h)))
    border = np.concatenate([blurred[:edge].ravel(), blurred[-edge:].ravel(),
                             blurred[:, :edge].ravel(), blurred[:, -edge:].ravel()])
    background = int(np.median(border))
    ink = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV,
                                _odd(0.035 * max(w, h)), 10)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    small = stats[:, cv2.CC_STAT_AREA] < DUST_PIXELS
    small[0] = False
    ink[small[labels]] = 0
    return ink, background


def _papers(gray: np.ndarray, background: int) -> tuple[list[tuple[np.ndarray, str | None]], bool]:
    """Bright sheets on a darker background (a photo on a table): their contours, each
    with a review note or None, and whether some sheet could not be told apart.
    On a white scanner bed the paper is the background: ([], False)."""
    if background >= 200:
        return [], False
    h, w = gray.shape
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    threshold, paper = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if threshold - background < 20:
        return [], False
    k = _odd(CLOSE_FRACTION * max(w, h))
    paper = cv2.morphologyEx(paper, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    contours, _ = cv2.findContours(paper, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found: list[tuple[np.ndarray, str | None]] = []
    unsure = False
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        if not _ticket_sized((x, y, x + bw, y + bh), w, h):
            continue
        if _rectangular(contour):
            # One sheet filling most of the frame is the page itself, not a ticket on it.
            if bw * bh < 0.7 * w * h:
                found.append((contour, None))
            continue
        # Not one rectangle: sheets touching or overlapping. Pull them apart.
        sheets = _split_sheets(contour, w, h)
        if sheets:
            found += [(sheet, TOUCHING_REVIEW) for sheet in sheets]
        elif bw * bh < 0.7 * w * h:
            found.append((contour, TOUCHING_REVIEW))
        else:
            unsure = True
    return found, unsure


def _rectangular(contour: np.ndarray) -> bool:
    (_, _), (rw, rh), _ = cv2.minAreaRect(contour)
    return rw * rh > 0 and cv2.contourArea(contour) / (rw * rh) >= FRAME_RECTANGULAR


def _split_sheets(contour: np.ndarray, w: int, h: int) -> list[np.ndarray]:
    """Touching sheets, separated by shrinking the shape until it falls apart at the
    narrow joins, then growing each part back inside the original shape. [] if it won't."""
    shape = np.zeros((h, w), np.uint8)
    cv2.drawContours(shape, [contour], -1, 255, thickness=cv2.FILLED)
    for fraction in (0.02, 0.03, 0.045, 0.06, 0.08):
        r = _odd(fraction * max(w, h))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r))
        count, parts = cv2.connectedComponents(cv2.erode(shape, kernel), connectivity=8)
        cores = [i for i in range(1, count) if np.count_nonzero(parts == i) >= MIN_TICKET_AREA / 3 * w * h]
        if len(cores) < 2:
            continue
        sheets = []
        for i in cores:
            grown = cv2.dilate((parts == i).astype(np.uint8) * 255, kernel) & shape
            outline, _ = cv2.findContours(grown, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            sheets.append(max(outline, key=cv2.contourArea))
        return sheets
    return []


def _group_gap(boxes: dict[int, list[int]], w: int, h: int) -> float:
    """How far apart the parts of one ticket may be. A ticket's layout scales with its
    print, so this follows the page's typical line height: a full-page ticket printed
    large keeps its header and columns together, while small tickets laid close stay apart."""
    lines = sorted(b[3] - b[1] for b in boxes.values()
                   if b[3] - b[1] >= 3 and b[2] - b[0] >= 1.5 * (b[3] - b[1]) and not _ticket_sized(b, w, h))
    floor = GROUP_CLOSE_FRACTION * max(w, h)
    if len(lines) < 5:
        return floor
    return min(max(floor, LINE_GAPS * lines[len(lines) // 2]), GROUP_MAX_FRACTION * max(w, h))


def _is_frame(labels: np.ndarray, label: int, box) -> bool:
    """A printed border: a hollow, rectangular outline (straight or tilted)."""
    x0, y0, x1, y1 = box
    mask = (labels[y0:y1, x0:x1] == label).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    outline = max(contours, key=cv2.contourArea)
    filled = cv2.contourArea(outline)
    (_, _), (rw, rh), _ = cv2.minAreaRect(outline)
    if filled <= 0 or rw * rh <= 0:
        return False
    return filled / (rw * rh) >= FRAME_RECTANGULAR and np.count_nonzero(mask) <= FRAME_HOLLOW * filled


def _reading_order(boxes: list[list[int]]) -> list[int]:
    """Indexes of boxes top-to-bottom in rows, left-to-right within a row."""
    order = sorted(range(len(boxes)), key=lambda i: (boxes[i][1], boxes[i][0]))
    rows: list[list[int]] = []
    for i in order:
        top, bottom = boxes[i][1], boxes[i][3]
        middle = (top + bottom) / 2
        for row in rows:
            r_top = min(boxes[j][1] for j in row)
            r_bottom = max(boxes[j][3] for j in row)
            if r_top <= middle <= r_bottom and boxes[row[0]][1] <= middle:
                row.append(i)
                break
        else:
            rows.append([i])
    return [i for row in rows for i in sorted(row, key=lambda j: boxes[j][0])]


def _angle(points: np.ndarray) -> float:
    """Counter-clockwise degrees that straighten a rectangular blob, or 0 when unsure."""
    if points is None or len(points) < 5:
        return 0.0
    (_, _), (rw, rh), angle = cv2.minAreaRect(points)
    if rw * rh <= 0:
        return 0.0
    hull = cv2.contourArea(cv2.convexHull(points))
    if hull / (rw * rh) < RECTANGULAR:
        return 0.0
    # OpenCV versions differ in the range they report; a rectangle's tilt repeats every
    # 90 degrees, so take the smallest equivalent, within [-45, 45).
    tilt = (angle + 45) % 90 - 45
    if not DESKEW_MIN <= abs(tilt) <= DESKEW_MAX:
        return 0.0
    # minAreaRect measures clockwise on screen (y down); PIL rotates counter-clockwise.
    return float(tilt)


def _whole_page(w: int, h: int, review: str | None, reason: str, coverage: float) -> Segmentation:
    return Segmentation([Region((0, 0, w, h), "page", 0.0, review)], confident=review is None,
                        reason=reason, ink_coverage=coverage)


def segment_page(image: Image.Image) -> Segmentation:
    """The ticket regions on one page image. Never returns an empty list."""
    W, H = image.size
    scale = min(1.0, WORK_SIZE / max(W, H))
    w, h = max(1, round(W * scale)), max(1, round(H * scale))
    gray = np.asarray(image.convert("L"))
    if scale < 1:
        # Thicken dark strokes first (a minimum filter), so a hairline rule or a thin
        # pen mark survives the reduction instead of averaging away to paper colour.
        # Speckle goes first, or the minimum filter would spread it too.
        spread = _odd(1 / scale)
        gray = cv2.medianBlur(gray, 3)
        gray = cv2.erode(gray, cv2.getStructuringElement(cv2.MORPH_RECT, (spread, spread)))
        gray = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA)
    ink, background = ink_mask(gray)
    total_ink = int(np.count_nonzero(ink))
    if total_ink < STRAY_MIN_INK * w * h:
        # Blank, or almost: one region, and the existing blank-slot checks decide.
        return _whole_page(W, H, None, "no marks on this page", 1.0)

    groups: list[_Group] = []
    # Sheets of paper on a dark background are tickets, whatever they hold. The marks on
    # them, and the outline the threshold draws just outside their edges, belong to them.
    sheets, unsure = _papers(gray, background)
    if unsure:
        return _whole_page(W, H, TOUCHING_PAGE_REVIEW, "sheets touching, not separable", 1.0)
    zone = np.zeros((h, w), np.uint8)
    ring = _odd(0.035 * max(w, h)) // 2 + 2
    for contour, note in sheets:
        x, y, bw, bh = cv2.boundingRect(contour)
        box = [max(0, x - ring), max(0, y - ring), min(w, x + bw + ring), min(h, y + bh + ring)]
        groups.append(_Group(box, [], paper=contour, note=note))
        cv2.drawContours(zone, [contour], -1, 255, thickness=cv2.FILLED)
    if sheets:
        zone = cv2.dilate(zone, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * ring + 1, 2 * ring + 1)))
    loose = ink & ~zone  # marks on no sheet (all of them on a scan)

    k = _odd(CLOSE_FRACTION * max(w, h))
    closed = cv2.morphologyEx(loose, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    ink_per_label = np.bincount(labels[loose > 0], minlength=count)
    boxes = {i: [int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 0] + stats[i, 2]), int(stats[i, 1] + stats[i, 3])]
             for i in range(1, count) if ink_per_label[i] > 0}

    unassigned, big = [], []
    for i, box in boxes.items():
        if _ticket_sized(box, w, h) and _is_frame(labels, i, box):
            groups.append(_Group(list(box), [i]))  # a printed border: one ticket
        elif _ticket_sized(box, w, h):
            big.append(i)  # dense print with no border: grouped below
        else:
            unassigned.append(i)

    # A box drawn around two or more tickets (or the table showing between photographed
    # sheets) is a container, not a ticket: the tickets inside it are. Its own marks stay
    # outside every region, so unless they are slight, the coverage check below falls back
    # to the whole page.
    containers = [g for g in groups if g.paper is None and sum(
        1 for o in groups if o is not g and _intersection(o.box, g.box) >= 0.9 * _area(o.box)) >= 2]
    for container in containers:
        groups.remove(container)
        for o in groups:
            if _intersection(o.box, container.box) >= 0.9 * _area(o.box):
                o.note = o.note or CONTAINER_REVIEW

    # Small blobs join the nearest ticket within reach...
    reach = max(2, int(GROUP_FRACTION * max(w, h)))
    # Measured against each ticket as first found, so a trail of specks can't stretch it.
    fixed = [list(g.box) for g in groups]
    leftovers = []
    for i in unassigned:
        near = [(_gap(boxes[i], box), n) for n, box in enumerate(fixed) if _gap(boxes[i], box) <= reach]
        if near:
            g = groups[min(near)[1]]
            g.labels.append(i)
            g.box = _union(g.box, boxes[i])
        else:
            leftovers.append(i)
    leftovers += big
    # ...or group with each other at a coarser scale: a ticket with no printed border
    # is many small blobs (a header, label and value columns) with wider gaps.
    clusters: list[_Group] = []
    k2 = _odd(_group_gap(boxes, w, h))
    if leftovers:
        rest = np.isin(labels, leftovers).astype(np.uint8) * 255
        coarse = cv2.morphologyEx(rest, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k2, k2)))
        _, coarse_labels = cv2.connectedComponents(coarse, connectivity=8)
        members: dict[int, list[int]] = {}
        for i in leftovers:
            x0, y0, x1, y1 = boxes[i]
            window = coarse_labels[y0:y1, x0:x1][labels[y0:y1, x0:x1] == i]
            members.setdefault(int(window.max()), []).append(i)
        for key in sorted(members):
            ids = members[key]
            box = list(boxes[ids[0]])
            for i in ids[1:]:
                box = _union(box, boxes[i])
            clusters.append(_Group(box, ids))
    strays: list[_Group] = []
    small: list[_Group] = []
    for c in clusters:
        held = int(ink_per_label[c.labels].sum())
        # Printed lines are dense; specks scattered over a photo's background are not.
        if _ticket_sized(c.box, w, h) and held >= MIN_DENSITY * _area(c.box):
            c.framed = False
            groups.append(c)
        else:
            small.append(c)
    fixed = [list(g.box) for g in groups]
    for c in small:
        near = [(_gap(c.box, box), n) for n, box in enumerate(fixed) if _gap(c.box, box) <= reach]
        if near:  # a stamp or note beside a ticket with no border belongs to it
            g = groups[min(near)[1]]
            g.labels += c.labels
            g.box = _union(g.box, c.box)
        elif int(ink_per_label[c.labels].sum()) >= STRAY_MIN_INK * w * h:
            c.kind = "stray"
            strays.append(c)
        # else: dust, well below what any ticket holds

    # Tickets that mostly overlap are one ticket (a stamp across a border, a torn edge).
    changed = True
    while changed:
        changed = False
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                inter = _intersection(groups[a].box, groups[b].box)
                if inter >= 0.5 * min(_area(groups[a].box), _area(groups[b].box)):
                    groups[a].box = _union(groups[a].box, groups[b].box)
                    groups[a].labels += groups[b].labels
                    if groups[a].paper is None:
                        groups[a].paper = groups[b].paper
                    del groups[b]
                    changed = True
                    break
            if changed:
                break

    if not groups:
        return _whole_page(W, H, WHOLE_PAGE_REVIEW, "no ticket-sized area found", _coverage(ink, [], zone, total_ink))
    if len(groups) > MAX_TICKETS:
        return _whole_page(W, H, TOO_MANY_REVIEW, f"{len(groups)} areas found", 1.0)

    final = groups + strays
    coverage = _coverage(ink, [g.box for g in final], zone, total_ink)
    if coverage < MIN_COVERAGE:
        return _whole_page(W, H, WHOLE_PAGE_REVIEW, f"regions held only {coverage:.0%} of the ink", coverage)

    stray_ink = sum(int(ink_per_label[g.labels].sum()) for g in strays)
    if len(strays) > MAX_STRAYS or stray_ink > MAX_STRAY_SHARE * total_ink:
        # Much of the print belongs to no ticket: the tickets weren't understood.
        return _whole_page(W, H, WHOLE_PAGE_REVIEW, f"{len(strays)} unexplained area(s)", coverage)

    def larger_than_the_rest(g: _Group) -> bool:
        others = sorted(_area(o.box) for o in groups if o is not g)
        return bool(others) and _area(g.box) >= LARGE_FACTOR * others[len(others) // 2]

    pad = int(round(PAD_FRACTION * max(W, H)))
    ticket_order = _reading_order([g.box for g in groups])
    stray_order = _reading_order([g.box for g in strays])
    ordered = [groups[i] for i in ticket_order] + [strays[i] for i in stray_order]
    regions: list[Region] = []
    for g in ordered:
        review = None
        if g.kind == "stray":
            review = STRAY_REVIEW
        elif g.note:
            review = g.note
        elif (not g.framed or g.paper is not None) and larger_than_the_rest(g):
            # A printed border is one ticket; touching sheets or unbordered print may be two.
            review = LARGE_REVIEW
        elif any(o is not g and o.kind == "ticket" and _intersection(g.box, o.box) > 0.1 * _area(g.box)
                 for o in groups):
            review = OVERLAP_REVIEW
        elif not g.framed and any(o is not g and not o.framed and _gap(g.box, o.box) <= NEAR_FACTOR * k2
                                  for o in groups):
            review = NEAR_REVIEW
        angle = 0.0
        if g.kind == "ticket" and g.framed:
            # Only a printed border or a sheet's edge gives a trustworthy angle.
            if g.paper is not None:
                points = g.paper
            else:
                x0, y0, x1, y1 = g.box
                window = np.isin(labels[y0:y1, x0:x1], g.labels)
                points = cv2.findNonZero(window.astype(np.uint8))
                points = None if points is None else points + np.array([[x0, y0]])
            angle = _angle(points)
        x0, y0, x1, y1 = g.box
        box = (max(0, int(x0 / scale) - pad), max(0, int(y0 / scale) - pad),
               min(W, int(np.ceil(x1 / scale)) + pad), min(H, int(np.ceil(y1 / scale)) + pad))
        regions.append(Region(box, g.kind, angle, review))
    confident = all(r.review is None for r in regions)
    reason = f"{len(groups)} ticket(s)" + (f" and {len(strays)} other mark(s)" if strays else "")
    return Segmentation(regions, confident, reason, coverage)


def _coverage(ink: np.ndarray, boxes: list[list[int]], zone: np.ndarray, total: int) -> float:
    """Share of the page's ink inside the regions (or on a detected sheet of paper)."""
    if total == 0:
        return 1.0
    covered = zone > 0
    for x0, y0, x1, y1 in boxes:
        covered[y0:y1, x0:x1] = True
    return float(np.count_nonzero(ink[covered])) / total


def crop_region(image: Image.Image, region: Region) -> Image.Image:
    """The region's crop, straightened when it is tilted. The page is not changed."""
    crop = image.crop(region.box)
    if not region.angle:
        return crop
    # Fill the corners a rotation exposes with the crop's own paper colour.
    rgb = np.asarray(crop.convert("RGB"))
    edge = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    fill = tuple(int(v) for v in np.median(edge, axis=0))
    return crop.convert("RGB").rotate(region.angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=fill)
