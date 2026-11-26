"""Deterministic validation of extracted values. Model output is untrusted data, never instructions."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, field_validator

from ..config import settings
from .units import WEIGHT_UNITS, canonical


class ExtractedTicket(BaseModel):
    """Strict schema for what the vision model must return. Missing/unreadable => None."""

    ticket_number: str | None = None
    delivery_date: str | None = None  # kept as text here; normalized by parse_date
    material_type: str | None = None
    tonnage: str | float | int | None = None
    supplier: str | None = None
    job_location: str | None = None
    # What the quantity (the tonnage field) is in, as printed: ton | lb | cy | load | each |
    # gal. Never converted by the reader: pounds become tons in the pipeline, keeping the
    # pounds. None when the ticket names no unit, or one Quarvera doesn't know.
    unit: str | None = None
    # Scale weights as printed, in weight_unit (lb | ton). Checked against the quantity, never used as it.
    gross_weight: str | float | int | None = None
    tare_weight: str | float | int | None = None
    weight_unit: str | None = None
    truck_number: str | None = None

    @field_validator("ticket_number", "material_type", "supplier", "job_location", "truck_number", mode="before")
    @classmethod
    def _clean_text(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("unit", mode="before")
    @classmethod
    def _known_unit(cls, v):
        return quantity_unit(v)

    @field_validator("weight_unit", mode="before")
    @classmethod
    def _known_weight_unit(cls, v):
        unit = quantity_unit(v)
        return unit if unit in WEIGHT_UNITS else None

    @property
    def reports_unit(self) -> bool:
        """Whether this reader answered for the unit at all. Every reader using PROMPT does
        (validated_ticket passes every key). One that never returns the key, written
        before units, keeps the old contract: its tonnage field is tons."""
        return "unit" in self.model_fields_set


_POUNDS = {"lb", "lbs", "pound", "pounds"}


def quantity_unit(text) -> str | None:
    """The unit a ticket's unit text means: a billing unit or pounds. Anything else is None."""
    if text is None:
        return None
    key = str(text).strip().lower().rstrip(".")
    return "lb" if key in _POUNDS else canonical(key)


_AMOUNT = re.compile(r"(?P<number>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)(?:\s*(?P<unit>[A-Za-z]+\.?))?")


def parse_amount(value) -> tuple[Decimal | None, str | None]:
    """(number, unit) from "49,000 lb", "24.50" or 12. A unit Quarvera doesn't know, or
    anything but a plain positive number, gives (None, None): never a partial guess."""
    if value is None or value == "":
        return None, None
    match = _AMOUNT.fullmatch(str(value).strip())
    if match is None:
        return None, None
    unit = None
    if match.group("unit"):
        unit = quantity_unit(match.group("unit"))
        if unit is None:
            return None, None
    amount = parse_tonnage(match.group("number"))
    return (amount, unit) if amount is not None else (None, None)


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


# Never strip arbitrary OCR characters from a numeric field. For example,
# "1O.5" (letter O), "12/3" and "1e2" must not become valid, wrong tons.
# Accept only a literal number, optional grouped thousands, and a known unit.
_TONNAGE_PATTERN = re.compile(
    r"(?P<number>[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)"
    r"(?:\s+(?:net\s+)?(?:tons?|tonnes?|t))?",
    re.IGNORECASE,
)


def parse_tonnage(value) -> Decimal | None:
    if value is None or value == "":
        return None
    match = _TONNAGE_PATTERN.fullmatch(str(value).strip())
    if match is None:
        return None
    try:
        amount = Decimal(match.group("number").replace(",", ""))
        # Oversized/overprecise model outputs must fail review, never crash a
        # worker or get silently truncated to the database's Numeric(12, 3).
        if not amount.is_finite():
            return None
        rounded = amount.quantize(Decimal("0.001"))
        if abs(rounded) >= Decimal("1000000000"):
            return None
        return rounded
    except (InvalidOperation, ValueError):
        return None


EARLIEST_PLAUSIBLE_DATE = date(2000, 1, 1)
UNIT_NOT_STATED = "Unit not stated"
SAME_NUMBER_IN_BATCH = "Same ticket number as another ticket in this batch"


def unit_not_stated_flag(raw_quantity) -> str:
    """The reader saw a quantity but no unit it could name. Tons aren't assumed: a person enters it."""
    return (f"{UNIT_NOT_STATED}: the ticket shows {str(raw_quantity)[:40]} with no unit Quarvera knows. "
            "Enter the quantity and its unit.")


def same_number_in_batch_flag(page: int | None, position: int | None) -> str:
    """Two crops of one batch read the same supplier ticket number."""
    where = f" (page {page}, ticket {position + 1})" if page is not None and position is not None else ""
    return (f"{SAME_NUMBER_IN_BATCH}{where}. Check it isn't the same delivery scanned twice "
            "before approving either.")


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
    unit: str | None = None,
    gross_weight: Decimal | None = None,
    tare_weight: Decimal | None = None,
    weight_unit: str | None = None,
) -> list[str]:
    """Return human-readable reasons this record needs a person's attention.

    The quantity checks name tons for a ticket in tons (every ticket the reader
    produces) and "quantity" for one a person entered in another unit. Only tons
    have a plausible-size warning: a large count of each or gallons is normal.
    """
    from .units import net_check
    in_tons = (unit or "ton") == "ton"
    today = today or date.today()
    flags: list[str] = []
    if not ticket_number:
        flags.append("Ticket number missing or unreadable")
    if delivery_date is None:
        flags.append("Date unreadable" if raw_date else "Date missing")
    elif delivery_date > today:
        flags.append("Date is in the future")
    elif delivery_date < EARLIEST_PLAUSIBLE_DATE:
        # 1900-01-01 is a spreadsheet's empty date and 0001-01-01 a program's; neither
        # is a delivery. A person confirms the real date before approval.
        flags.append(f"Date is before {EARLIEST_PLAUSIBLE_DATE.year}: check it against the ticket")
    if not material_type:
        flags.append("Material missing")
    word = "Tonnage" if in_tons else "Quantity"
    if tonnage is None:
        flags.append(f"{word} unreadable" if raw_tonnage not in (None, "") else f"{word} missing")
    elif tonnage <= 0:
        flags.append(f"{word} must be positive")
    elif in_tons and tonnage > Decimal(str(settings.max_plausible_tons)):
        flags.append(f"Tonnage unusually large (> {settings.max_plausible_tons:g})")
    elif unit == "load" and tonnage != tonnage.to_integral_value():
        flags.append("Loads must be a whole number")
    weights = net_check(tonnage, unit, gross_weight, tare_weight, weight_unit)
    if weights:
        flags.append(weights)
    if not supplier:
        flags.append("Supplier missing")
    if not job_location:
        flags.append("Job location missing")
    return flags
