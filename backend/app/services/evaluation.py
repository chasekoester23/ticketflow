"""Private, ground-truth evaluation of TicketFlow's six-field extractor.

No real tickets, annotations or predictions are bundled with this module.
A benchmark is meaningful only when a human labels each ticket from the image
independently of the AI. Output contains field-level verdicts, not document text.
"""
from __future__ import annotations

import json
import re
import statistics
import time
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image

from ..config import settings
from . import splitter
from .extraction import ExtractionError, Extractor
from .validation import ExtractedTicket, parse_date, parse_tonnage

FIELDS = (
    "ticket_number", "delivery_date", "material_type",
    "tonnage", "supplier", "job_location",
)
LAYOUTS = {"single": 1, "grid_2x2": 4}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
PDF_SUFFIXES = {".pdf"}
SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES


class DatasetError(ValueError):
    """An incomplete or unsafe evaluation dataset, never a failed AI call."""


def _valid_source(root: Path, name: Any) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise DatasetError("Every source must be a nonempty relative file path")
    source = (root / name).resolve()
    if not source.is_relative_to(root.resolve()):
        raise DatasetError("Source path escapes the dataset folder")
    if source.suffix.lower() not in SUFFIXES:
        raise DatasetError(f"Unsupported source format: {source.suffix}")
    if not source.is_file():
        raise DatasetError(f"Missing source file: {name}")
    return source


def _normalize(field: str, value: Any) -> str | None:
    if value is None:
        return None
    if field == "delivery_date":
        parsed = parse_date(str(value))
        return parsed.isoformat() if parsed else None
    if field == "tonnage":
        parsed = parse_tonnage(value)
        return str(parsed) if parsed is not None else None
    if not isinstance(value, str):
        value = str(value)
    text = re.sub(r"\s+", " ", value).strip()
    return text.casefold() if text else None


def _reference_is_valid(field: str, value: Any) -> bool:
    if value is None:
        return True  # human explicitly marked this field missing or unreadable
    if not isinstance(value, str) or not value.strip():
        return False  # "" denotes an unfinished annotation template
    if field == "delivery_date":
        parsed = parse_date(value)
        return parsed is not None and parsed.isoformat() == value
    if field == "tonnage":
        try:
            d = Decimal(value)
            return d.is_finite() and d > 0 and d.as_tuple().exponent >= -3
        except (ValueError, ArithmeticError):
            return False
    return True


def load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    """Validate all labels and files *before* any paid API call occurs."""
    manifest_path = manifest_path.resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"Cannot read evaluation manifest: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise DatasetError("Manifest must be an object with version: 1")
    raw_cases = payload.get("tickets")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise DatasetError("Manifest must contain at least one labeled ticket")
    root = manifest_path.parent
    seen_ids: set[str] = set()
    seen_positions: set[tuple[str, int, int]] = set()
    cases: list[dict[str, Any]] = []
    for index, row in enumerate(raw_cases, start=1):
        if not isinstance(row, dict):
            raise DatasetError(f"Ticket #{index} must be an object")
        case_id = row.get("id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen_ids:
            raise DatasetError(f"Ticket #{index} needs a unique nonempty string id")
        seen_ids.add(case_id)
        source = _valid_source(root, row.get("source"))
        layout = row.get("layout")
        if layout not in LAYOUTS:
            raise DatasetError(f"{case_id}: layout must be single or grid_2x2")
        page = row.get("page")
        position = row.get("position")
        if type(page) is not int or page < 1:
            raise DatasetError(f"{case_id}: page must be a positive integer (1-based)")
        if type(position) is not int or not 0 <= position < LAYOUTS[layout]:
            raise DatasetError(f"{case_id}: invalid zero-based crop position for {layout}")
        if source.suffix.lower() in IMAGE_SUFFIXES and page != 1:
            raise DatasetError(f"{case_id}: single image sources only have page 1")
        unique_position = (str(source), page, position)
        if unique_position in seen_positions:
            raise DatasetError(f"{case_id}: same source/page/crop was labeled twice")
        seen_positions.add(unique_position)
        expected = row.get("expected")
        if not isinstance(expected, dict) or set(expected) != set(FIELDS):
            raise DatasetError(f"{case_id}: expected must have exactly six labeled fields")
        for field in FIELDS:
            if not _reference_is_valid(field, expected[field]):
                raise DatasetError(
                    f"{case_id}: {field} needs a human label "
                    "(string, YYYY-MM-DD for dates, decimal string for tons, or null for unreadable)"
                )
        cases.append({
            "id": case_id, "source": source, "page": page,
            "position": position, "layout": layout, "expected": expected,
        })
    return cases


def load_crop(row: dict[str, Any]) -> Image.Image:
    """Read only the requested PDF page, not the entire scanned binder."""
    source: Path = row["source"]
    if source.suffix.lower() == ".pdf":
        try:
            with pymupdf.open(str(source)) as document:
                if document.needs_pass:
                    raise DatasetError("Password-protected PDF in evaluation dataset")
                if row["page"] > len(document):
                    raise DatasetError("Referenced PDF page does not exist")
                pix = document.load_page(row["page"] - 1).get_pixmap(
                    dpi=settings.render_dpi, alpha=False,
                )
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except DatasetError:
            raise
        except Exception as exc:
            raise DatasetError(f"Could not render dataset PDF: {type(exc).__name__}") from exc
    else:
        try:
            image = splitter.load_image(source.read_bytes())
        except (splitter.PdfError, OSError) as exc:
            raise DatasetError(f"Could not read evaluation image: {type(exc).__name__}") from exc
    crops = splitter.split_page(image, row["layout"])
    result = crops[row["position"]].image
    if splitter.is_blank(result):
        raise DatasetError(
            f"{row['id']}: crop looks blank; check page, layout and position "
            "against the original before scoring"
        )
    return result


def prepare_manifest(folder: Path, layout: str) -> dict[str, Any]:
    """Generate a labeling template; empty labels MUST be completed manually."""
    folder = folder.resolve()
    if layout not in LAYOUTS:
        raise DatasetError("layout must be single or grid_2x2")
    if not folder.is_dir():
        raise DatasetError("Scans folder does not exist")
    sources = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUFFIXES)
    if not sources:
        raise DatasetError("No PDF/JPG/PNG scans found in the provided folder")
    rows = []
    for source in sources:
        if source.suffix.lower() == ".pdf":
            try:
                with pymupdf.open(str(source)) as doc:
                    if doc.needs_pass or len(doc) == 0:
                        raise DatasetError("Empty or password-protected evaluation PDF")
                    pages = len(doc)
            except DatasetError:
                raise
            except Exception as exc:
                raise DatasetError(f"Unreadable evaluation PDF: {type(exc).__name__}") from exc
        else:
            pages = 1
        for page in range(1, pages + 1):
            for position in range(LAYOUTS[layout]):
                rows.append({
                    "id": f"ticket-{len(rows)+1:04d}",
                    "source": source.name,
                    "layout": layout,
                    "page": page,
                    "position": position,
                    "expected": {field: "" for field in FIELDS},
                })
    return {"version": 1, "tickets": rows}


def _field_verdict(field: str, expected: Any, actual: Any) -> str:
    truth = _normalize(field, expected)
    prediction = _normalize(field, actual)
    # A nonempty but malformed predicted date/tonnage is NOT an acceptable
    # empty value when the human reference is null.
    invalid_prediction = (actual is not None and str(actual).strip() and prediction is None)
    if truth == prediction and not invalid_prediction:
        return "correct"
    if truth is None:
        return "unexpected"  # non-null or malformed model invention
    if prediction is None and not invalid_prediction:
        return "missing"
    return "mismatch"


def evaluate(
    cases: list[dict[str, Any]], extractor: Extractor | None = None, *,
    check_only: bool = False,
) -> dict[str, Any]:
    """Benchmark every labeled crop; no real OCR runs in check-only mode.

    For extraction errors, every field is scored as missing/unexpected as
    appropriate. The error is counted separately; no fake OCR values are used.
    """
    outcomes: list[dict[str, Any]] = []
    durations: list[float] = []
    summaries = {field: Counter() for field in FIELDS}
    labeled_non_null = Counter()
    correct_non_null = Counter()
    errors = 0
    for row in cases:
        image = load_crop(row)  # validate the actual scan/crop
        if check_only:
            continue
        if extractor is None:
            raise DatasetError("A real extractor must be provided to evaluate")
        started = time.perf_counter()
        try:
            result, _raw = extractor.extract(image)
            if not isinstance(result, ExtractedTicket):
                raise ExtractionError("Extractor returned an invalid ticket object")
            prediction = result.model_dump()
            failure = None
        except Exception as exc:
            # Unexpected provider/runtime failures are counted, never presented
            # as valid OCR. The report intentionally omits exception messages
            # because provider errors may contain document details.
            prediction = {field: None for field in FIELDS}
            failure = type(exc).__name__
            errors += 1
        elapsed = time.perf_counter() - started
        durations.append(elapsed)
        verdicts = {}
        for field in FIELDS:
            verdict = _field_verdict(field, row["expected"][field], prediction[field])
            summaries[field][verdict] += 1
            if row["expected"][field] is not None:
                labeled_non_null[field] += 1
                if verdict == "correct":
                    correct_non_null[field] += 1
            verdicts[field] = verdict
        outcomes.append({
            "id": row["id"],
            "fields": verdicts,
            "all_fields_correct": all(v == "correct" for v in verdicts.values()) and failure is None,
            "extraction_error": failure,
            "seconds": round(elapsed, 3),
        })
    if check_only:
        return {"mode": "check_only", "labeled_tickets_checked": len(cases), "api_calls": 0}

    total = len(outcomes)
    field_stats = {}
    for field, counts in summaries.items():
        correct = counts["correct"]
        field_stats[field] = {
            "total": total, "correct": correct,
            "mismatch": counts["mismatch"], "missing": counts["missing"],
            "unexpected": counts["unexpected"],
            "exact_accuracy": round(correct / total, 4),
            "labeled_non_null": labeled_non_null[field],
            "correct_non_null": correct_non_null[field],
            "non_null_accuracy": (
                round(correct_non_null[field] / labeled_non_null[field], 4)
                if labeled_non_null[field] else None
            ),
        }
    correct_all = sum(o["all_fields_correct"] for o in outcomes)
    return {
        "mode": "evaluate",
        "tickets": total, "extraction_errors": errors,
        "tickets_all_six_correct": correct_all,
        "ticket_exact_accuracy": round(correct_all / total, 4),
        "fields": field_stats,
        "mean_model_seconds": round(statistics.mean(durations), 3),
        "median_model_seconds": round(statistics.median(durations), 3),
        "cases": outcomes,
        "note": (
            "Metrics use independent human labels. Exact-field accuracy includes "
            "correctly absent values; non-null accuracy counts only readable truth "
            "fields. No ground-truth labels or extracted document "
            "text are saved in this report. Timing excludes manual review."
        ),
    }
