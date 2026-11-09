"""Build a synthetic dataset on disk: records, labels, render log, scanned pages,
PDFs, and ground truth in the format scripts/evaluate_workflow.py reads.

Layout of a dataset folder:
  dataset.json      manifest: schema, stamp, seed, preset, counts, sha256 of every file
  records.json/csv  the structured records (source of truth) and the fictional rate sheet
  labels.json       per ticket: plan, and per field true/written/visible values and effects
  render_log.json   per ticket and field: the glyphs the renderer actually drew, and where
  pages.json        per page: which ticket is in which slot, its corners, rotation, visibility
  truth.json        evaluate_workflow ground truth (synthetic: true)
  scans/*.pdf       multi-page scans, as uploaded to TicketFlow
  pages/*.png       the same pages as PNG (optional)
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import (DATASET_SCHEMA, GENERATOR_VERSION, LABELS_SCHEMA, PAGES_SCHEMA, STAMP)
from .presets import DATASETS, PRESETS, make_plan, stratified_templates
from .records import SUPPLIERS, generate_records, records_csv, records_document
from .render import render_ticket
from .scan import PAGE_SIZE, SCAN_DPI, Placement, compose_page, write_pdf
from .templates import CORE_FIELDS, TEMPLATES, TICKETFLOW_FIELD
from .util import read_json, rng, sha256_file, write_json

SUPPLIER_FOR_TEMPLATE = {s["template"]: s["id"] for s in SUPPLIERS}
PAGES_PER_FILE = 10
CLIPPED_FIELD = 0.95  # a field with less than this much of its box on the page can't be read


class DatasetError(ValueError):
    pass


def spec_for(name: str | None, difficulty: str | None, seed: int | None, count: int | None) -> dict:
    """Resolve a named dataset or a one-difficulty custom set."""
    if name in DATASETS:
        spec = dict(DATASETS[name])
        if seed is not None:
            spec["seed"] = seed
        if count is not None:
            spec["per_difficulty"] = count
        return {"name": name, **spec}
    if difficulty not in PRESETS:
        raise DatasetError(f"Pick --set {sorted(DATASETS)} or --difficulty {sorted(PRESETS)}")
    return {"name": name or f"custom-{difficulty}", "seed": 1 if seed is None else seed,
            "difficulties": (difficulty,), "per_difficulty": count or 12}


def build_records(spec: dict) -> tuple[list[dict], dict[str, str]]:
    suppliers, difficulty_of = [], {}
    for difficulty in spec["difficulties"]:
        for template in stratified_templates(spec["seed"], difficulty, spec["per_difficulty"]):
            suppliers.append(SUPPLIER_FOR_TEMPLATE[template])
    records = generate_records(spec["seed"], len(suppliers), suppliers=suppliers)
    i = 0
    for difficulty in spec["difficulties"]:
        for _ in range(spec["per_difficulty"]):
            difficulty_of[records[i]["ticket_id"]] = difficulty
            i += 1
    return records, difficulty_of


def render_all(records: list[dict], difficulty_of: dict, seed: int):
    tickets, renders = [], {}
    for rec in records:
        difficulty = difficulty_of[rec["ticket_id"]]
        plan = make_plan(rec, difficulty, seed)
        tr = render_ticket(rec, plan, seed)
        renders[rec["ticket_id"]] = tr
        tickets.append({"ticket_id": rec["ticket_id"], "difficulty": difficulty, "template": rec["template"],
                        "kind": TEMPLATES[rec["template"]]["kind"], "style": plan["style"], "plan": plan,
                        "size": list(tr.image.size), "fields": tr.labels})
    return tickets, renders


def page_plan(tickets: list[dict], spec: dict) -> list[dict]:
    """Which tickets share a page, in which slot, and which pages are 1-up."""
    pages = []
    for difficulty in spec["difficulties"]:
        p = PRESETS[difficulty]
        r = rng("pages", spec["seed"], difficulty)
        mine = [t for t in tickets if t["difficulty"] == difficulty]
        singles, grid = [], []
        for t in mine:
            long_form = TEMPLATES[t["template"]]["size"][1] > 900
            if (long_form and not p["long_on_grid"]) or r.random() < p["single_fraction"]:
                singles.append(t["ticket_id"])
            else:
                grid.append(t["ticket_id"])
        long_ids = {t["ticket_id"] for t in mine if TEMPLATES[t["template"]]["size"][1] > 900}
        while grid:
            slots, kept_free = [], set()
            for position in range(4):
                if position in kept_free or not grid or r.random() < p["blank_slot"]:
                    slots.append(None)
                    continue
                pick = grid[0]
                if pick in long_ids and position >= 2:
                    # A long ticket only fits the top row, with the slot under it left free.
                    shorter = [tid for tid in grid if tid not in long_ids]
                    if not shorter:
                        slots.append(None)
                        continue
                    pick = shorter[0]
                grid.remove(pick)
                slots.append(pick)
                if pick in long_ids:
                    kept_free.add(position + 2)
            if any(slots):
                pages.append({"difficulty": difficulty, "layout": "grid_2x2", "slots": slots})
        for tid in singles:
            pages.append({"difficulty": difficulty, "layout": "single", "slots": [tid]})
    # Group pages into files of one difficulty and layout (a TicketFlow batch has one layout).
    counters: dict[tuple, int] = {}
    for page in pages:
        key = (page["difficulty"], page["layout"])
        n = counters.get(key, 0)
        counters[key] = n + 1
        tag = "4up" if page["layout"] == "grid_2x2" else "1up"
        page["file"] = f"{spec['name']}-{page['difficulty']}-{tag}-{n // PAGES_PER_FILE + 1:02d}.pdf"
        page["page"] = n % PAGES_PER_FILE + 1
    return pages


def assemble(out: Path, spec: dict, tickets: list[dict], renders: dict, formats: set[str]) -> list[dict]:
    by_id = {t["ticket_id"]: t for t in tickets}
    pages_out, jpegs_by_file = [], {}
    for page in page_plan(tickets, spec):
        placements = []
        for tid in page["slots"]:
            if tid is None:
                placements.append(None)
                continue
            t = by_id[tid]
            boxes = {name: f["box"] for name, f in t["fields"].items()}
            placements.append(Placement(tid, renders[tid].image, boxes, bool(TEMPLATES[t["template"]].get("landscape"))))
        stem = Path(page["file"]).stem
        key = f"{stem}-p{page['page']}"
        image, slots, info = compose_page(placements, page["layout"], PRESETS[page["difficulty"]]["scan"],
                                          spec["seed"], key)
        jpegs_by_file.setdefault(page["file"], []).append(info.pop("_jpeg"))
        png = None
        if "png" in formats:
            png = f"pages/{key}.png"
            (out / "pages").mkdir(exist_ok=True)
            image.save(out / png, optimize=False, compress_level=6)
        for slot in slots:
            if slot["ticket_id"]:
                by_id[slot["ticket_id"]]["placement"] = {"file": page["file"], "page": page["page"],
                                                         "position": slot["position"]}
        pages_out.append({"file": page["file"], "page": page["page"], "difficulty": page["difficulty"],
                          "layout": page["layout"], "png": png, "slots": slots, "scan": info})
    if "pdf" in formats:
        (out / "scans").mkdir(exist_ok=True)
        for name, jpegs in jpegs_by_file.items():
            write_pdf(out / "scans" / name, jpegs)
    return pages_out


def paper_value(field: dict, page_visibility: float | None):
    """What a careful person reading the paper would write down for a field: the
    meaning, unless nothing legible is there or it's cut off the scan."""
    if field["visible_value"] is None:
        return None
    if page_visibility is not None and page_visibility < CLIPPED_FIELD:
        return None
    return field["true_value"]


def expected_six(ticket: dict, slot: dict) -> dict:
    out = {}
    for ours, theirs in TICKETFLOW_FIELD.items():
        f = ticket["fields"][ours]
        vis = slot["fields"].get(ours, {}).get("visible_fraction") if slot else None
        out[theirs] = paper_value(f, vis)
    return out


def export_truth(labels: dict, pages: dict, records: dict) -> dict:
    """Ground truth in evaluate_workflow's format, extended compatibly with
    synthetic-only keys that it ignores."""
    tickets = {t["ticket_id"]: t for t in labels["tickets"]}
    slots = []
    for page in pages["pages"]:
        for slot in page["slots"]:
            row = {"file": page["file"], "page": page["page"], "position": slot["position"], "synthetic": True,
                   "difficulty": page["difficulty"], "layout": page["layout"]}
            if slot["ticket_id"] is None:
                row["ticket"] = False
            else:
                t = tickets[slot["ticket_id"]]
                row.update(ticket=True, ticket_id=t["ticket_id"], template=t["template"], style=t["style"],
                           expected=expected_six(t, slot), visible_fraction=slot["visible_fraction"],
                           fully_visible=slot["fully_visible"])
            slots.append(row)
    return {"labelled_by": f"ticketflow synthetic generator {GENERATOR_VERSION}", "labelled_independently": True,
            "synthetic": True, "label_source": "synthetic", "stamp": STAMP, "dataset": labels["dataset"],
            "seed": labels["seed"], "slots": slots, "invoice_checks": [],
            "rate_sheet": records["rate_sheet"]}


def build(out: Path, spec: dict, *, formats: set[str] = frozenset({"pdf", "png"}), overwrite: bool = False) -> dict:
    final = Path(out)
    if final.exists() and any(final.iterdir()):
        if not overwrite:
            raise DatasetError(f"{final} isn't empty. Pass --overwrite to replace it.")
        if not (final / "dataset.json").is_file():
            raise DatasetError(f"{final} has files but isn't a synthetic dataset; refusing to delete it.")
    # Built beside the target and moved in at the end, so a failed build never
    # leaves a half-written dataset that looks complete.
    out = final.with_name(final.name + ".partial")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    _build_into(out, spec, formats)
    if final.exists():
        shutil.rmtree(final)
    out.rename(final)
    return read_json(final / "dataset.json")


def _build_into(out: Path, spec: dict, formats) -> None:
    records, difficulty_of = build_records(spec)
    tickets, renders = render_all(records, difficulty_of, spec["seed"])
    pages = assemble(out, spec, tickets, renders, set(formats))
    common = {"synthetic": True, "stamp": STAMP, "dataset": spec["name"], "seed": spec["seed"]}
    records_doc = records_document(spec["seed"], records)
    labels = {"schema": LABELS_SCHEMA, **common, "tickets": tickets}
    pages_doc = {"schema": PAGES_SCHEMA, **common, "scan_dpi": SCAN_DPI, "page_size": list(PAGE_SIZE),
                 "pages": pages}
    write_json(out / "records.json", records_doc)
    (out / "records.csv").write_text(records_csv(records), encoding="utf-8")
    write_json(out / "labels.json", labels)
    write_json(out / "render_log.json", {"synthetic": True, "stamp": STAMP,
                                         "tickets": {tid: tr.log for tid, tr in sorted(renders.items())}}, compact=True)
    write_json(out / "pages.json", pages_doc, compact=True)
    write_json(out / "truth.json", export_truth(labels, pages_doc, records_doc))
    write_manifest(out, spec, formats, counts={
        "tickets": len(tickets), "pages": len(pages),
        "blank_slots": sum(1 for p in pages for s in p["slots"] if s["ticket_id"] is None),
        "by_difficulty": {d: sum(1 for t in tickets if t["difficulty"] == d) for d in spec["difficulties"]}})


def write_manifest(out: Path, spec: dict, formats, counts: dict) -> None:
    files = {str(p.relative_to(out)): sha256_file(p) for p in sorted(out.rglob("*"))
             if p.is_file() and p.name != "dataset.json" and not p.name.startswith(".")}
    write_json(out / "dataset.json", {
        "schema": DATASET_SCHEMA, "synthetic": True, "stamp": STAMP, "generator_version": GENERATOR_VERSION,
        "name": spec["name"], "seed": spec["seed"], "difficulties": list(spec["difficulties"]),
        "per_difficulty": spec["per_difficulty"], "formats": sorted(formats), "counts": counts,
        "core_fields": list(CORE_FIELDS), "files": files})


def load(path: Path) -> dict:
    """A dataset folder's documents. Refuses anything not marked synthetic."""
    path = Path(path)
    manifest_path = path / "dataset.json"
    if not manifest_path.is_file():
        raise DatasetError(f"{path} has no dataset.json; not a synthetic dataset.")
    manifest = read_json(manifest_path)
    if manifest.get("synthetic") is not True or manifest.get("stamp") != STAMP:
        raise DatasetError(f"{path} isn't marked as a synthetic dataset.")
    docs = {"root": path, "manifest": manifest}
    for name in ("records", "labels", "pages", "truth", "render_log"):
        docs[name] = read_json(path / f"{name}.json")
    return docs
