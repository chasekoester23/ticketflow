import hashlib
import json
import uuid
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import AuditEvent, Batch, InvoiceTicketLink, Project, ScanEvent, ScanSlot, Ticket, UploadedFile, User, as_utc
from ..permissions import has_permission, require
from ..rate_limit import limited
from ..schemas import BatchOut, BatchQueueRow, FlagIn, TicketOut, TicketPatch, VerifyIn
from ..services import export as export_svc
from ..services import duplicates as dup_svc
from ..services import pipeline, jobqueue, scan_manifest, storage
from ..services.locks import take_turn
from ..services import pricing as pricing_svc
from ..services.accounts import names_by_id
from ..services.pricing import norm
from ..services.extraction import ExtractionError, get_extractor
from ..services.splitter import CROP_CHECK_PREFIX
from ..services.validation import compute_flags

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
MAGIC = {b"%PDF": ".pdf", b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png"}
EDITABLE = ["ticket_number", "delivery_date", "material_type", "tonnage", "supplier", "job_location"]


def _sniff_ok(data: bytes, ext: str) -> bool:
    """Check real content, not just the filename."""
    for magic, kind in MAGIC.items():
        if data.startswith(magic):
            return kind == ext or (kind == ".jpg" and ext == ".jpeg")
    return False


def _notify_worker(_background: BackgroundTasks, _job_id: int) -> None:
    """The dedicated CLI worker polls the persisted queue independently.

    Deliberately NO FastAPI BackgroundTasks extraction here: the web request
    must not execute expensive AI work or depend on the server staying alive.
    """


def _drain_job_for_tests() -> None:
    """Test-only adapter used by the integration fixture.

    Synthetic tests retain synchronous TestClient behavior without enabling
    in-process extraction in any production request handler.
    """
    with SessionLocal() as db:
        claimed = jobqueue.claim_next(db)
        if claimed is None:
            return
        job_id, token = claimed
        jobqueue.execute_claimed(db, job_id, token, extractor_factory=get_extractor)


@router.post("/batches", response_model=BatchOut, dependencies=[Depends(limited("uploads"))])
def create_batch(
    background: BackgroundTasks,
    layout: str = Form("single"),
    intake_mode: str = Form("extract"),
    project_id: int | None = Form(None),
    expected_ticket_count: int | None = Form(None, ge=0, le=100_000),
    files: list[UploadFile] = File(...),
    user: User = Depends(require("tickets.upload")),
    db: Session = Depends(get_db),
):
    # Not async: reading and storing files here would otherwise stop every other request.
    organization_id = user.organization_id
    if layout not in ("single", "grid_2x2"):
        raise HTTPException(422, "layout must be 'single' or 'grid_2x2'")
    if intake_mode not in ("extract", "manual"):
        raise HTTPException(422, "intake_mode must be 'extract' or 'manual'")
    if project_id is not None:
        project = db.scalar(select(Project).where(
            Project.id == project_id, Project.organization_id == organization_id
        ))
        if not project:
            raise HTTPException(404, "Project not found")
        if project.status != "active":
            raise HTTPException(409, "Project is archived; unarchive it to upload more tickets")
    if len(files) > settings.max_batch_files:
        raise HTTPException(
            413,
            f"Too many files in one batch (limit: {settings.max_batch_files}). "
            "Split the upload into smaller batches.",
        )
    # Validate the entire selection BEFORE creating a batch or storing any
    # files. Silently dropping an invalid file is dangerous: a contractor could
    # believe every delivery ticket in the scan was recorded.
    validated: list[tuple[UploadFile, str, str, str]] = []
    rejected: list[str] = []
    total_bytes = 0
    for up in files:
        original_name = Path(up.filename or "").name or "(unnamed file)"
        ext = Path(original_name).suffix.lower()
        data = up.file.read(settings.max_upload_bytes + 1)
        total_bytes += len(data)
        if len(original_name) > 255:
            rejected.append("A file name exceeds 255 characters; rename the scan before uploading")
        elif ext not in ALLOWED_EXT:
            rejected.append(f"{original_name}: unsupported file type (PDF, JPG, PNG only)")
        elif not data:
            rejected.append(f"{original_name}: file is empty")
        elif len(data) > settings.max_upload_bytes:
            rejected.append(f"{original_name}: exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB file limit")
        elif not _sniff_ok(data, ext):
            rejected.append(f"{original_name}: content does not match its file extension")
        else:
            validated.append((up, original_name, ext, pipeline.sha256_bytes(data)))
        up.file.seek(0)

    if total_bytes > settings.max_batch_total_bytes:
        raise HTTPException(
            413,
            f"Batch exceeds {settings.max_batch_total_bytes // (1024 * 1024)} MB total. "
            "Split the upload into smaller batches; no files were processed.",
        )
    if rejected:
        raise HTTPException(400, "Upload rejected; no files were processed. Remove or replace: " + "; ".join(rejected))
    if not validated:
        raise HTTPException(400, "No valid files were provided")

    batch = Batch(layout=layout, intake_mode=intake_mode, status="uploaded",
                  project_id=project_id, organization_id=organization_id,
                  created_by_user_id=user.id, expected_ticket_count=expected_ticket_count)
    store = storage.current()
    upload_folder = None
    try:
        db.add(batch)
        db.flush()
        upload_folder = f"uploads/{batch.id}"
        for up, original_name, ext, checksum in validated:
            # Stored privately, under a random name, and never over another file.
            key = store.save(f"{upload_folder}/{uuid.uuid4().hex}{ext}", up.file.read())
            db.add(UploadedFile(
                batch_id=batch.id, original_name=original_name,
                storage_path=key, sha256=checksum,
            ))
        job = jobqueue.enqueue(db, batch.id)
        db.commit()
    except Exception:
        db.rollback()
        if upload_folder is not None:
            store.delete_prefix(upload_folder)
        raise

    _notify_worker(background, job.id)
    db.refresh(batch)
    return batch

@router.get("/batches", response_model=list[BatchOut])
def list_batches(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    return db.scalars(select(Batch).where(
        Batch.organization_id == organization_id
    ).order_by(Batch.id.desc())).all()


OPEN_TICKET_STATES = ("needs_review", "flagged", "failed")


@router.get("/batches/queue", response_model=list[BatchQueueRow])
def batch_queue(open_only: bool = True, project_id: int | None = None, limit: int = Query(100, ge=1, le=500),
                db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    """Uploads people can come back to, newest first. A fixed number of queries."""
    query = select(Batch).where(Batch.organization_id == organization_id)
    if project_id is not None:
        query = query.where(Batch.project_id == project_id)
    if open_only:
        unfinished = select(Ticket.id).where(Ticket.batch_id == Batch.id, Ticket.status.in_(OPEN_TICKET_STATES)).exists()
        faint = select(ScanSlot.id).where(ScanSlot.batch_id == Batch.id, ScanSlot.detection == "faint",
                                          ScanSlot.decision.is_(None)).exists()
        query = query.where(Batch.status.in_(("uploaded", "processing", "failed")) | unfinished | faint)
    batches = list(db.scalars(query.order_by(Batch.id.desc()).limit(limit)))
    ids = [b.id for b in batches]
    if not ids:
        return []
    counts: dict[int, dict[str, int]] = {}
    for batch_id, status, count in db.execute(
        select(Ticket.batch_id, Ticket.status, func.count()).where(Ticket.batch_id.in_(ids))
        .group_by(Ticket.batch_id, Ticket.status)
    ):
        counts.setdefault(batch_id, {})[status] = count
    files: dict[int, list[int]] = {}
    for batch_id, status, count in db.execute(
        select(UploadedFile.batch_id, UploadedFile.status, func.count()).where(UploadedFile.batch_id.in_(ids))
        .group_by(UploadedFile.batch_id, UploadedFile.status)
    ):
        row = files.setdefault(batch_id, [0, 0])
        row[0] += count
        row[1] += count if status == "failed" else 0
    # A faint slot that already became a ticket needs no decision.
    open_slots = set(db.execute(select(ScanSlot.batch_id, ScanSlot.file_id, ScanSlot.page, ScanSlot.position).where(
        ScanSlot.batch_id.in_(ids), ScanSlot.detection == "faint", ScanSlot.decision.is_(None))).all())
    if open_slots:
        ticketed = set(db.execute(select(Ticket.batch_id, Ticket.file_id, Ticket.source_page, Ticket.position).where(
            Ticket.batch_id.in_({slot[0] for slot in open_slots}))).all())
        open_slots -= ticketed
    faint_counts: dict[int, int] = {}
    for slot in open_slots:
        faint_counts[slot[0]] = faint_counts.get(slot[0], 0) + 1
    projects = {p.id: p for p in db.scalars(select(Project).where(
        Project.id.in_({b.project_id for b in batches if b.project_id}), Project.organization_id == organization_id))}
    people = names_by_id(db, organization_id)
    rows = []
    for batch in batches:
        count = counts.get(batch.id, {})
        file_count = files.get(batch.id, [0, 0])
        project = projects.get(batch.project_id)
        rows.append(BatchQueueRow(
            id=batch.id, project_id=batch.project_id, project_code=project.code if project else None,
            project_name=project.name if project else None, status=batch.status, intake_mode=batch.intake_mode,
            created_at=batch.created_at, created_by=people.get(batch.created_by_user_id),
            files=file_count[0], failed_files=file_count[1], expected_ticket_count=batch.expected_ticket_count,
            tickets=sum(count.values()), needs_review=count.get("needs_review", 0), flagged=count.get("flagged", 0),
            verified=count.get("verified", 0), skipped=count.get("skipped", 0), failed=count.get("failed", 0),
            faint_slots=faint_counts.get(batch.id, 0),
        ))
    return rows


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    batch = db.scalar(select(Batch).where(
        Batch.id == batch_id, Batch.organization_id == organization_id
    ))
    if not batch:
        raise HTTPException(404, "Batch not found")
    return batch


class BatchProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)


@router.patch("/batches/{batch_id}/project", response_model=BatchOut)
def move_batch(batch_id: int, body: BatchProjectIn, db: Session = Depends(get_db),
               user: User = Depends(require("projects.manage"))):
    """Fix an upload filed under the wrong project. Invoice comparisons recompute
    from current tickets, so they follow automatically; a ticket a person already
    linked to an invoice line has to be released first."""
    organization_id = user.organization_id
    batch = db.scalar(select(Batch).where(Batch.id == batch_id, Batch.organization_id == organization_id))
    if batch is None:
        raise HTTPException(404, "Batch not found")
    target = db.scalar(select(Project).where(Project.id == body.project_id, Project.organization_id == organization_id))
    if target is None:
        raise HTTPException(404, "Project not found")
    if target.status != "active":
        raise HTTPException(409, "Project is archived; unarchive it to move tickets into it")
    if batch.project_id == target.id:
        return batch
    linked = db.scalar(select(func.count()).select_from(InvoiceTicketLink).join(Ticket, Ticket.id == InvoiceTicketLink.ticket_id).where(
        Ticket.batch_id == batch_id, InvoiceTicketLink.released_at.is_(None)))
    if linked:
        raise HTTPException(409, f"{linked} ticket(s) in this batch are linked to an invoice line. Release those links first.")
    old_code = db.scalar(select(Project.code).where(Project.id == batch.project_id)) if batch.project_id else None
    db.add(ScanEvent(batch_id=batch_id, actor_user_id=user.id, action="project_changed",
                     old_value=old_code, new_value=target.code))
    batch.project_id = target.id
    db.commit()
    db.refresh(batch)
    return batch


@router.post("/batches/{batch_id}/retry", response_model=BatchOut, dependencies=[Depends(limited("uploads"))])
def retry_batch(batch_id: int, background: BackgroundTasks, db: Session = Depends(get_db),
                user: User = Depends(require("tickets.upload"))):
    organization_id = user.organization_id
    batch = db.scalar(select(Batch).where(
        Batch.id == batch_id, Batch.organization_id == organization_id
    ))
    if not batch:
        raise HTTPException(404, "Batch not found")
    if batch.status != "failed":
        raise HTTPException(409, "Only failed batches can be retried; wait until processing finishes")
    if not any(f.status in ("failed", "uploaded", "processing") for f in batch.files):
        raise HTTPException(409, "No failed or interrupted files to retry")
    # Atomically reserve this retry before enqueuing. Two simultaneous retry
    # requests must not create duplicate worker attempts for the same batch.
    claimed = db.execute(
        update(Batch).where(
            Batch.id == batch_id, Batch.organization_id == organization_id,
            Batch.status == "failed",
        ).values(status="processing")
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "A retry is already in progress")
    job = jobqueue.enqueue(db, batch_id, only_failed=True)
    db.commit()
    _notify_worker(background, job.id)
    db.refresh(batch)
    return batch


@router.get("/batches/{batch_id}/tickets", response_model=list[TicketOut])
def list_tickets(batch_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    batch = db.scalar(select(Batch.id).where(
        Batch.id == batch_id, Batch.organization_id == organization_id
    ))
    if batch is None:
        raise HTTPException(404, "Batch not found")
    q = select(Ticket).where(Ticket.batch_id == batch_id).order_by(Ticket.file_id, Ticket.source_page, Ticket.position)
    tickets = db.scalars(q).all()
    table = pricing_svc.rate_table(db, organization_id)
    index = dup_svc.load_index(db, organization_id, tickets)
    names = pricing_svc.catalog_names(db, organization_id)
    people = names_by_id(db, organization_id)
    aliases = pricing_svc.alias_suggestions(db, organization_id, names)
    return [
        _out(db, ticket, organization_id, table, duplicate_index=index, catalog=names, people=people, aliases=aliases)
        for ticket in tickets
    ]


def _out(
    db: Session, t: Ticket, organization_id: int, table=None, *,
    duplicate_index: dup_svc.DuplicateIndex | None = None,
    catalog: tuple[set[str], set[str]] | None = None,
    people: dict[int, str] | None = None,
    aliases: dict[tuple[str, str], str] | None = None,
) -> TicketOut:
    """Ticket plus pricing and duplicates, reusing per-list data when supplied."""
    table = table if table is not None else pricing_svc.rate_table(db, organization_id)
    price, source = pricing_svc.effective_price_for(t, table)
    if t.status == "verified" and t.price_snapshot_state is None:
        notes = ["Legacy verified ticket: its historical rate is unknown. Enter the documented rate as an override, then re-verify to lock its cost."]
    elif t.status == "verified" and t.price_snapshot_state == "unpriced":
        notes = ["No rate was available at verification. Enter the documented rate as an override and re-verify this ticket."]
    elif price is None:
        notes = pricing_svc.unresolved_names(db, t, organization_id, catalog) or ["No rate configured for this supplier and material"]
    else:
        notes = []
    out = TicketOut.model_validate(t)
    return out.model_copy(update=dict(
        unit_price=price, price_source=source, cost=pricing_svc.cost_for(t, price),
        duplicates=(duplicate_index.find(t) if duplicate_index is not None
                    else dup_svc.find_duplicates(db, t, organization_id)),
        pricing_notes=notes,
        verified_by=_verified_by(db, t, organization_id, people),
        suggestions=pricing_svc.suggestions_for(
            t, aliases if aliases is not None else pricing_svc.alias_suggestions(db, organization_id)),
        as_read={field: str(t.raw_extraction[field]) for field in EDITABLE
                 if isinstance(t.raw_extraction, dict) and t.raw_extraction.get(field) not in (None, "")},
        revision=ticket_revision(t, price),
    ))


def ticket_revision(t: Ticket, price: Decimal | None) -> str:
    """A short fingerprint of what a reviewer relies on, including the price that
    approving would lock. Decimals are formatted at stored precision, so a value
    and its reload always agree."""
    parts = [
        t.ticket_number, t.delivery_date.isoformat() if t.delivery_date else None, t.material_type,
        f"{t.tonnage:.3f}" if t.tonnage is not None else None, t.supplier, t.job_location,
        f"{t.unit_price_override:.4f}" if t.unit_price_override is not None else None,
        f"{price:.4f}" if price is not None else None,
        t.status, bool(t.tonnage_confirmed),
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


def _require_revision(db: Session, t: Ticket, organization_id: int, if_match: str | None) -> None:
    """Refuse to act on a ticket that changed since the person last saw it.

    Without this, a manager with a stale page could approve 22 tons while looking
    at 20, or lock a rate they never saw. Optional, so scripts that read first
    still work; the browser always sends it.
    """
    if if_match is None:
        return
    price, _ = pricing_svc.effective_price_for(t, pricing_svc.rate_table(db, organization_id))
    if if_match.strip().strip('"') != ticket_revision(t, price):
        raise HTTPException(409, {
            "code": "ticket_changed",
            "message": "Someone else changed this ticket or its rate while you had it open. Nothing was saved. "
                       "The latest values are shown now: check them against the scan.",
        })


def _verified_by(db: Session, t: Ticket, organization_id: int,
                 people: dict[int, str] | None) -> str | None:
    if t.status != "verified" or t.verified_by_user_id is None:
        return None
    if people is None:
        people = names_by_id(db, organization_id)
    return people.get(t.verified_by_user_id)


def _withdraw_approval(t: Ticket) -> None:
    """Clear the current approver; the audit trail keeps who approved before."""
    t.verified_by_user_id = None
    t.verified_at = None


def _audit(db: Session, t: Ticket, user: User, field: str, old, new) -> None:
    db.add(AuditEvent(
        ticket_id=t.id, actor_user_id=user.id, field=field,
        old_value=None if old is None else str(old),
        new_value=None if new is None else str(new),
    ))


def _ticket_or_404(db: Session, ticket_id: int, organization_id: int, *, lock: bool = False) -> Ticket:
    query = select(Ticket).join(Batch, Batch.id == Ticket.batch_id).where(
        Ticket.id == ticket_id, Batch.organization_id == organization_id
    )
    if lock:  # Held until commit on PostgreSQL. SQLite serializes writers already.
        query = query.with_for_update(of=Ticket)
    t = db.scalar(query)
    if not t:
        raise HTTPException(404, "Ticket not found")
    return t


def apply_review_edit(db: Session, t: Ticket, user: User, field: str, new) -> bool:
    """One audited change to an unapproved ticket. False when nothing changed.
    A previewed price no longer applies; it is captured at approval as usual."""
    old = getattr(t, field)
    if old == new:
        return False
    _audit(db, t, user, field, old, new)
    setattr(t, field, new)
    t.price_snapshot_state = "pending"
    t.applied_unit_price = None
    t.applied_price_source = None
    _refresh_flags(t)
    return True


def _refresh_flags(t: Ticket) -> None:
    # A crop check comes from the scan, not the values, so editing can't clear it.
    checks = [f for f in (t.flags or []) if f.startswith(CROP_CHECK_PREFIX)]
    t.flags = compute_flags(
        ticket_number=t.ticket_number, delivery_date=t.delivery_date, material_type=t.material_type,
        tonnage=t.tonnage, supplier=t.supplier, job_location=t.job_location,
        raw_date="x" if t.delivery_date else None) + checks


@router.patch("/tickets/{ticket_id}", response_model=TicketOut)
def patch_ticket(ticket_id: int, patch: TicketPatch, db: Session = Depends(get_db),
                 user: User = Depends(require("tickets.edit")), if_match: str | None = Header(None)):
    organization_id = user.organization_id
    t = _ticket_or_404(db, ticket_id, organization_id, lock=True)
    _require_revision(db, t, organization_id, if_match)
    changes = patch.model_dump(exclude_unset=True)
    if (
        "unit_price_override" in changes
        and changes["unit_price_override"] != t.unit_price_override
        and not has_permission(user, "tickets.price_override")
    ):
        # A price override changes locked project cost; it is a manager decision.
        raise HTTPException(403, "Your role does not allow changing a ticket's unit price.")
    changed = False
    for field, new in changes.items():
        if isinstance(new, str):
            new = new.strip() or None
        if field == "tonnage" and new is not None:
            new = new.quantize(Decimal("0.001"))  # same precision as stored values
        if field == "unit_price_override" and new is not None:
            new = new.quantize(Decimal("0.0001"))
        old = getattr(t, field)
        if old == new:
            continue
        changed = True
        _audit(db, t, user, field, old, new)
        setattr(t, field, new)
        if field == "tonnage":  # a changed quantity must be re-confirmed
            t.tonnage_confirmed = False
        if t.status == "verified":
            # Any changed field (including price) invalidates the previous approval.
            t.status = "needs_review"
            t.tonnage_confirmed = False
            _withdraw_approval(t)
    if t.status == "failed" and changed:
        t.status, t.error = "needs_review", None  # manual entry recovers a failed extraction
        db.flush()
        scan_manifest.settle_statuses(db, db.get(Batch, t.batch_id))
    if changed:
        # Any edit invalidates a previously applied historical price; the
        # updated rate must be explicitly captured during re-verification.
        t.price_snapshot_state = "pending"
        t.applied_unit_price = None
        t.applied_price_source = None
        t.approved_cost = None
    _refresh_flags(t)
    db.commit()
    return _out(db, t, organization_id)


@router.post("/tickets/{ticket_id}/verify", response_model=TicketOut)
def verify_ticket(ticket_id: int, body: VerifyIn | None = Body(default=None), db: Session = Depends(get_db),
                  user: User = Depends(require("tickets.verify")), if_match: str | None = Header(None)):
    organization_id = user.organization_id
    t = _ticket_or_404(db, ticket_id, organization_id, lock=True)
    _require_revision(db, t, organization_id, if_match)
    # Validate the current values rather than trusting stale extraction flags.
    _refresh_flags(t)
    # Unusually high tonnage is a warning, not an absolute rejection: some
    # legitimate delivery tickets may be for a bulk shipment. A crop check is a
    # warning too: the person approving compares the record with the scan, and
    # tonnage still needs their explicit confirmation.
    blocking = [f for f in t.flags if not f.startswith(("Tonnage unusually large", CROP_CHECK_PREFIX))]
    if blocking:
        raise HTTPException(422, "Resolve ticket issues before verification: " + "; ".join(blocking))
    if t.status != "verified":
        # People approving copies of one delivery take turns, so the second sees
        # the first's result. Other companies' documents never influence this.
        if t.crop_sha256:
            take_turn(db, "approve-scan", organization_id, t.crop_sha256)
            # An EXACT scan already approved in this company would double-count
            # tons and cost. A person can't override this one.
            approved_copy = db.scalar(select(Ticket.id).where(
                Ticket.organization_id == organization_id, Ticket.crop_sha256 == t.crop_sha256,
                Ticket.id != t.id, Ticket.status == "verified").limit(1))
            if approved_copy is not None:
                raise HTTPException(
                    409,
                    "Identical scanned ticket already verified in this company. "
                    "Review the original; flag or skip the duplicate instead of "
                    "counting its quantity and cost twice.",
                )
        # The same supplier ticket number is very likely the same delivery, but a
        # supplier could reuse one. So a person decides, and that's on the record.
        if t.ticket_number and t.supplier:
            take_turn(db, "approve-number", organization_id, norm(t.supplier), t.number_key)
            other = db.scalar(select(Ticket).where(
                Ticket.organization_id == organization_id, Ticket.number_key == t.number_key,
                Ticket.supplier_key == t.supplier_key, Ticket.id != t.id, Ticket.status == "verified").limit(1))
            if other is not None:
                if not (body and body.confirm_duplicate):
                    raise HTTPException(409, {
                        "code": "possible_duplicate",
                        "message": f"{t.supplier} ticket {t.ticket_number} is already approved "
                                   f"(batch {other.batch_id}, page {other.source_page}, ticket {other.position + 1}).",
                    })
                _audit(db, t, user, "duplicate_override", None, f"approved alongside ticket {other.id}")
    if t.status == "verified":
        if t.price_snapshot_state is None:
            raise HTTPException(409, "Historical unit price is unknown. Enter the documented rate as a ticket override and verify again.")
        if t.price_snapshot_state in ("priced", "unpriced"):
            return _out(db, t, organization_id)  # repeated verify must not reprice an approved ticket

    price, source = pricing_svc.unit_price_for(t, pricing_svc.rate_table(db, organization_id))
    _audit(db, t, user, "status", t.status, "verified")
    _audit(db, t, user, "verified_unit_price", None, price)
    t.applied_unit_price = price
    t.applied_price_source = source
    t.price_snapshot_state = "priced" if price is not None else "unpriced"
    t.approved_cost = pricing_svc.to_cents(t.tonnage * price) if price is not None and t.tonnage is not None else None
    t.status, t.tonnage_confirmed = "verified", True
    if t.flag_note is not None:  # Settled by this approval; the audit trail keeps it.
        _audit(db, t, user, "flag_note", t.flag_note, None)
        t.flag_note = None
    t.verified_by_user_id = user.id
    t.verified_at = datetime.now(timezone.utc)
    db.commit()
    return _out(db, t, organization_id)


@router.post("/tickets/{ticket_id}/flag", response_model=TicketOut)
def flag_ticket(ticket_id: int, body: FlagIn | None = Body(default=None), db: Session = Depends(get_db),
                user: User = Depends(require("tickets.flag")), if_match: str | None = Header(None)):
    """Defer a questionable ticket without approving its quantity or cost."""
    organization_id = user.organization_id
    t = _ticket_or_404(db, ticket_id, organization_id, lock=True)
    _require_revision(db, t, organization_id, if_match)
    if t.status != "flagged":
        _audit(db, t, user, "status", t.status, "flagged")
    note = (body.note or "").strip() or None if body is not None else None
    if note != t.flag_note and (body is not None):
        _audit(db, t, user, "flag_note", t.flag_note, note)
        t.flag_note = note
    t.status = "flagged"
    _withdraw_approval(t)
    t.tonnage_confirmed = False
    t.price_snapshot_state = "pending"
    t.applied_unit_price = None
    t.applied_price_source = None
    t.approved_cost = None
    db.commit()
    return _out(db, t, organization_id)


@router.post("/tickets/{ticket_id}/skip", response_model=TicketOut)
def skip_ticket(ticket_id: int, db: Session = Depends(get_db),
                user: User = Depends(require("tickets.skip")), if_match: str | None = Header(None)):
    organization_id = user.organization_id
    t = _ticket_or_404(db, ticket_id, organization_id, lock=True)
    _require_revision(db, t, organization_id, if_match)
    _audit(db, t, user, "status", t.status, "skipped")
    t.status = "skipped"
    _withdraw_approval(t)
    t.tonnage_confirmed = False
    t.price_snapshot_state = "pending"
    t.applied_unit_price = None
    t.applied_price_source = None
    t.approved_cost = None
    db.commit()
    return _out(db, t, organization_id)


@router.get("/tickets/{ticket_id}/image")
def ticket_image(ticket_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    t = _ticket_or_404(db, ticket_id, organization_id)
    image = storage.current().serve(t.crop_path, "image/png")
    if image is None:
        raise HTTPException(404, "The stored image for this ticket is missing.")
    return image


@router.get("/tickets/{ticket_id}/audit")
def ticket_audit(ticket_id: int, db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    _ticket_or_404(db, ticket_id, organization_id)
    events = db.scalars(select(AuditEvent).where(AuditEvent.ticket_id == ticket_id).order_by(AuditEvent.id)).all()
    people = names_by_id(db, organization_id)
    # actor None means the change predates individual accounts; it is never
    # attributed after the fact to whoever owns the workspace today.
    return [
        {"field": e.field, "old": e.old_value, "new": e.new_value, "at": as_utc(e.created_at),
         "actor": people.get(e.actor_user_id) if e.actor_user_id else None}
        for e in events
    ]


@router.get("/batches/{batch_id}/export.xlsx", dependencies=[Depends(limited("exports"))])
def export_batch(batch_id: int, view_id: int | None = None, db: Session = Depends(get_db),
                 organization_id: int = Depends(current_organization_id)):
    from .records import view_choice
    if db.scalar(select(Batch.id).where(
        Batch.id == batch_id, Batch.organization_id == organization_id
    )) is None:
        raise HTTPException(404, "Batch not found")
    keys, labels = view_choice(db, organization_id, view_id, None)
    table = pricing_svc.rate_table(db, organization_id)
    query = export_svc.row_query().where(Ticket.batch_id == batch_id)
    data = export_svc.build_workbook(export_svc.rows(db, query, order=(Ticket.file_id, Ticket.source_page, Ticket.position)),
                                     names_by_id(db, organization_id), keys, price_of=lambda t: pricing_svc.effective_price_for(t, table)[0],
                                     labels=labels)
    return Response(
        data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="delivery-records-batch-{batch_id}.xlsx"'})
