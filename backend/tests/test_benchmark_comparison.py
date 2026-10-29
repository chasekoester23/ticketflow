"""Offline report-comparison tests; no real images or providers involved."""
import json

import pytest

from scripts.compare_extraction_reports import ComparisonError, compare_reports


def _report(tmp_path, name, *, digest="same", wrong=0):
    path = tmp_path / f"{name}.json"
    payload = {
        "mode": "evaluate",
        "provider": name,
        "model": "fake-offline-model",
        "dataset_sha256": digest,
        "tickets": 1,
        "tickets_all_six_correct": 1 - wrong,
        "extraction_errors": 0,
        "mean_model_seconds": 0.5,
        "fields": {
            "tonnage": {"mismatch": wrong, "unexpected": 0, "missing": 0},
            "ticket_number": {"mismatch": 0, "unexpected": 0, "missing": 0},
        },
        "cases": [{"id": "synthetic-case-1"}],
    }
    path.write_text(json.dumps(payload))
    return path


def test_compare_reports_counts_wrong_tonnage_separately(tmp_path):
    first = _report(tmp_path, "gemini", wrong=0)
    second = _report(tmp_path, "anthropic", wrong=1)
    rows = compare_reports([first, second])
    assert [r["provider"] for r in rows] == ["gemini", "anthropic"]
    assert rows[0]["all_six_correct"] == 1
    assert rows[1]["tonnage_wrong"] == 1
    assert rows[1]["all_six_correct"] == 0


def test_compare_refuses_different_human_reference_manifest(tmp_path):
    first = _report(tmp_path, "gemini", digest="first")
    second = _report(tmp_path, "anthropic", digest="different")
    with pytest.raises(ComparisonError, match="mismatched original labels"):
        compare_reports([first, second])


def test_compare_requires_multiple_reports(tmp_path):
    with pytest.raises(ComparisonError, match="at least two"):
        compare_reports([_report(tmp_path, "gemini")])
