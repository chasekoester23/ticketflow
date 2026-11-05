"""SQLAlchemy models (Phase 1 subset of the full design).

Company-owned parent rows and fail-closed read/write scoping enforce tenant isolation.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import false as sa_false
from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns stored UTC timestamps without tzinfo; mark them as UTC so
    browsers do not reinterpret them as local time."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class User(Base):
    """One individual person. Shared logins defeat the audit trail."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # owner | manager | clerk | viewer -- see app/permissions.py.
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="owner", server_default="owner")
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Set for owner-issued temporary passwords; blocks the API until changed.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false(),
    )
    # NULL for accounts created before this was recorded; never backfilled.
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True, nullable=False)


class AccountEvent(Base):
    """Immutable record of team/account administration within one company.

    actor_user_id is NULL only for actions taken from a trusted terminal
    script (e.g. owner password recovery), never for browser actions.
    """
    __tablename__ = "account_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    target_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_supplier_org_name"),)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    contact: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_material_org_name"),)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(20), default="ton")


class SupplierRate(Base):
    """Tenant-scoped price per supplier+material. Historical snapshots stay immutable."""
    __tablename__ = "supplier_rates"
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    supplier: Mapped[Supplier] = relationship()
    material: Mapped[Material] = relationship()


class NameAlias(Base):
    """"Means the same as": another spelling of a catalog supplier or material.

    Only ever produces a suggestion a person may apply on review; it never
    changes a stored ticket or a rate lookup. Removal stamps the row, so it is
    also its own history. Names are kept as text so history survives a catalog
    entry being deleted; an alias whose target has left the catalog suggests
    nothing.
    """
    __tablename__ = "name_aliases"
    __table_args__ = (
        Index("uq_name_alias_active", "organization_id", "kind", "alias_key", unique=True,
              sqlite_where=text("removed_at IS NULL"), postgresql_where=text("removed_at IS NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # supplier | material
    alias_text: Mapped[str] = mapped_column(String(200), nullable=False)
    alias_key: Mapped[str] = mapped_column(String(200), nullable=False)
    target_name: Mapped[str] = mapped_column(String(200), nullable=False)
    target_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    removed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PricingEvent(Base):
    """Immutable history of supplier, material and unit-rate changes.

    Rates decide the price locked into each ticket at approval, so every
    change records who made it. Names are snapshotted so history stays
    readable after a supplier or material is removed.
    """
    __tablename__ = "pricing_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # supplier_added | supplier_removed | material_added | material_removed |
    # rate_set | rate_changed | rate_removed
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    material_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    old_value: Mapped[str | None] = mapped_column(String(40), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "code", name="uq_project_org_code"),)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50))  # the user's own project/job number
    name: Mapped[str] = mapped_column(String(200))
    job_location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Batch(Base):
    __tablename__ = "batches"
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    layout: Mapped[str] = mapped_column(String(20))  # single | grid_2x2
    intake_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="extract")  # extract | manual
    status: Mapped[str] = mapped_column(String(20), default="uploaded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # NULL for uploads made before individual accounts existed.
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # How many paper tickets the person says are in this batch. NULL = not stated.
    expected_ticket_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    files: Mapped[list["UploadedFile"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class ProcessingJob(Base):
    """Durable extraction work item. Claimed by the local single-instance worker."""

    __tablename__ = "processing_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending")
    only_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # Who approved the CURRENT verification. Cleared when approval is
    # withdrawn; the full history stays in audit_events. NULL on a verified
    # ticket means it was approved before individual accounts existed.
    verified_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    file: Mapped[UploadedFile] = relationship(back_populates="tickets")


class ScanSlot(Base):
    """One physical position on one scanned page, ticket or not.

    Joined to Ticket by (file_id, page, position), so ticket data is never
    duplicated here. crop_sha256 identifies the unrotated crop.
    """
    __tablename__ = "scan_slots"
    __table_args__ = (UniqueConstraint("file_id", "page", "position", name="uq_scan_slot_position"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True, nullable=False)
    file_id: Mapped[int] = mapped_column(ForeignKey("uploaded_files.id"), index=True, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    crop_box: Mapped[list] = mapped_column(JSON, nullable=False)
    crop_path: Mapped[str] = mapped_column(String(500), nullable=False)  # internal; never returned
    crop_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ink_fraction: Mapped[float] = mapped_column(Float, nullable=False)  # image measure, never money
    detection: Mapped[str] = mapped_column(String(20), nullable=False)  # content | faint | empty
    # A person's decision: real_ticket | confirmed_blank | NULL (undecided).
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0, 90, 180, 270 clockwise


class ScanEvent(Base):
    """Immutable history of people's decisions about scans."""
    __tablename__ = "scan_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True, nullable=False)
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("scan_slots.id"), index=True, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    # NULL only for history recorded before individual accounts existed.
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field: Mapped[str] = mapped_column(String(50))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InvoiceComparison(Base):
    """One manually entered supplier invoice MATERIAL line (not a payable).

    Each entry has one project, supplier, material and delivery date range.
    No AI reads invoices, and comparison never approves payment. Matching is
    recalculated against the CURRENT verified tickets, not cached balances.
    """
    __tablename__ = "invoice_comparisons"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    supplier_key: Mapped[str] = mapped_column(String(200), nullable=False)
    material_name: Mapped[str] = mapped_column(String(200), nullable=False)
    material_key: Mapped[str] = mapped_column(String(200), nullable=False)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    billed_tons: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    billed_unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    billed_subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class InvoiceTicketRef(Base):
    """One delivery ticket number listed on a supplier invoice line.

    ticket_number keeps exactly what was entered (leading zeros included);
    ticket_key is the match key (case and surrounding spaces ignored only).
    A line with no rows is a totals-only comparison. The list is replaced as
    a whole on correction, and every change is recorded in
    invoice_comparison_events.
    """
    __tablename__ = "invoice_ticket_refs"
    __table_args__ = (UniqueConstraint("comparison_id", "position", name="uq_invoice_ticket_ref_position"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_comparisons.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_number: Mapped[str] = mapped_column(String(100), nullable=False)
    ticket_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    billed_tons: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)


class InvoiceDocument(Base):
    """An uploaded supplier invoice PDF. Separate from comparisons: nothing here
    counts until a manager confirms a draft line."""
    __tablename__ = "invoice_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)  # internal; never returned
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # needs_review | unsupported | failed | voided
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    extractor: Mapped[str] = mapped_column(String(30), nullable=False)  # manual | text_layer
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    unread_rows: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InvoiceDraftLine(Base):
    """Editable draft of one invoice line. NULL stays NULL until a person fills
    it in. A confirmed line is locked and points at the comparison it created."""
    __tablename__ = "invoice_draft_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("invoice_documents.id"), index=True, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # the row as read; never edited
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_review")  # pending_review|confirmed|rejected
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    material_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    billed_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    billed_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    billed_subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    ticket_list: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comparison_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_comparisons.id"), index=True, nullable=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InvoiceDocumentEvent(Base):
    """Immutable history of what people did to an invoice document."""
    __tablename__ = "invoice_document_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("invoice_documents.id"), index=True, nullable=False)
    line_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_draft_lines.id"), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InvoiceComparisonEvent(Base):
    """Immutable edit history; corrections do not silently replace invoice data."""
    __tablename__ = "invoice_comparison_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_comparisons.id"), nullable=False, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field: Mapped[str] = mapped_column(String(60), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
