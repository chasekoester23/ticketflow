"""Deterministic validation of extracted values. Model output is untrusted data, never instructions."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, field_validator

from ..config import settings


class ExtractedTicket(BaseModel):
    """Strict schema for what the vision model must return. Missing/unreadable => None."""

    ticket_number: str | None = None
    delivery_date: str | None = None  # kept as text here; normalized by parse_date
    material_type: str | None = None
    tonnage: str | float | int | None = None
    supplier: str | None = None
    job_location: str | None = None

    @field_validator("ticket_number", "material_type", "supplier", "job_location", mode="before")
    @classmethod
    def _clean_text(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None


_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%B %d, %Y", "%b %d, %Y"]


def parse_date(value: str | None) -> date | None:
    """US-style dates (month first), the convention on North American tickets."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_tonnage(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        text = re.sub(r"[^\d.\-]", "", str(value))  # strips "tons", commas, spaces
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return d.quantize(Decimal("0.001"))


def compute_flags(
    *,
    ticket_number: str | None,
    delivery_date: date | None,
    material_type: str | None,
    tonnage: Decimal | None,
    supplier: str | None,
    job_location: str | None,
    raw_date: str | None = None,
    raw_tonnage=None,
    today: date | None = None,
) -> list[str]:
    """Return human-readable reasons this record needs a person's attention."""
    today = today or date.today()
    flags: list[str] = []
    if not ticket_number:
        flags.append("Ticket number missing or unreadable")
    if delivery_date is None:
        flags.append("Date unreadable" if raw_date else "Date missing")
    elif delivery_date > today:
        flags.append("Date is in the future")
    if not material_type:
        flags.append("Material missing")
    if tonnage is None:
        flags.append("Tonnage unreadable" if raw_tonnage not in (None, "") else "Tonnage missing")
    elif tonnage <= 0:
        flags.append("Tonnage must be positive")
    elif tonnage > Decimal(str(settings.max_plausible_tons)):
        flags.append(f"Tonnage unusually large (> {settings.max_plausible_tons:g})")
    if not supplier:
        flags.append("Supplier missing")
    if not job_location:
        flags.append("Job location missing")
    return flags
