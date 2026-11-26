from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    original_name: str
    status: str
    error: str | None


class BatchProgress(BaseModel):
    """How far the reader has got with a batch, while it's being processed."""
    expected: int  # crops with something on them, so far: the reader's whole job
    read: int  # tickets ready for review
    failed: int  # couldn't be read
    reading_again: int  # failed before, being read again now
    files_waiting: int  # files not opened yet; expected grows as they are


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int | None = None
    layout: str
    intake_mode: str
    status: str
    created_at: datetime
    expected_ticket_count: int | None = None
    files: list[FileOut] = []
    progress: BatchProgress | None = None

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        from .models import as_utc
        return as_utc(value)


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_id: int
    file_id: int
    source_page: int
    position: int
    ticket_number: str | None
    delivery_date: date | None
    material_type: str | None
    tonnage: Decimal | None  # the quantity, in `unit`
    unit: str = "ton"
    # As written, when it was converted (pounds to tons); and scale weights, when shown.
    source_quantity: Decimal | None = None
    source_unit: str | None = None
    gross_weight: Decimal | None = None
    tare_weight: Decimal | None = None
    weight_unit: str | None = None
    supplier: str | None
    job_location: str | None
    flags: list[str]
    status: str
    tonnage_confirmed: bool
    error: str | None
    unit_price_override: Decimal | None = None
    price_snapshot_state: str | None = None  # pending | priced | unpriced | None (legacy)
    # Computed at read time (not stored), so they always reflect current rates and tickets.
    unit_price: Decimal | None = None
    price_source: str | None = None  # "project_rate" | "rate" | "override" | None
    cost: Decimal | None = None
    duplicates: list[str] = []
    pricing_notes: list[str] = []
    # Current approver (None if unverified or approved before individual accounts).
    verified_by: str | None = None
    verified_at: datetime | None = None
    # A known catalog name for a spelling on this ticket; a person applies it.
    suggestions: dict[str, str] = {}
    # A catalog name this one probably means, apart from words like Inc, Trucking or No. Suggestion only.
    similar: dict[str, str] = {}
    # Why the ticket number may be the job, PO or truck number instead. Never blocks approval.
    number_warning: str | None = None
    # What the reader returned, kept separate from the current values.
    as_read: dict[str, str] = {}
    flag_note: str | None = None
    # Who hauled it and the hauling charge locked at approval (see services/haul_billing.py).
    hauler_id: int | None = None
    truck_number: str | None = None
    haul_billing: str | None = None
    haul_unit_rate: Decimal | None = None
    haul_cost: Decimal | None = None
    # Changes whenever anything a reviewer relies on changes. Send it back in
    # If-Match, so nobody approves values they weren't looking at.
    revision: str = ""

    @field_validator("verified_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        from .models import as_utc
        return as_utc(value)


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A person has seen that this supplier's ticket number is already approved,
    # and has decided both are real. Recorded in the ticket's history.
    confirm_duplicate: bool = False


class FlagIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=300)


class BatchQueueRow(BaseModel):
    """One upload in the work queue. Counts only, so it stays fast."""
    id: int
    project_id: int | None
    project_code: str | None
    project_name: str | None
    status: str
    intake_mode: str
    created_at: datetime
    created_by: str | None
    files: int
    failed_files: int
    expected_ticket_count: int | None
    tickets: int
    needs_review: int
    flagged: int
    verified: int
    skipped: int
    failed: int
    faint_slots: int

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        from .models import as_utc
        return as_utc(value)


class TicketPatch(BaseModel):
    """Only fields present in the request are changed; each change is audited."""
    model_config = ConfigDict(extra="forbid")
    ticket_number: str | None = Field(default=None, max_length=100)
    delivery_date: date | None = None
    material_type: str | None = Field(default=None, max_length=200)
    tonnage: Decimal | None = Field(default=None, ge=0)
    supplier: str | None = Field(default=None, max_length=200)
    job_location: str | None = Field(default=None, max_length=200)
    unit_price_override: Decimal | None = Field(default=None, ge=0)
    hauler_id: int | None = Field(default=None, gt=0)  # who hauled it, when billed per ton or per load
    truck_number: str | None = Field(default=None, max_length=50)
    # The unit `tonnage` is in. "lb" is converted to tons on save and the pounds are kept.
    unit: Literal["ton", "cy", "load", "each", "gal", "lb"] | None = None
    gross_weight: Decimal | None = Field(default=None, ge=0)
    tare_weight: Decimal | None = Field(default=None, ge=0)
    weight_unit: Literal["lb", "ton"] | None = None

    @field_validator("gross_weight", "tare_weight")
    @classmethod
    def weight_fits(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        rounded = value.quantize(Decimal("0.001"))
        if not rounded.is_finite() or rounded >= Decimal(10) ** 9:
            raise ValueError("Number exceeds supported storage precision")
        return rounded

    @field_validator("tonnage", "unit_price_override")
    @classmethod
    def fits_storage_precision(cls, value: Decimal | None, info: ValidationInfo) -> Decimal | None:
        """Reject unrepresentable manual values before a DB write or audit event.

        The API intentionally accepts additional decimal places and rounds to
        the stored precision, but rounding may not exceed Numeric(12, scale).
        """
        if value is None:
            return None
        scale = 3 if info.field_name == "tonnage" else 4
        try:
            rounded = value.quantize(Decimal(1).scaleb(-scale))
        except InvalidOperation as exc:
            raise ValueError("Number exceeds supported storage precision") from exc
        if not rounded.is_finite() or rounded >= Decimal(10) ** (12 - scale):
            raise ValueError("Number exceeds supported storage precision")
        return rounded
