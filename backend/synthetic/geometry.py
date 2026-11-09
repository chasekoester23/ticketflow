"""2-D affine transforms and polygon areas, in continuous pixel coordinates
(pixel edges on integers, y down)."""
from __future__ import annotations

import math

Matrix = tuple[float, float, float, float, float, float]  # x' = a x + b y + c ; y' = d x + e y + f
IDENTITY: Matrix = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def compose(m2: Matrix, m1: Matrix) -> Matrix:
    """m2 after m1."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a2 * a1 + b2 * d1, a2 * b1 + b2 * e1, a2 * c1 + b2 * f1 + c2,
            d2 * a1 + e2 * d1, d2 * b1 + e2 * e1, d2 * c1 + e2 * f1 + f2)


def translate(tx: float, ty: float) -> Matrix:
    return (1.0, 0.0, tx, 0.0, 1.0, ty)


def rotate(degrees: float) -> Matrix:
    """Counter-clockwise on screen (y down), about the origin."""
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    return (c, s, 0.0, -s, c, 0.0)


def shear(kx: float, ky: float = 0.0) -> Matrix:
    return (1.0, kx, 0.0, ky, 1.0, 0.0)


def about(m: Matrix, cx: float, cy: float) -> Matrix:
    return compose(translate(cx, cy), compose(m, translate(-cx, -cy)))


def invert(m: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    det = a * e - b * d
    ia, ib, id_, ie = e / det, -b / det, -d / det, a / det
    return (ia, ib, -(ia * c + ib * f), id_, ie, -(id_ * c + ie * f))


def apply(m: Matrix, point) -> tuple[float, float]:
    x, y = point
    a, b, c, d, e, f = m
    return (a * x + b * y + c, d * x + e * y + f)


def box_quad(box) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = box
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def area(poly) -> float:
    n = len(poly)
    return abs(sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))) / 2


def clip_to_box(poly, box) -> list[tuple[float, float]]:
    """Sutherland-Hodgman: the part of a convex polygon inside an axis-aligned box."""
    x0, y0, x1, y1 = box
    edges = [(lambda p: p[0] >= x0, lambda p, q: _cut_x(p, q, x0)), (lambda p: p[0] <= x1, lambda p, q: _cut_x(p, q, x1)),
             (lambda p: p[1] >= y0, lambda p, q: _cut_y(p, q, y0)), (lambda p: p[1] <= y1, lambda p, q: _cut_y(p, q, y1))]
    out = list(poly)
    for inside, cut in edges:
        if not out:
            break
        src, out = out, []
        for i, cur in enumerate(src):
            prev = src[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
    return out


def _cut_x(p, q, x):
    t = (x - p[0]) / (q[0] - p[0])
    return (x, p[1] + t * (q[1] - p[1]))


def _cut_y(p, q, y):
    t = (y - p[1]) / (q[1] - p[1])
    return (p[0] + t * (q[0] - p[0]), y)


def inside_fraction(poly, box) -> float:
    whole = area(poly)
    return area(clip_to_box(poly, box)) / whole if whole else 0.0


def separated(p, q, gap: float = 0.0) -> bool:
    """True when two convex polygons are at least `gap` apart (separating axis test)."""
    for poly in (p, q):
        n = len(poly)
        for i in range(n):
            (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
            nx, ny = y1 - y0, x0 - x1
            length = (nx * nx + ny * ny) ** 0.5 or 1.0
            nx, ny = nx / length, ny / length
            pa = [nx * x + ny * y for x, y in p]
            qa = [nx * x + ny * y for x, y in q]
            if max(pa) + gap <= min(qa) or max(qa) + gap <= min(pa):
                return True
    return False


def bounds(poly) -> list[float]:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def visible_part(poly, placement: Matrix, page: Matrix, box) -> list[tuple[float, float]]:
    """What is left of a polygon (ticket px) after it is placed on a canvas `box`
    (anything off the canvas is lost), and the canvas is then moved by `page`
    and seen through the same `box`. Both cuts matter: a corner pushed off the
    canvas doesn't come back when the sheet is turned."""
    placed = clip_to_box([apply(placement, p) for p in poly], box)
    return clip_to_box([apply(page, p) for p in placed], box) if placed else []


def visible_fraction(poly, placement: Matrix, page: Matrix, box) -> float:
    whole = area(poly)  # affine maps here preserve area up to the shear's determinant
    det = (page[0] * page[4] - page[1] * page[3]) * (placement[0] * placement[4] - placement[1] * placement[3])
    return area(visible_part(poly, placement, page, box)) / (whole * abs(det)) if whole else 0.0
