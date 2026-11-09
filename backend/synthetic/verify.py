"""Check a synthetic dataset end to end, and fail loudly on any mismatch.

  * schema and stamps; every file's sha256 against the manifest;
  * records: gross - tare = net, net in tons, unique ticket numbers, valid dates;
  * truth integrity: each field's glyphs, re-derived from the render log, equal
    the label (visible, struck and corrected values; ambiguous characters);
    written values mean the true value; ink stays inside its box and the ticket;
  * re-rendering every ticket from its record and plan gives the same log;
  * pages: corners re-derived from each slot's matrix, bounding boxes, visible
    fractions, no ticket covering another, nothing that matters cut off unless
    the clip was intended;
  * truth.json equals a fresh export;
  * optionally, a full rebuild gives byte-identical files (determinism).
"""
from __future__ import annotations

import re
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import LABELS_SCHEMA, PAGES_SCHEMA, RECORDS_SCHEMA, STAMP
from . import geometry as g
from .dataset import DatasetError, build, export_truth, load
from .glyphs import base_char
from .presets import PRESETS
from .records import check_weights
from .render import MONTHS, render_ticket, written_value
from .templates import ALIASES, CORE_FIELDS, TEMPLATES
from .util import sha256_file

LEGIBLE_ROLES = ("value", "correction", "check")
INK_TOLERANCE = 1.5


def _glyph_text(seg: dict) -> str:
    return "".join(base_char(k) for k in seg["glyphs"])


def _parse_written_date(text: str, fmt: str) -> str | None:
    try:
        if fmt == "Mon D YYYY":
            mon, day, year = text.split()
            return date(int(year), MONTHS.index(mon.capitalize()) + 1, int(day)).isoformat()
        parts = [int(p) for p in re.split(r"[/-]", text)]
        month, day, year = parts
        return date(year + 2000 if year < 100 else year, month, day).isoformat()
    except (ValueError, IndexError):
        return None


def means_the_same(field: str, written: str, true, fmt: str | None) -> bool:
    key = ALIASES.get(field, field)
    if key == "delivery_date":
        return _parse_written_date(written, fmt or "M/D/YY") == true
    if key in ("net_tons", "gross", "tare", "net_weight"):
        try:
            return Decimal(written.replace(",", "")) == Decimal(true)
        except InvalidOperation:
            return False
    return written.casefold() == str(true).casefold()


def _inside(ink, box, tol) -> bool:
    return ink[0] >= box[0] - tol and ink[1] >= box[1] - tol and ink[2] <= box[2] + tol and ink[3] <= box[3] + tol


def check_field(tid: str, spec: dict, label: dict, segs: list[dict], size) -> list[str]:
    name, p = spec["field"], []
    where = f"{tid}.{name}"
    by_role: dict[str, list[dict]] = {}
    for s in segs:
        by_role.setdefault(s["role"], []).append(s)
    written = label["written_value"]
    if written is None:
        if any(r in by_role for r in ("value", "correction", "struck", "check")):
            p.append(f"{where}: label says nothing written, but the log drew {sorted(by_role)}")
        if label["visible_value"] is not None:
            p.append(f"{where}: nothing written, yet visible_value is {label['visible_value']!r}")
        return p
    if not means_the_same(name, written, label["true_value"], spec.get("fmt")):
        p.append(f"{where}: written {written!r} doesn't mean the true value {label['true_value']!r}")
    if spec["ink"] == "check":
        checks = by_role.get("check", [])
        if len(checks) != 1 or checks[0]["text"] != label["visible_value"] or label["visible_value"] != label["true_value"]:
            p.append(f"{where}: check mark {[c['text'] for c in checks]} vs label {label['visible_value']!r}")
        legible = checks[0] if checks else None
    elif spec["ink"] in ("print", "stamp"):
        values = by_role.get("value", [])
        if len(values) != 1 or _glyph_text(values[0]) != written or label["visible_value"] != written:
            p.append(f"{where}: printed {[_glyph_text(v) for v in values]} vs label {label['visible_value']!r}")
        legible = values[0] if values else None
    else:
        role = "correction" if label["crossed_out"] else "value"
        drawn = by_role.get(role, [])
        if len(drawn) != 1:
            p.append(f"{where}: expected one {role} segment, the log has {len(drawn)}")
            return p
        legible = drawn[0]
        if _glyph_text(legible) != written:
            p.append(f"{where}: drew {_glyph_text(legible)!r} but the label says {written!r}")
        if label["crossed_out"]:
            struck = by_role.get("struck", [])
            if len(struck) != 1 or _glyph_text(struck[0]) != label["struck_value"] or "strike" not in by_role:
                p.append(f"{where}: crossed-out value doesn't match the log")
            if label["struck_value"] == written:
                p.append(f"{where}: the crossed-out value equals the correction")
        elif "struck" in by_role:
            p.append(f"{where}: log has a crossed-out value the label doesn't mention")
        drawn_amb = [(i, k.split("~")[1]) for i, k in enumerate(legible["glyphs"]) if "~" in k]
        label_amb = [(a["index"], a["confusable_with"]) for a in label["ambiguous_chars"]]
        if drawn_amb != label_amb:
            p.append(f"{where}: ambiguous glyphs drawn {drawn_amb} vs labelled {label_amb}")
        if any(written[a["index"]] != a["char"] for a in label["ambiguous_chars"]):
            p.append(f"{where}: ambiguous_chars name the wrong character")
        if label["smudged"] != ("smudge" in by_role):
            p.append(f"{where}: smudge in log and label disagree")
        expected_visible = None if label["smudged"] else written
        if label["visible_value"] != expected_visible:
            p.append(f"{where}: visible_value {label['visible_value']!r}, expected {expected_visible!r}")
    w, h = size
    for s in segs:
        if not s["ink"]:
            continue
        tol = 4.0 if s["role"] in ("strike", "smudge") else INK_TOLERANCE
        if not _inside(s["ink"], spec["box"], tol):
            p.append(f"{where}: {s['role']} ink {s['ink']} leaves its box {spec['box']}")
        if not _inside(s["ink"], [0, 0, w, h], 0):
            p.append(f"{where}: {s['role']} ink is clipped by the ticket edge")
    if legible is not None and label["ink_bbox"] != (None if not legible["ink"] else [round(v, 1) for v in legible["ink"]]):
        p.append(f"{where}: ink_bbox doesn't match the log")
    return p


def check_records(records_doc: dict) -> list[str]:
    p = []
    if records_doc.get("schema") != RECORDS_SCHEMA or records_doc.get("synthetic") is not True:
        p.append("records.json: wrong schema or not marked synthetic")
    rates = {(r["supplier"], r["material"]): r["unit_price"] for r in records_doc["rate_sheet"]}
    seen = set()
    for rec in records_doc["records"]:
        tid = rec["ticket_id"]
        p += [f"{tid}: {msg}" for msg in check_weights(rec)]
        key = (rec["supplier"], rec["ticket_number"])
        if key in seen:
            p.append(f"{tid}: ticket number {rec['ticket_number']} repeats for {rec['supplier']}")
        seen.add(key)
        try:
            if date.fromisoformat(rec["delivery_date"]) > date.today():
                p.append(f"{tid}: date in the future")
        except ValueError:
            p.append(f"{tid}: bad date {rec['delivery_date']!r}")
        tons = Decimal(rec["net_tons"])
        if tons <= 0 or tons.as_tuple().exponent < -3:
            p.append(f"{tid}: net tons {tons} isn't a positive number with at most 3 decimals")
        if rates.get((rec["supplier"], rec["material"])) != rec["unit_price"]:
            p.append(f"{tid}: unit price isn't the rate sheet's")
    return p


def check_labels(docs: dict, rerender: bool) -> list[str]:
    p = []
    labels, records = docs["labels"], {r["ticket_id"]: r for r in docs["records"]["records"]}
    if labels.get("schema") != LABELS_SCHEMA or labels.get("stamp") != STAMP:
        p.append("labels.json: wrong schema or stamp")
    log = docs["render_log"]["tickets"]
    tickets = labels["tickets"]
    if {t["ticket_id"] for t in tickets} != set(records) or set(log) != set(records):
        p.append("records, labels and render log don't cover the same tickets")
    seed = docs["manifest"]["seed"]
    for t in tickets:
        tid = t["ticket_id"]
        rec = records.get(tid)
        if rec is None:
            continue
        template = TEMPLATES[t["template"]]
        if t["difficulty"] not in PRESETS:
            p.append(f"{tid}: unknown difficulty {t['difficulty']!r}")
        if set(t["fields"]) != {s["field"] for s in template["fields"]}:
            p.append(f"{tid}: labelled fields aren't the template's")
            continue
        for spec in template["fields"]:
            label = t["fields"][spec["field"]]
            true = rec.get(ALIASES.get(spec["field"], spec["field"]))
            if label["true_value"] != true:
                p.append(f"{tid}.{spec['field']}: true_value {label['true_value']!r} isn't the record's {true!r}")
            drawn = None if (spec["field"] in t["plan"]["omit"] or spec["field"] in t["plan"]["missing"]) \
                else written_value(rec, spec, t["plan"])
            if label["written_value"] != drawn:
                p.append(f"{tid}.{spec['field']}: written_value {label['written_value']!r}, plan gives {drawn!r}")
            p += check_field(tid, spec, label, log.get(tid, {}).get(spec["field"], []), t["size"])
        if rerender:
            again = render_ticket(rec, t["plan"], seed)
            if again.log != log.get(tid):
                p.append(f"{tid}: re-rendering draws something different from the stored log")
            if {k: v for k, v in again.labels.items()} != t["fields"]:
                p.append(f"{tid}: re-rendering gives different labels")
    return p


def check_pages(docs: dict) -> list[str]:
    p = []
    pages = docs["pages"]
    if pages.get("schema") != PAGES_SCHEMA or pages.get("stamp") != STAMP:
        p.append("pages.json: wrong schema or stamp")
    w, h = pages["page_size"]
    page_box = (0, 0, w, h)
    tickets = {t["ticket_id"]: t for t in docs["labels"]["tickets"]}
    placed: dict[str, int] = {}
    for page in pages["pages"]:
        where = f"{page['file']} p{page['page']}"
        positions = [s["position"] for s in page["slots"]]
        if positions != list(range(4 if page["layout"] == "grid_2x2" else 1)):
            p.append(f"{where}: slot positions {positions} don't fit layout {page['layout']}")
        quads = []
        for slot in page["slots"]:
            tid = slot["ticket_id"]
            if tid is None:
                continue
            placed[tid] = placed.get(tid, 0) + 1
            t = tickets.get(tid)
            if t is None:
                p.append(f"{where}: unknown ticket {tid}")
                continue
            if t.get("placement") != {"file": page["file"], "page": page["page"], "position": slot["position"]}:
                p.append(f"{tid}: placement in labels.json doesn't match pages.json")
            m = tuple(slot["matrix"])
            tw, th = t["size"]
            quad = [g.apply(m, c) for c in g.box_quad((0, 0, tw, th))]
            if any(abs(a - b) > 0.05 for q, s in zip(quad, slot["quad"]) for a, b in zip(q, s)):
                p.append(f"{tid}: stored corners aren't where the matrix puts the ticket")
            if any(abs(a - b) > 0.05 for a, b in zip(g.bounds(quad), slot["bbox"])):
                p.append(f"{tid}: bbox isn't the corners' bounds")
            pm, page_m = tuple(slot["placement_matrix"]), tuple(page["scan"]["page_matrix"])
            # Stored to 6 decimals: allow for that rounding times ~1000 px of translation.
            if any(abs(a - b) > (0.01 if i in (2, 5) else 1e-4) for i, (a, b) in enumerate(zip(g.compose(page_m, pm), m))):
                p.append(f"{tid}: placement and page matrices don't compose to the slot's matrix")
            vf = g.visible_fraction(g.box_quad((0, 0, tw, th)), pm, page_m, page_box)
            if abs(vf - slot["visible_fraction"]) > 1e-3 or slot["fully_visible"] != (vf >= 0.999):
                p.append(f"{tid}: visible fraction {slot['visible_fraction']} vs recomputed {vf:.4f}")
            for name, info in slot["fields"].items():
                fq = [g.apply(m, c) for c in g.box_quad(t["fields"][name]["box"])]
                if abs(g.visible_fraction(g.box_quad(t["fields"][name]["box"]), pm, page_m, page_box)
                       - info["visible_fraction"]) > 1e-3:
                    p.append(f"{tid}.{name}: field visibility doesn't match its corners")
                if (name in CORE_FIELDS and info["visible_fraction"] < 0.999 and not slot["intentionally_clipped"]
                        and t["fields"][name]["written_value"] is not None):
                    p.append(f"{tid}.{name}: cut off the page, but no clip was intended")
            for other in quads:
                if not g.separated(quad, other, gap=0):
                    p.append(f"{tid}: overlaps another ticket on {where}")
            quads.append(quad)
    for tid in tickets:
        if placed.get(tid) != 1:
            p.append(f"{tid}: placed {placed.get(tid, 0)} times")
    return p


def check_files(root: Path, manifest: dict) -> list[str]:
    p = []
    actual = {str(f.relative_to(root)) for f in root.rglob("*")
              if f.is_file() and f.name != "dataset.json" and not f.name.startswith(".")}
    listed = set(manifest["files"])
    for name in sorted(listed - actual):
        p.append(f"{name}: listed in the manifest but missing")
    for name in sorted(actual - listed):
        p.append(f"{name}: present but not in the manifest")
    for name in sorted(listed & actual):
        if sha256_file(root / name) != manifest["files"][name]:
            p.append(f"{name}: contents changed since the dataset was built")
    return p


def check_determinism(docs: dict) -> list[str]:
    m = docs["manifest"]
    spec = {"name": m["name"], "seed": m["seed"], "difficulties": tuple(m["difficulties"]),
            "per_difficulty": m["per_difficulty"]}
    with tempfile.TemporaryDirectory() as tmp:
        again = build(Path(tmp) / "again", spec, formats=set(m["formats"]))
    if again["files"] == m["files"]:
        return []
    diff = sorted(k for k in set(again["files"]) | set(m["files"]) if again["files"].get(k) != m["files"].get(k))
    return [f"rebuilding with seed {m['seed']} changed {len(diff)} file(s): {diff[:6]}"]


def validate(path: Path, *, rerender: bool = True, determinism: bool = False) -> dict:
    try:
        docs = load(path)
    except (DatasetError, OSError, ValueError) as exc:
        return {"ok": False, "problems": [str(exc)], "checked": {}}
    checked, problems = {}, []
    for name, fn in (("files", lambda: check_files(docs["root"], docs["manifest"])),
                     ("records", lambda: check_records(docs["records"])),
                     ("labels_and_render_log", lambda: check_labels(docs, rerender)),
                     ("pages", lambda: check_pages(docs)),
                     ("truth_export", lambda: [] if export_truth(docs["labels"], docs["pages"], docs["records"])
                      == docs["truth"] else ["truth.json isn't what the labels and pages export to"])):
        found = fn()
        checked[name] = len(found)
        problems += found
    if determinism:
        found = check_determinism(docs)
        checked["determinism"] = len(found)
        problems += found
    return {"ok": not problems, "problems": problems, "checked": checked, "stamp": STAMP,
            "tickets": len(docs["labels"]["tickets"]), "pages": len(docs["pages"]["pages"])}
