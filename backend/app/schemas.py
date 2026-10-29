from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    original_name: str
    status: str
    error: str | None


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int | None = None
    layout: str
    intake_mode: str
    status: str
    created_at: datetime
    files: list[FileOut] = []


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
    tonnage: Decimal | None
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
    price_source: str | None = None  # "rate" | "override" | None
    cost: Decimal | None = None
    duplicates: list[str] = []
    pricing_notes: list[str] = []


class TicketPatch(BaseModel):
    """Only fields present in the request are changed; each change is audited."""
    model_config = ConfigDict(extra="forbid")
    ticket_number: str | None = None
    delivery_date: date | None = None
    material_type: str | None = None
    tonnage: Decimal | None = Field(default=None, ge=0)
    supplier: str | None = None
    job_location: str | None = None
    unit_price_override: Decimal | None = Field(default=None, ge=0)

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
