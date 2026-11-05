"""Write a fictional 4-up scan and supplier invoice for the local demo.

Usage (from backend/): python scripts/make_demo_kit.py --output-dir demo_output/kit

The invoice's total equals the deliveries' total but lists a ticket number that
was never delivered, which is the discrepancy totals alone would miss. No AI,
no network, no database. Never overwrites a file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.sample_data import make_invoice_pdf, make_page, make_pdf  # noqa: E402

STONE = dict(delivery_date="2026-10-15", material_type="No. 53 Crushed Stone",
             supplier="Example Aggregates", job_location="North Lot")
FIRST = dict(STONE, ticket_number="10482", tonnage="24.50")
SECOND = dict(STONE, ticket_number="10483", tonnage="22.10")
# What the faint slot says. People type this in; it can't be read automatically.
FAINT = dict(STONE, ticket_number="55120", tonnage="18.40")
TOTAL_TONS, RATE, SUBTOTAL = "65.000", "30.0000", "1,950.00"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    tickets, invoice = args.output_dir / "demo-tickets.pdf", args.output_dir / "demo-invoice.pdf"
    for path in (tickets, invoice):
        if path.exists():
            print(f"Refusing to overwrite {path}", file=sys.stderr)
            return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    tickets.write_bytes(make_pdf([make_page([FIRST, "faint", None, SECOND])]))
    invoice.write_bytes(make_invoice_pdf(
        rows=(("No. 53 Crushed Stone", TOTAL_TONS, "TON", RATE, SUBTOTAL),),
        tickets=(("10482", "24.50"), ("10483", "22.10"), ("55999", "18.40")),
        extras=(("Freight", "120.00"), ("Invoice total", "2,070.00")),
    ))
    print(f"Wrote {tickets} and {invoice}. All fictional.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
