"""SQLAlchemy models (Phase 1 subset of the full design).

Company-owned parent rows and fail-closed read/write scoping enforce tenant isolation.
"""
import re
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import false as sa_false
from sqlalchemy import true as sa_true
from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, event,
    select, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from . import db_guards  # noqa: F401 - installs rules the database itself enforces


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


class AccessRequest(Base):
    """A company asking for a pilot workspace, from the public sign-up page.

    Deliberately holds no company data and grants nothing: it's a note for whoever
    runs this server. organization_id is set only once a workspace has been made.
    """
    __tablename__ = "access_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    monthly_tickets: Mapped[str | None] = mapped_column(String(20), nullable=True)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # new | contacted | provisioned | declined
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new", server_default="new", index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True, nullable=False)
    # Naive UTC, like expires_at. NULL for a session made before this was recorded.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


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


_COMPANY, _PROJECT = "project_id IS NULL", "project_id IS NOT NULL"
_UNDATED, _DATED = "effective_from IS NULL", "effective_from IS NOT NULL"


def _rate_scope_index(name: str, columns: tuple[str, ...], where: str) -> Index:
    return Index(name, "organization_id", "supplier_id", "material_id", *columns, unique=True,
                 sqlite_where=text(where), postgresql_where=text(where))


class SupplierRate(Base):
    """A price for one supplier and material, company-wide or for one project
    (``project_id``), from a date or with no start date (``effective_from``).

    A ticket takes its project's agreement over the company rate, and within
    one scope the latest rate starting on or before its delivery date. Prices
    locked into approved tickets never change with these rows.
    """
    __tablename__ = "supplier_rates"
    # One row per scope and start date, or the price locked at approval would be
    # arbitrary. Blank scopes and dates need their own partial indexes, since a
    # plain unique index treats every blank as different.
    __table_args__ = (
        _rate_scope_index("uq_supplier_rate", (), f"{_COMPANY} AND {_UNDATED}"),
        _rate_scope_index("uq_supplier_rate_dated", ("effective_from",), f"{_COMPANY} AND {_DATED}"),
        _rate_scope_index("uq_supplier_rate_project", ("project_id",), f"{_PROJECT} AND {_UNDATED}"),
        _rate_scope_index("uq_supplier_rate_project_dated", ("project_id", "effective_from"), f"{_PROJECT} AND {_DATED}"),
    )
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Last day the price is valid (a quote's expiry). Blank: open-ended.
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
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
    # rate_set | rate_changed | rate_validity_changed | rate_removed
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    material_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    old_value: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Which rate a rate_* event is about: "Company-wide", or a project and start date.
    scope: Mapped[str | None] = mapped_column(String(240), nullable=True)
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
    """Durable extraction work item. Claimed by one worker at a time, under a lease."""

    __tablename__ = "processing_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending")
    only_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Which worker holds a running job, and until when. The holder renews the lease from its
    # heartbeat. Once it lapses, any worker may set the job back to pending. See jobqueue.py.
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerHeartbeat(Base):
    """One row per live worker process, so any process can see who is working on what."""

    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    host: Mapped[str] = mapped_column(String(255))
    pid: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(20))
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    current_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ExtractionUsage(Base):
    """One reader call per ticket: what it used, never what it read. Cost is worked out
    when reported, from MODEL_PRICES, so a price change never rewrites history."""

    __tablename__ = "extraction_usage"
    __table_args__ = (Index("ix_extraction_usage_org_created", "organization_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True, index=True)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seconds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    outcome: Mapped[str] = mapped_column(String(30))  # ok, or the failure's kind
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    __table_args__ = (Index("ix_uploaded_files_batch_id", "batch_id"),)
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
    __table_args__ = (
        Index("ix_tickets_batch_status", "batch_id", "status"),
        Index("ix_tickets_organization_id", "organization_id"),
        Index("ix_tickets_org_status", "organization_id", "status"),
        Index("ix_tickets_org_date", "organization_id", "delivery_date", "id"),
        Index("ix_tickets_org_scope", "organization_id", "supplier_key", "material_key", "delivery_date"),
        Index("ix_tickets_org_number", "organization_id", "number_key"),
        Index("ix_tickets_org_loose", "organization_id", "number_loose"),
        Index("ix_tickets_org_load", "organization_id", "delivery_date", "tonnage"),
        # A project filter reaches its tickets through their batches. Without this,
        # the database walked every ticket the company has to find one project's.
        Index("ix_tickets_org_batch_status", "organization_id", "batch_id", "status"),
        # Copies of one scanned image. Without it, SQLite walked every ticket the company
        # has on each approval and ticket read.
        Index("ix_tickets_org_image", "organization_id", "crop_sha256"),
        # One record per physical position, so a retried worker can't make a second.
        Index("uq_tickets_slot", "file_id", "source_page", "position", unique=True),
        # The same scan can't be approved twice in a company, whichever code path tries.
        Index("uq_tickets_approved_scan", "organization_id", "crop_sha256", unique=True,
              sqlite_where=text("status = 'verified'"), postgresql_where=text("status = 'verified'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # Copied from the batch. A batch never changes company.
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Every UPDATE checks this, so two people saving at once can't overwrite each other.
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
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
    # Why someone set this ticket aside. Never affects totals or approval.
    flag_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Normalized forms that matching compares. Kept in step by the events below.
    supplier_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    material_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    number_key: Mapped[str | None] = mapped_column(String(100), nullable=True)  # case and outer spaces ignored
    number_loose: Mapped[str | None] = mapped_column(String(100), nullable=True)  # leading zeros ignored too
    # Tons x locked rate, set at approval and cleared when approval is withdrawn.
    approved_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # The truck shift that carried this delivery, if a person linked it. The hourly cost
    # belongs to the shift alone: linking never charges a delivery for trucking.
    truck_shift_id: Mapped[int | None] = mapped_column(ForeignKey("truck_shifts.id"), nullable=True, index=True)

    file: Mapped[UploadedFile] = relationship(back_populates="tickets")

    __mapper_args__ = {"version_id_col": lock_version}


def normal_name(name: str | None) -> str:
    """Case, punctuation and spacing ignored, so 'ABC Aggregates.' matches 'abc  aggregates'."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _derive_keys(ticket: "Ticket") -> None:
    ticket.supplier_key = normal_name(ticket.supplier) if ticket.supplier is not None else None
    ticket.material_key = normal_name(ticket.material_type) if ticket.material_type is not None else None
    number = (ticket.ticket_number or "").strip().upper() if ticket.ticket_number is not None else None
    ticket.number_key = number
    ticket.number_loose = number.lstrip("0") if number is not None else None


@event.listens_for(Ticket, "before_insert")
def _ticket_company(_mapper, connection, ticket: Ticket) -> None:
    if ticket.organization_id is None:
        ticket.organization_id = connection.scalar(select(Batch.organization_id).where(Batch.id == ticket.batch_id))
    _derive_keys(ticket)


@event.listens_for(Ticket, "before_update")
def _ticket_keys(_mapper, _connection, ticket: Ticket) -> None:
    _derive_keys(ticket)


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


class SavedView(Base):
    """A named set of record filters and export columns, shared within a company.
    Holds no ticket data, so removing one loses nothing."""
    __tablename__ = "saved_views"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_saved_view_org_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    columns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # {column key: header text}, so an export can match a team's own spreadsheet.
    labels: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ReviewTiming(Base):
    """Opt-in pilot measurement: how long a ticket took to accept, and whether
    it needed correcting. Deliberately has no person and no ticket contents."""
    __tablename__ = "review_timings"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), nullable=False)
    intake_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    seconds: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False)
    fields_corrected: Mapped[int] = mapped_column(Integer, nullable=False)
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
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": lock_version}


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


class InvoiceTicketLink(Base):
    """A person's confirmation that a verified ticket is billed in full by one
    invoice line. Released links stay as history. The partial unique index means
    a ticket can have only one active link, enforced by the database. Partial
    allocation is deliberately unsupported."""
    __tablename__ = "invoice_ticket_links"
    __table_args__ = (
        Index("uq_invoice_ticket_link_active", "ticket_id", unique=True,
              sqlite_where=text("released_at IS NULL"), postgresql_where=text("released_at IS NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(ForeignKey("invoice_comparisons.id"), index=True, nullable=False)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    listed_number: Mapped[str] = mapped_column(String(100), nullable=False)
    # What the person saw: a later change to the ticket invalidates the link.
    verified_tons: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    ticket_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    released_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class InvoiceDocument(Base):
    """An uploaded supplier invoice PDF. Separate from comparisons: nothing here
    counts until a manager confirms a draft line."""
    __tablename__ = "invoice_documents"
    __table_args__ = (Index("uq_invoice_document_file", "organization_id", "sha256", unique=True,
                            sqlite_where=text("status <> 'voided'"), postgresql_where=text("status <> 'voided'")),)
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


class ExceptionReview(Base):
    """A person's handling of one billing exception: who owns it, where it stands, and
    any credit the supplier has confirmed. The exception itself is never stored: it is
    recomputed from invoices, tickets and rates, and found again by its key. Nothing
    here changes an invoice line, a ticket or a locked price.
    """
    __tablename__ = "exception_reviews"
    __table_args__ = (Index("uq_exception_review_key", "organization_id", "key", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    # open | investigating | disputed | recovered | resolved | dismissed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    assignee_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Credit the supplier confirmed, as a person recorded it. Never computed.
    recovered_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the exception said when a person last acted on it, kept for the record.
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    # Its figures at that moment. If they change after it was closed, it needs a person again.
    basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ExceptionEvent(Base):
    """Append-only history of every action on a billing exception."""
    __tablename__ = "exception_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # status | assignee | recovered_amount | note
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Hauler(Base):
    """A trucking company. Often a different business from the material supplier."""
    __tablename__ = "haulers"
    __table_args__ = (Index("uq_hauler_name", "organization_id", "name_key", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False)
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Truck(Base):
    __tablename__ = "trucks"
    __table_args__ = (Index("uq_truck_identifier", "organization_id", "hauler_id", "identifier", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    hauler_id: Mapped[int] = mapped_column(ForeignKey("haulers.id"), nullable=False, index=True)
    # The number painted on the truck or used on the time sheet.
    identifier: Mapped[str] = mapped_column(String(50), nullable=False)
    # tandem | tri_axle | quad_axle | semi_end_dump | belly_dump | other
    equipment_type: Mapped[str] = mapped_column(String(30), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())


class HaulingRate(Base):
    """An hourly agreement with a hauler: for one project or all, one truck type or all,
    from a date or with no start, until a date or open-ended. See services/trucking.py."""
    __tablename__ = "hauling_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    hauler_id: Mapped[int] = mapped_column(ForeignKey("haulers.id"), nullable=False, index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    equipment_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    # Paid hours below this are topped up at the hourly rate. Blank: no minimum.
    minimum_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    # Blank: standby isn't payable under this agreement, so standby leaves a shift unpriced.
    standby_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TruckShift(Base):
    """One truck's working day (or night). Times as written on the time sheet; hours a
    person approves; the price locked at approval. Approved figures can only change by
    reopening, which the database enforces (db_guards)."""
    __tablename__ = "truck_shifts"
    __table_args__ = (Index("ix_truck_shifts_org_date", "organization_id", "work_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    truck_id: Mapped[int] = mapped_column(ForeignKey("trucks.id"), nullable=False, index=True)
    hauler_id: Mapped[int] = mapped_column(ForeignKey("haulers.id"), nullable=False, index=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    driver_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # local time on site, as written
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    unpaid_break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    standby_hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal(0), server_default="0")
    # What the hauler's time sheet or invoice claims, if it says. Evidence, never used to pay.
    claimed_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    loads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Who signed the time sheet in the field, as written on it.
    field_signoff: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cost_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # needs_review | approved | disputed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="needs_review", server_default="needs_review")
    # Locked at approval.
    approved_work_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    approved_standby_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    approved_billable_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    applied_rate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the agreement, kept even if it's removed
    applied_hourly_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    applied_standby_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    applied_minimum_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    approved_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ShiftAllocation(Base):
    """Where an approved shift's hours and cost go: a project and, optionally, a cost code.
    Written at approval; the parts add up to the shift's cost to the cent."""
    __tablename__ = "shift_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("truck_shifts.id"), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    cost_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class TruckShiftEvent(Base):
    """Append-only history of every change to a truck shift."""
    __tablename__ = "truck_shift_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("truck_shifts.id"), nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
