"""Print blank SYNTHETIC practice tickets and a matching answer key.

Usage from backend/:
    python scripts/make_practice_tickets.py --sheets 5

Writes (refusing to overwrite) into the git-ignored evaluation folder:
    evaluation_data/practice/practice-tickets.pdf   print this, 4 tickets per page
    evaluation_data/practice/answer-key.csv         fill in what each ticket says

No network, no AI calls, no database access. See backend/EVALUATION.md,
"Practice tickets you write yourself".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.practice_kit import MAX_SHEETS, answer_key_csv, build_practice_pdf  # noqa: E402

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "evaluation_data" / "practice"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create printable synthetic practice tickets")
    parser.add_argument("--sheets", type=int, default=5, help=f"Pages to print, 4 tickets each (1-{MAX_SHEETS})")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    if not 1 <= args.sheets <= MAX_SHEETS:
        parser.error(f"--sheets must be between 1 and {MAX_SHEETS}")
    out = args.output_dir
    pdf_path, key_path = out / "practice-tickets.pdf", out / "answer-key.csv"
    existing = [p for p in (pdf_path, key_path) if p.exists()]
    if existing:
        print("Refusing to overwrite existing practice files (your filled-in key may be there):", file=sys.stderr)
        for p in existing:
            print(f"  {p}", file=sys.stderr)
        print("Use --output-dir with a NEW folder name instead.", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    pdf_bytes, key_text = build_practice_pdf(args.sheets), answer_key_csv(args.sheets)
    with pdf_path.open("xb") as handle:
        handle.write(pdf_bytes)
    with key_path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(key_text)
    print(f"Printable tickets: {pdf_path}  ({args.sheets} page(s), {args.sheets * 4} tickets)")
    print(f"Answer key:        {key_path}")
    print("Print at 100% scale (not 'fit to page'), fill by hand, then scan the sheets IN ORDER into one PDF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
