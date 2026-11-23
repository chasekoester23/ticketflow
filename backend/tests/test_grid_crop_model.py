"""The fixed-grid crop model (scripts/measure_grid_crops.py): its arithmetic, not real paper."""
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_grid_crops.py"
spec = importlib.util.spec_from_file_location("measure_grid_crops", SCRIPT)
model = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model  # dataclasses look their module up here
spec.loader.exec_module(model)


def test_tickets_squared_up_in_their_quarters_are_never_cut():
    perfect = model.Scenario("perfect", fill=0.9, jitter=0, tilt_degrees=0, shift=0, note="")
    result = model.measure(perfect, trials=200)
    assert result["complete"] == result["tickets"] == 800 and result["worst_cut_fraction_of_ticket"] == 0


def test_tilt_and_off_centre_framing_cut_more_tickets_and_deeper():
    steady = model.measure(model.Scenario("a", 0.85, 0.02, 0.5, 0.005, ""), trials=2000)
    hurried = model.measure(model.Scenario("b", 0.85, 0.04, 5.0, 0.05, ""), trials=2000)
    assert steady["material_cut_rate"] < hurried["material_cut_rate"]
    assert steady["worst_cut_fraction_of_ticket"] < hurried["worst_cut_fraction_of_ticket"]


def test_the_same_seed_gives_the_same_answer(tmp_path):
    assert model.measure(model.SCENARIOS[2], 500, seed=3) == model.measure(model.SCENARIOS[2], 500, seed=3)
    assert model.main(["--trials", "50", "--json", str(tmp_path / "out.json")]) == 0
    assert '"model": true' in (tmp_path / "out.json").read_text()
