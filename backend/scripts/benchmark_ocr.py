"""How fast and how reliably TicketFlow reads tickets. Three modes:

  encode    Size and encoding time of the image Gemini receives, per format. No API calls.
  simulate  The real worker pipeline and the real Gemini retry code, against a stand-in
            Gemini with a delay and error rate you choose. No API calls. Fictional tickets.
  live      Real Gemini calls on your own labelled tickets: speed, field accuracy, exact
            tonnage, tokens and estimated cost, per model. Costs money: refuses to run
            without --i-authorize-paid-calls.

From backend/:
  python scripts/benchmark_ocr.py encode
  python scripts/benchmark_ocr.py simulate --tickets 1 4 20 --latency 4 --concurrency 1 4
  python scripts/benchmark_ocr.py live --manifest evaluation_data/labels.json \\
      --models gemini-3.1-pro-preview,gemini-3-flash-preview --concurrency 4 \\
      --price gemini-3.1-pro-preview=2.00,12.00 --i-authorize-paid-calls

Simulated numbers measure TicketFlow's own pipeline around a reader of the delay you set.
They say nothing about Gemini's real speed or accuracy; only 'live' does.
"""
from __future__ import annotations

import argparse
import inspect
import io
import json
import os
import random
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def _fictional_pdf(count: int) -> bytes:
    sys.path.insert(0, str(ROOT))
    from tests.sample_data import TICKETS, make_scan_page
    tickets = [{**TICKETS[i % len(TICKETS)], "ticket_number": str(80001 + i)} for i in range(count)]
    pages = [make_scan_page(tickets[i:i + 4]) for i in range(0, count, 4)]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", resolution=200, save_all=True, append_images=pages[1:])
    return buffer.getvalue()


# ------------------------------------------------------------------ encode

def _scan_like(image):
    """Paper tint, grain and a little softness: what makes a real scan bigger than a clean drawing."""
    from PIL import Image, ImageChops, ImageFilter
    grain = Image.effect_noise(image.size, 40).point(lambda v: 214 + v // 6).convert("RGB")
    paper = Image.new("RGB", image.size, (246, 241, 226))
    return ImageChops.multiply(ImageChops.multiply(image.convert("RGB"), paper), grain).filter(ImageFilter.GaussianBlur(0.6))


def encode(args) -> int:
    sys.path.insert(0, str(ROOT))
    from PIL import Image
    from app.services import splitter
    from app.services.benchmark_providers import encode_image
    if args.images:
        crops = [Image.open(path).convert("RGB") for path in args.images]
        source = f"{len(crops)} image(s) you gave"
    else:
        from tests.sample_data import TICKETS, make_scan_page
        crops = [c.image for c in splitter.split_page(make_scan_page(TICKETS), "grid_2x2")]
        crops.append(make_scan_page(TICKETS).convert("RGB"))  # a whole page, as a single-ticket photo would be
        if args.scan_like:
            crops = [_scan_like(crop) for crop in crops]
        source = "fictional sample tickets (4 crops and 1 full page)" + (", with paper texture and scanner noise added" if args.scan_like else ", clean")
    print(f"Images: {source}. Sizes are what one ticket uploads to Gemini.\n")
    print(f"{'format':<15}{'median KB':>11}{'largest KB':>12}{'encode ms':>11}  pixels")
    for fmt in ("png", "webp_lossless", "jpeg"):
        sizes, times = [], []
        for crop in crops:
            started = time.perf_counter()
            data, _ = encode_image(crop, fmt=fmt, max_side=args.max_side, quality=args.quality)
            times.append((time.perf_counter() - started) * 1000)
            sizes.append(len(data) / 1024)
        same = "identical" if fmt != "jpeg" else f"lossy, quality {args.quality}"
        print(f"{fmt:<15}{statistics.median(sizes):>11.0f}{max(sizes):>12.0f}{statistics.median(times):>11.0f}  {same}")
    return 0


# ------------------------------------------------------------------ simulate

def simulate(args) -> int:
    work = Path(tempfile.mkdtemp(prefix="ticketflow-ocr-bench-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{work / 'bench.db'}"
    os.environ["STORAGE_DIR"] = str(work / "storage")
    os.environ.setdefault("GEMINI_API_KEY", "not-used-simulated")
    sys.path.insert(0, str(ROOT))
    from alembic import command
    from alembic.config import Config
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    command.upgrade(config, "head")

    from google.genai import errors as genai_errors
    from app.config import settings
    from app.db import SessionLocal
    from app.models import Batch, Ticket, UploadedFile
    from app.services import pipeline, storage
    from app.services.benchmark_providers import GeminiBenchmarkExtractor
    from tests.sample_data import TICKETS

    rng = random.Random(args.seed)
    answer = json.dumps(TICKETS[0])

    class StandInGemini:
        """Answers like Gemini after a delay; fails with a 503 at the rate asked for."""
        def __init__(self):
            self.calls = 0
            self.models = self

        def generate_content(self, **_request):
            self.calls += 1
            time.sleep(max(0.05, rng.lognormvariate(0, 0.25) * args.latency))
            if rng.random() < args.error_rate:
                raise genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}, None)
            return SimpleNamespace(text=answer, usage_metadata=None)

    supports_retries = "sleep" in inspect.signature(GeminiBenchmarkExtractor.__init__).parameters
    print(f"SIMULATED reader: about {args.latency:.1f} s per ticket, {args.error_rate:.0%} temporary 503s. "
          f"{'This code retries.' if supports_retries else 'This code does not retry.'}\n")
    print(f"{'tickets':>8}{'concurrency':>13}{'seconds':>9}{'per ticket':>12}{'read':>6}{'failed':>8}{'calls':>7}")
    store = storage.current()
    results = []
    for count in args.tickets:
        pdf = _fictional_pdf(count)
        for workers in args.concurrency:
            if hasattr(settings, "extraction_concurrency"):
                settings.extraction_concurrency = workers
            elif workers != 1:
                continue  # code from before parallel reading: one at a time only
            with SessionLocal() as db:
                batch = Batch(layout="grid_2x2", intake_mode="extract", status="uploaded", organization_id=1)
                db.add(batch)
                db.flush()
                key = f"uploads/{batch.id}/scan.pdf"
                db.add(UploadedFile(batch_id=batch.id, original_name="scan.pdf",
                                    storage_path=store.save(key, pdf), sha256="simulated"))
                db.commit()
                batch_id = batch.id
            gemini = StandInGemini()
            reader = GeminiBenchmarkExtractor(model="simulated", client=gemini)
            started = time.perf_counter()
            with SessionLocal() as db:
                pipeline.process_batch(db, batch_id, reader)
            seconds = time.perf_counter() - started
            with SessionLocal() as db:
                statuses = [t.status for t in db.query(Ticket).filter(Ticket.batch_id == batch_id)]
            read, failed = sum(s != "failed" for s in statuses), statuses.count("failed")
            results.append({"tickets": count, "concurrency": workers, "seconds": round(seconds, 2),
                            "read": read, "failed": failed, "calls": gemini.calls})
            print(f"{count:>8}{workers:>13}{seconds:>9.1f}{seconds / count:>12.2f}{read:>6}{failed:>8}{gemini.calls:>7}")
    if args.json:
        Path(args.json).write_text(json.dumps({"simulated": True, "latency": args.latency, "error_rate": args.error_rate,
                                               "retries": supports_retries, "results": results}, indent=2))
    return 0


# ------------------------------------------------------------------ live

def live(args) -> int:
    if not args.i_authorize_paid_calls:
        print("Refusing: 'live' sends your ticket images to Gemini and costs money. "
              "Add --i-authorize-paid-calls when you mean it.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ROOT))
    from app.services.benchmark_providers import GeminiBenchmarkExtractor
    from app.services.evaluation import FIELDS, _field_verdict, load_crop, load_manifest
    cases = load_manifest(Path(args.manifest))
    prices = {}
    for item in args.price or []:
        model, _, pair = item.partition("=")
        inp, _, out = pair.partition(",")
        prices[model] = (float(inp), float(out))
    images = [load_crop(row) for row in cases]
    print(f"LIVE: {len(cases)} labelled tickets, concurrency {args.concurrency}, image format {args.image_format}.\n")
    report = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        reader = GeminiBenchmarkExtractor(model=model, image_format=args.image_format)

        def one(index):
            started = time.perf_counter()
            try:
                ticket, raw = reader.extract(images[index])
                return index, time.perf_counter() - started, ticket.model_dump(), raw.get("_ticketflow", {}), None
            except Exception as exc:  # counted, never shown as OCR; no provider text in the report
                return index, time.perf_counter() - started, None, {}, getattr(exc, "kind", type(exc).__name__)

        wall = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            outcomes = list(pool.map(one, range(len(cases))))
        wall = time.perf_counter() - wall
        correct = total = exact_tons = tons_cases = failed = 0
        tokens_in = tokens_out = 0
        latencies = []
        for index, seconds, prediction, meta, error in outcomes:
            latencies.append(seconds)
            if error:
                failed += 1
            tokens_in += meta.get("prompt_tokens") or 0
            tokens_out += (meta.get("output_tokens") or 0) + (meta.get("thinking_tokens") or 0)
            for field in FIELDS:
                expected = cases[index]["expected"][field]
                verdict = _field_verdict(field, expected, (prediction or {}).get(field))
                total += 1
                correct += verdict in ("correct", "correct_null")
                if field == "tonnage" and expected is not None:
                    tons_cases += 1
                    exact_tons += verdict == "correct"
        cost = None
        if model in prices:
            cost = (tokens_in * prices[model][0] + tokens_out * prices[model][1]) / 1_000_000
        row = {"model": model, "tickets": len(cases), "wall_seconds": round(wall, 1),
               "median_seconds": round(statistics.median(latencies), 2),
               "p95_seconds": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 2),
               "field_accuracy": round(correct / total, 4) if total else None,
               "exact_tonnage": f"{exact_tons}/{tons_cases}", "failed": failed,
               "input_tokens": tokens_in, "output_and_thinking_tokens": tokens_out,
               "estimated_cost_usd": round(cost, 4) if cost is not None else "add --price"}
        report.append(row)
        print(json.dumps(row))
    if args.json:
        Path(args.json).write_text(json.dumps({"live": True, "results": report}, indent=2))
    print("\nOnly change GEMINI_MODEL if a faster model matches the current one on field accuracy AND exact tonnage.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TicketFlow OCR speed and reliability benchmark")
    commands = parser.add_subparsers(dest="mode", required=True)
    e = commands.add_parser("encode")
    e.add_argument("--images", nargs="*", type=Path)
    e.add_argument("--max-side", type=int, default=1800)
    e.add_argument("--quality", type=int, default=92)
    e.add_argument("--scan-like", action="store_true", help="add paper texture and scanner noise to the sample tickets")
    s = commands.add_parser("simulate")
    s.add_argument("--tickets", nargs="+", type=int, default=[1, 4, 20])
    s.add_argument("--concurrency", nargs="+", type=int, default=[1, 4])
    s.add_argument("--latency", type=float, default=4.0, help="seconds per simulated Gemini call")
    s.add_argument("--error-rate", type=float, default=0.0, help="share of calls that fail with a temporary 503")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--json")
    live_args = commands.add_parser("live")
    live_args.add_argument("--manifest", required=True)
    live_args.add_argument("--models", required=True, help="comma-separated Gemini model names")
    live_args.add_argument("--concurrency", type=int, default=4)
    live_args.add_argument("--image-format", default="png", choices=["png", "webp_lossless", "jpeg"])
    live_args.add_argument("--price", action="append", help="MODEL=INPUT,OUTPUT US dollars per million tokens, from Google's price page")
    live_args.add_argument("--json")
    live_args.add_argument("--i-authorize-paid-calls", action="store_true")
    args = parser.parse_args()
    return {"encode": encode, "simulate": simulate, "live": live}[args.mode](args)


if __name__ == "__main__":
    raise SystemExit(main())
