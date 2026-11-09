"""The public "request access" form: a note for whoever runs this server.

This is not sign-up. It creates no company, no account and no session, and reads
nothing back, so it can't reach any company's data. The answer is the same whether
the address is new or already waiting, so it can't be used to learn who has asked.

Real self-service sign-up also needs email verification and abuse controls at the
network edge. The limit here is per process, like every other limit in this app.
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import rate_limit
from ..config import settings
from ..db import get_db
from ..models import AccessRequest
from .setup import _clean

router = APIRouter(prefix="/api/access-requests")

# A flood can fill the list but not the disk: past this, new requests are turned away.
MAX_WAITING_REQUESTS = 2000
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_RECEIVED = {"received": True}


class AccessRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_name: str = Field(min_length=1, max_length=200)
    contact_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    monthly_tickets: Literal["under_500", "500_2000", "2000_10000", "over_10000"] | None = None
    message: str | None = Field(default=None, max_length=1000)
    # Hidden from people on the form. Only a form-filling bot puts anything here.
    website: str = Field(default="", max_length=200)


def _optional(value: str | None, label: str, limit: int) -> str | None:
    return _clean(value, label, limit) if value and value.strip() else None


@router.post("", status_code=202)
def request_access(body: AccessRequestIn, request: Request, db: Session = Depends(get_db)):
    limit = settings.rate_limit_access_requests_per_minute
    if limit > 0:
        wait = rate_limit.allowed("access-requests", request.client.host if request.client else "unknown", limit)
        if wait is not None:
            raise HTTPException(429, f"Too many requests from this network. Try again in {wait} seconds.",
                                headers={"Retry-After": str(wait)})
    if body.website.strip():
        return _RECEIVED
    company = _clean(body.company_name, "company name", 200)
    contact = _clean(body.contact_name, "name", 100)
    email = body.email.strip().lower()
    if not _EMAIL.fullmatch(email):
        raise HTTPException(422, "Enter a valid email address.")
    phone = _optional(body.phone, "phone number", 40)
    message = _optional(body.message, "message", 1000)
    if db.scalar(select(AccessRequest.id).where(AccessRequest.email == email, AccessRequest.status == "new").limit(1)) is not None:
        return _RECEIVED
    if db.scalar(select(func.count()).select_from(AccessRequest).where(AccessRequest.status == "new")) >= MAX_WAITING_REQUESTS:
        raise HTTPException(503, "We can't take new requests right now. Please try again later.")
    db.add(AccessRequest(company_name=company, contact_name=contact, email=email, phone=phone,
                         monthly_tickets=body.monthly_tickets, message=message))
    db.commit()
    return _RECEIVED
