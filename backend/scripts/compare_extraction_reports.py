"""Compare PRIVATE handwriting benchmark reports from identical human labels.

Usage:
    python scripts/compare_extraction_reports.py \
      evaluation_data/accuracy-report-anthropic.json \
      evaluation_data/accuracy-report-openai.json \
      evaluation_data/accuracy-report-gemini.json

Never print individual ticket values or include real scans in the repository.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


class ComparisonError(ValueError):
    pass


def compare_reports(paths: list[Path]) -> list[dict]:
    if len(paths) < 2:
        raise ComparisonError("Supply at least two benchmark reports")
    reports = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ComparisonError(f"Cannot read benchmark report {path.name}") from exc
        if (
            data.get("mode") != "evaluate"
            or not data.get("dataset_sha256")
            or not isinstance(data.get("cases"), list)
            or not isinstance(data.get("fields"), dict)
            or not data.get("provider")
            or not data.get("model")
        ):
            raise ComparisonError(f"{path.name}: missing provider, labels digest, or results")
        reports.append((path, data))

    first = reports[0][1]
    same_labels = first["dataset_sha256"]
    case_ids = [c["id"] for c in first["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise ComparisonError("Repeated case IDs in comparison report")
    rows = []
    for path, data in reports:
        if (data["dataset_sha256"] != same_labels
                or [c["id"] for c in data["cases"]] != case_ids):
            raise ComparisonError(
                f"{path.name}: mismatched original labels/cases; refusing unfair comparison"
            )
        tonnage = data["fields"].get("tonnage")
        number = data["fields"].get("ticket_number")
        if not isinstance(tonnage, dict) or not isinstance(number, dict):
            raise ComparisonError(f"{path.name}: missing critical field metrics")
        rows.append({
            "provider": data["provider"],
            "model": data["model"],
            "tickets": data["tickets"],
            "all_six_correct": data["tickets_all_six_correct"],
            "tonnage_wrong": tonnage["mismatch"] + tonnage["unexpected"],
            "tonnage_missing": tonnage["missing"],
            "ticket_number_wrong": number["mismatch"] + number["unexpected"],
            "extraction_errors": data["extraction_errors"],
            "seconds_per_ticket": data["mean_model_seconds"],
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare the SAME human-labeled tickets across OCR providers"
    )
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        rows = compare_reports(args.reports)
    except ComparisonError as exc:
        parser.error(str(exc))
    print("Provider/model | all 6 exact | WRONG tons | blank tons | wrong ticket # | API errors | avg seconds")
    for r in rows:
        print(
            f"{r['provider']}/{r['model']} | "
            f"{r['all_six_correct']}/{r['tickets']} | "
            f"{r['tonnage_wrong']} | {r['tonnage_missing']} | "
            f"{r['ticket_number_wrong']} | {r['extraction_errors']} | "
            f"{r['seconds_per_ticket']}"
        )
    print(
        "Wrong tons are mismatches or invented values; blank tons require human "
        "review. Timing excludes manual review and cost is not measured here. "
        "Do not claim real-world accuracy from synthetic or tiny samples."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
