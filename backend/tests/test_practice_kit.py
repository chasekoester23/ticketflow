"""Printable practice tickets, answer-key conversion, benchmark model choice,
and provider error diagnostics. Entirely offline: no AI or network calls."""
import csv
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from app.config import settings
from app.services import splitter
from app.services.benchmark_providers import DEFAULT_MODELS, resolve_model
from app.services.evaluation import DatasetError, evaluate, load_manifest
from app.services.practice_kit import (
    KEY_COLUMNS, SLOTS, answer_key_csv, build_practice_pdf, labels_from_answer_key,
)
from app.services.provider_diagnostics import explain_failure, model_name_problem, redact
from app.services.validation import ExtractedTicket
from scripts import check_api_key, make_practice_tickets, practice_key_to_labels

FAKE_KEY = "AIza" + "x" * 35


# ---------------------------------------------------------------- printable PDF

def _near_white_border(crop: Image.Image, fraction: float = 0.015) -> bool:
    gray = crop.convert("L")
    w, h = gray.size
    bw, bh = max(2, int(w * fraction)), max(2, int(h * fraction))
    strips = [gray.crop((0, 0, w, bh)), gray.crop((0, h - bh, w, h)),
              gray.crop((0, 0, bw, h)), gray.crop((w - bw, 0, w, h))]
    return all(strip.getextrema()[0] > 200 for strip in strips)


def test_practice_pdf_splits_into_whole_tickets_with_the_real_splitter():
    pages = splitter.render_pdf_pages(build_practice_pdf(2), dpi=settings.render_dpi)
    assert len(pages) == 2
    for page in pages:
        crops = splitter.split_page(page, "grid_2x2")
        assert len(crops) == 4
        for crop in crops:
            assert not splitter.is_blank(crop.image)
            # White all around: the ticket border is inside the crop, and no
            # neighbouring ticket leaks in.
            assert _near_white_border(crop.image), crop.position


def test_answer_key_is_prenumbered_and_layouts_vary():
    rows = list(csv.DictReader(io.StringIO(answer_key_csv(3))))
    assert [r["practice_id"] for r in rows[:5]] == ["S01-A", "S01-B", "S01-C", "S01-D", "S02-A"]
    assert len(rows) == 12
    assert len({r["layout"] for r in rows[:4]}) >= 2
    assert all(r["ticket_number"] == "" for r in rows)
    with pytest.raises(ValueError):
        build_practice_pdf(0)
    with pytest.raises(ValueError):
        answer_key_csv(26)


# ---------------------------------------------------------------- answer key

def _key(updates: dict[str, dict[str, str]], sheets: int = 2) -> str:
    rows = list(csv.DictReader(io.StringIO(answer_key_csv(sheets))))
    for row in rows:
        row.update(updates.get(row["practice_id"], {}))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=KEY_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


FILLED = {
    "ticket_number": "10482", "delivery_date": "10/3/2026", "material": "No. 57 Stone",
    "net_tons": "24.50", "supplier": "Example Aggregates", "job_location": "North Lot",
}


def test_filled_key_becomes_a_valid_manifest_and_unused_rows_are_skipped(tmp_path):
    (tmp_path / "scan.pdf").write_bytes(build_practice_pdf(2))
    manifest, unused = labels_from_answer_key(_key({
        "S01-A": FILLED,
        "S02-D": {**FILLED, "ticket_number": "77121", "delivery_date": "2026-10-04",
                  "supplier": "unreadable", "net_tons": "19.75"},
    }), "scan.pdf", 2)
    assert unused == 6 and manifest["synthetic_practice"] is True
    first, second = manifest["tickets"]
    assert (first["id"], first["page"], first["position"]) == ("practice-S01-A", 1, 0)
    assert first["expected"]["delivery_date"] == "2026-10-03"
    assert first["expected"]["tonnage"] == "24.50"
    assert first["expected"]["material_type"] == "No. 57 Stone"
    assert (second["page"], second["position"]) == (2, SLOTS.index("D"))
    assert second["expected"]["supplier"] is None  # explicitly unreadable, never guessed

    path = tmp_path / "labels.json"
    path.write_text(json.dumps(manifest))
    cases = load_manifest(path)  # the real benchmark loader accepts it
    assert evaluate(cases, check_only=True)["labeled_tickets_checked"] == 2


@pytest.mark.parametrize("update, message", [
    ({"S01-A": {**FILLED, "supplier": ""}}, "'supplier' is blank"),
    ({"S01-A": {**FILLED, "delivery_date": "Oct 3"}}, "date must be"),
    ({"S01-A": {**FILLED, "delivery_date": "2/30/2026"}}, "date must be"),
    ({"S01-A": {**FILLED, "net_tons": "49000 lb"}}, "TONS"),
    ({"S01-A": {**FILLED, "net_tons": "-2"}}, "positive"),
    ({"S01-A": {**FILLED, "practice_id": "S09-A"}}, "practice_id/sheet/slot"),
])
def test_answer_key_mistakes_are_reported_by_line(update, message):
    with pytest.raises(DatasetError) as error:
        labels_from_answer_key(_key(update), "scan.pdf", 2)
    assert message in str(error.value)


def test_answer_key_guards_against_wrong_scan_and_empty_or_broken_files():
    with pytest.raises(DatasetError, match="scan has only 1 page"):
        labels_from_answer_key(_key({"S02-A": FILLED}), "scan.pdf", 1)
    with pytest.raises(DatasetError, match="No filled-in tickets"):
        labels_from_answer_key(_key({}), "scan.pdf", 2)
    with pytest.raises(DatasetError, match="missing column"):
        labels_from_answer_key("practice_id,sheet\nS01-A,1\n", "scan.pdf", 2)


# ---------------------------------------------------------------- CLIs

def test_cli_creates_kit_and_never_overwrites(tmp_path, capsys):
    out = tmp_path / "practice"
    assert make_practice_tickets.main(["--sheets", "1", "--output-dir", str(out)]) == 0
    assert (out / "practice-tickets.pdf").read_bytes().startswith(b"%PDF")
    key = out / "answer-key.csv"
    key.write_text(_key({"S01-B": FILLED}, sheets=1))  # the person's filled answers
    assert make_practice_tickets.main(["--sheets", "1", "--output-dir", str(out)]) == 1
    assert "S01-B" in key.read_text() and "Example Aggregates" in key.read_text()

    scan = out / "scanned.pdf"
    scan.write_bytes(build_practice_pdf(1))
    assert practice_key_to_labels.main(["--key", str(key), "--scan", str(scan)]) == 0
    labels = json.loads((out / "labels.json").read_text())
    assert [t["id"] for t in labels["tickets"]] == ["practice-S01-B"]
    assert practice_key_to_labels.main(["--key", str(key), "--scan", str(scan)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err


# ---------------------------------------------------------------- benchmark run

class _FakeExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, image):
        self.calls += 1
        values = {"ticket_number": "10482", "delivery_date": "2026-10-03",
                  "material_type": "No. 57 Stone", "tonnage": "24.5",
                  "supplier": "Example Aggregates", "job_location": "North Lot"}
        return ExtractedTicket.model_validate(values), values


def test_practice_manifest_scores_and_pauses_between_calls(tmp_path):
    (tmp_path / "scan.pdf").write_bytes(build_practice_pdf(1))
    manifest, _ = labels_from_answer_key(_key({"S01-A": FILLED, "S01-C": FILLED}, sheets=1), "scan.pdf", 1)
    (tmp_path / "labels.json").write_text(json.dumps(manifest))
    pauses = []
    extractor = _FakeExtractor()
    report = evaluate(load_manifest(tmp_path / "labels.json"), extractor,
                      pause_seconds=6, sleep=pauses.append)
    assert extractor.calls == 2 and pauses == [6]  # no pause before the first call
    assert report["tickets_all_six_correct"] == 2


def test_benchmark_uses_the_configured_gemini_model(monkeypatch):
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.5-flash")
    assert resolve_model("gemini") == "gemini-3.5-flash"
    assert resolve_model("gemini", "gemini-3.1-pro-preview") == "gemini-3.1-pro-preview"
    assert resolve_model("openai") == DEFAULT_MODELS["openai"]
    with pytest.raises(ValueError):
        resolve_model("other")


# ---------------------------------------------------------------- diagnostics

class _ProviderError(Exception):
    def __init__(self, code, status, message):
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


@pytest.mark.parametrize("code, status, message, expected", [
    (429, "RESOURCE_EXHAUSTED", "Quota exceeded ... limit: 0, model: gemini-3.1-pro", "no quota for"),
    (429, "RESOURCE_EXHAUSTED", "Too many requests per minute", "add --pause-seconds"),
    (404, "NOT_FOUND", "models/x is not found", "not found"),
    (400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", "rejected the API key"),
    (503, "UNAVAILABLE", "The model is overloaded", "Wait a minute"),
])
def test_failures_get_specific_advice(code, status, message, expected):
    text = explain_failure(_ProviderError(code, status, message), model="gemini-3.1-pro-preview")
    assert expected in text and str(code) in text


def test_keys_are_never_echoed_in_diagnostics():
    leaked = f"Bad request for key={FAKE_KEY} and header {FAKE_KEY}"
    text = explain_failure(_ProviderError(400, "INVALID_ARGUMENT", leaked), model="m", secret=FAKE_KEY)
    assert FAKE_KEY not in text and "[redacted]" in text
    assert FAKE_KEY not in redact("token AIza" + "y" * 30 + " end")
    assert model_name_problem("GEMINI_MODEL=gemini-3.5-flash")
    assert model_name_problem(" ")
    assert model_name_problem('"gemini-3.5-flash"')
    assert model_name_problem("gemini-3.5-flash") is None


def test_check_script_catches_malformed_model_before_any_request(monkeypatch, capsys):
    monkeypatch.setattr(settings, "extraction_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", FAKE_KEY)
    monkeypatch.setattr(settings, "gemini_model", "GEMINI_MODEL=gemini-3.5-flash")
    import builtins
    real_import = builtins.__import__

    def no_network(name, *args, **kwargs):
        if name.startswith("google"):
            raise AssertionError("must not contact the provider")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_network)
    assert check_api_key.main([]) == 1
    output = capsys.readouterr().out
    assert "malformed" in output and "No API calls made" in output and FAKE_KEY not in output
