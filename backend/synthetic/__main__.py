"""Synthetic handwritten delivery tickets: one command line.

Run from backend/:
  python -m synthetic generate --set benchmark-mini --out eval_data/synthetic/generated/benchmark-mini
  python -m synthetic generate --difficulty hard --count 40 --seed 7 --out /tmp/hard40
  python -m synthetic assemble DATASET --formats pdf,png        # (re)make pages from the stored records
  python -m synthetic export-labels DATASET --out truth.json     # evaluate_workflow ground truth
  python -m synthetic preview DATASET --out sheet.png --tickets tickets.png
  python -m synthetic validate DATASET [--determinism]
  python -m synthetic evaluate DATASET --extractor oracle|oracle-crop-aware|noisy-simulated [--out r.json]

Everything is fictional and offline. `evaluate --extractor gemini` makes paid calls
and refuses to run without --i-authorize-paid-calls.

SYNTHETIC — engineering benchmark, not real-world accuracy.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import STAMP

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "eval_data" / "synthetic" / "generated"


def _formats(text: str) -> set[str]:
    chosen = {f.strip() for f in text.split(",") if f.strip()}
    if not chosen or chosen - {"pdf", "png"}:
        raise argparse.ArgumentTypeError("formats are pdf and/or png, e.g. pdf,png")
    return chosen


def _error_model(text: str) -> dict:
    out = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        key, _, value = part.partition("=")
        try:
            out[key.strip()] = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"--error-model wants key=number pairs, got {part!r}") from None
    return out


def cmd_generate(args) -> int:
    from .dataset import build, spec_for
    spec = spec_for(args.set, args.difficulty, args.seed, args.count)
    out = args.out or DEFAULT_ROOT / spec["name"]
    manifest = build(out, spec, formats=args.formats, overwrite=args.overwrite)
    print(json.dumps({"stamp": STAMP, "out": str(out), "seed": manifest["seed"], **manifest["counts"]}, indent=2))
    return 0


def cmd_assemble(args) -> int:
    from .dataset import (PAGES_SCHEMA, PAGE_SIZE, SCAN_DPI, export_truth, load, render_all, assemble,
                          write_manifest)
    from .util import write_json
    docs = load(args.dataset)
    m = docs["manifest"]
    spec = {"name": m["name"], "seed": m["seed"], "difficulties": tuple(m["difficulties"]),
            "per_difficulty": m["per_difficulty"]}
    difficulty_of = {t["ticket_id"]: t["difficulty"] for t in docs["labels"]["tickets"]}
    tickets, renders = render_all(docs["records"]["records"], difficulty_of, m["seed"])
    if [{k: v for k, v in t.items() if k != "placement"} for t in docs["labels"]["tickets"]] != tickets:
        print("The stored labels don't match a fresh render; run validate first.", file=sys.stderr)
        return 1
    root = docs["root"]
    for folder in ("scans", "pages"):
        for f in (root / folder).glob("*") if (root / folder).is_dir() else []:
            f.unlink()
    pages = assemble(root, spec, tickets, renders, args.formats)
    common = {"synthetic": True, "stamp": STAMP, "dataset": m["name"], "seed": m["seed"]}
    pages_doc = {"schema": PAGES_SCHEMA, **common, "scan_dpi": SCAN_DPI, "page_size": list(PAGE_SIZE), "pages": pages}
    labels = dict(docs["labels"], tickets=tickets)
    write_json(root / "labels.json", labels)
    write_json(root / "pages.json", pages_doc, compact=True)
    write_json(root / "truth.json", export_truth(labels, pages_doc, docs["records"]))
    write_manifest(root, spec, args.formats, m["counts"])
    print(json.dumps({"stamp": STAMP, "pages": len(pages), "formats": sorted(args.formats)}))
    return 0


def cmd_export_labels(args) -> int:
    from .dataset import export_truth, load
    from .util import dumps
    docs = load(args.dataset)
    text = dumps(export_truth(docs["labels"], docs["pages"], docs["records"]))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out} ({STAMP})")
    else:
        sys.stdout.write(text)
    return 0


def cmd_preview(args) -> int:
    from .preview import contact_sheet, ticket_sheet
    out = contact_sheet(args.dataset, args.out, max_pages=args.pages, per_difficulty=args.per_difficulty)
    print(f"Wrote {out}")
    if args.tickets:
        print(f"Wrote {ticket_sheet(args.dataset, args.tickets, count=args.ticket_count)}")
    return 0


def cmd_validate(args) -> int:
    from .verify import validate
    result = validate(args.dataset, rerender=not args.quick, determinism=args.determinism)
    print(json.dumps({k: v for k, v in result.items() if k != "problems"}, indent=2))
    for problem in result["problems"][:200]:
        print("PROBLEM:", problem, file=sys.stderr)
    if not result["ok"]:
        print(f"{len(result['problems'])} problem(s) found.", file=sys.stderr)
    return 0 if result["ok"] else 1


def cmd_evaluate(args) -> int:
    if args.extractor == "gemini" and not args.i_authorize_paid_calls:
        print("Refused: --extractor gemini makes paid API calls. Add --i-authorize-paid-calls to allow it. "
              "Nothing was run.", file=sys.stderr)
        return 2
    keep = args.workdir is not None
    workdir = Path(args.workdir) if keep else Path(tempfile.mkdtemp(prefix="tf-synthetic-eval-"))
    if keep and workdir.exists() and any(workdir.iterdir()):
        print(f"{workdir} isn't empty; the evaluation needs a fresh folder.", file=sys.stderr)
        return 2
    from .evaluate import prepare_environment
    prepare_environment(workdir)  # before anything imports the app
    from .dataset import DatasetError
    from .evaluate import evaluate, report_markdown
    try:
        report = evaluate(args.dataset, extractor_name=args.extractor, workdir=workdir / "sandbox",
                          catch_rate=args.catch_rate, seed=args.seed, error_model=args.error_model,
                          authorize_paid=args.i_authorize_paid_calls)
    except DatasetError as exc:
        print(f"Not evaluated: {exc}", file=sys.stderr)
        return 2
    finally:
        if not keep:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    md = report_markdown(report)
    if args.markdown:
        Path(args.markdown).write_text(md, encoding="utf-8")
    print(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synthetic", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="records, drawn tickets, scanned pages, PDFs and labels")
    p.add_argument("--set", choices=["benchmark-mini", "benchmark", "stress"])
    p.add_argument("--difficulty", choices=["easy", "moderate", "hard", "extreme", "stress"])
    p.add_argument("--count", type=int, help="tickets per difficulty")
    p.add_argument("--seed", type=int)
    p.add_argument("--out", type=Path)
    p.add_argument("--formats", type=_formats, default={"pdf", "png"})
    p.add_argument("--overwrite", action="store_true", help="replace an existing synthetic dataset")
    p.set_defaults(fn=cmd_generate)

    p = sub.add_parser("assemble", help="(re)compose pages and PDFs from a dataset's stored records")
    p.add_argument("dataset", type=Path)
    p.add_argument("--formats", type=_formats, default={"pdf", "png"})
    p.set_defaults(fn=cmd_assemble)

    p = sub.add_parser("export-labels", help="ground truth in scripts/evaluate_workflow.py's format")
    p.add_argument("dataset", type=Path)
    p.add_argument("--out", type=Path)
    p.set_defaults(fn=cmd_export_labels)

    p = sub.add_parser("preview", help="contact sheet of pages (and tickets) with boxes and labels")
    p.add_argument("dataset", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--pages", type=int, default=16)
    p.add_argument("--per-difficulty", type=int)
    p.add_argument("--tickets", type=Path, help="also write a sheet of drawn tickets with field labels")
    p.add_argument("--ticket-count", type=int, default=8)
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("validate", help="schema, truth integrity, geometry, hashes; non-zero exit on any problem")
    p.add_argument("dataset", type=Path)
    p.add_argument("--quick", action="store_true", help="skip re-rendering every ticket")
    p.add_argument("--determinism", action="store_true", help="rebuild from the seed and compare every file")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("evaluate", help="run through TicketFlow's real intake on a temporary database and score")
    p.add_argument("dataset", type=Path)
    p.add_argument("--extractor", default="oracle",
                   choices=["oracle", "oracle-crop-aware", "noisy-simulated", "gemini"])
    p.add_argument("--i-authorize-paid-calls", action="store_true", help="required for --extractor gemini")
    p.add_argument("--catch-rate", type=float, default=0.9, help="chance the simulated reviewer fixes a wrong field")
    p.add_argument("--seed", type=int, default=0, help="seed for the error model and the reviewer")
    p.add_argument("--error-model", type=_error_model, default=None,
                   help="noisy-simulated settings, e.g. drop=0.05,ambiguous_misread=0.7")
    p.add_argument("--workdir", type=Path, help="keep the temporary database and storage here (must be empty)")
    p.add_argument("--out", type=Path, help="full JSON report")
    p.add_argument("--markdown", type=Path, help="summary table")
    p.set_defaults(fn=cmd_evaluate)

    args = parser.parse_args(argv)
    if args.command == "generate" and not args.set and not args.difficulty:
        parser.error("generate needs --set or --difficulty")
    from .dataset import DatasetError
    try:
        return args.fn(args)
    except DatasetError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
