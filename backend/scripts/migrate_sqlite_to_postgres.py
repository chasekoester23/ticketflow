"""Copy a TicketFlow SQLite database into an empty PostgreSQL database, or refuse.

Usage (from backend/, with the API and worker stopped):

    python scripts/migrate_sqlite_to_postgres.py --source ticketflow.db --preflight-only
    python scripts/migrate_sqlite_to_postgres.py --source ticketflow.db \
        --target postgresql://user@host/ticketflow --report copy-report.json

The source is opened read-only, and its checksum is compared before and after.
Preflight reports what PostgreSQL would refuse or change: it never guesses at a fix
and never edits anything. The copy runs in ONE transaction. Before it commits it
proves, from what is now in PostgreSQL, that every row is identical, that each
company's approved tons and cost are exact, and that relationships and audit history
are intact. Any difference rolls everything back and exits non-zero.

Reports hold table and column names, row ids, counts, checksums and money totals.
They never hold a ticket's values, a password hash or a session token.

See docs/engineering/postgres.md. Exit codes: 0 clean, 1 refused or rolled back, 2 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import quote

# This tool only touches the two databases named on its command line. It must never
# reach for whatever DATABASE_URL happens to be configured on this machine.
os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, JSON, Numeric, String, Table, UniqueConstraint, create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import Connection, make_url  # noqa: E402
from sqlalchemy.exc import DBAPIError, SQLAlchemyError  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app import models  # noqa: E402,F401 - registers every table
from app.db import Base, normalized_url  # noqa: E402
from app.db_guards import HISTORY_TABLES  # noqa: E402
from app.services.system_health import expected_revision  # noqa: E402

_BATCH_ROWS = 1000
_SAMPLE_IDS = 5
# A value stored by the app through a float is exact to within a float's noise.
# Further off than this and it really has more decimals than its column can hold.
_FLOAT_NOISE = 1e-9
_INT32 = 2 ** 31
_INT64 = 2 ** 63
# Migration 20261029_03 creates this placeholder company, so a brand-new database at
# the current migration has this one row and nothing else. It's what "empty" means.
PLACEHOLDER_COMPANY = "Original workspace"


class Unusable(Exception):
    """One value PostgreSQL would refuse or silently change."""

    def __init__(self, kind: str, detail: str):
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


class CopyFailed(Exception):
    """A check failed. The transaction is rolled back, so nothing was kept."""


# ------------------------------------------------------------------ what the code expects

@dataclass(frozen=True)
class ColumnSpec:
    name: str
    kind: str  # text | integer | decimal | float | boolean | date | timestamp | timestamptz | json
    nullable: bool
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    bits: int = 32


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]  # sorted by name, so a canonical row never depends on layout
    pk: str
    pk_is_integer: bool
    has_id_sequence: bool


def _column_spec(column) -> ColumnSpec:
    kind = column.type
    nullable = bool(column.nullable) and not column.primary_key
    if isinstance(kind, Boolean):
        return ColumnSpec(column.name, "boolean", nullable)
    if isinstance(kind, DateTime):
        return ColumnSpec(column.name, "timestamptz" if kind.timezone else "timestamp", nullable)
    if isinstance(kind, Date):
        return ColumnSpec(column.name, "date", nullable)
    if isinstance(kind, JSON):
        return ColumnSpec(column.name, "json", nullable)
    if isinstance(kind, Float):
        return ColumnSpec(column.name, "float", nullable)
    if isinstance(kind, Numeric):
        return ColumnSpec(column.name, "decimal", nullable, precision=kind.precision, scale=kind.scale)
    if isinstance(kind, Integer):
        return ColumnSpec(column.name, "integer", nullable, bits=64 if isinstance(kind, BigInteger) else 32)
    if isinstance(kind, String):  # Text is a String with no length
        return ColumnSpec(column.name, "text", nullable, length=kind.length)
    raise RuntimeError(f"{column.table.name}.{column.name} has a type this tool doesn't know how to copy exactly.")


def table_specs() -> list[TableSpec]:
    """Every table, parents before children, read from the models test_schema.py
    proves are identical to what the migrations build."""
    specs = []
    for table in Base.metadata.sorted_tables:
        pk = [column.name for column in table.primary_key.columns]
        if len(pk) != 1:
            raise RuntimeError(f"{table.name} doesn't have a single-column primary key.")
        columns = tuple(sorted((_column_spec(column) for column in table.columns), key=lambda spec: spec.name))
        pk_is_integer = isinstance(table.primary_key.columns[pk[0]].type, Integer)
        specs.append(TableSpec(table.name, columns, pk[0], pk_is_integer, pk_is_integer))
    return specs


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ------------------------------------------------------------------ values, one column type at a time

def _decimal(spec: ColumnSpec, raw) -> Decimal:
    if isinstance(raw, bytes):
        try:
            value = Decimal(raw.decode("utf-8"))
        except (UnicodeDecodeError, InvalidOperation):
            raise Unusable("not_a_number", "isn't a number") from None
    elif isinstance(raw, int):
        value = Decimal(raw)
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            raise Unusable("not_a_number", "isn't a finite number")
        # The same formatting SQLAlchemy uses to read a float back from SQLite, so
        # the copy holds exactly the figure the app was already showing.
        value = Decimal(f"{raw:.{spec.scale}f}")
        if abs(raw - float(value)) > _FLOAT_NOISE:
            raise Unusable("too_many_decimals", f"has more than {spec.scale} decimal places, so its column would round it")
    else:
        raise Unusable("not_a_number", "isn't a number")
    if not value.is_finite():
        raise Unusable("not_a_number", "isn't a finite number")
    try:
        exact = value.quantize(Decimal(1).scaleb(-spec.scale))
    except InvalidOperation:
        raise Unusable("number_too_large", f"doesn't fit NUMERIC({spec.precision}, {spec.scale})") from None
    if exact != value:
        raise Unusable("too_many_decimals", f"has more than {spec.scale} decimal places, so its column would round it")
    if abs(exact) >= Decimal(10) ** (spec.precision - spec.scale):
        raise Unusable("number_too_large", f"doesn't fit NUMERIC({spec.precision}, {spec.scale})")
    return exact


def _timestamp(spec: ColumnSpec, raw) -> datetime:
    if not isinstance(raw, bytes):
        raise Unusable("bad_timestamp", "isn't a timestamp")
    try:
        value = datetime.fromisoformat(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Unusable("bad_timestamp", "isn't a readable timestamp") from None
    # SQLite keeps UTC with the zone left off. Anything carrying a zone is converted.
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=timezone.utc) if spec.kind == "timestamptz" else value


def canonical_timestamp(spec: ColumnSpec, value: datetime) -> str:
    if spec.kind == "timestamptz":
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _has_nul(value) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(_has_nul(item) for item in value)
    if isinstance(value, dict):
        return any(_has_nul(key) or _has_nul(item) for key, item in value.items())
    return False


def _no_constants(name: str):
    raise ValueError(name)


def canonical_json(document: str) -> str:
    """Key order and spacing never count as a difference. JSON null stays apart from
    SQL NULL, because the app wrote both and reads them differently."""
    return json.dumps(json.loads(document, parse_constant=_no_constants), sort_keys=True, separators=(",", ":"))


def read_value(spec: ColumnSpec, raw) -> tuple[object, object]:
    """One raw SQLite value as (what to store in PostgreSQL, its canonical form).
    Raises Unusable rather than adjusting anything."""
    if raw is None:
        if not spec.nullable:
            raise Unusable("null_in_required_column", "is blank, but PostgreSQL requires a value")
        return None, None
    if spec.kind == "text":
        if not isinstance(raw, bytes):
            raw = str(raw).encode("utf-8")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise Unusable("not_utf8", "isn't valid UTF-8 text") from None
        if "\x00" in value:
            raise Unusable("nul_byte", "contains a NUL byte, which PostgreSQL text can't hold")
        if spec.length is not None and len(value) > spec.length:
            raise Unusable("value_too_long", f"is longer than the column's {spec.length} characters")
        return value, value
    if spec.kind == "integer":
        if isinstance(raw, float) and raw.is_integer():
            raw = int(raw)
        if not isinstance(raw, int):
            raise Unusable("not_an_integer", "isn't a whole number")
        limit = _INT64 if spec.bits == 64 else _INT32
        if not -limit <= raw < limit:
            raise Unusable("integer_out_of_range", f"doesn't fit a {spec.bits}-bit integer")
        return raw, raw
    if spec.kind == "decimal":
        value = _decimal(spec, raw)
        return value, format(value, f".{spec.scale}f")
    if spec.kind == "float":
        if not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise Unusable("not_a_number", "isn't a finite number")
        return float(raw), repr(float(raw))
    if spec.kind == "boolean":
        if raw not in (0, 1) or isinstance(raw, (bytes, float)):
            raise Unusable("not_a_boolean", "isn't 0 or 1")
        return bool(raw), bool(raw)
    if spec.kind == "date":
        if not isinstance(raw, bytes):
            raise Unusable("bad_date", "isn't a date")
        try:
            value = date.fromisoformat(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Unusable("bad_date", "isn't a readable date") from None
        return value, value.isoformat()
    if spec.kind in ("timestamp", "timestamptz"):
        value = _timestamp(spec, raw)
        return value, canonical_timestamp(spec, value)
    if spec.kind == "json":
        try:
            document = raw.decode("utf-8") if isinstance(raw, bytes) else json.dumps(raw)
            parsed = json.loads(document, parse_constant=_no_constants)
        except (UnicodeDecodeError, ValueError):
            raise Unusable("bad_json", "isn't valid JSON") from None
        if _has_nul(parsed):
            raise Unusable("nul_byte", "contains a NUL byte, which PostgreSQL text can't hold")
        return document, canonical_json(document)
    raise RuntimeError(spec.kind)


def _row_line(values: Iterable) -> bytes:
    return json.dumps(list(values), ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


# ------------------------------------------------------------------ what gets recorded

class Findings:
    """What preflight found, with row ids a person can look up and no values."""

    def __init__(self) -> None:
        self._by_key: dict[tuple, dict] = {}

    def add(self, kind: str, table: str | None, *, column: str | None = None, detail: str = "", row_id=None, rows: int = 1, **extra) -> None:
        key = (kind, table, column, tuple(sorted(extra.items())))
        entry = self._by_key.setdefault(key, {
            "kind": kind, "table": table, "column": column, "detail": detail, "rows": 0, "sample_ids": [], **extra})
        entry["rows"] += rows
        if row_id is not None and len(entry["sample_ids"]) < _SAMPLE_IDS:
            entry["sample_ids"].append(row_id)

    def __bool__(self) -> bool:
        return bool(self._by_key)

    def out(self) -> list[dict]:
        return list(self._by_key.values())


def describe(problem: dict) -> str:
    where = ".".join(part for part in (problem.get("table"), problem.get("column")) if part)
    rows = f"{problem['rows']} row(s)" if problem.get("rows") else ""
    ids = f", ids {', '.join(str(i) for i in problem['sample_ids'])}" if problem.get("sample_ids") else ""
    index = f" ({problem['index']})" if problem.get("index") else ""
    return f"{problem['kind']}: {where}{index} {rows}{ids}. {problem.get('detail', '')}".strip()


# ------------------------------------------------------------------ source

def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Source:
    """A read-only view of the SQLite file. Even a bug here can't change it."""

    def __init__(self, path: Path):
        self.path = path
        uri = f"file:{quote(str(path.resolve()))}?mode=ro"
        self.meta = sqlite3.connect(uri, uri=True)
        self.raw = sqlite3.connect(uri, uri=True)
        self.raw.text_factory = bytes  # So bad UTF-8 is reported, not raised mid-scan.
        for connection in (self.meta, self.raw):
            connection.execute("PRAGMA query_only = ON")

    def close(self) -> None:
        self.meta.close()
        self.raw.close()

    def revision(self) -> str | None:
        try:
            rows = self.meta.execute("SELECT version_num FROM alembic_version").fetchall()
        except sqlite3.DatabaseError:
            return None
        return rows[0][0] if len(rows) == 1 else None

    def scalar(self, sql: str):
        return self.meta.execute(sql).fetchone()[0]


def sidecar_in_use(path: Path) -> bool:
    """A journal or write-ahead log means a writer is active, or crashed mid-write.
    A read-only copy can't safely take a snapshot of that."""
    return any((sidecar := path.with_name(path.name + suffix)).exists() and sidecar.stat().st_size > 0
               for suffix in ("-journal", "-wal"))


def _check_schema(source: Source, specs: list[TableSpec], findings: Findings) -> set[str]:
    """Which tables exist as the code expects. Others can't be scanned column by column."""
    actual = {name for (name,) in source.meta.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    usable = set()
    for table in sorted(actual - {spec.name for spec in specs} - {"alembic_version"}):
        findings.add("unexpected_table", table, detail="isn't part of the current schema, so it wouldn't be copied", rows=0)
    for spec in specs:
        if spec.name not in actual:
            findings.add("missing_table", spec.name, detail="is in the current schema but not in this database", rows=0)
            continue
        have = {row[1] for row in source.meta.execute(f"PRAGMA table_info({_quote(spec.name)})")}
        want = {column.name for column in spec.columns}
        for name in sorted(want - have):
            findings.add("missing_column", spec.name, column=name, detail="is in the current schema but not in this database", rows=0)
        for name in sorted(have - want):
            findings.add("unexpected_column", spec.name, column=name, detail="isn't part of the current schema, so it wouldn't be copied", rows=0)
        if have == want:
            usable.add(spec.name)
    return usable


def _check_references(source: Source, usable: set[str], findings: Findings) -> None:
    """Every foreign key the schema declares, whether or not this file enforces it."""
    for table in Base.metadata.sorted_tables:
        if table.name not in usable:
            continue
        pk = next(iter(table.primary_key.columns)).name
        for key in table.foreign_keys:
            parent = key.column.table
            if parent.name not in usable:
                continue
            child, other = key.parent.name, key.column.name
            sql = (f"SELECT c.{_quote(pk)} FROM {_quote(table.name)} c LEFT JOIN {_quote(parent.name)} p "
                   f"ON c.{_quote(child)} = p.{_quote(other)} "
                   f"WHERE c.{_quote(child)} IS NOT NULL AND p.{_quote(other)} IS NULL")
            for (row_id,) in source.meta.execute(sql):
                findings.add("missing_reference", table.name, column=child, row_id=row_id,
                             detail=f"points at a row of {parent.name} that doesn't exist", references=parent.name)


def _unique_rules(table: Table) -> list[tuple[str, list[str], str | None]]:
    rules = []
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            columns = [column.name for column in constraint.columns]
            rules.append((constraint.name or f"{table.name}_{'_'.join(columns)}_key", columns, None))
    for index in table.indexes:
        if index.unique:
            where = index.dialect_options["sqlite"].get("where")
            rules.append((index.name, [column.name for column in index.columns], None if where is None else str(where)))
    return rules


def _check_duplicates(source: Source, usable: set[str], findings: Findings) -> None:
    """Rows a unique rule on the target would refuse. The source may lack that index:
    migration 20261106_18 leaves one out rather than touch existing rows."""
    for table in Base.metadata.sorted_tables:
        if table.name not in usable:
            continue
        pk = next(iter(table.primary_key.columns)).name
        for name, columns, where in _unique_rules(table):
            filters = [f"{_quote(column)} IS NOT NULL" for column in columns]  # NULLs never collide, on either database.
            if where:
                filters.append(f"({where})")
            group = ", ".join(_quote(column) for column in columns)
            sql = (f"SELECT GROUP_CONCAT({_quote(pk)}) FROM {_quote(table.name)} WHERE {' AND '.join(filters)} "
                   f"GROUP BY {group} HAVING COUNT(*) > 1")
            for (ids,) in source.meta.execute(sql):
                findings.add("duplicate_rows", table.name, index=name, row_id=str(ids),
                             detail="these rows break a unique rule PostgreSQL enforces")


# Rows that must agree about which company they belong to. Foreign keys can't state
# this, and a wrong answer here would show one company's records to another. Each
# returns the offending row's id, and is plain SQL that runs on both databases.
RELATIONSHIP_CHECKS = (
    ("tickets", "ticket and its batch are in different companies",
     "SELECT t.id FROM tickets t JOIN batches b ON b.id = t.batch_id WHERE t.organization_id IS NULL OR t.organization_id <> b.organization_id"),
    ("tickets", "ticket and its file are in different batches",
     "SELECT t.id FROM tickets t JOIN uploaded_files f ON f.id = t.file_id WHERE f.batch_id <> t.batch_id"),
    ("batches", "batch and its project are in different companies",
     "SELECT b.id FROM batches b JOIN projects p ON p.id = b.project_id WHERE b.organization_id <> p.organization_id"),
    ("scan_slots", "scan slot and its file are in different batches",
     "SELECT s.id FROM scan_slots s JOIN uploaded_files f ON f.id = s.file_id WHERE f.batch_id <> s.batch_id"),
    ("supplier_rates", "rate and its supplier are in different companies",
     "SELECT r.id FROM supplier_rates r JOIN suppliers s ON s.id = r.supplier_id WHERE r.organization_id <> s.organization_id"),
    ("supplier_rates", "rate and its material are in different companies",
     "SELECT r.id FROM supplier_rates r JOIN materials m ON m.id = r.material_id WHERE r.organization_id <> m.organization_id"),
    ("invoice_comparisons", "invoice line and its project are in different companies",
     "SELECT c.id FROM invoice_comparisons c JOIN projects p ON p.id = c.project_id WHERE c.organization_id <> p.organization_id"),
    ("invoice_documents", "invoice document and its project are in different companies",
     "SELECT d.id FROM invoice_documents d JOIN projects p ON p.id = d.project_id WHERE d.organization_id <> p.organization_id"),
    ("invoice_ticket_links", "invoice link joins a line and a ticket from different companies",
     "SELECT l.id FROM invoice_ticket_links l JOIN invoice_comparisons c ON c.id = l.comparison_id "
     "JOIN tickets t ON t.id = l.ticket_id WHERE c.organization_id <> t.organization_id"),
)
AUDIT_BY_COMPANY = ("SELECT t.organization_id, COUNT(*) FROM audit_events a JOIN tickets t ON t.id = a.ticket_id "
                    "GROUP BY t.organization_id ORDER BY t.organization_id")


@dataclass
class TableScan:
    rows: int
    sha256: str


class Totals:
    """Each company's approved figures, added up in Decimal from the canonical rows."""

    def __init__(self) -> None:
        self.by_company: dict[str, dict] = {}

    def add(self, row: dict) -> None:
        if row["status"] != "verified" or not row["tonnage_confirmed"]:
            return
        entry = self.by_company.setdefault(str(row["organization_id"]), {
            "approved_tickets": 0, "approved_tons": Decimal(0), "approved_cost": Decimal(0), "without_cost": 0})
        entry["approved_tickets"] += 1
        entry["approved_tons"] += Decimal(row["tonnage"]) if row["tonnage"] is not None else Decimal(0)
        if row["approved_cost"] is None:
            entry["without_cost"] += 1
        else:
            entry["approved_cost"] += Decimal(row["approved_cost"])

    def out(self) -> dict:
        return {company: format_totals(entry) for company, entry in sorted(self.by_company.items())}


def format_totals(entry: dict) -> dict:
    return {"approved_tickets": int(entry["approved_tickets"]),
            "approved_tons": format(Decimal(entry["approved_tons"]), ".3f"),
            "approved_cost": format(Decimal(entry["approved_cost"]), ".2f"),
            "without_cost": int(entry["without_cost"])}


def scan_table(source: Source, spec: TableSpec, findings: Findings, totals: Totals | None = None,
               sink: Callable[[TableSpec, list[dict]], None] | None = None) -> TableScan:
    """Read one table in primary-key order, a chunk at a time so memory stays flat.
    Every value is checked, hashed in canonical form and optionally sent to the target."""
    names = [column.name for column in spec.columns]
    sql = f"SELECT {', '.join(_quote(name) for name in names)} FROM {_quote(spec.name)} ORDER BY {_quote(spec.pk)}"
    digest = hashlib.sha256()
    rows = 0
    pk_index = names.index(spec.pk)
    batch: list[dict] = []
    cursor = source.raw.execute(sql)
    while True:
        chunk = cursor.fetchmany(_BATCH_ROWS)
        if not chunk:
            break
        for raw_row in chunk:
            rows += 1
            row_id = raw_row[pk_index] if spec.pk_is_integer else None
            stored: dict = {}
            canonical = []
            usable = True
            for column, raw in zip(spec.columns, raw_row):
                try:
                    value, form = read_value(column, raw)
                except Unusable as exc:
                    findings.add(exc.kind, spec.name, column=column.name, row_id=row_id, detail=exc.detail)
                    usable = False
                    continue
                stored[column.name] = value
                canonical.append(form)
            if not usable:
                continue
            digest.update(_row_line(canonical))
            if totals is not None and spec.name == "tickets":
                totals.add(dict(zip(names, canonical)))
            if sink is not None:
                batch.append(stored)
        if sink is not None and batch:
            sink(spec, batch)
            batch = []
    return TableScan(rows, digest.hexdigest())


def history_counts(execute) -> dict:
    """``execute`` runs one SQL string and returns all its rows."""
    return {"tables": {name: int(execute(f"SELECT COUNT(*) FROM {name}")[0][0]) for name in HISTORY_TABLES},
            "audit_events_by_company": {str(company): int(count) for company, count in execute(AUDIT_BY_COMPANY)}}


def preflight(source: Source, specs: list[TableSpec]) -> dict:
    """Everything PostgreSQL would refuse or change, and the figures a copy must match."""
    findings = Findings()
    expected, revision = expected_revision(), source.revision()
    if revision != expected:
        findings.add("source_not_at_current_migration", None, rows=0,
                     detail=f"This database is at {revision or 'no recorded version'}, and this code needs {expected}. "
                            "Back it up, then run 'python -m alembic upgrade head' against it.")
    try:
        integrity = source.scalar("PRAGMA integrity_check")
    except sqlite3.DatabaseError as exc:
        integrity = type(exc).__name__
    if integrity != "ok":
        findings.add("source_corrupt", None, rows=0, detail="SQLite's integrity check failed. Restore from a backup first.")
    usable = _check_schema(source, specs, findings)
    _check_references(source, usable, findings)
    _check_duplicates(source, usable, findings)
    for table, detail, sql in RELATIONSHIP_CHECKS:
        tables_used = {word for word in sql.replace(",", " ").split() if word in {spec.name for spec in specs}}
        if tables_used <= usable:
            for (row_id,) in source.meta.execute(sql):
                findings.add("company_mismatch", table, row_id=row_id, detail=detail)
    scans: dict[str, TableScan] = {}
    totals = Totals()
    for spec in specs:
        if spec.name in usable:
            scans[spec.name] = scan_table(source, spec, findings, totals)
    result = {
        "problems": findings.out(),
        "tables": {name: {"rows": scan.rows, "sha256": scan.sha256} for name, scan in scans.items()},
        "company_totals": totals.out(),
    }
    if not findings:
        result["history"] = history_counts(lambda sql: source.meta.execute(sql).fetchall())
    return result


# ------------------------------------------------------------------ target

class PostgresTarget:
    """Everything the copy asks of PostgreSQL, in one transaction on one connection.
    Its methods are all a test stand-in has to provide, so most of the copy's logic
    can be tested where PostgreSQL isn't available."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def revision(self) -> str | None:
        if not inspect(self.connection).has_table("alembic_version"):
            return None
        rows = self.connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def fetch(self, sql: str) -> list:
        return list(self.connection.execute(text(sql)).fetchall())

    def row_count(self, spec: TableSpec) -> int:
        return int(self.connection.execute(text(f"SELECT COUNT(*) FROM {_quote(spec.name)}")).scalar())

    def holds_only_the_placeholder_company(self, spec: TableSpec) -> bool:
        return spec.name == "organizations" and [row[0] for row in self.fetch("SELECT name FROM organizations")] == [PLACEHOLDER_COMPANY]

    def remove_placeholder_company(self) -> None:
        """Inside the transaction, so a rollback puts it back."""
        self.connection.execute(text("DELETE FROM organizations WHERE name = :name"), {"name": PLACEHOLDER_COMPANY})

    def missing_unique_indexes(self, specs: list[TableSpec]) -> list[str]:
        missing = []
        inspector = inspect(self.connection)
        for table in Base.metadata.sorted_tables:
            present = {index["name"] for index in inspector.get_indexes(table.name)}
            missing.extend(index.name for index in table.indexes if index.unique and index.name not in present)
        return missing

    def lock(self, specs: list[TableSpec]) -> None:
        """Nobody else can write until this commits or rolls back."""
        names = ", ".join(_quote(spec.name) for spec in specs)
        self.connection.execute(text(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE"))

    def _insert_sql(self, spec: TableSpec) -> str:
        columns = ", ".join(_quote(column.name) for column in spec.columns)
        values = ", ".join(f"CAST(:{column.name} AS json)" if column.kind == "json" else f":{column.name}"
                           for column in spec.columns)
        return f"INSERT INTO {_quote(spec.name)} ({columns}) VALUES ({values})"

    def insert(self, spec: TableSpec, rows: list[dict]) -> None:
        self.connection.execute(text(self._insert_sql(spec)), rows)

    def _select_sql(self, spec: TableSpec) -> str:
        columns = ", ".join(f"CAST({_quote(column.name)} AS text)" if column.kind == "json" else _quote(column.name)
                            for column in spec.columns)
        # A byte-order sort, whatever this server's locale: the source sorts the same way.
        order = _quote(spec.pk) if spec.pk_is_integer else f'{_quote(spec.pk)} COLLATE "C"'
        return f"SELECT {columns} FROM {_quote(spec.name)} ORDER BY {order}"

    def canonical_rows(self, spec: TableSpec) -> Iterator[list]:
        result = self.connection.execution_options(yield_per=_BATCH_ROWS).execute(text(self._select_sql(spec)))
        for row in result:
            yield [self._canonical(column, value) for column, value in zip(spec.columns, row)]

    @staticmethod
    def _canonical(spec: ColumnSpec, value):
        if value is None:
            return None
        if spec.kind == "decimal":
            return format(value, f".{spec.scale}f")
        if spec.kind == "float":
            return repr(float(value))
        if spec.kind == "date":
            return value.isoformat()
        if spec.kind in ("timestamp", "timestamptz"):
            return canonical_timestamp(spec, value)
        if spec.kind == "json":
            return canonical_json(value)
        return value  # text, integer and boolean arrive as they are.

    def company_totals(self) -> dict:
        rows = self.fetch(
            "SELECT organization_id, COUNT(*), COALESCE(SUM(tonnage), 0), COALESCE(SUM(approved_cost), 0), "
            "SUM(CASE WHEN approved_cost IS NULL THEN 1 ELSE 0 END) FROM tickets "
            "WHERE status = 'verified' AND tonnage_confirmed IS TRUE GROUP BY organization_id")
        return {str(row[0]): format_totals({"approved_tickets": row[1], "approved_tons": row[2], "approved_cost": row[3], "without_cost": row[4]})
                for row in sorted(rows, key=lambda r: str(r[0]))}

    def set_sequences(self, specs: list[TableSpec]) -> dict[str, int]:
        """Next id for every table. ALTER SEQUENCE ... RESTART is transactional, unlike
        setval, so a rollback also leaves these exactly where they were."""
        next_ids = {}
        for spec in specs:
            if not spec.has_id_sequence:
                continue
            sequence = self.connection.execute(text("SELECT pg_get_serial_sequence(:name, :column)"),
                                               {"name": _quote(spec.name), "column": spec.pk}).scalar()
            if sequence is None:
                raise CopyFailed(f"{spec.name} has no id sequence.")
            highest = self.connection.execute(text(f"SELECT COALESCE(MAX({_quote(spec.pk)}), 0) FROM {_quote(spec.name)}")).scalar()
            self.connection.execute(text(f"ALTER SEQUENCE {sequence} RESTART WITH {int(highest) + 1}"))
            next_ids[spec.name] = int(highest) + 1
        return next_ids

    def next_ids(self, specs: list[TableSpec]) -> dict[str, int]:
        found = {}
        for spec in specs:
            if spec.has_id_sequence:
                sequence = self.connection.execute(text("SELECT pg_get_serial_sequence(:name, :column)"),
                                                   {"name": _quote(spec.name), "column": spec.pk}).scalar()
                last, called = self.connection.execute(text(f"SELECT last_value, is_called FROM {sequence}")).one()
                found[spec.name] = int(last) + 1 if called else int(last)
        return found


def _refusal_for_target(target: PostgresTarget, specs: list[TableSpec]) -> list[str]:
    expected, revision = expected_revision(), target.revision()
    if revision != expected:
        return [f"The target is at {revision or 'no recorded version'}, and this code needs {expected}. "
                "Run 'python -m alembic upgrade head' against the empty target first."]
    occupied = [spec.name for spec in specs if target.row_count(spec) and not target.holds_only_the_placeholder_company(spec)]
    if occupied:
        return ["The target isn't empty: " + ", ".join(occupied) + " already hold rows. Nothing was changed."]
    missing = target.missing_unique_indexes(specs)
    if missing:
        return ["The target is missing unique rules this copy relies on: " + ", ".join(missing) + "."]
    return []


def copy_into(source: Source, target: PostgresTarget, specs: list[TableSpec], *, source_digest: str,
              after_copy: Callable[[PostgresTarget], None] | None = None) -> dict:
    """Copy and verify inside the caller's transaction. Returns the verification, or
    raises CopyFailed, in which case the caller rolls back."""
    refusal = _refusal_for_target(target, specs)
    if refusal:
        raise CopyFailed(refusal[0])
    target.lock(specs)
    # Checked again now that nothing else can write.
    refusal = _refusal_for_target(target, specs)
    if refusal:
        raise CopyFailed(refusal[0])
    target.remove_placeholder_company()

    findings = Findings()
    totals = Totals()
    scans = {spec.name: scan_table(source, spec, findings, totals, target.insert) for spec in specs}
    if findings:
        raise CopyFailed("The source changed after preflight: " + "; ".join(describe(p) for p in findings.out()))
    sequences = target.set_sequences(specs)
    if after_copy is not None:
        after_copy(target)
    return verify(source, target, specs, scans, totals, sequences, source_digest=source_digest)


def verify(source: Source, target: PostgresTarget, specs: list[TableSpec], scans: dict[str, TableScan], totals: Totals,
           sequences: dict[str, int], *, source_digest: str) -> dict:
    """Every comparison is read back from the target, so this checks what's really there."""
    problems: list[str] = []
    tables: dict[str, dict] = {}
    for spec in specs:
        digest = hashlib.sha256()
        rows = 0
        for canonical in target.canonical_rows(spec):
            rows += 1
            digest.update(_row_line(canonical))
        scan = scans[spec.name]
        counted = target.row_count(spec)
        match = rows == scan.rows and counted == scan.rows and digest.hexdigest() == scan.sha256
        tables[spec.name] = {"source_rows": scan.rows, "target_rows": counted, "source_sha256": scan.sha256,
                             "target_sha256": digest.hexdigest(), "match": match}
        if counted != scan.rows:
            problems.append(f"{spec.name}: {scan.rows} row(s) in the source, {counted} in the target.")
        elif not match:
            problems.append(f"{spec.name}: row contents differ between the source and the target.")

    source_totals, target_totals = totals.out(), target.company_totals()
    if source_totals != target_totals:
        problems.append("Approved tons or cost differ for at least one company.")

    relationships = {}
    for table, detail, sql in RELATIONSHIP_CHECKS:
        wrong = len(target.fetch(sql))
        relationships[detail] = wrong
        if wrong:
            problems.append(f"{table}: {wrong} row(s) where the {detail}.")
    orphaned = _target_orphans(target)
    if orphaned:
        problems.append(f"{orphaned} row(s) in the target point at a row that doesn't exist.")

    source_history = history_counts(lambda sql: source.meta.execute(sql).fetchall())
    target_history = history_counts(target.fetch)
    if source_history != target_history:
        problems.append("Audit history counts differ between the source and the target.")

    next_ids = target.next_ids(specs)
    if next_ids != sequences:
        problems.append("An id sequence isn't where this copy set it.")
    if file_digest(source.path) != source_digest:
        problems.append("The source file changed during the copy. Stop the API and worker, then run this again.")

    result = {
        "tables": tables,
        "company_totals": {"source": source_totals, "target": target_totals, "match": source_totals == target_totals},
        "relationships": relationships,
        "references_missing": orphaned,
        "history": {"source": source_history, "target": target_history, "match": source_history == target_history},
        "next_ids": next_ids,
        "problems": problems,
    }
    if problems:
        error = CopyFailed("; ".join(problems))
        error.verification = result  # type: ignore[attr-defined]
        raise error
    return result


def _target_orphans(target: PostgresTarget) -> int:
    total = 0
    for table in Base.metadata.sorted_tables:
        for key in table.foreign_keys:
            parent, child, other = key.column.table.name, key.parent.name, key.column.name
            total += int(target.fetch(
                f"SELECT COUNT(*) FROM {_quote(table.name)} c LEFT JOIN {_quote(parent)} p ON c.{_quote(child)} = p.{_quote(other)} "
                f"WHERE c.{_quote(child)} IS NOT NULL AND p.{_quote(other)} IS NULL")[0][0])
    return total


# ------------------------------------------------------------------ running it

def write_report(path: Path, report: dict) -> None:
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2, default=str)


def _target_label(url: str) -> str:
    parsed = make_url(url)
    return f"{parsed.host or 'local socket'}:{parsed.port or 5432}/{parsed.database}"


def _target_engine(url: str):
    # No statement timeout: a large copy legitimately runs long. A short lock timeout,
    # so a busy target refuses clearly rather than hanging. Parameters stay out of errors.
    return create_engine(normalized_url(url), poolclass=NullPool, hide_parameters=True,
                         connect_args={"options": "-c lock_timeout=10000 -c statement_timeout=0 -c timezone=UTC"})


def _is_postgres(url: str) -> bool:
    try:
        return make_url(normalized_url(url)).get_backend_name() == "postgresql"
    except SQLAlchemyError:
        return False


def run(*, source_path: Path, target_url: str | None, report_path: Path, preflight_only: bool,
        after_copy: Callable[[PostgresTarget], None] | None = None) -> int:
    report: dict = {"tool": "migrate_sqlite_to_postgres", "started_at": datetime.now(timezone.utc).isoformat(),
                    "mode": "preflight" if preflight_only else "copy", "outcome": "refused", "problems": []}

    def finish(code: int, outcome: str, *problems: str) -> int:
        report["outcome"] = outcome
        report["problems"].extend(problems)
        write_report(report_path, report)
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"{outcome}. Report: {report_path}")
        return code

    if not source_path.is_file():
        return finish(1, "refused", f"Source database not found: {source_path}")
    if sidecar_in_use(source_path):
        return finish(1, "refused", "The source has an active journal or write-ahead log. Stop the API and worker, then run this again.")
    if not preflight_only and not _is_postgres(target_url or ""):
        return finish(1, "refused", "The target must be a PostgreSQL database.")

    digest_before = file_digest(source_path)
    report["source"] = {"path": str(source_path), "bytes": source_path.stat().st_size, "sha256_before": digest_before}
    specs = table_specs()
    source = Source(source_path)
    try:
        report["source"]["revision"] = source.revision()
        found = preflight(source, specs)
        report["preflight"] = found
        digest_after = file_digest(source_path)
        report["source"]["sha256_after"] = digest_after
        report["source"]["unchanged"] = digest_after == digest_before
        if digest_after != digest_before:
            return finish(1, "refused", "The source file changed during preflight. Stop the API and worker, then run this again.")
        if found["problems"]:
            print(f"Preflight found {len(found['problems'])} kind(s) of problem. Nothing was changed, and nothing is guessed at:", file=sys.stderr)
            for problem in found["problems"]:
                print("  - " + describe(problem), file=sys.stderr)
            return finish(1, "preflight_found_problems")
        if preflight_only:
            print(f"Preflight clean: {sum(table['rows'] for table in found['tables'].values())} row(s) in {len(found['tables'])} tables.")
            return finish(0, "preflight_clean")

        report["target"] = {"database": _target_label(target_url)}
        engine = _target_engine(target_url)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    target = PostgresTarget(connection)
                    report["target"]["revision"] = target.revision()
                    report["verification"] = copy_into(source, target, specs, source_digest=digest_before, after_copy=after_copy)
                    report["source"]["sha256_after"] = file_digest(source_path)
                    transaction.commit()
                except CopyFailed as exc:
                    transaction.rollback()
                    if hasattr(exc, "verification"):
                        report["verification"] = exc.verification
                    return finish(1, "rolled_back", f"Nothing was kept in the target. {exc}")
                except DBAPIError as exc:
                    # The database's own message can quote a row's values, so only its
                    # kind is reported.
                    transaction.rollback()
                    return finish(1, "rolled_back", f"PostgreSQL refused the copy ({type(exc.orig).__name__}). Nothing was kept in the target.")
                except Exception as exc:
                    transaction.rollback()
                    return finish(1, "rolled_back", f"The copy stopped ({type(exc).__name__}). Nothing was kept in the target.")
        except SQLAlchemyError as exc:
            cause = getattr(exc, "orig", exc)
            return finish(1, "refused", f"Couldn't use the target database ({type(cause).__name__}). Nothing was changed.")
        finally:
            engine.dispose()
        rows = sum(table["target_rows"] for table in report["verification"]["tables"].values())
        print(f"Copied and verified {rows} row(s) in {len(report['verification']['tables'])} tables.")
        return finish(0, "copied")
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True, help="The SQLite database file. Opened read-only.")
    parser.add_argument("--target", help="The PostgreSQL URL: empty, and already at the current migration. "
                                         "Leave the password out of the URL and use PGPASSWORD or ~/.pgpass.")
    parser.add_argument("--preflight-only", action="store_true", help="Check the source and stop. Needs no target.")
    parser.add_argument("--report", type=Path, help="Where to write the JSON report.")
    args = parser.parse_args(argv)
    if args.preflight_only == bool(args.target):
        parser.error("Give either --preflight-only or --target.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path = args.report or Path(f"sqlite-to-postgres-{stamp}.json")
    return run(source_path=args.source, target_url=args.target, report_path=report_path, preflight_only=args.preflight_only)


if __name__ == "__main__":
    raise SystemExit(main())
