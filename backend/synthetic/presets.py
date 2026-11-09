"""Difficulty presets and the per-ticket rendering plan.

A preset fixes how hard a ticket is: which handwriting styles, how often a
field is crossed out, missing, ambiguous or smudged, and how badly the page is
scanned. Named datasets (benchmark, benchmark-mini, stress) fix a seed and a
stratified mix of layouts and difficulties.
"""
from __future__ import annotations

from .glyphs import AMBIGUOUS, GLYPHS
from .render import HAND_INKS, wrong_value, written_value
from .templates import TEMPLATE_IDS, TEMPLATES
from .util import rng

DIFFICULTIES = ("easy", "moderate", "hard", "extreme")

PRESETS = {
    "easy": {
        "styles": {"neat": 0.7, "rushed": 0.3}, "caps": 0.5, "trim_tons": 0.2, "omit_scale": 0.5,
        "missing": 0.0, "crossout": 0.0, "ambiguous": 0.04, "smudge": 0.0,
        "long_on_grid": False, "single_fraction": 0.1, "blank_slot": 0.04,
        "scan": {"rotation_sd": 0.6, "offset_sd": 0.04, "upside_down": 0.0, "clip": 0.0, "page_rotation_sd": 0.2,
                 "shear": 0.0, "page_shift": 0.03, "blur": (0.0, 0.4), "contrast": (0.95, 1.0), "faint": 0.0,
                 "gradient": 0.03, "noise": (1.0, 2.5), "colour": 0.3, "jpeg": (80, 90)},
    },
    "moderate": {
        "styles": {"neat": 0.35, "rushed": 0.35, "messy": 0.2, "faint": 0.1}, "caps": 0.5, "trim_tons": 0.3,
        "omit_scale": 1.0, "missing": 0.04, "crossout": 0.08, "ambiguous": 0.12, "smudge": 0.0,
        "long_on_grid": False, "single_fraction": 0.1, "blank_slot": 0.08,
        "scan": {"rotation_sd": 2.0, "offset_sd": 0.16, "upside_down": 0.02, "clip": 0.0, "page_rotation_sd": 0.6,
                 "shear": 0.003, "page_shift": 0.1, "blur": (0.3, 0.9), "contrast": (0.85, 1.0), "faint": 0.05,
                 "gradient": 0.1, "noise": (2.0, 4.0), "colour": 0.25, "jpeg": (70, 88)},
    },
    "hard": {
        "styles": {"rushed": 0.3, "messy": 0.3, "faint": 0.2, "difficult": 0.2}, "caps": 0.5, "trim_tons": 0.35,
        "omit_scale": 1.3, "missing": 0.1, "crossout": 0.18, "ambiguous": 0.25, "smudge": 0.04,
        "long_on_grid": True, "single_fraction": 0.1, "blank_slot": 0.12,
        "scan": {"rotation_sd": 4.0, "offset_sd": 0.3, "upside_down": 0.04, "clip": 0.06, "page_rotation_sd": 1.2,
                 "shear": 0.008, "page_shift": 0.2, "blur": (0.6, 1.5), "contrast": (0.7, 0.95), "faint": 0.15,
                 "gradient": 0.2, "noise": (4.0, 7.0), "colour": 0.2, "jpeg": (50, 72)},
    },
    "extreme": {
        "styles": {"messy": 0.3, "faint": 0.3, "difficult": 0.4}, "caps": 0.5, "trim_tons": 0.4,
        "omit_scale": 1.6, "missing": 0.18, "crossout": 0.3, "ambiguous": 0.45, "smudge": 0.08,
        "long_on_grid": True, "single_fraction": 0.1, "blank_slot": 0.15,
        "scan": {"rotation_sd": 7.0, "offset_sd": 0.45, "upside_down": 0.08, "clip": 0.14, "page_rotation_sd": 2.5,
                 "shear": 0.015, "page_shift": 0.35, "blur": (1.0, 2.2), "contrast": (0.5, 0.85), "faint": 0.3,
                 "gradient": 0.3, "noise": (7.0, 11.0), "colour": 0.15, "jpeg": (30, 55)},
    },
    "stress": {
        "styles": {"messy": 0.2, "faint": 0.3, "difficult": 0.5}, "caps": 0.5, "trim_tons": 0.5,
        "omit_scale": 2.0, "missing": 0.3, "crossout": 0.4, "ambiguous": 0.6, "smudge": 0.12,
        "long_on_grid": True, "single_fraction": 0.05, "blank_slot": 0.2,
        "scan": {"rotation_sd": 11.0, "offset_sd": 0.65, "upside_down": 0.12, "clip": 0.25, "page_rotation_sd": 4.0,
                 "shear": 0.025, "page_shift": 0.5, "blur": (1.5, 2.8), "contrast": (0.4, 0.8), "faint": 0.5,
                 "gradient": 0.4, "noise": (9.0, 14.0), "colour": 0.1, "jpeg": (20, 40)},
    },
}

# Named sets: a fixed seed, which difficulties, and how many tickets of each.
DATASETS = {
    "benchmark-mini": {"seed": 20261108, "difficulties": DIFFICULTIES, "per_difficulty": 24},
    "benchmark": {"seed": 20261109, "difficulties": DIFFICULTIES, "per_difficulty": 60},
    "stress": {"seed": 20261110, "difficulties": ("stress",), "per_difficulty": 200},
}

CROSSABLE = ("net_tons", "delivery_date", "ticket_number", "job_location", "truck")
MISSABLE = ("ticket_number", "delivery_date", "material", "net_tons", "job_location")
AMBIGUABLE = ("net_tons", "ticket_number", "delivery_date", "truck", "gross", "tare", "net_weight", "stub_net_tons")


def _pick(r, weights: dict) -> str:
    x, total = r.random(), 0.0
    for key, w in weights.items():
        total += w
        if x < total:
            return key
    return key


def stratified_templates(seed, difficulty: str, count: int) -> list[str]:
    """Each template equally often (as near as `count` allows), in a seeded order."""
    r = rng("strata", seed, difficulty)
    out = []
    while len(out) < count:
        block = list(TEMPLATE_IDS)
        r.shuffle(block)
        out.extend(block)
    return out[:count]


def make_plan(record: dict, difficulty: str, seed) -> dict:
    """How this ticket is written: style and the deliberate effects, all recorded."""
    p = PRESETS[difficulty]
    r = rng("plan", seed, record["ticket_id"])
    t = TEMPLATES[record["template"]]
    plan = {"difficulty": difficulty, "style": _pick(r, p["styles"]), "caps": r.random() < p["caps"],
            "trim_tons": r.random() < p["trim_tons"], "omit": [], "missing": [], "crossout": {}, "ambiguous": {},
            "smudge": []}
    specs = {s["field"]: s for s in t["fields"]}
    hand = [f for f, s in specs.items() if s["ink"] == "hand" and written_value(record, s, plan) is not None]
    for f, s in specs.items():
        if not s["core"] and s["present"] < 1 and r.random() < min(1.0, (1 - s["present"]) * p["omit_scale"]):
            plan["omit"].append(f)
    if r.random() < p["missing"]:
        choices = [f for f in MISSABLE if f in hand]
        if choices:
            plan["missing"].append(r.choice(choices))
    if r.random() < p["crossout"]:
        choices = [f for f in CROSSABLE if f in hand and f not in plan["missing"] and f not in plan["omit"]]
        if choices:
            f = r.choice(choices)
            plan["crossout"][f] = {"wrong": wrong_value(record, specs[f], plan, r),
                                   "kind": r.choice(["line", "double", "scribble"])}
    if r.random() < p["ambiguous"]:
        for f in [f for f in AMBIGUABLE if f in hand and f not in plan["missing"] and f not in plan["omit"]]:
            text = written_value(record, specs[f], plan)
            spots = [i for i, c in enumerate(text) if c in AMBIGUOUS and f"{c}~{AMBIGUOUS[c]}" in GLYPHS]
            if spots and r.random() < 0.6:
                chosen = r.sample(spots, k=min(len(spots), r.choice([1, 1, 2])))
                plan["ambiguous"][f] = {str(i): f"{text[i]}~{AMBIGUOUS[text[i]]}" for i in sorted(chosen)}
    if r.random() < p["smudge"]:
        choices = [f for f in hand if f not in plan["missing"] and f not in plan["omit"] and f not in plan["crossout"]
                   and specs[f]["ink"] in HAND_INKS and specs[f]["ink"] != "check"]
        if choices:
            plan["smudge"].append(r.choice(choices))
    return plan
