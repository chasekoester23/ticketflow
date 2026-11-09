"""Run a synthetic dataset through TicketFlow's real intake, splitter, pipeline,
review and approval code, offline, on a fresh temporary database and storage
folder, and score it.

What runs for real: POST /api/batches (validation, storage, queueing), the job
queue and worker code, PDF rendering and the fixed 2x2 / single splitter, slot
detection, ticket creation, flags, PATCH /api/tickets (corrections, audit),
POST /api/tickets/{id}/verify (approval, duplicate and price snapshot rules),
and scripts/evaluate_workflow's scoring.

What is simulated: the extractor (see extractors.py) and the person reviewing
(they fix a wrong field with a seeded probability, and flag what they can't
approve). The report is stamped synthetic and names the extractor used.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from statistics import mean, median

from . import GENERATOR_VERSION, REPORT_SCHEMA, STAMP
from . import geometry as g
from .dataset import DatasetError, load
from .extractors import DESCRIPTIONS, SIX, NoisySimulatedExtractor, SlotInfo, crop_key, make_extractor
from .util import rng

COMPLETE = 0.995  # a crop holding at least this much of the visible ticket counts as complete
MAX_FILES_PER_BATCH = 10


class MixedEvidenceError(DatasetError):
    """Synthetic and real labels must never be scored into one report."""


def check_synthetic_only(docs: dict) -> None:
    truth = docs["truth"]
    root = Path(docs["root"]).resolve()
    if "real" in root.parts:
        raise MixedEvidenceError(f"{root} sits under a 'real' folder; synthetic evaluation won't read it.")
    if truth.get("synthetic") is not True or truth.get("label_source") != "synthetic":
        raise MixedEvidenceError("truth.json isn't marked synthetic; refusing to mix it into a synthetic report.")
    if any(slot.get("synthetic") is not True for slot in truth["slots"]):
        raise MixedEvidenceError("truth.json mixes slots not marked synthetic; synthetic and real labels "
                                 "can't share a report.")


@contextmanager
def ticketflow_sandbox(workdir: Path):
    """TicketFlow's FastAPI app on a brand-new SQLite file and storage folder.

    Every global the app uses for its database, storage, worker and rate limits
    is pointed at the sandbox and put back afterwards. Nothing here can reach
    another database: the engine is created from the sandbox path only.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db as db_module
    from app import main as main_module
    from app.auth import current_user
    from app.config import settings
    from app.db import enforce_references
    from app.models import Organization, User
    from app.routers import api as api_module

    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "synthetic-eval.db"
    if db_path.exists():
        raise DatasetError(f"{db_path} already exists; the evaluation only uses a fresh database.")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    enforce_references(engine)
    db_module.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as s:
        s.add(Organization(id=1, name="Synthetic Evaluation Co"))
        s.add(User(id=1, organization_id=1, username="synthetic-reviewer", display_name="Synthetic Reviewer",
                   password_hash="not-a-login", active=True, role="owner"))
        s.commit()
    saved = {
        "engine": main_module.engine, "SessionLocal": api_module.SessionLocal, "notify": api_module._notify_worker,
        "storage_dir": settings.storage_dir, "uploads": settings.rate_limit_uploads_per_minute,
        "overrides": dict(main_module.app.dependency_overrides),
    }
    main_module.engine = engine
    api_module.SessionLocal = Session
    api_module._notify_worker = lambda _background, _job_id: None  # the harness runs the worker itself
    settings.storage_dir = workdir / "storage"
    settings.rate_limit_uploads_per_minute = 0

    def get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    reviewer = User(id=1, organization_id=1, username="synthetic-reviewer", display_name="Synthetic Reviewer",
                    password_hash="not-a-login", active=True, role="owner", must_change_password=False)
    main_module.app.dependency_overrides[db_module.get_db] = get_db
    main_module.app.dependency_overrides[current_user] = lambda: reviewer
    try:
        with TestClient(main_module.app) as client:
            client.headers.update({"X-TicketFlow-Request": "1"})
            yield client, Session
    finally:
        main_module.engine = saved["engine"]
        api_module.SessionLocal = saved["SessionLocal"]
        api_module._notify_worker = saved["notify"]
        settings.storage_dir = saved["storage_dir"]
        settings.rate_limit_uploads_per_minute = saved["uploads"]
        main_module.app.dependency_overrides.clear()
        main_module.app.dependency_overrides.update(saved["overrides"])
        engine.dispose()


def _seed_rates(Session, rate_sheet: list[dict]) -> int:
    from app.models import Material, Project, Supplier, SupplierRate
    with Session() as s:
        s.add(Project(id=1, organization_id=1, code="SYN", name="Synthetic evaluation"))
        suppliers, materials = {}, {}
        for row in rate_sheet:
            if row["supplier"] not in suppliers:
                suppliers[row["supplier"]] = Supplier(organization_id=1, name=row["supplier"])
                s.add(suppliers[row["supplier"]])
            if row["material"] not in materials:
                materials[row["material"]] = Material(organization_id=1, name=row["material"])
                s.add(materials[row["material"]])
        s.flush()
        for row in rate_sheet:
            s.add(SupplierRate(organization_id=1, supplier_id=suppliers[row["supplier"]].id,
                               material_id=materials[row["material"]].id, unit_price=Decimal(row["unit_price"])))
        s.commit()
    return len(rate_sheet)


def index_crops(docs: dict) -> dict[str, SlotInfo]:
    """Cut every page with TicketFlow's own renderer and splitter, as the pipeline
    will, and remember which slot each crop is."""
    from app.config import settings
    from app.services import splitter
    tickets = {t["ticket_id"]: t for t in docs["labels"]["tickets"]}
    truth = {(s["file"], s["page"], s["position"]): s for s in docs["truth"]["slots"]}
    page_slots = {(p["file"], p["page"]): p for p in docs["pages"]["pages"]}
    scale = settings.render_dpi / docs["pages"]["scan_dpi"]
    out = {}
    for pdf in sorted((docs["root"] / "scans").glob("*.pdf")):
        layouts = {p["layout"] for p in docs["pages"]["pages"] if p["file"] == pdf.name}
        layout = layouts.pop()
        images = splitter.render_pdf_pages(pdf.read_bytes(), dpi=settings.render_dpi, max_pages=settings.max_pdf_pages,
                                           max_page_pixels=settings.max_rendered_page_pixels,
                                           max_total_pixels=settings.max_total_pdf_pixels)
        for page_no, image in enumerate(images, start=1):
            page = page_slots[(pdf.name, page_no)]
            for crop in splitter.split_page(image, layout):
                row = truth.get((pdf.name, page_no, crop.position), {})
                slot = next((s for s in page["slots"] if s["position"] == crop.position), None)
                ticket = tickets.get(row.get("ticket_id")) if row.get("ticket") else None
                out[crop_key(crop.image)] = SlotInfo(pdf.name, page_no, crop.position, crop.box, scale,
                                                     row.get("expected") if ticket else None, ticket,
                                                     slot if ticket else None)
    return out


def _drain(Session, extractor) -> None:
    from app.services import jobqueue
    with Session() as db:
        while True:
            claimed = jobqueue.claim_next(db)
            if claimed is None:
                return
            job_id, token = claimed
            jobqueue.execute_claimed(db, job_id, token, extractor_factory=lambda: extractor)


def upload_all(client, Session, docs: dict, extractor) -> tuple[dict, dict]:
    """One batch per difficulty and layout (a batch has one layout), as a person would."""
    groups = defaultdict(list)
    for p in docs["pages"]["pages"]:
        groups[(p["difficulty"], p["layout"])].append(p["file"])
    batches, timing = {}, Counter()
    for (difficulty, layout), names in sorted(groups.items()):
        names = sorted(set(names))
        for start in range(0, len(names), MAX_FILES_PER_BATCH):
            chunk = names[start:start + MAX_FILES_PER_BATCH]
            files = [("files", (n, (docs["root"] / "scans" / n).read_bytes(), "application/pdf")) for n in chunk]
            t0 = time.perf_counter()
            resp = client.post("/api/batches", data={"layout": layout, "intake_mode": "extract", "project_id": "1"},
                               files=files)
            timing["upload_seconds"] += time.perf_counter() - t0
            if resp.status_code != 200:
                raise DatasetError(f"TicketFlow refused the upload of {chunk}: {resp.status_code} {resp.text[:300]}")
            batches.setdefault(difficulty, []).append(resp.json()["id"])
            t0 = time.perf_counter()
            _drain(Session, extractor)
            timing["processing_seconds"] += time.perf_counter() - t0
    return batches, timing


def snapshot_detection(Session) -> set[tuple]:
    """(file name, page, position) of every ticket record the pipeline made by itself."""
    from app.models import Ticket, UploadedFile
    with Session() as db:
        return {(name, t.source_page, t.position) for t, name in db.query(Ticket, UploadedFile.original_name)
                .join(UploadedFile, UploadedFile.id == Ticket.file_id)}


def resolve_slots(client, Session, docs: dict, extractor) -> dict:
    """What a person does with the scan manifest: a slot the detector didn't treat
    as a ticket, but which holds one on paper, is marked as a ticket (one click
    each) and the batch retried so it is read; a faint slot that is really empty
    is confirmed blank. Uses the real endpoints."""
    from app.models import ScanSlot, Ticket, UploadedFile
    truth = {(s["file"], s["page"], s["position"]): s for s in docs["truth"]["slots"]}
    stats, touched = Counter(), set()
    with Session() as db:
        have = {(t.file_id, t.source_page, t.position) for t in db.query(Ticket)}
        work = [(s.id, s.batch_id, name, s.page, s.position, s.detection)
                for s, name in db.query(ScanSlot, UploadedFile.original_name)
                .join(UploadedFile, UploadedFile.id == ScanSlot.file_id).order_by(ScanSlot.id)
                if (s.file_id, s.page, s.position) not in have]
    for slot_id, batch_id, name, page, position, detection in work:
        label = truth.get((name, page, position), {})
        if label.get("ticket"):
            resp = client.post(f"/api/scan-slots/{slot_id}/mark-ticket")
            stats[f"marked_ticket_{detection}" if resp.status_code == 200 else f"mark_ticket_{resp.status_code}"] += 1
            touched.add(batch_id)
        elif detection == "faint":
            resp = client.post(f"/api/scan-slots/{slot_id}/confirm-blank")
            stats["confirmed_blank" if resp.status_code == 200 else f"confirm_blank_{resp.status_code}"] += 1
    for batch_id in sorted(touched):
        resp = client.post(f"/api/batches/{batch_id}/retry")
        stats["batches_retried" if resp.status_code == 200 else f"retry_{resp.status_code}"] += 1
        _drain(Session, extractor)
    return dict(stats)


def review_all(client, Session, docs: dict, catch_rate: float, seed: int) -> dict:
    """A simulated reviewer working the queue through the real endpoints: compares
    each ticket with the paper (the label), fixes a wrong field with probability
    `catch_rate`, approves what can be approved and flags the rest."""
    from app.models import Ticket, UploadedFile
    from app.services.evaluation import _field_verdict
    truth = {(s["file"], s["page"], s["position"]): s for s in docs["truth"]["slots"]}
    stats = Counter()
    with Session() as db:
        rows = db.query(Ticket, UploadedFile.original_name).join(UploadedFile, UploadedFile.id == Ticket.file_id) \
            .filter(Ticket.status.in_(("needs_review", "failed", "flagged"))).order_by(Ticket.id).all()
        work = [(t.id, name, t.source_page, t.position, {f: getattr(t, f) for f in SIX}) for t, name in rows]
    for ticket_id, name, page, position, current in work:
        label = truth.get((name, page, position))
        if not label or not label.get("ticket"):
            # Not a ticket on paper (a fragment of a neighbour, or an empty slot): skip it.
            stats["skipped_not_a_ticket"] += 1
            client.post(f"/api/tickets/{ticket_id}/skip")
            continue
        expected = label["expected"]
        patch = {}
        for f in SIX:
            if _field_verdict(f, expected[f], None if current[f] is None else str(current[f])) == "correct":
                continue
            stats["fields_wrong_before_review"] += 1
            if rng("review", seed, name, page, position, f).random() < catch_rate:
                patch[f] = expected[f]
            else:
                stats["fields_missed_by_reviewer"] += 1
        if patch:
            resp = client.patch(f"/api/tickets/{ticket_id}", json=patch)
            stats["patch_ok" if resp.status_code == 200 else f"patch_{resp.status_code}"] += 1
            stats["fields_corrected"] += len(patch)
        resp = client.post(f"/api/tickets/{ticket_id}/verify")
        if resp.status_code == 200:
            stats["approved"] += 1
        else:
            stats[f"not_approved_{resp.status_code}"] += 1
            client.post(f"/api/tickets/{ticket_id}/flag", json={"note": "Synthetic reviewer: can't approve from the paper"})
            stats["flagged"] += 1
    return dict(stats)


def crop_measurements(Session, docs: dict, automatic: set) -> list[dict]:
    """How much of each true ticket the crop TicketFlow actually made contains, and
    whether the pipeline made a record for the slot by itself (`automatic`)."""
    from app.config import settings
    from app.models import ScanSlot, UploadedFile
    scale = settings.render_dpi / docs["pages"]["scan_dpi"]
    w, h = docs["pages"]["page_size"]
    page_box = (0, 0, w, h)
    with Session() as db:
        names = {f.id: f.original_name for f in db.query(UploadedFile)}
        slots = {(names[s.file_id], s.page, s.position): s for s in db.query(ScanSlot)}
        crops = {k: (tuple(v / scale for v in s.crop_box), s.detection) for k, s in slots.items()}
    out = []
    for page in docs["pages"]["pages"]:
        quads = {s["position"]: [tuple(p) for p in s["quad"]] for s in page["slots"] if s["ticket_id"]}
        for slot in page["slots"]:
            key = (page["file"], page["page"], slot["position"])
            crop, detection = crops.get(key, (None, None))
            row = {"file": page["file"], "page": page["page"], "position": slot["position"],
                   "difficulty": page["difficulty"], "layout": page["layout"], "ticket_id": slot["ticket_id"],
                   "detection": detection, "record": key in automatic}
            if crop is not None:
                crop_area = (crop[2] - crop[0]) * (crop[3] - crop[1])
                others = sum(g.area(g.clip_to_box(q, crop)) for p, q in quads.items() if p != slot["position"])
                row["intrusion"] = round(others / crop_area, 4)  # share of this crop showing other tickets
            if slot["ticket_id"]:
                quad = quads[slot["position"]]
                on_page = g.clip_to_box(quad, page_box)
                visible = g.area(on_page)
                inside = g.area(g.clip_to_box(on_page, crop)) if crop else 0.0
                coverage = inside / visible if visible else 0.0
                crop_area = (crop[2] - crop[0]) * (crop[3] - crop[1]) if crop else 0.0
                row.update(coverage=round(coverage, 4), complete=coverage >= COMPLETE,
                           iou=round(inside / (visible + crop_area - inside), 4) if crop else 0.0,
                           fields_cut=sorted(
                               name for name in ("ticket_number", "delivery_date", "material", "net_tons",
                                                 "supplier", "job_location")
                               if crop and g.inside_fraction([tuple(p) for p in slot["fields"][name]["quad"]], crop) < 0.98
                               and slot["fields"][name]["visible_fraction"] >= 0.95),
                           rotation_deg=slot["rotation_deg"], visible_fraction=slot["visible_fraction"])
            out.append(row)
    return out


def _summarise_crops(rows: list[dict]) -> dict:
    tickets = [r for r in rows if r["ticket_id"]]
    blanks = [r for r in rows if not r["ticket_id"]]
    cov = [r["coverage"] for r in tickets]
    out = {"tickets": len(tickets), "complete": sum(r["complete"] for r in tickets),
           "completeness_rate": round(sum(r["complete"] for r in tickets) / len(tickets), 4) if tickets else None,
           "mean_coverage": round(mean(cov), 4) if cov else None, "median_coverage": round(median(cov), 4) if cov else None,
           "min_coverage": round(min(cov), 4) if cov else None,
           "mean_iou": round(mean(r["iou"] for r in tickets), 4) if tickets else None,
           "tickets_with_a_core_field_cut": sum(1 for r in tickets if r["fields_cut"]),
           "core_fields_cut": dict(Counter(f for r in tickets for f in r["fields_cut"])),
           "detected_automatically": sum(1 for r in tickets if r["record"]),
           "automatic_detection_rate": round(sum(1 for r in tickets if r["record"]) / len(tickets), 4) if tickets else None,
           "tickets_left_to_a_person": sum(1 for r in tickets if not r["record"]),
           "ticket_slots_by_detection": dict(Counter(str(r["detection"]) for r in tickets)),
           "crops_showing_another_ticket_over_1pct": sum(1 for r in rows if r.get("intrusion", 0) > 0.01),
           "blank_slots": len(blanks), "blank_slots_with_a_record": sum(1 for r in blanks if r["record"]),
           "blank_slots_detected_as_content": sum(1 for r in blanks if r["detection"] == "content")}
    return out


def financial_check(Session, docs: dict) -> dict:
    """Approved cost against the true tons times the fictional rate sheet."""
    from app.models import Ticket, UploadedFile
    from app.services.pricing import to_cents
    rates = {(r["supplier"], r["material"]): Decimal(r["unit_price"]) for r in docs["records"]["rate_sheet"]}
    records = {r["ticket_id"]: r for r in docs["records"]["records"]}
    truth = {(s["file"], s["page"], s["position"]): s for s in docs["truth"]["slots"] if s.get("ticket")}
    out = Counter()
    approved_total = expected_total = unpriced_total = Decimal(0)
    with Session() as db:
        for t, name in db.query(Ticket, UploadedFile.original_name).join(UploadedFile, UploadedFile.id == Ticket.file_id) \
                .filter(Ticket.status == "verified"):
            label = truth.get((name, t.source_page, t.position))
            if label is None:
                out["approved_without_a_ticket_on_paper"] += 1
                continue
            rec = records[label["ticket_id"]]
            should = to_cents(Decimal(rec["net_tons"]) * rates[(rec["supplier"], rec["material"])])
            out["approved_tickets"] += 1
            if t.approved_cost is None:
                out["approved_unpriced"] += 1  # e.g. a misread supplier or material matched no rate
                unpriced_total += should
                continue
            approved_total += t.approved_cost
            expected_total += should
            out["cost_correct" if t.approved_cost == should else "cost_wrong"] += 1
    return {**{k: out[k] for k in ("approved_tickets", "cost_correct", "cost_wrong", "approved_unpriced",
                                   "approved_without_a_ticket_on_paper")},
            "approved_cost_total": str(approved_total), "true_cost_of_those_tickets": str(expected_total),
            "difference": str(approved_total - expected_total), "true_cost_of_unpriced_approvals": str(unpriced_total)}


def _headline(wf: dict) -> dict:
    ext, fin = wf["extraction"], wf["final"]
    corr = wf["corrections"]
    return {
        "detection_rate": wf["detection"]["detection_rate"], "missed_tickets": wf["detection"]["missed"],
        "extraction_all_six_rate": ext["all_six_rate"],
        "extraction_field_accuracy": {f: v["accuracy"] for f, v in ext["fields"].items()},
        "critical_errors": wf["critical_errors"],
        "correction_rate": round(corr["tickets_corrected"] / corr["approved_tickets_with_extraction"], 4)
        if corr["approved_tickets_with_extraction"] else None,
        "final_approved_all_six_rate": fin["all_six_rate"],
        "final_field_accuracy": {f: v["accuracy"] for f, v in fin["fields"].items()},
        "approved": fin["tickets_approved_and_scored"], "not_yet_approved": fin["not_yet_approved"],
        "processing_failures": wf["processing"],
    }


def evaluate(dataset: Path, *, extractor_name: str = "oracle", workdir: Path | None = None, catch_rate: float = 0.9,
             seed: int = 0, error_model: dict | None = None, authorize_paid: bool = False) -> dict:
    docs = load(dataset)
    check_synthetic_only(docs)
    if not (docs["root"] / "scans").is_dir():
        raise DatasetError("This dataset has no scans/ folder; build it with PDFs (--formats pdf).")
    own_tmp = None
    if workdir is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="tf-synthetic-eval-")
        workdir = Path(own_tmp.name)
    started = time.perf_counter()
    try:
        from app.services.workflow_evaluation import evaluate_workflow, load_truth
        with ticketflow_sandbox(Path(workdir)) as (client, Session):
            _seed_rates(Session, docs["records"]["rate_sheet"])
            t0 = time.perf_counter()
            index = index_crops(docs)
            index_seconds = time.perf_counter() - t0
            extractor = make_extractor(extractor_name, index, seed=seed, error_model=error_model,
                                       authorize_paid=authorize_paid)
            batches, timing = upload_all(client, Session, docs, extractor)
            automatic = snapshot_detection(Session)
            crops = crop_measurements(Session, docs, automatic)
            t0 = time.perf_counter()
            manifest = resolve_slots(client, Session, docs, extractor)
            timing["manifest_seconds"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            review = review_all(client, Session, docs, catch_rate, seed)
            timing["review_seconds"] = time.perf_counter() - t0
            finance = financial_check(Session, docs)
            truth_path = Path(workdir) / "truth.json"
            truth_path.write_text((docs["root"] / "truth.json").read_text(encoding="utf-8"), encoding="utf-8")
            truth = load_truth(truth_path)
            by_difficulty, workflow = {}, {}
            files_of = defaultdict(set)
            for p in docs["pages"]["pages"]:
                files_of[p["difficulty"]].add(p["file"])
            with Session() as db:
                for difficulty, ids in sorted(batches.items()):
                    subset = dict(truth, slots=[s for s in truth["slots"] if s.file in files_of[difficulty]])
                    wf = evaluate_workflow(db, 1, ids, subset)
                    workflow[difficulty] = wf
                    by_difficulty[difficulty] = {**_headline(wf),
                                                 "crop": _summarise_crops([c for c in crops if c["difficulty"] == difficulty])}
                overall_wf = evaluate_workflow(db, 1, [i for ids in batches.values() for i in ids], truth)
    finally:
        if own_tmp is not None:
            own_tmp.cleanup()
    total = time.perf_counter() - started
    tickets = len(docs["labels"]["tickets"])
    pages = len(docs["pages"]["pages"])
    report = {
        "schema": REPORT_SCHEMA, "stamp": STAMP, "synthetic": True,
        "extractor": {"name": extractor_name, "what_this_measures": DESCRIPTIONS[extractor_name],
                      "calls": getattr(extractor, "calls", None),
                      "unrecognised_crops": getattr(extractor, "unrecognised", None)},
        "dataset": {"name": docs["manifest"]["name"], "seed": docs["manifest"]["seed"],
                    "generator_version": GENERATOR_VERSION, "tickets": tickets, "pages": pages,
                    "files": len(list((docs["root"] / "scans").glob("*.pdf")))},
        "reviewer": {"catch_rate": catch_rate, "seed": seed, "scan_manifest": manifest, **review},
        "overall": {**_headline(overall_wf), "crop": _summarise_crops(crops), "financial": finance},
        "by_difficulty": by_difficulty,
        "timing": {"total_seconds": round(total, 2), "crop_index_seconds": round(index_seconds, 2),
                   **{k: round(v, 2) for k, v in timing.items()},
                   "processing_seconds_per_page": round(timing["processing_seconds"] / pages, 3) if pages else None,
                   "end_to_end_seconds_per_ticket": round(total / tickets, 3) if tickets else None},
        "crops": crops,
        "workflow_evaluation": {"overall": overall_wf, "by_difficulty": workflow},
        "notes": [
            STAMP + ".",
            f"Extractor '{extractor_name}': {DESCRIPTIONS[extractor_name]}",
            "Crop completeness and detection come from TicketFlow's real splitter on synthetic scans: these are "
            "genuine measurements of the splitter's geometry, on fictional page layouts.",
            "The reviewer is simulated; correction and approval figures test the workflow code, not people.",
        ],
    }
    if isinstance(extractor, NoisySimulatedExtractor):
        report["extractor"]["error_model"] = extractor.model
        report["extractor"]["planted_errors"] = dict(Counter(p.kind for p in extractor.planted.values()))
        report["planted"] = [vars(p) for p in sorted(extractor.planted.values(), key=lambda p: (p.ticket_id, p.field))]
    if extractor_name == "oracle-crop-aware":
        report["extractor"]["fields_hidden_by_crop"] = extractor.hidden
    return report


def report_markdown(report: dict) -> str:
    o = report["overall"]
    lines = [f"# Synthetic evaluation: {report['dataset']['name']}", "", f"**{STAMP}**", "",
             f"Extractor: `{report['extractor']['name']}`: {report['extractor']['what_this_measures']}", "",
             f"{report['dataset']['tickets']} tickets on {report['dataset']['pages']} pages; "
             f"{report['timing']['total_seconds']} s end to end.", "",
             "| Difficulty | Crop complete | Mean coverage | Min coverage | Core field cut | Blank slots with a record "
             "| Auto-detected | Detected after manifest | Extraction all-six | Correction rate | Approved all-six |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d, v in list(report["by_difficulty"].items()) + [("overall", o)]:
        c = v["crop"]
        lines.append(f"| {d} | {c['complete']}/{c['tickets']} ({c['completeness_rate']}) | {c['mean_coverage']} | "
                     f"{c['min_coverage']} | {c['tickets_with_a_core_field_cut']} | {c['blank_slots_with_a_record']}/"
                     f"{c['blank_slots']} | {c['automatic_detection_rate']} | {v['detection_rate']} | {v['extraction_all_six_rate']} | "
                     f"{v['correction_rate']} | {v['final_approved_all_six_rate']} |")
    f = o["financial"]
    lines += ["", f"Financial: {f['cost_correct']} approved costs right, {f['cost_wrong']} wrong, "
              f"{f['approved_unpriced']} unpriced; difference {f['difference']}.", ""]
    lines += [f"- {n}" for n in report["notes"]]
    return "\n".join(lines) + "\n"


def prepare_environment(workdir: Path) -> None:
    """Called by the CLI BEFORE anything imports `app`: the app's own settings
    then point at the sandbox too, so even an unexpected use of the default
    engine lands in a temporary file, never a real database. Once the app is
    imported (inside a test run, say) its settings are fixed and the sandbox
    patches them instead, so this changes nothing."""
    if "app" in sys.modules:
        return
    os.environ["DATABASE_URL"] = f"sqlite:///{workdir / 'app-default.db'}"
    os.environ["STORAGE_DIR"] = str(workdir / "storage")
    os.environ.setdefault("TICKETFLOW_ENV", "development")
