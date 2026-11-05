"""Upload an invoice PDF, correct its draft lines beside the original, confirm.

Nothing here approves an invoice or posts anywhere. A draft counts for nothing
until a manager confirms it, which creates an ordinary invoice comparison
through the same code as typed entry.
"""
import io
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pymupdf as fitz
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..auth import current_organization_id
from ..config import settings
from ..db import get_db
from ..models import (
    InvoiceComparison, InvoiceDocument, InvoiceDocumentEvent, InvoiceDraftLine, Project, User, as_utc, utcnow,
)
from ..permissions import require
from ..services import invoice_extraction, pipeline, splitter
from ..services.accounts import names_by_id
from ..services.invoice_extraction import InvoiceExtractionError
from ..services.pricing import CENTS, norm
from .invoices import InvoiceLineIn, add_comparison

router = APIRouter(prefix="/api/invoice-documents")

_EDITABLE = ("project_id", "invoice_number", "supplier_name", "material_name", "billed_quantity", "unit",
             "billed_unit_price", "billed_subtotal", "date_from", "date_to", "ticket_list")
_REQUIRED = {"project_id": "project", "invoice_number": "invoice number", "supplier_name": "supplier",
             "material_name": "material", "billed_quantity": "billed quantity", "unit": "unit",
             "billed_unit_price": "rate", "billed_subtotal": "material subtotal",
             "date_from": "delivery date from", "date_to": "delivery date through"}


class DraftLinePatch(BaseModel):
    """Only fields present are changed. Every change is recorded."""
    model_config = ConfigDict(extra="forbid")
    project_id: int | None = None
    invoice_number: str | None = Field(default=None, max_length=100)
    supplier_name: str | None = Field(default=None, max_length=200)
    material_name: str | None = Field(default=None, max_length=200)
    billed_quantity: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=3)
    unit: str | None = Field(default=None, max_length=20)
    billed_unit_price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=4)
    billed_subtotal: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    date_from: date | None = None
    date_to: date | None = None
    ticket_list: str | None = Field(default=None, max_length=200_000)


def _document_or_404(db: Session, document_id: int, organization_id: int) -> InvoiceDocument:
    document = db.scalar(select(InvoiceDocument).where(
        InvoiceDocument.id == document_id, InvoiceDocument.organization_id == organization_id))
    if document is None:
        raise HTTPException(404, "Invoice document not found")
    return document


def _line_or_404(db: Session, document: InvoiceDocument, line_id: int) -> InvoiceDraftLine:
    line = db.scalar(select(InvoiceDraftLine).where(
        InvoiceDraftLine.id == line_id, InvoiceDraftLine.document_id == document.id))
    if line is None:
        raise HTTPException(404, "Draft line not found")
    return line


def _project_or_404(db: Session, project_id: int, organization_id: int) -> None:
    if db.scalar(select(Project.id).where(Project.id == project_id, Project.organization_id == organization_id)) is None:
        raise HTTPException(404, "Project not found")


def _event(db: Session, document: InvoiceDocument, user: User, action: str, *,
           line: InvoiceDraftLine | None = None, old: str | None = None, new: str | None = None) -> None:
    db.add(InvoiceDocumentEvent(document_id=document.id, line_id=line.id if line else None,
                                actor_user_id=user.id, action=action, old_value=old, new_value=new))


def _blockers(line: InvoiceDraftLine) -> list[str]:
    """Why this line can't be confirmed. Comparison only understands tons, and a
    quantity in any other unit is never converted."""
    problems = [f"{label} is missing" for name, label in _REQUIRED.items()
                if getattr(line, name) in (None, "")]
    if line.unit and line.unit.strip().lower() != "ton":
        problems.append(f"billed in '{line.unit}': comparison works in tons only, and units are never converted")
    return problems


def _warnings(line: InvoiceDraftLine) -> list[str]:
    if None in (line.billed_quantity, line.billed_unit_price, line.billed_subtotal):
        return []
    extension = (line.billed_quantity * line.billed_unit_price).quantize(CENTS)
    if extension != line.billed_subtotal:
        return [f"Quantity × rate is {extension}, but the subtotal is {line.billed_subtotal}."]
    return []


def _line_out(line: InvoiceDraftLine) -> dict:
    # Decimals go out as strings. In a plain dict FastAPI would make them floats.
    return {
        "id": line.id, "page": line.page, "source_text": line.source_text, "state": line.state,
        **{name: str(value) if isinstance(value := getattr(line, name), Decimal) else value for name in _EDITABLE},
        "comparison_id": line.comparison_id, "blockers": _blockers(line) if line.state == "pending_review" else [],
        "warnings": _warnings(line),
    }


def _document_out(db: Session, document: InvoiceDocument) -> dict:
    lines = db.scalars(select(InvoiceDraftLine).where(InvoiceDraftLine.document_id == document.id)
                       .order_by(InvoiceDraftLine.id))
    return {
        "id": document.id, "project_id": document.project_id, "original_name": document.original_name,
        "sha256": document.sha256, "page_count": document.page_count, "status": document.status,
        "extractor": document.extractor, "message": document.message, "unread_rows": document.unread_rows,
        "created_at": as_utc(document.created_at), "lines": [_line_out(line) for line in lines],
    }


@router.get("")
def list_documents(db: Session = Depends(get_db), organization_id: int = Depends(current_organization_id)):
    documents = db.scalars(select(InvoiceDocument).where(InvoiceDocument.organization_id == organization_id)
                           .order_by(InvoiceDocument.id.desc()))
    return [_document_out(db, d) for d in documents]


@router.post("", status_code=201)
async def upload_document(file: UploadFile = File(...), project_id: int | None = Form(None),
                          db: Session = Depends(get_db), user: User = Depends(require("invoices.manage"))):
    organization_id = user.organization_id
    if project_id is not None:
        _project_or_404(db, project_id, organization_id)
    name = Path(file.filename or "").name or "invoice.pdf"
    data = await file.read(settings.max_upload_bytes + 1)
    if len(name) > 255 or not name.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        raise HTTPException(400, "Upload a PDF invoice.")
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, f"This file exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit.")
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise HTTPException(400, "This PDF is password protected.")
            page_count = doc.page_count
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "This PDF can't be opened. Check the file and try again.") from None
    if not 1 <= page_count <= settings.max_pdf_pages:
        raise HTTPException(413, f"Invoices can have at most {settings.max_pdf_pages} pages.")

    checksum = pipeline.sha256_bytes(data)
    same = db.scalar(select(InvoiceDocument.id).where(
        InvoiceDocument.organization_id == organization_id, InvoiceDocument.sha256 == checksum,
        InvoiceDocument.status != "voided").limit(1))
    if same is not None:
        raise HTTPException(409, f"This exact file is already uploaded as invoice document #{same}.")

    directory = settings.storage_dir / "invoices" / str(organization_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{uuid.uuid4().hex}.pdf"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)

    status, message, unread, extractor_name, drafted = "needs_review", None, [], "manual", []
    try:
        extractor = invoice_extraction.get_invoice_extractor()
        if extractor is not None:
            extractor_name = extractor.name
            result = extractor.extract(data)
            unread, drafted = result.unread_rows, result.lines
            if result.unsupported_reason:
                # Scanned or credit documents: flagged, with no drafted figures.
                status, message, drafted = "unsupported", result.unsupported_reason, []
            elif not drafted:
                message = "No material lines were recognized. Add them by hand beside the original."
    except InvoiceExtractionError as exc:
        # The original stays stored, and people can still enter lines by hand.
        status, message = "failed", str(exc)

    document = InvoiceDocument(
        organization_id=organization_id, project_id=project_id, original_name=name, storage_path=str(path),
        sha256=checksum, page_count=page_count, status=status, extractor=extractor_name, message=message,
        unread_rows=unread, created_by_user_id=user.id,
    )
    db.add(document)
    db.flush()
    for draft in drafted:
        db.add(InvoiceDraftLine(
            document_id=document.id, page=draft.page, source_text=draft.source_text, state="pending_review",
            project_id=project_id, invoice_number=draft.invoice_number, supplier_name=draft.supplier_name,
            material_name=draft.material_name, billed_quantity=draft.billed_quantity, unit=draft.unit,
            billed_unit_price=draft.billed_unit_price, billed_subtotal=draft.billed_subtotal,
            date_from=draft.date_from, date_to=draft.date_to, ticket_list=draft.ticket_list,
        ))
    _event(db, document, user, "uploaded", new=name)
    db.commit()
    return _document_out(db, document)


@router.get("/{document_id}")
def get_document(document_id: int, db: Session = Depends(get_db),
                 organization_id: int = Depends(current_organization_id)):
    return _document_out(db, _document_or_404(db, document_id, organization_id))


@router.get("/{document_id}/original")
def original_file(document_id: int, db: Session = Depends(get_db),
                  organization_id: int = Depends(current_organization_id)):
    document = _document_or_404(db, document_id, organization_id)
    if not Path(document.storage_path).is_file():
        raise HTTPException(404, "The stored file is missing.")
    return FileResponse(document.storage_path, media_type="application/pdf")


@router.get("/{document_id}/pages/{page_no}/image")
def page_image(document_id: int, page_no: int, db: Session = Depends(get_db),
               organization_id: int = Depends(current_organization_id)):
    document = _document_or_404(db, document_id, organization_id)
    try:
        image = splitter.render_pdf_page(Path(document.storage_path).read_bytes(), page_no, dpi=110,
                                         max_page_pixels=settings.max_rendered_page_pixels)
    except (splitter.PdfError, OSError):
        raise HTTPException(404, "That page is not available.") from None
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return Response(stream.getvalue(), media_type="image/png")


@router.get("/{document_id}/history")
def history(document_id: int, db: Session = Depends(get_db),
            organization_id: int = Depends(current_organization_id)):
    _document_or_404(db, document_id, organization_id)
    people = names_by_id(db, organization_id)
    events = db.scalars(select(InvoiceDocumentEvent).where(InvoiceDocumentEvent.document_id == document_id)
                        .order_by(InvoiceDocumentEvent.id))
    return [{"action": e.action, "line_id": e.line_id, "old": e.old_value, "new": e.new_value,
             "at": as_utc(e.created_at), "actor": people.get(e.actor_user_id) if e.actor_user_id else None}
            for e in events]


def _require_open(document: InvoiceDocument) -> None:
    if document.status == "voided":
        raise HTTPException(409, "This document was voided.")


@router.post("/{document_id}/lines", status_code=201)
def add_line(document_id: int, page: int = 1, db: Session = Depends(get_db),
             user: User = Depends(require("invoices.manage"))):
    """A blank line for people to fill in. Amounts must still be positive, so a
    credit can't be recorded this way."""
    document = _document_or_404(db, document_id, user.organization_id)
    _require_open(document)
    if not 1 <= page <= document.page_count:
        raise HTTPException(422, "That page isn't in this document.")
    line = InvoiceDraftLine(document_id=document.id, page=page, state="pending_review",
                            project_id=document.project_id, ticket_list="")
    db.add(line)
    db.flush()
    _event(db, document, user, "line_added", line=line, new=f"page {page}")
    db.commit()
    return _line_out(line)


@router.patch("/{document_id}/lines/{line_id}")
def edit_line(document_id: int, line_id: int, patch: DraftLinePatch, db: Session = Depends(get_db),
              user: User = Depends(require("invoices.manage"))):
    document = _document_or_404(db, document_id, user.organization_id)
    _require_open(document)
    line = _line_or_404(db, document, line_id)
    if line.state != "pending_review":
        raise HTTPException(409, "This line is no longer a draft. Correct the saved comparison instead.")
    changes = patch.model_dump(exclude_unset=True)
    if changes.get("project_id") is not None:
        _project_or_404(db, changes["project_id"], user.organization_id)
    for name, new in changes.items():
        if isinstance(new, str):
            new = new.strip() or ("" if name == "ticket_list" else None)
        if name == "ticket_list" and new is None:
            new = ""
        old = getattr(line, name)
        if old != new:
            _event(db, document, user, f"edited:{name}", line=line,
                   old=None if old is None else str(old), new=None if new is None else str(new))
            setattr(line, name, new)
    db.commit()
    return _line_out(line)


@router.post("/{document_id}/lines/{line_id}/reject")
def reject_line(document_id: int, line_id: int, db: Session = Depends(get_db),
                user: User = Depends(require("invoices.manage"))):
    document = _document_or_404(db, document_id, user.organization_id)
    line = _line_or_404(db, document, line_id)
    if line.state == "confirmed":
        raise HTTPException(409, "This line created a comparison and can't be rejected.")
    if line.state != "rejected":
        line.state, line.reviewed_by_user_id, line.reviewed_at = "rejected", user.id, utcnow()
        _event(db, document, user, "rejected", line=line)
        db.commit()
    return _line_out(line)


@router.post("/{document_id}/lines/{line_id}/confirm")
def confirm_line(document_id: int, line_id: int, db: Session = Depends(get_db),
                 user: User = Depends(require("invoices.manage"))):
    """A manager's explicit decision. Creates a comparison through the same code
    as typed entry, with the same overlap rules. Never approves payment."""
    document = _document_or_404(db, document_id, user.organization_id)
    _require_open(document)
    line = _line_or_404(db, document, line_id)
    if line.state != "pending_review":
        raise HTTPException(409, f"This line is already {line.state.replace('_', ' ')}.")
    blockers = _blockers(line)
    if blockers:
        raise HTTPException(422, "This line can't be confirmed yet: " + "; ".join(blockers) + ".")
    try:
        body = InvoiceLineIn(
            project_id=line.project_id, invoice_number=line.invoice_number, supplier_name=line.supplier_name,
            material_name=line.material_name, date_from=line.date_from, date_to=line.date_to,
            billed_tons=line.billed_quantity, billed_unit_price=line.billed_unit_price,
            billed_subtotal=line.billed_subtotal, ticket_list=line.ticket_list,
        )
    except ValidationError as exc:
        raise HTTPException(422, "This line has values a comparison can't accept: " + "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())) from None
    # The same invoice billing the same material twice is a mistake, even in
    # another date range. Typed entry can still record that deliberately.
    repeat = db.scalar(select(InvoiceComparison.id).where(
        InvoiceComparison.organization_id == user.organization_id,
        InvoiceComparison.invoice_number == body.invoice_number,
        InvoiceComparison.supplier_key == norm(body.supplier_name),
        InvoiceComparison.material_key == norm(body.material_name),
    ).limit(1))
    if repeat is not None:
        raise HTTPException(409, f"Invoice {body.invoice_number} already has a saved line for this "
                            f"supplier and material (comparison #{repeat}).")
    # Atomic claim: double-clicks and concurrent managers create one comparison.
    claimed = db.execute(update(InvoiceDraftLine).where(
        InvoiceDraftLine.id == line.id, InvoiceDraftLine.state == "pending_review",
    ).values(state="confirmed", reviewed_by_user_id=user.id, reviewed_at=utcnow()))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "This line was just confirmed by someone else.")
    item = add_comparison(db, body, user)
    db.execute(update(InvoiceDraftLine).where(InvoiceDraftLine.id == line.id).values(comparison_id=item.id))
    _event(db, document, user, "confirmed", line=line, new=f"comparison {item.id}")
    db.commit()
    db.refresh(line)
    return _line_out(line)


@router.post("/{document_id}/void")
def void_document(document_id: int, db: Session = Depends(get_db),
                  user: User = Depends(require("invoices.manage"))):
    """For the wrong file. The file and its history are kept. Refused once a line
    has created a comparison, since that comparison cites this document."""
    document = _document_or_404(db, document_id, user.organization_id)
    if document.status == "voided":
        return _document_out(db, document)
    lines = list(db.scalars(select(InvoiceDraftLine).where(InvoiceDraftLine.document_id == document.id)))
    if any(line.state == "confirmed" for line in lines):
        raise HTTPException(409, "A line from this document created a comparison, so it can't be voided.")
    for line in lines:
        if line.state == "pending_review":
            line.state, line.reviewed_by_user_id, line.reviewed_at = "rejected", user.id, utcnow()
    document.status = "voided"
    _event(db, document, user, "voided")
    db.commit()
    return _document_out(db, document)
