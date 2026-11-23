"""How often does the fixed 2x2 grid cut a ticket? A geometric simulation.

Usage (from backend/):
    python scripts/measure_grid_crops.py
    python scripts/measure_grid_crops.py --trials 20000 --json grid.json

Four tickets are laid on a page, one per quarter, then "captured" with the
placement error, tilt and off-centre framing of a scanner or a phone photo. The
crop is the production one (splitter.grid_boxes), so the only question is
geometric: how much of each ticket's outline falls outside its own crop? A cut up
to EDGE (4%) of the ticket's own width or height takes only the blank printed
border; beyond it, a line of text can be lost.

This answers whether automatic ticket-boundary detection is worth building, for
the conditions listed. It is a model, not a measurement of real paper: the
ranges below are assumptions. The real measurement is crop completeness labelled
from paper in the workflow evaluation (scripts/evaluate_workflow.py); this script
says which conditions to look for in that sample. No AI, no files, no database.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.splitter import grid_boxes  # noqa: E402

WIDTH, HEIGHT = 1700, 2200  # US Letter at 200 dpi; the result doesn't depend on scale
EDGE = 0.04  # a ticket's blank border, as a fraction of its size: cutting into it loses nothing


@dataclass(frozen=True)
class Scenario:
    name: str
    fill: float          # ticket size as a fraction of its quarter of the page
    jitter: float        # how far a ticket sits from the centre of its quarter, as a fraction of the page
    tilt_degrees: float  # rotation of the whole capture, up to this either way
    shift: float         # off-centre framing of the whole capture, as a fraction of the page
    note: str


SCENARIOS = [
    Scenario("scanner, tidy", 0.85, 0.02, 0.5, 0.005, "Tickets squared up on a flatbed, some room around each"),
    Scenario("scanner, large tickets", 0.95, 0.015, 0.5, 0.005, "Tickets nearly filling their quarter"),
    Scenario("phone, careful", 0.85, 0.03, 2.0, 0.02, "Photo taken square-on, sheet filling the frame"),
    Scenario("phone, hurried", 0.85, 0.04, 5.0, 0.05, "Photo at an angle, sheet off-centre"),
    Scenario("phone, hurried, large tickets", 0.95, 0.04, 5.0, 0.05, "As above, tickets nearly filling their quarter"),
]


def _captured_outline(box, angle: float, dx: float, dy: float):
    """The ticket's corners after rotating the capture about the page centre and shifting it."""
    cx, cy = WIDTH / 2, HEIGHT / 2
    cos, sin = math.cos(angle), math.sin(angle)
    left, top, right, bottom = box
    for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
        rx, ry = x - cx, y - cy
        yield cx + rx * cos - ry * sin + dx, cy + rx * sin + ry * cos + dy


def measure(scenario: Scenario, trials: int, seed: int = 7) -> dict:
    rng = random.Random(seed)
    crops = grid_boxes(WIDTH, HEIGHT, rows=2, cols=2)
    complete = edge = material = 0
    worst = 0.0  # the deepest cut into a ticket, as a fraction of the ticket's size
    cell_w, cell_h = WIDTH / 2, HEIGHT / 2
    w, h = cell_w * scenario.fill, cell_h * scenario.fill
    for _ in range(trials):
        angle = math.radians(rng.uniform(-scenario.tilt_degrees, scenario.tilt_degrees))
        dx = rng.uniform(-scenario.shift, scenario.shift) * WIDTH
        dy = rng.uniform(-scenario.shift, scenario.shift) * HEIGHT
        for position, crop in enumerate(crops):
            col, row = position % 2, position // 2
            cx = (col + 0.5) * cell_w + rng.uniform(-scenario.jitter, scenario.jitter) * WIDTH
            cy = (row + 0.5) * cell_h + rng.uniform(-scenario.jitter, scenario.jitter) * HEIGHT
            # Paper can't sit off the page.
            cx = min(max(cx, w / 2), WIDTH - w / 2)
            cy = min(max(cy, h / 2), HEIGHT - h / 2)
            corners = list(_captured_outline((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), angle, dx, dy))
            left, top, right, bottom = crop
            depth = max(0.0, left - min(x for x, _ in corners), max(x for x, _ in corners) - right) / w
            depth = max(depth, max(0.0, top - min(y for _, y in corners), max(y for _, y in corners) - bottom) / h)
            worst = max(worst, depth)
            if depth == 0:
                complete += 1
            elif depth <= EDGE:
                edge += 1
            else:
                material += 1
    total = complete + edge + material
    return {**asdict(scenario), "tickets": total, "complete": complete, "edge_only": edge, "material_cut": material,
            "usable_rate": round((complete + edge) / total, 4), "material_cut_rate": round(material / total, 4),
            "worst_cut_fraction_of_ticket": round(worst, 4)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=10_000, help="pages per scenario (4 tickets each)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path, help="also write the results here")
    args = parser.parse_args(argv)
    results = [measure(s, args.trials, args.seed) for s in SCENARIOS]
    print("MODEL, NOT A MEASUREMENT OF REAL PAPER: the conditions are assumptions (see --help).")
    print(f"Usable: nothing outside the crop beyond {EDGE:.0%} of the ticket (its blank border).")
    print(f"{'scenario':32} {'usable':>8} {'text cut':>9} {'worst':>7}  conditions")
    for r in results:
        print(f"{r['name']:32} {r['usable_rate']:>8.2%} {r['material_cut_rate']:>9.2%} {r['worst_cut_fraction_of_ticket']:>7.1%}  "
              f"fill {r['fill']:.0%}, placement ±{r['jitter']:.1%}, tilt ±{r['tilt_degrees']}°, framing ±{r['shift']:.1%}")
    if args.json:
        args.json.write_text(json.dumps({"model": True, "trials": args.trials, "seed": args.seed, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
