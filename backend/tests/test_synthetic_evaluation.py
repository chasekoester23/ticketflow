"""The offline synthetic evaluation, run through TicketFlow's real intake, splitter,
pipeline, review and approval endpoints on a throwaway database.

SYNTHETIC — engineering benchmark, not real-world accuracy. The oracle and
simulated extractors test the pipeline and the workflow arithmetic, not OCR.
"""
import json
import shutil
from collections import Counter

import pytest

from app.services.evaluation import FIELDS, DatasetError, _field_verdict
from app.services.workflow_evaluation import combine_truths, load_truth
from synthetic import STAMP
from synthetic.__main__ import main
from synthetic.dataset import build
from synthetic.evaluate import MixedEvidenceError, evaluate
from synthetic.util import read_json, write_json

SMALL = {"name": "small", "seed": 4, "difficulties": ("stress",), "per_difficulty": 10}


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    out = tmp_path_factory.mktemp("synthetic-eval") / "small"
    build(out, SMALL, formats={"pdf"})
    return out


def _fields(report, stage):
    return report["workflow_evaluation"]["overall"][stage]["fields"]


def test_oracle_scores_every_field_and_every_cost_exactly(small, tmp_path):
    report = evaluate(small, extractor_name="oracle", workdir=tmp_path / "sb")
    assert report["stamp"] == STAMP and report["synthetic"] is True
    assert report["extractor"]["name"] == "oracle" and "not OCR accuracy" in report["extractor"]["what_this_measures"]
    assert report["extractor"]["unrecognised_crops"] == 0
    assert report["overall"]["detection_rate"] == 1.0  # after a person resolves the scan manifest
    for stage in ("extraction", "final"):
        assert {f: v["accuracy"] for f, v in _fields(report, stage).items()} == {f: 1.0 for f in FIELDS}
    assert not any(report["overall"]["critical_errors"].values())
    money = report["overall"]["financial"]
    assert money["cost_wrong"] == 0 and money["difference"] == "0.00" and money["cost_correct"] == money["approved_tickets"] > 0
    assert report["overall"]["processing_failures"]["jobs_failed"] == 0
    crop = report["overall"]["crop"]
    assert crop["tickets"] == 10 and 0 < crop["mean_coverage"] <= 1
    assert all(0 <= c["coverage"] <= 1 for c in report["crops"] if c["ticket_id"])


def test_simulated_extractor_errors_are_counted_exactly(small, tmp_path):
    model = {"drop": 0.15, "ambiguous_misread": 1.0, "digit_slip_clear": 0.05, "digit_slip_degraded": 0.08,
             "digit_slip_poor": 0.1, "digit_slip_ambiguous": 0.08, "guess_unreadable": 0.6}
    report = evaluate(small, extractor_name="noisy-simulated", workdir=tmp_path / "sb", seed=3, error_model=model,
                      catch_rate=1.0)
    planted = report["planted"]
    assert planted and {p["kind"] for p in planted} >= {"dropped", "misread"}
    expected = Counter((p["field"], _field_verdict(p["field"], p["expected"], p["returned"])) for p in planted)
    for field, row in _fields(report, "extraction").items():
        for verdict in ("mismatch", "missing", "unexpected"):
            assert row[verdict] == expected[(field, verdict)], (field, verdict)
        assert row["correct"] == row["scored"] - sum(expected[(field, v)] for v in ("mismatch", "missing", "unexpected"))
    # A reviewer who catches everything leaves nothing wrong in what was approved.
    for row in _fields(report, "final").values():
        assert row["mismatch"] == row["unexpected"] == 0
    assert report["overall"]["financial"]["cost_wrong"] == 0
    # Same seed, same errors.
    again = evaluate(small, extractor_name="noisy-simulated", workdir=tmp_path / "sb2", seed=3, error_model=model,
                     catch_rate=1.0)
    assert again["planted"] == planted


def test_missed_corrections_reach_approved_figures(small, tmp_path):
    report = evaluate(small, extractor_name="noisy-simulated", workdir=tmp_path / "sb", seed=3, catch_rate=0.0,
                      error_model={"drop": 0.0, "ambiguous_misread": 1.0, "guess_unreadable": 0.0})
    wrong_raw = sum(r["mismatch"] for r in _fields(report, "extraction").values())
    assert wrong_raw > 0
    assert sum(r["mismatch"] for r in _fields(report, "final").values()) > 0  # nobody fixed them


def test_cli_end_to_end_on_a_tiny_set(tmp_path, capsys):
    ds = tmp_path / "ds"
    assert main(["generate", "--difficulty", "moderate", "--count", "4", "--seed", "3", "--out", str(ds),
                 "--formats", "pdf"]) == 0
    assert main(["validate", str(ds)]) == 0
    truth = tmp_path / "truth.json"
    assert main(["export-labels", str(ds), "--out", str(truth)]) == 0
    loaded = load_truth(truth)
    assert loaded["synthetic"] is True and sum(s.is_ticket for s in loaded["slots"]) == 4
    assert main(["preview", str(ds), "--out", str(tmp_path / "pages.png"), "--tickets", str(tmp_path / "t.png")]) == 0
    assert (tmp_path / "pages.png").stat().st_size > 0 and (tmp_path / "t.png").stat().st_size > 0
    assert main(["assemble", str(ds), "--formats", "pdf,png"]) == 0
    assert list((ds / "pages").glob("*.png")) and main(["validate", str(ds), "--quick"]) == 0
    report = tmp_path / "report.json"
    assert main(["evaluate", str(ds), "--extractor", "oracle", "--out", str(report)]) == 0
    data = json.loads(report.read_text())
    assert data["stamp"] == STAMP and data["extractor"]["name"] == "oracle"
    assert STAMP in capsys.readouterr().out
    assert main(["generate", "--difficulty", "easy", "--count", "2", "--out", str(ds)]) == 2  # not empty: refused


def test_generate_refuses_to_overwrite_without_asking(tmp_path):
    ds = tmp_path / "ds"
    assert main(["generate", "--difficulty", "easy", "--count", "1", "--out", str(ds), "--formats", "pdf"]) == 0
    assert main(["generate", "--difficulty", "easy", "--count", "1", "--out", str(ds), "--formats", "pdf"]) == 2
    assert main(["generate", "--difficulty", "easy", "--count", "1", "--out", str(ds), "--formats", "pdf",
                 "--overwrite"]) == 0
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "keep.txt").write_text("mine")
    # Never deletes a folder that isn't a synthetic dataset, even when asked to overwrite.
    assert main(["generate", "--difficulty", "easy", "--count", "1", "--out", str(tmp_path / "notes"),
                 "--overwrite"]) == 2
    assert (tmp_path / "notes" / "keep.txt").read_text() == "mine"


def test_gemini_is_refused_without_explicit_authorisation(small, capsys):
    assert main(["evaluate", str(small), "--extractor", "gemini"]) == 2
    assert "paid" in capsys.readouterr().err


def _copy(small, tmp_path, name="copy"):
    out = tmp_path / name
    shutil.copytree(small, out)
    return out


def test_synthetic_and_real_labels_cannot_be_mixed(small, tmp_path):
    real_flag = _copy(small, tmp_path, "a")
    data = read_json(real_flag / "truth.json")
    write_json(real_flag / "truth.json", dict(data, synthetic=False, label_source="person"))
    with pytest.raises(MixedEvidenceError):
        evaluate(real_flag, workdir=tmp_path / "sb-a")

    one_real_slot = _copy(small, tmp_path, "b")
    data = read_json(one_real_slot / "truth.json")
    data["slots"][0]["synthetic"] = False
    write_json(one_real_slot / "truth.json", data)
    with pytest.raises(MixedEvidenceError):
        evaluate(one_real_slot, workdir=tmp_path / "sb-b")
    with pytest.raises(DatasetError, match="synthetic and real"):
        load_truth(one_real_slot / "truth.json")

    (tmp_path / "real").mkdir()
    under_real = _copy(small, tmp_path / "real", "c")
    with pytest.raises(MixedEvidenceError):
        evaluate(under_real, workdir=tmp_path / "sb-c")


def test_workflow_evaluation_refuses_a_synthetic_and_real_mix(small, tmp_path):
    synthetic = load_truth(small / "truth.json")
    person = {"labelled_by": "AB", "labelled_independently": True,
              "slots": [{"file": "scan.pdf", "page": 1, "position": 0, "ticket": False}]}
    (tmp_path / "real.json").write_text(json.dumps(person))
    real = load_truth(tmp_path / "real.json")
    with pytest.raises(DatasetError, match="mix synthetic and real"):
        combine_truths([synthetic, real])
    assert combine_truths([synthetic])["synthetic"] is True
    from scripts.evaluate_workflow import main as evaluate_workflow_main
    assert evaluate_workflow_main(["--database", str(tmp_path / "none.db"), "--organization", "1", "--batch", "1",
                                   "--truth", str(small / "truth.json"), "--truth", str(tmp_path / "real.json")]) == 2
