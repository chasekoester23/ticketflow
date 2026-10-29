"""SQLAlchemy models (Phase 1 subset of the full design).

The single-workspace owner protects access; multi-company isolation is NOT built.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True, nullable=False)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    contact: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    unit: Mapped[str] = mapped_column(String(20), default="ton")


class SupplierRate(Base):
    """Price per unit for a supplier+material pair. Effective dates / project rates are later work."""
    __tablename__ = "supplier_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    supplier: Mapped[Supplier] = relationship()
    material: Mapped[Material] = relationship()


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)  # the user's own project/job number
    name: Mapped[str] = mapped_column(String(200))
    job_location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    layout: Mapped[str] = mapped_column(String(20))  # single | grid_2x2
    status: Mapped[str] = mapped_column(String(20), default="uploaded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    files: Mapped[list["UploadedFile"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    original_name: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(500))  # internal, never the user-supplied name
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="uploaded")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch: Mapped[Batch] = relationship(back_populates="files")
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="file", cascade="all, delete-orphan")


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("uploaded_files.id"))
    source_page: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)  # index within the page (0..3 for 2x2)
    crop_box: Mapped[list] = mapped_column(JSON)  # [left, top, right, bottom] in page pixels
    crop_path: Mapped[str] = mapped_column(String(500))
    crop_sha256: Mapped[str] = mapped_column(String(64), index=True)

    # Current (possibly user-corrected) values. None means "not read / not provided".
    ticket_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    material_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tonnage: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    supplier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    job_location: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Set only by a user who explicitly overrides the supplier/material rate for this ticket.
    unit_price_override: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    # Financial snapshot taken at verification. Never recalculate verified tickets
    # against the current supplier rate table.
    # NULL state represents a ticket verified before this migration, for which
    # the original price cannot be reliably recovered.
    price_snapshot_state: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="pending"
    )  # pending | priced | unpriced | NULL (legacy)
    applied_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    applied_price_source: Mapped[str | None] = mapped_column(String(20), nullable=True)

    raw_extraction: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # untouched model output
    flags: Mapped[list] = mapped_column(JSON, default=list)  # deterministic review flags
    status: Mapped[str] = mapped_column(String(20), default="needs_review")  # needs_review|verified|flagged|skipped|failed
    tonnage_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    file: Mapped[UploadedFile] = relationship(back_populates="tickets")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    field: Mapped[str] = mapped_column(String(50))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
