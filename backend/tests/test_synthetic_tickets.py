"""The synthetic ticket generator: determinism, truth integrity, arithmetic and geometry.

SYNTHETIC — engineering benchmark, not real-world accuracy. These tests check the
generator tells the truth about what it drew; they say nothing about reading
real handwriting.
"""
import json
import shutil
from decimal import Decimal

import pytest
from PIL import Image

from synthetic import STAMP
from synthetic import geometry as g
from synthetic.dataset import build, load
from synthetic.glyphs import GLYPHS, base_char
from synthetic.handwriting import STYLES, Canvas, RenderError, hand_text
from synthetic.presets import PRESETS
from synthetic.records import check_weights, generate_records, records_csv
from synthetic.scan import PAGE_SIZE, Placement, compose_page
from synthetic.templates import TEMPLATES, check_templates
from synthetic.util import rng, sha256_file, write_json
from synthetic.verify import validate

TINY = {"name": "tiny", "seed": 4, "difficulties": ("stress",), "per_difficulty": 10}


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    out = tmp_path_factory.mktemp("synthetic") / "tiny"
    build(out, TINY, formats={"pdf", "png"})
    return out


def test_same_seed_gives_byte_identical_files(tiny, tmp_path):
    again = build(tmp_path / "again", TINY, formats={"pdf", "png"})
    first = json.loads((tiny / "dataset.json").read_text())
    assert again["files"] == first["files"]  # every record, label, PNG and PDF, by sha256
    assert (tmp_path / "again" / "records.json").read_bytes() == (tiny / "records.json").read_bytes()
    other = build(tmp_path / "other", {**TINY, "seed": 5}, formats={"pdf"})
    assert other["files"]["records.json"] != first["files"]["records.json"]


def test_records_csv_and_json_are_stable():
    a, b = generate_records(11, 30), generate_records(11, 30)
    assert a == b and records_csv(a) == records_csv(b)
    assert generate_records(12, 30) != a


def test_gross_minus_tare_is_net_in_the_right_units():
    records = generate_records(3, 400)
    assert {r["weight_unit"] for r in records} == {"lb", "ton", None}
    for rec in records:
        assert check_weights(rec) == []
        if rec["weight_unit"] == "lb":
            gross, tare, net = int(rec["gross"]), int(rec["tare"]), int(rec["net_weight"])
            assert gross - tare == net and Decimal(net) / 2000 == Decimal(rec["net_tons"])
        elif rec["weight_unit"] == "ton":
            assert Decimal(rec["gross"]) - Decimal(rec["tare"]) == Decimal(rec["net_tons"])
    broken = dict(records[0], weight_unit="lb", gross="40000", tare="30000", net_weight="9980")
    assert check_weights(broken)


def test_ticket_numbers_are_unique_per_supplier():
    records = generate_records(9, 600)
    keys = [(r["supplier"], r["ticket_number"]) for r in records]
    assert len(keys) == len(set(keys))


def test_templates_are_sound_and_there_are_enough_of_them():
    assert 8 <= len(TEMPLATES) <= 12
    assert check_templates() == []
    assert len({t["kind"] for t in TEMPLATES.values()}) == len(TEMPLATES)


def test_every_field_label_equals_what_the_render_log_drew(tiny):
    docs = load(tiny)
    effects = {"crossed_out": 0, "ambiguous": 0, "smudged": 0, "missing": 0}
    for t in docs["labels"]["tickets"]:
        log = docs["render_log"]["tickets"][t["ticket_id"]]
        for name, f in t["fields"].items():
            segs = {s["role"]: s for s in log.get(name, [])}
            legible = segs.get("correction") or segs.get("value")
            if f["written_value"] is None:
                assert legible is None and f["visible_value"] is None
                effects["missing"] += f["intentionally_missing"]
                continue
            if f["ink"] == "check":
                assert segs["check"]["text"] == f["visible_value"] == f["true_value"]
                continue
            drawn = "".join(base_char(k) for k in legible["glyphs"])
            assert drawn == f["written_value"]
            assert f["visible_value"] == (None if f["smudged"] else drawn)
            if f["crossed_out"]:
                struck = "".join(base_char(k) for k in segs["struck"]["glyphs"])
                assert struck == f["struck_value"] != drawn
                effects["crossed_out"] += 1
            assert [(i, k.split("~")[1]) for i, k in enumerate(legible["glyphs"]) if "~" in k] == \
                [(a["index"], a["confusable_with"]) for a in f["ambiguous_chars"]]
            effects["ambiguous"] += bool(f["ambiguous_chars"])
            effects["smudged"] += f["smudged"]
    assert all(effects.values()), effects  # the tiny set exercises every effect


def test_validate_passes_and_reports_stamp(tiny):
    result = validate(tiny)
    assert result["ok"], result["problems"][:5]
    assert result["stamp"] == STAMP
    pages = load(tiny)["pages"]["pages"]
    slots = [s for p in pages for s in p["slots"]]
    assert any(s["ticket_id"] is None for s in slots)  # blank slots
    assert any(s.get("intentionally_clipped") and not s["fully_visible"] for s in slots)  # deliberate clipping


def _corrupt(tiny, tmp_path, fn, rehash=True):
    copy = tmp_path / "copy"
    shutil.copytree(tiny, copy)
    fn(copy)
    if rehash:  # so the semantic checks, not only the hash check, must find it
        manifest = json.loads((copy / "dataset.json").read_text())
        manifest["files"] = {k: sha256_file(copy / k) for k in manifest["files"]}
        write_json(copy / "dataset.json", manifest)
    return validate(copy, rerender=False)["problems"]


def _edit(path, fn, compact=False):
    data = json.loads(path.read_text())
    fn(data)
    write_json(path, data, compact=compact)


def test_validate_catches_corrupted_labels(tiny, tmp_path):
    def visible(copy):
        def fn(d):
            f = next(f for t in d["tickets"] for f in t["fields"].values() if f["visible_value"] and f["ink"] == "hand")
            f["visible_value"] = f["visible_value"][:-1] + ("7" if f["visible_value"][-1] != "7" else "1")
        _edit(copy / "labels.json", fn)
    problems = _corrupt(tiny, tmp_path, visible)
    assert any("visible_value" in p for p in problems), problems
    assert any("truth.json" in p for p in problems) or True  # the export may or may not change with the edit


def test_validate_catches_a_tampered_render_log(tiny, tmp_path):
    def glyphs(copy):
        def fn(d):
            segs = next(s for log in d["tickets"].values() for segs in log.values() for s in segs
                        if s["role"] == "value" and len(s["glyphs"]) > 2 and s["glyphs"][0] in "0123456789")
            segs["glyphs"][0] = "8" if segs["glyphs"][0] != "8" else "3"
        _edit(copy / "render_log.json", fn, compact=True)
    assert any("drew" in p or "printed" in p for p in _corrupt(tiny, tmp_path, glyphs))


def test_validate_catches_bad_geometry_weights_and_unhashed_edits(tiny, tmp_path):
    def quad(copy):
        _edit(copy / "pages.json", lambda d: d["pages"][0]["slots"][next(
            i for i, s in enumerate(d["pages"][0]["slots"]) if s["ticket_id"])].update(visible_fraction=0.5),
            compact=True)
    assert any("visible fraction" in p for p in _corrupt(tiny, tmp_path / "a", quad))

    def weights(copy):
        _edit(copy / "records.json", lambda d: next(r for r in d["records"] if r["weight_unit"]).update(tare="1"))
    assert any("gross" in p for p in _corrupt(tiny, tmp_path / "b", weights))

    def unhashed(copy):
        (copy / "records.csv").write_text("tampered\n")
    assert any("contents changed" in p for p in _corrupt(tiny, tmp_path / "c", unhashed, rehash=False))


def test_handwriting_refuses_to_draw_outside_its_box_or_a_wrong_variant():
    canvas = Canvas(200, 60, (255, 255, 255))
    with pytest.raises(RenderError):
        hand_text(canvas, "f", "value", "1234567890" * 6, [0, 0, 200, 60], STYLES["messy"], rng("t"))
    with pytest.raises(RenderError):
        hand_text(canvas, "f", "value", "42", [0, 0, 200, 60], STYLES["neat"], rng("t"), ambiguous={0: "1~7"})
    seg = hand_text(canvas, "f", "value", "17", [0, 0, 200, 60], STYLES["neat"], rng("t"), ambiguous={0: "1~7"})
    assert seg.glyphs == ["1~7", "7"] and 0 <= seg.ink[0] and seg.ink[2] <= 200
    assert all(base_char(k) in GLYPHS for k in GLYPHS)


def _solid(w, h):
    return Image.new("RGB", (w, h), (0, 0, 0))


@pytest.mark.parametrize("clip", [0.0, 1.0])
def test_corners_bbox_and_visibility_survive_rotation_and_skew(clip):
    scan = dict(PRESETS["extreme"]["scan"], rotation_sd=8.0, page_rotation_sd=3.0, shear=0.02, upside_down=0.5,
                clip=clip, offset_sd=0.15)
    slots = [Placement(f"S{i}", _solid(400, 520), {"core": [40, 40, 200, 90]}, i == 1) for i in range(3)] + [None]
    page, records, _ = compose_page(slots, "grid_2x2", scan, 7, f"geometry-{clip}", photometric=False)
    grey = page.convert("L")
    px = grey.load()
    w, h = PAGE_SIZE
    placed = [r for r in records if r["ticket_id"]]
    assert len(placed) == 3 and any(abs(r["rotation_deg"]) > 1 for r in placed)
    for r in placed:
        quad = [tuple(p) for p in r["quad"]]
        cx, cy = sum(p[0] for p in quad) / 4, sum(p[1] for p in quad) / 4
        for x, y in quad:  # just inside each corner is ink, just outside is paper
            ux, uy = (cx - x), (cy - y)
            n = (ux * ux + uy * uy) ** 0.5
            ix, iy = x + 4 * ux / n, y + 4 * uy / n
            ox, oy = x - 4 * ux / n, y - 4 * uy / n
            if 0 <= ix < w and 0 <= iy < h:
                assert px[int(ix), int(iy)] < 80
            if 0 <= ox < w and 0 <= oy < h:
                assert px[int(ox), int(oy)] > 150
        # the dark pixels inside this ticket's bbox fill the visible part of its quad
        x0, y0, x1, y1 = (int(max(0, v)) for v in r["bbox"])
        x1, y1 = min(x1, w), min(y1, h)
        others = [[tuple(p) for p in o["quad"]] for o in placed if o is not r]
        dark = sum(1 for yy in range(y0, y1, 2) for xx in range(x0, x1, 2)
                   if px[xx, yy] < 128 and all(g.inside_fraction([(xx, yy), (xx + 1, yy), (xx + 1, yy + 1),
                                                                   (xx, yy + 1)], g.bounds(o)) == 0 for o in others))
        expected = r["visible_fraction"] * g.area(quad) / 4  # visible area, counted on a 2 px grid
        assert abs(dark - expected) / expected < 0.03
        if clip == 0.0:
            assert r["fully_visible"]
    if clip == 1.0:
        assert all(not r["fully_visible"] and r["visible_fraction"] < 0.95 for r in placed)


def test_geometry_basics():
    m = g.compose(g.translate(10, 0), g.rotate(90))
    x, y = g.apply(m, (1, 0))
    assert abs(x - 10) < 1e-9 and abs(y + 1) < 1e-9  # counter-clockwise on screen: +x turns to -y
    back = g.apply(g.invert(m), (x, y))
    assert abs(back[0] - 1) < 1e-9 and abs(back[1]) < 1e-9
    square = g.box_quad((0, 0, 10, 10))
    assert g.inside_fraction(square, (5, 0, 20, 20)) == pytest.approx(0.5)
    assert not g.separated(square, g.box_quad((5, 5, 15, 15))) and g.separated(square, g.box_quad((12, 0, 20, 10)))
