"""The invoice exceptions center: every billing question in one list, with who owns
it and where it stands. Exceptions are recomputed on every request (services/
exceptions.py); only people's handling of them is stored. Nothing here changes an
invoice line, a ticket or a locked price, and nothing approves a payment."""
from __future__ import annotations

import csv
from decimal import Decimal
from io import StringIO
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..models import ExceptionEvent, ExceptionReview, InvoiceComparison, Project, User, as_utc, utcnow
from ..permissions import has_permission, require
from ..rate_limit import limited
from ..services import exceptions as svc
from ..services.accounts import names_by_id
from ..services.invoice_context import LineContext
from ..services.locks import take_turn
from ..services.pricing import norm

router = APIRouter(prefix="/api")

OPEN = ("open", "investigating", "disputed")
CLOSED = ("recovered", "resolved", "dismissed")
STATUSES = ("open", "investigating", "disputed", "recovered", "resolved", "dismissed")
# Closing an exception says why, so the next person doesn't reopen the same question.
NEEDS_NOTE = ("recovered", "resolved", "dismissed")


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value, ".2f")


def _tons(value: Decimal | None) -> str | None:
    return None if value is None else format(value, ".3f")


def _compute(db: Session, organization_id: int) -> tuple[list[dict], dict[str, ExceptionReview], dict[int, str]]:
    from .invoices import _line_out  # the one place an invoice line's comparison is computed
    items = list(db.scalars(select(InvoiceComparison).where(InvoiceComparison.organization_id == organization_id)
                            .order_by(InvoiceComparison.id)))
    context = LineContext(db, organization_id, items)
    lines = [_line_out(db, item, organization_id, context) for item in items]
    reviews = {r.key: r for r in db.scalars(select(ExceptionReview).where(ExceptionReview.organization_id == organization_id))}
    projects = dict(db.execute(select(Project.id, Project.code).where(Project.organization_id == organization_id)).all())
    people = names_by_id(db, organization_id)
    out = []
    for found in svc.detect(db, organization_id, items, lines):
        review = reviews.get(found.key)
        out.append(_item({**found.as_dict(), "basis": found.basis()}, review, projects, people, detected=True))
    return out, reviews, people


def _item(data: dict, review: ExceptionReview | None, projects: dict[int, str], people: dict[int, str], *, detected: bool) -> dict:
    # Closed by a person, then its figures changed: it needs a person again. Their status and
    # history stay exactly as they left them; only how it's counted and listed changes.
    changed = bool(review and review.status in CLOSED and review.basis is not None
                   and data.get("basis") is not None and review.basis != data["basis"])
    status = review.status if review else "open"
    return {
        **data,
        "changed_since_closed": changed,
        "needs_attention": status in OPEN or changed,
        "category_label": svc.CATEGORIES.get(data.get("category"), data.get("category")),
        "project": projects.get(data.get("project_id")),
        "detected": detected,
        "status": review.status if review else "open",
        "assignee_user_id": review.assignee_user_id if review else None,
        "assignee": people.get(review.assignee_user_id) if review and review.assignee_user_id else None,
        "recovered_amount": review.recovered_amount if review else None,
        "note": review.note if review else None,
        "revision": str(review.lock_version) if review else "new",
        "updated_at": as_utc(review.updated_at) if review else None,
        "updated_by": people.get(review.updated_by_user_id) if review and review.updated_by_user_id else None,
    }


def _wire(item: dict) -> dict:
    out = {k: v for k, v in item.items() if k != "basis"}
    for key in ("amount", "line_billed", "line_approved", "unbilled_cost", "recovered_amount"):
        out[key] = _money(out.get(key))
    for key in ("billed_tons", "approved_tons"):
        out[key] = _tons(out.get(key))
    return out


def _filtered(items: list[dict], *, project_id, supplier, category, severity, status, assignee) -> list[dict]:
    def keep(i: dict) -> bool:
        if project_id is not None and i["project_id"] != project_id:
            return False
        if supplier and norm(i["supplier"]) != norm(supplier):
            return False
        if category and i["category"] != category:
            return False
        if severity and i["severity"] != severity:
            return False
        if status == "open" and not i["needs_attention"]:
            return False
        if status and status != "open" and status != "all" and i["status"] != status:
            return False
        if assignee == "me" or (assignee and assignee.isdigit()):
            return False if i["assignee_user_id"] is None else (assignee == "me" or i["assignee_user_id"] == int(assignee))
        if assignee == "none" and i["assignee_user_id"] is not None:
            return False
        return True
    return [i for i in items if keep(i)]


_ORDER = {s: n for n, s in enumerate(svc.SEVERITIES)}


@router.get("/exceptions", dependencies=[Depends(limited("reports"))])
def list_exceptions(project_id: int | None = None, supplier: str | None = None, category: str | None = None,
                    severity: str | None = None, status: str = "open", assignee: str | None = None,
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    """status: open (open, investigating or disputed; the default), all, or one status.
    assignee: me, none, or a person's id."""
    if category and category not in svc.CATEGORIES:
        raise HTTPException(422, "Unknown category")
    if severity and severity not in svc.SEVERITIES:
        raise HTTPException(422, "Unknown severity")
    if status not in (*STATUSES, "all"):
        raise HTTPException(422, "Unknown status")
    organization_id = user.organization_id
    items, reviews, people = _compute(db, organization_id)
    if assignee == "me":
        assignee = str(user.id)
    detected_keys = {i["key"] for i in items}
    chosen = _filtered(items, project_id=project_id, supplier=supplier, category=category, severity=severity,
                       status=status, assignee=assignee)
    chosen.sort(key=lambda i: (not i["needs_attention"], _ORDER[i["severity"]], -(i["amount"] or Decimal(0)), i["key"]))
    projects = dict(db.execute(select(Project.id, Project.code).where(Project.organization_id == organization_id)).all())
    # Handled once, then no longer found because the data changed: kept, with what it said.
    cleared = [{"key": r.key, "title": r.title, "status": r.status, "recovered_amount": _money(r.recovered_amount),
                "note": r.note, "assignee": people.get(r.assignee_user_id) if r.assignee_user_id else None,
                "updated_at": as_utc(r.updated_at)}
               for r in reviews.values() if r.key not in detected_keys]
    scope = _filtered(items, project_id=project_id, supplier=supplier, category=None, severity=None, status="open", assignee=None)
    summary = svc.summarize(scope, [{"status": r.status, "recovered_amount": r.recovered_amount} for r in reviews.values()])
    return {
        "summary": summary,
        "items": [_wire(i) for i in chosen],
        "cleared": sorted(cleared, key=lambda c: c["updated_at"], reverse=True)[:100],
        "categories": svc.CATEGORIES,
        "people": [{"id": uid, "name": name} for uid, name in sorted(people.items(), key=lambda p: p[1] or "")],
        "can_manage": has_permission(user, "invoices.manage"),
        "projects": [{"id": pid, "code": code} for pid, code in sorted(projects.items(), key=lambda p: p[1])],
        "note": "Each amount is one cause of an invoice line's billed-minus-approved difference, counted once. "
                "A possible problem for a person to check, not a finding, and not an approval to pay.",
    }


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)
    status: Literal["open", "investigating", "disputed", "recovered", "resolved", "dismissed"] | None = None
    # Send null to unassign; leave out to keep the current owner.
    assignee_user_id: int | None = Field(default=None)
    recovered_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/exceptions/review")
def review_exception(body: ReviewIn, db: Session = Depends(get_db), user: User = Depends(require("invoices.manage")),
                     if_match: str | None = Header(None)):
    """Take ownership of, comment on, or settle one exception. Needs the revision you
    were looking at, so two people can't overwrite each other."""
    organization_id = user.organization_id
    if if_match is None:
        raise HTTPException(428, "Reload and try again: this needs the revision you were looking at.")
    take_turn(db, "exception", organization_id, body.key)
    items, reviews, people = _compute(db, organization_id)
    current = next((i for i in items if i["key"] == body.key), None)
    review = reviews.get(body.key)
    if current is None and review is None:
        raise HTTPException(404, "That exception isn't found. The data may have changed: reload.")
    if if_match.strip().strip('"') != (str(review.lock_version) if review else "new"):
        raise HTTPException(409, {"code": "exception_changed",
                                  "message": "Someone else updated this exception while you had it open. Nothing was saved. Reload to see theirs."})
    given = body.model_fields_set
    status = body.status if "status" in given and body.status else (review.status if review else "open")
    note = body.note.strip() if body.note and body.note.strip() else None
    if "assignee_user_id" in given and body.assignee_user_id is not None:
        if db.scalar(select(User.id).where(User.id == body.assignee_user_id, User.organization_id == organization_id,
                                           User.active.is_(True))) is None:
            raise HTTPException(404, "That person isn't an active member of this company.")
    recovered = body.recovered_amount if "recovered_amount" in given else (review.recovered_amount if review else None)
    if status == "recovered" and recovered is None:
        raise HTTPException(422, "Enter the credit the supplier confirmed.")
    if status != "recovered" and "recovered_amount" in given and body.recovered_amount is not None:
        raise HTTPException(422, "A credit is recorded only with the status 'recovered'.")
    if status in NEEDS_NOTE and status != (review.status if review else "open") and note is None:
        raise HTTPException(422, "Say why, so the next person doesn't reopen the same question.")
    if status != "recovered":
        recovered = None
    if review is None:
        review = ExceptionReview(organization_id=organization_id, key=body.key, status="open",
                                 title=(current or {}).get("title", body.key)[:300], lock_version=0)
        db.add(review)
    changes = []
    if status != review.status:
        changes.append(("status", review.status, status))
        review.status = status
    if "assignee_user_id" in given and body.assignee_user_id != review.assignee_user_id:
        changes.append(("assignee", people.get(review.assignee_user_id), people.get(body.assignee_user_id)))
        review.assignee_user_id = body.assignee_user_id
    if recovered != review.recovered_amount:
        changes.append(("recovered_amount", _money(review.recovered_amount), _money(recovered)))
        review.recovered_amount = recovered
    if note is not None:
        changes.append(("note", None, note))
        review.note = note
    if not changes:
        raise HTTPException(422, "Nothing to change.")
    if current is not None:
        review.title = current["title"][:300]
        review.basis = current["basis"]  # what this person decided on
    review.lock_version += 1
    review.updated_by_user_id, review.updated_at = user.id, utcnow()
    for name, old, new in changes:
        db.add(ExceptionEvent(organization_id=organization_id, key=body.key, actor_user_id=user.id, field=name,
                              old_value=None if old is None else str(old), new_value=None if new is None else str(new)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {"code": "exception_changed", "message": "Someone else updated this exception at the same moment. Reload."}) from None
    projects = dict(db.execute(select(Project.id, Project.code).where(Project.organization_id == organization_id)).all())
    if current is not None:
        return _wire(_item({k: v for k, v in current.items() if k in svc.Detected.__dataclass_fields__}, review, projects, people, detected=True))
    return {"key": review.key, "title": review.title, "status": review.status, "detected": False,
            "revision": str(review.lock_version), "recovered_amount": _money(review.recovered_amount), "note": review.note}


@router.get("/exceptions/history")
def exception_history(key: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    people = names_by_id(db, user.organization_id)
    events = db.scalars(select(ExceptionEvent).where(ExceptionEvent.organization_id == user.organization_id,
                                                     ExceptionEvent.key == key).order_by(ExceptionEvent.id))
    return [{"field": e.field, "old": e.old_value, "new": e.new_value, "at": as_utc(e.created_at),
             "actor": people.get(e.actor_user_id) if e.actor_user_id else None} for e in events]


def _csv(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not _is_number(text) else text


def _is_number(text: str) -> bool:
    try:
        Decimal(text)
        return True
    except Exception:
        return False


@router.get("/exceptions.csv", dependencies=[Depends(limited("exports"))])
def exceptions_csv(project_id: int | None = None, status: str = "open", db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    if status not in (*STATUSES, "all"):
        raise HTTPException(422, "Unknown status")
    items, _reviews, _people = _compute(db, user.organization_id)
    chosen = _filtered(items, project_id=project_id, supplier=None, category=None, severity=None, status=status, assignee=None)
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Severity", "Category", "Status", "Project", "Supplier", "Material", "Invoice(s)", "Ticket",
                     "Billed tons", "Approved tons", "Amount (billed minus approved, this cause)", "Line billed",
                     "Line approved", "Owner", "Credit recorded", "Explanation", "Key"])
    for i in sorted(chosen, key=lambda i: (_ORDER[i["severity"]], i["key"])):
        w = _wire(i)
        writer.writerow([_csv(v) for v in (i["severity"], i["category_label"], i["status"], i["project"], i["supplier"],
                                           i["material"], " ".join(i["invoice_numbers"]), i["ticket_number"], w["billed_tons"],
                                           w["approved_tons"], w["amount"], w["line_billed"], w["line_approved"], i["assignee"],
                                           w["recovered_amount"], i["explanation"], i["key"])])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="invoice-exceptions.csv"', "Cache-Control": "no-store"})
