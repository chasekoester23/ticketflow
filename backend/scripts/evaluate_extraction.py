"""Local-only handwritten-ticket benchmark.

Usage from backend/:
  python scripts/evaluate_extraction.py prepare --scans evaluation_data --layout grid_2x2
  # Fill in each of the six expected fields by reading the ORIGINAL scan.
  python scripts/evaluate_extraction.py check --manifest evaluation_data/labels.json
  python scripts/check_api_key.py
  python scripts/evaluate_extraction.py run --manifest evaluation_data/labels.json
  # For permitted comparisons: --provider openai | --provider gemini

Real documents, labels and reports are deliberately ignored by Git.
Running 'check' never makes an API call. Running 'run' uses the explicitly chosen paid provider and may incur
actual API charges. Alternative providers are evaluation-only, not production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.evaluation import (  # noqa: E402
    DatasetError, evaluate, load_manifest, prepare_manifest,
)
from app.services.extraction import ExtractionError  # noqa: E402
from app.services.benchmark_providers import (  # noqa: E402
    DEFAULT_MODELS, make_benchmark_extractor,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure six-field extraction accuracy against independently labeled construction tickets."
    )
    commands = parser.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare", help="Generate an EMPTY annotation template, without calling AI")
    prepare.add_argument("--scans", required=True, type=Path, help="Folder with real PDF/JPG/PNG scans")
    prepare.add_argument("--layout", required=True, choices=("single", "grid_2x2"))
    prepare.add_argument("--output", type=Path, help="Default: SCANS/labels.json")

    check = commands.add_parser("check", help="Validate labels and scan crops; NO API calls")
    check.add_argument("--manifest", required=True, type=Path)
    run = commands.add_parser("run", help="Run paid vision extraction and calculate accuracy")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--output", type=Path, help="Default: manifest folder/accuracy-report-<provider>.json")
    run.add_argument(
        "--provider", choices=tuple(DEFAULT_MODELS), default="anthropic",
        help="Explicit paid benchmark provider (default: anthropic)",
    )
    run.add_argument(
        "--model", help="Optional API model ID; defaults to the current documented candidate",
    )

    args = parser.parse_args()
    try:
        if args.action == "prepare":
            folder = args.scans.resolve()
            output = (args.output or folder / "labels.json").resolve()
            if output.parent != folder:
                raise DatasetError("Put the annotation manifest in the scan folder so relative paths resolve")
            if output.exists():
                raise DatasetError("Labels file already exists; refusing to overwrite human annotations")
            payload = prepare_manifest(folder, args.layout)
            output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"Created template for {len(payload['tickets'])} crop(s).")
            print("Fill every blank expected field by examining the scan; use null ONLY if truly unreadable.")
            print("Remove entries for empty quadrants. Then run 'check' before paid 'run'.")
            return 0

        cases = load_manifest(args.manifest)
        if args.action == "check":
            results = evaluate(cases, check_only=True)
            print(f"CHECK OK: {results['labeled_tickets_checked']} labeled ticket crops validated; zero API calls.")
            return 0

        # Check the output path and *all* crops before making paid calls.
        output = (
            args.output or args.manifest.resolve().parent /
            f"accuracy-report-{args.provider}.json"
        ).resolve()
        if output.parent != args.manifest.resolve().parent:
            raise DatasetError("Write reports beside the manifest, not into the source-code folder")
        if output.exists():
            raise DatasetError("Accuracy report already exists; choose a new output filename")
        evaluate(cases, check_only=True)
        extractor = make_benchmark_extractor(args.provider, model=args.model)
        report = evaluate(cases, extractor)
        report["provider"] = args.provider
        report["model"] = args.model or DEFAULT_MODELS[args.provider]
        # A digest permits comparison of reports only when every original
        # human label is unchanged, without copying any sensitive label values.
        report["dataset_sha256"] = hashlib.sha256(
            args.manifest.resolve().read_bytes()
        ).hexdigest()
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Benchmark: {report['provider']} / {report['model']}, "
              f"{report['tickets']} independently labeled tickets.")
        print(f"All six fields correct: {report['tickets_all_six_correct']} / {report['tickets']} ({report['ticket_exact_accuracy']:.1%})")
        for field, stats in report["fields"].items():
            print(f"  {field}: {stats['correct']}/{stats['total']} ({stats['exact_accuracy']:.1%}), "
                  f"missing={stats['missing']}, unexpected={stats['unexpected']}, mismatch={stats['mismatch']}")
        print(f"Extraction failures: {report['extraction_errors']}; "
              f"mean model time: {report['mean_model_seconds']} seconds per ticket.")
        print(f"Privacy-conscious report saved to {output}")
        print("This measures the labeled sample only, not accuracy on all handwritten tickets.")
        return 0 if report["extraction_errors"] == 0 else 2
    except (DatasetError, ExtractionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
