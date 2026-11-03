"""Turn a filled practice answer key into an evaluation labels file.

Usage from backend/ (after scanning the filled sheets, in order, into one PDF):
    python scripts/practice_key_to_labels.py \
        --key evaluation_data/practice/answer-key.csv \
        --scan evaluation_data/practice/scanned-practice.pdf

Writes labels.json beside the scan (refuses to overwrite), then run:
    python scripts/evaluate_extraction.py check --manifest evaluation_data/practice/labels.json

No network, no AI calls, no database access.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.evaluation import DatasetError  # noqa: E402
from app.services.practice_kit import labels_from_answer_key  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a practice answer key into labels.json")
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--scan", required=True, type=Path, help="Scanned PDF, sheet 1 first")
    args = parser.parse_args(argv)
    scan = args.scan.resolve()
    output = scan.parent / "labels.json"
    try:
        if scan.suffix.lower() != ".pdf" or not scan.is_file():
            raise DatasetError("--scan must be an existing PDF of the filled sheets")
        if output.exists():
            raise DatasetError(f"{output} already exists; refusing to overwrite labels")
        try:
            with pymupdf.open(str(scan)) as document:
                pages = len(document)
        except Exception as exc:
            raise DatasetError(f"Could not open the scan PDF: {type(exc).__name__}") from exc
        manifest, unused = labels_from_answer_key(
            args.key.read_text(encoding="utf-8-sig"), scan.name, pages,
        )
        with output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, indent=2) + "\n")
    except (DatasetError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {output} with {len(manifest['tickets'])} labeled practice ticket(s); "
          f"{unused} unused row(s) skipped.")
    print("Next: run 'evaluate_extraction.py check' and confirm each practice ID matches its crop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
