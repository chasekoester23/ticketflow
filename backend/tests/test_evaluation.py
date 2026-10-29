"""Private evaluation tools use synthetic PDFs and fake extractors in CI.

These tests do NOT establish accuracy on real handwritten delivery tickets.
"""
import json
from pathlib import Path

import pytest

from app.services.evaluation import (
    DatasetError, _field_verdict, evaluate, load_manifest, prepare_manifest,
)
from app.services.extraction import ExtractionError, PROMPT
from app.services.validation import ExtractedTicket
from .sample_data import TICKETS, make_scan_pdf


def _labeled_manifest(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(make_scan_pdf())
    draft = prepare_manifest(tmp_path, "grid_2x2")
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(draft))
    for idx, row in enumerate(draft["tickets"]):
        row["expected"] = dict(TICKETS[idx])
    path.write_text(json.dumps(draft))
    return path


class KnownAnswers:
    """A stand-in with deliberate errors to verify scoring, not model quality."""

    def __init__(self, *, wrong=False, fail_at=None):
        self.calls = 0
        self.wrong = wrong
        self.fail_at = fail_at

    def extract(self, image):
        idx = self.calls
        self.calls += 1
        if idx == self.fail_at:
            raise ExtractionError("synthetic failure")
        row = dict(TICKETS[idx])
        if self.wrong and idx == 1:
            row["tonnage"] = "22.00"
        return ExtractedTicket.model_validate(row), dict(row)


def test_preparing_label_template_requires_human_annotation(tmp_path):
    (tmp_path / "scan.pdf").write_bytes(make_scan_pdf())
    prepared = prepare_manifest(tmp_path, "grid_2x2")
    assert prepared["version"] == 1
    assert len(prepared["tickets"]) == 4
    assert {r["position"] for r in prepared["tickets"]} == {0, 1, 2, 3}
    assert all(all(v == "" for v in r["expected"].values()) for r in prepared["tickets"])
    p = tmp_path / "labels.json"
    p.write_text(json.dumps(prepared))
    with pytest.raises(DatasetError, match="needs a human label"):
        load_manifest(p)


def test_preflight_uses_real_crops_without_spending_api_calls(tmp_path):
    manifest = _labeled_manifest(tmp_path)
    cases = load_manifest(manifest)
    fake = KnownAnswers()
    result = evaluate(cases, fake, check_only=True)
    assert result == {"mode": "check_only", "labeled_tickets_checked": 4, "api_calls": 0}
    assert fake.calls == 0


def test_scoring_catches_wrong_tonnage_without_claiming_accuracy(tmp_path):
    cases = load_manifest(_labeled_manifest(tmp_path))
    fake = KnownAnswers(wrong=True)
    score = evaluate(cases, fake)
    assert fake.calls == 4
    assert score["tickets"] == 4
    assert score["tickets_all_six_correct"] == 3
    assert score["ticket_exact_accuracy"] == .75
    assert score["fields"]["tonnage"]["correct"] == 3
    assert score["fields"]["tonnage"]["mismatch"] == 1
    assert all(score["fields"][f]["correct"] == 4 for f in (
        "ticket_number", "delivery_date", "material_type", "supplier", "job_location",
    ))
    assert score["cases"][1]["fields"]["tonnage"] == "mismatch"
    # Reports include decisions, never raw extracted fields or human annotations.
    serialized = json.dumps(score)
    assert "Example Aggregates" not in serialized
    assert "No. 53 Crushed Stone" not in serialized
    assert "22.00" not in serialized


def test_failed_ai_request_is_counted_not_misreported_as_success(tmp_path):
    cases = load_manifest(_labeled_manifest(tmp_path))
    results = evaluate(cases, KnownAnswers(fail_at=2))
    assert results["extraction_errors"] == 1
    assert results["tickets_all_six_correct"] == 3
    assert results["cases"][2]["extraction_error"] == "ExtractionError"
    assert results["fields"]["tonnage"]["missing"] == 1


def test_null_truth_distinguishes_unexpected_values_from_missing():
    assert _field_verdict("tonnage", None, None) == "correct"
    assert _field_verdict("tonnage", None, "42") == "unexpected"
    assert _field_verdict("tonnage", "22.100", None) == "missing"
    assert _field_verdict("tonnage", "22.100", "22.1") == "correct"
    assert _field_verdict("tonnage", "22.100", "22.2") == "mismatch"
    assert _field_verdict("delivery_date", "2026-10-15", "10/15/26") == "correct"
    assert _field_verdict("supplier", "  Example    Aggregates", "example aggregates") == "correct"


@pytest.mark.parametrize("modifier, error", [
    (lambda p: p["tickets"][0]["expected"].pop("tonnage"), "six labeled"),
    (lambda p: p["tickets"][0].update(source="../secret.pdf"), "escapes the dataset folder"),
    (lambda p: p["tickets"][0].update(page=999), "page"),
    (lambda p: p["tickets"][0].update(position=4), "crop position"),
    (lambda p: p["tickets"][1].update(id=p["tickets"][0]["id"]), "unique"),
    (lambda p: p["tickets"][1].update(position=0), "labeled twice"),
    (lambda p: p["tickets"][0]["expected"].update(tonnage="twenty"), "tonnage"),
    (lambda p: p["tickets"][0]["expected"].update(delivery_date="10/15/26"), "delivery_date"),
])
def test_manifest_guards_bad_annotations(tmp_path, modifier, error):
    manifest = _labeled_manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    modifier(payload)
    manifest.write_text(json.dumps(payload))
    if error == "page":
        with pytest.raises(DatasetError, match="page"):
            evaluate(load_manifest(manifest), check_only=True)
    else:
        with pytest.raises(DatasetError, match=error):
            load_manifest(manifest)


def test_blank_crop_not_scored_as_real_ticket(tmp_path):
    from PIL import Image
    manifest = _labeled_manifest(tmp_path)
    blank = tmp_path / "blank.png"
    Image.new("RGB", (500, 400), "white").save(blank)
    data = json.loads(manifest.read_text())
    data["tickets"][0]["source"] = "blank.png"
    data["tickets"][0]["layout"] = "single"
    data["tickets"][0]["position"] = 0
    manifest.write_text(json.dumps(data))
    with pytest.raises(DatasetError, match="looks blank"):
        evaluate(load_manifest(manifest), check_only=True)


def test_extraction_prompt_does_not_guess_unit_conversions():
    assert "return null for tonnage" in PROMPT
    assert "Never convert units" in PROMPT
    assert "gross weight" in PROMPT.lower()


def test_malformed_model_value_is_not_scored_as_correct_null():
    assert _field_verdict("tonnage", None, "definitely not tons") == "unexpected"
    assert _field_verdict("delivery_date", None, "sometime Tuesday") == "unexpected"
    assert _field_verdict("tonnage", "24.500", "bad value") == "mismatch"


def test_readable_accuracy_is_separate_from_correct_missing_values(tmp_path):
    manifest = _labeled_manifest(tmp_path)
    data = json.loads(manifest.read_text())
    data["tickets"][0]["expected"]["supplier"] = None
    manifest.write_text(json.dumps(data))
    results = evaluate(load_manifest(manifest), KnownAnswers())
    assert results["fields"]["supplier"]["labeled_non_null"] == 3
    assert results["fields"]["supplier"]["correct_non_null"] == 3
    assert results["fields"]["supplier"]["non_null_accuracy"] == 1.0
    assert results["fields"]["supplier"]["unexpected"] == 1


def test_cli_prepare_check_run_and_refuse_overwriting_report(tmp_path, monkeypatch, capsys):
    from scripts import evaluate_extraction as cli

    (tmp_path / "scan.pdf").write_bytes(make_scan_pdf())
    monkeypatch.setattr("sys.argv", [
        "evaluate_extraction.py", "prepare", "--scans", str(tmp_path),
        "--layout", "grid_2x2",
    ])
    assert cli.main() == 0
    manifest = tmp_path / "labels.json"
    assert manifest.exists()
    with pytest.raises(DatasetError):
        load_manifest(manifest)
    assert cli.main() == 1  # Existing labels cannot be overwritten.

    raw = json.loads(manifest.read_text())
    for idx, item in enumerate(raw["tickets"]):
        item["expected"] = dict(TICKETS[idx])
    manifest.write_text(json.dumps(raw))
    fake = KnownAnswers(wrong=True)
    monkeypatch.setattr(
        cli, "make_benchmark_extractor",
        lambda provider, *, model=None: fake,
    )
    monkeypatch.setattr("sys.argv", [
        "evaluate_extraction.py", "check", "--manifest", str(manifest),
    ])
    assert cli.main() == 0
    assert fake.calls == 0

    monkeypatch.setattr("sys.argv", [
        "evaluate_extraction.py", "run", "--manifest", str(manifest),
    ])
    assert cli.main() == 0
    assert fake.calls == 4
    report = json.loads((tmp_path / "accuracy-report-gemini.json").read_text())
    assert report["provider"] == "gemini"
    assert report["model"] == "gemini-3.1-pro-preview"
    assert len(report["dataset_sha256"]) == 64
    assert report["ticket_exact_accuracy"] == .75
    assert report["fields"]["tonnage"]["non_null_accuracy"] == .75
    assert report["cases"][1]["fields"]["tonnage"] == "mismatch"

    # Refuse to charge for duplicate runs when output would be overwritten.
    assert cli.main() == 1
    assert fake.calls == 4
    printed = capsys.readouterr()
    assert "cannot" in printed.err.lower() or "already exists" in printed.err.lower()
