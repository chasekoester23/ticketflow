"""Generate unmistakably fictional delivery scans for local UI practice.

Usage (from backend/):
    python scripts/make_demo_tickets.py
    python scripts/make_demo_tickets.py --layout single --output demo_output/one-ticket.pdf

No AI calls, no network, no database writes, and no real project documents.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.sample_data import TICKETS, make_scan_page, make_ticket_image  # noqa: E402


def make_demo_pdf(output: Path, layout: str = "grid_2x2") -> Path:
    """Write generated fake tickets to a new file without overwriting anything."""
    if layout not in ("single", "grid_2x2"):
        raise ValueError("Choose 'single' or 'grid_2x2'")
    if output.suffix.lower() != ".pdf":
        raise ValueError("Output must end with .pdf")
    # Our example 2x2 sheet is all-printed (not handwriting). The text is
    # deliberately absurdly explicit that this document is fictional.
    if layout == "grid_2x2":
        image = make_scan_page(TICKETS)
        positions = [(40, 40), (890, 40), (40, 1140), (890, 1140)]
    else:
        image = make_ticket_image(TICKETS[0])
        positions = [(10, 10)]

    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    for x, y in positions:
        mark_y = y + (875 if layout == "grid_2x2" else 925)
        draw.rectangle((x + 22, mark_y, x + 650, mark_y + 48), fill=(254, 236, 220))
        draw.text(
            (x + 34, mark_y + 11),
            "SYNTHETIC DEMO / NOT A REAL DELIVERY",
            fill=(151, 50, 15), font=font,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle:
        image.save(handle, format="PDF", resolution=200)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Create fictional, clearly labeled scanned tickets")
    parser.add_argument("--layout", choices=("single", "grid_2x2"), default="grid_2x2")
    parser.add_argument("--output", type=Path, default=Path("demo_output/synthetic-4up.pdf"))
    args = parser.parse_args()
    try:
        result = make_demo_pdf(args.output, args.layout)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(f"Created synthetic practice scan: {result}")
    print("All four/single tickets are fictional. Select Manual entry to test them without AI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
