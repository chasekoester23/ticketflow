"""Supplier delivery spreadsheets (CSV or Excel) into the normal ticket review.

What a file goes through, and what it never does:

- **Read, never run.** Excel files are opened with openpyxl in read-only mode with
  formulas kept as their text (``data_only=False``): nothing is calculated and no
  cached result is trusted. A formula in a mapped cell is a problem on that row, and
  its text never becomes a ticket value (it could also become a live formula in an
  export). A CSV cell starting with ``=`` is treated the same way.
- **Limits before work.** Size, rows, columns and cell length are capped, and an
  Excel file's uncompressed size is checked before it is opened.
- **The review form's rules.** Each row is interpreted with the same parsers and the
  same ``compute_flags`` as a ticket typed on the review screen, pounds become tons
  exactly as there (2,000 lb = 1 t, the pounds kept), and an imported ticket is an
  ordinary ``needs_review`` ticket. Approval, pricing and locks are unchanged.
- **Duplicates are a person's call.** The same supplier and ticket number already in
  the company (any project, any status), or earlier in the same file, is skipped
  unless the person chooses to import it anyway.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Batch, Project, Ticket, normal_name
from . import units
from .validation import compute_flags, parse_date, parse_tonnage

MAX_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED = 100 * 1024 * 1024  # an Excel file is a zip: refuse one that expands past this
MAX_ROWS = 2000                       # data rows; a month of one supplier's deliveries fits
MAX_SCANNED_ROWS = 20_000             # blank or formatted-only rows count toward this, so a sheet can't stall us
MAX_COLUMNS = 60
MAX_CELL = 500
HEADER_SEARCH = 20                    # the header is looked for in this many rows from the top
SAMPLE_ROWS = 5

FIELDS = ("ticket_number", "delivery_date", "material_type", "quantity", "unit", "supplier", "job_location", "truck_number")
LABELS = {"ticket_number": "Ticket number", "delivery_date": "Date", "material_type": "Material", "quantity": "Quantity",
          "unit": "Unit", "supplier": "Supplier", "job_location": "Job", "truck_number": "Truck"}
REQUIRED = ("ticket_number", "quantity")
LIMITS = {"ticket_number": 100, "material_type": 200, "supplier": 200, "job_location": 200, "truck_number": 50}
POUNDS = {"lb", "lbs", "pound", "pounds", "#"}
# Review flags that don't stop approval (api.verify_ticket): a warning on the preview, not a problem.
WARNINGS = ("Tonnage unusually large", "Weights: gross minus tare")

# Header text, normalised (see _normal_header), to the field it usually means.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "ticket_number": ("ticket", "ticket no", "ticket number", "ticket num", "tkt", "tkt no", "scale ticket", "scale ticket no",
                      "delivery ticket", "delivery ticket no", "ticket id", "load ticket", "weighmaster ticket"),
    "delivery_date": ("date", "delivery date", "ticket date", "ship date", "load date", "date shipped", "haul date",
                      "date delivered", "shipped", "ship dt"),
    "material_type": ("material", "product", "material description", "product description", "material name",
                      "product name", "item", "item description", "description", "mix", "material type"),
    "quantity": ("net tons", "tons", "net tonnage", "tonnage", "qty", "quantity", "net qty", "net quantity", "net",
                 "net weight", "net wt", "net lbs", "net pounds", "pounds", "lbs", "yards", "cy", "cubic yards",
                 "net yards", "loads", "quantity shipped", "qty shipped", "shipped qty"),
    "unit": ("uom", "unit", "units", "u m", "unit of measure", "um"),
    "supplier": ("supplier", "vendor", "plant", "quarry", "source", "supplier name", "vendor name", "plant name"),
    "job_location": ("job", "job name", "project", "job location", "delivery address", "ship to", "customer job",
                     "site", "location", "destination", "job site", "jobsite", "project name", "delivered to"),
    "truck_number": ("truck", "truck no", "truck number", "truck id", "vehicle", "vehicle no", "hauler truck"),
}
_NOT_QUANTITY = ("gross", "tare", "price", "amount", "total", "rate", "cost", "tax")


class SheetError(ValueError):
    """Why a file can't be used. The message is safe to show: it never holds a path."""


class Formula(str):
    """The text of a spreadsheet formula. Kept as text, never calculated."""


# ------------------------------------------------------------------ reading files

def kind_of(name: str, data: bytes) -> str:
    """'csv' or 'xlsx', from the name and the content together."""
    lower = name.lower()
    if lower.endswith(".xls") or data.startswith(b"\xd0\xcf\x11\xe0"):
        raise SheetError("Old Excel files (.xls) can't be read. Open it in Excel and save it as .xlsx or CSV.")
    if lower.endswith((".xlsx", ".xlsm")):
        if not data.startswith(b"PK\x03\x04"):
            raise SheetError("This file is named .xlsx but isn't an Excel workbook.")
        return "xlsx"
    if lower.endswith((".csv", ".txt", ".tsv")):
        if data.startswith(b"PK\x03\x04") or b"\x00" in data[:65536]:
            raise SheetError("This file is named CSV but isn't text. Export it from the supplier's system as CSV again.")
        return "csv"
    raise SheetError("Choose a CSV or Excel (.xlsx) file.")


def read_rows(kind: str, data: bytes) -> list[list]:
    """Every row of the file (the first sheet of a workbook), as cell values."""
    if not data:
        raise SheetError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise SheetError(f"The file is over {MAX_BYTES // (1024 * 1024)} MB. Split it into smaller exports.")
    rows = _read_xlsx(data) if kind == "xlsx" else _read_csv(data)
    while rows and _blank(rows[-1]):
        rows.pop()
    if not rows:
        raise SheetError("The file has no rows.")
    return rows


def _read_csv(data: bytes) -> list[list]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")  # what Excel on Windows saves "CSV" as
    sample = text[:20_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows: list[list] = []
    try:
        for row in csv.reader(io.StringIO(text, newline=""), dialect):
            rows.append([Formula(cell) if cell.lstrip().startswith("=") else cell for cell in row])
            _check_size(rows)
    except csv.Error:
        raise SheetError("The CSV file couldn't be read: a cell may be missing its closing quote.") from None
    return rows


def _read_xlsx(data: bytes) -> list[list]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if "xl/workbook.xml" not in names:
                raise SheetError("This file isn't an Excel workbook.")
            if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED:
                raise SheetError("This workbook is too large once opened. Export fewer rows, or as CSV.")
    except zipfile.BadZipFile:
        raise SheetError("This Excel file is damaged or isn't a workbook.") from None
    from openpyxl import load_workbook
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False, keep_vba=False)
    except SheetError:
        raise
    except Exception:  # openpyxl raises many kinds for a broken file; none of them is useful to show
        raise SheetError("This Excel file couldn't be opened. Save it again from Excel as .xlsx, or export it as CSV.") from None
    try:
        if not book.worksheets:
            raise SheetError("The workbook has no sheets.")
        sheet = book.worksheets[0]
        rows: list[list] = []
        for cells in sheet.iter_rows(max_col=MAX_COLUMNS + 1):
            row = []
            for cell in cells:
                value = getattr(cell, "value", None)
                if getattr(cell, "data_type", None) == "f" or (isinstance(value, str) and value.lstrip().startswith("=")):
                    value = Formula(str(getattr(value, "text", None) or value))
                row.append(value)
            rows.append(row)
            _check_size(rows)
        return rows
    except SheetError:
        raise
    except Exception:
        raise SheetError("This Excel file couldn't be read. Save it again from Excel as .xlsx, or export it as CSV.") from None
    finally:
        book.close()


def _check_size(rows: list[list]) -> None:
    row = rows[-1]
    if len(row) > MAX_COLUMNS and any(_text(value) for value in row[MAX_COLUMNS:]):
        raise SheetError(f"The file has more than {MAX_COLUMNS} columns. Export only the delivery columns.")
    if len(rows) > MAX_SCANNED_ROWS or (len(rows) > MAX_ROWS + HEADER_SEARCH and
                                        sum(1 for r in rows if not _blank(r)) > MAX_ROWS + HEADER_SEARCH):
        raise SheetError(f"The file has more than {MAX_ROWS:,} rows. Split it into smaller exports (by week or by job).")


# ------------------------------------------------------------------ cells

def _text(value) -> str:
    """What a cell shows, as text. A whole number held as a decimal loses its '.0'."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    text = str(value)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).strip()
    return text[:MAX_CELL]


def _blank(row: list) -> bool:
    return not any(_text(value) for value in row)


def _normal_header(text: str) -> str:
    text = text.lower().replace("#", " no ").replace("no.", "no ").replace("nbr", "no").replace("num.", "no ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _header_score(text: str) -> tuple[str | None, int]:
    """The field a header most likely names, and how sure: 2 exact, 1 contains, 0 neither."""
    header = _normal_header(text)
    if not header:
        return None, 0
    for name, words in SYNONYMS.items():
        if header in words:
            return name, 2
    for name, words in SYNONYMS.items():
        if name == "quantity" and any(word in header.split() for word in _NOT_QUANTITY):
            continue
        if any(len(word) > 3 and re.search(rf"\b{re.escape(word)}\b", header) for word in words):
            return name, 1
    return None, 0


def find_header_row(rows: list[list]) -> int:
    """0-based index of the row that names the columns: the one in the first rows most like a header."""
    best, best_score = None, 1
    for index, row in enumerate(rows[:HEADER_SEARCH]):
        found = {}
        for value in row:
            if isinstance(value, str) and not isinstance(value, Formula):
                name, score = _header_score(value)
                if name and score > found.get(name, 0):
                    found[name] = score
        score = sum(found.values())
        if len(found) >= 2 and score > best_score:
            best, best_score = index, score
    if best is not None:
        return best
    return next((i for i, row in enumerate(rows) if not _blank(row)), 0)


def columns_of(rows: list[list], header_index: int) -> list[dict]:
    header = rows[header_index] if header_index < len(rows) else []

    def used(row: list) -> int:
        return max((i + 1 for i, value in enumerate(row[:MAX_COLUMNS]) if _text(value)), default=0)
    width = max((used(r) for r in rows[header_index:header_index + 200]), default=0)
    return [{"index": i, "header": (_text(header[i]) if i < len(header) else "") or f"Column {i + 1}"} for i in range(width)]


def suggest(columns: list[dict]) -> tuple[dict, str | None]:
    """A suggested mapping ({field: column index}) and unit, for a person to check."""
    candidates = []
    for column in columns:
        name, score = _header_score(column["header"])
        if name:
            candidates.append((score, -column["index"], name, column["index"]))
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for score, _, name, index in sorted(candidates, reverse=True):
        if name not in mapping and index not in used:
            mapping[name] = index
            used.add(index)
    unit = None
    if "quantity" in mapping and "unit" not in mapping:
        header = _normal_header(columns[mapping["quantity"]]["header"])
        for words, value in ((("ton", "tons", "tonnage"), "ton"), (("lb", "lbs", "pounds", "pound"), "lb"),
                             (("yards", "yard", "cy", "yds"), "cy"), (("loads", "load"), "load"), (("gal", "gallons"), "gal")):
            if any(word in header.split() for word in words):
                unit = value
                break
    return mapping, unit


# ------------------------------------------------------------------ rows into tickets

@dataclass
class RowResult:
    row: int  # the row number in the file, as a spreadsheet shows it
    cells: list[str]
    values: dict
    flags: list[str]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate: dict | None = None
    key: tuple[str, str] | None = None


_NUMBER_AND_UNIT = re.compile(r"(?P<number>[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\s*(?P<unit>[A-Za-z#.]+)")


def _unit_of(text: str) -> str | None:
    word = (text or "").strip().lower().rstrip(".")
    if word in POUNDS:
        return "lb"
    return units.canonical(word)


def _quantity(value) -> tuple[Decimal | None, str | None]:
    """A number, and the unit written beside it if any ('12.5 cy')."""
    if isinstance(value, bool) or value is None or isinstance(value, Formula):
        return None, None
    if isinstance(value, (int, float, Decimal)):
        return parse_tonnage(_text(value)), None
    text = _text(value)
    parsed = parse_tonnage(text)
    if parsed is not None:
        return parsed, None
    match = _NUMBER_AND_UNIT.fullmatch(text)
    if match and _unit_of(match.group("unit")):
        return parse_tonnage(match.group("number")), _unit_of(match.group("unit"))
    return None, None


def _date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return None
    text = _text(value)
    found = parse_date(text)
    if found is None and len(text) > 10 and text[10] in " T":
        found = parse_date(text[:10])
    return found


def interpret(row_number: int, row: list, mapping: dict, defaults: dict) -> RowResult:
    """One row as the ticket it would become, with the review form's flags."""
    def cell(name: str):
        index = mapping.get(name)
        return row[index] if index is not None and index < len(row) else None

    errors: list[str] = []
    values: dict = {}
    for name in FIELDS:
        value = cell(name)
        if isinstance(value, Formula):
            errors.append(f"{LABELS[name]} is a formula ({_text(value)[:40]}). Quarvera never calculates spreadsheet formulas: "
                          "export the values, or enter this one on the review screen.")
    for name in ("ticket_number", "material_type", "supplier", "job_location", "truck_number"):
        value = cell(name)
        text = None if isinstance(value, Formula) else (_text(value) or None)
        if text is None and name in ("supplier", "job_location"):
            text = (defaults.get(name) or "").strip() or None
        if text is not None and len(text) > LIMITS[name]:
            errors.append(f"{LABELS[name]} is longer than {LIMITS[name]} characters.")
            text = None
        values[name] = text

    raw_date = cell("delivery_date")
    values["delivery_date"] = None if isinstance(raw_date, Formula) else _date(raw_date)
    explained = set()  # fields whose unreadable value an error above already explains
    if values["delivery_date"] is None and _text(raw_date):
        explained.add("Date")
        if not isinstance(raw_date, Formula):
            errors.append(f"Date '{_text(raw_date)[:40]}' isn't a date Quarvera can read (month/day/year, or year-month-day).")

    raw_quantity = cell("quantity")
    quantity, written_unit = _quantity(raw_quantity)
    if quantity is None and _text(raw_quantity):
        explained.update(("Tonnage", "Quantity"))
        if not isinstance(raw_quantity, Formula):
            errors.append(f"Quantity '{_text(raw_quantity)[:40]}' isn't a number.")
    unit_cell = cell("unit")
    unit_text = "" if isinstance(unit_cell, Formula) else _text(unit_cell)
    unit = _unit_of(unit_text) if unit_text else (written_unit or defaults.get("unit"))
    if unit_text and unit is None:
        errors.append(f"Unit '{unit_text[:20]}' isn't one Quarvera knows: tons, cubic yards, loads, each, gallons or pounds.")
    elif unit is None:
        errors.append("Unit missing: map a unit column or choose the unit for the whole file.")
    values.update(unit="ton" if unit in (None, "lb") else unit, source_quantity=None, source_unit=None)
    if quantity is not None and unit is None:
        quantity = None  # a figure in an unknown unit must not be recorded as tons
        explained.update(("Tonnage", "Quantity"))
    if quantity is not None and unit == "lb":
        tons = units.pounds_to_tons(quantity)
        if tons <= 0:
            errors.append("That's less than a thousandth of a ton. Check the pounds.")
            quantity = None
        else:
            values.update(source_quantity=quantity, source_unit="lb")
            quantity = tons
    values["tonnage"] = quantity

    flags = compute_flags(
        ticket_number=values["ticket_number"], delivery_date=values["delivery_date"], material_type=values["material_type"],
        tonnage=values["tonnage"], supplier=values["supplier"], job_location=values["job_location"],
        raw_date=_text(raw_date) or None, raw_tonnage=_text(raw_quantity) or None, unit=values["unit"])
    warnings = [f for f in flags if f.startswith(WARNINGS)]
    blocking = [f for f in flags if not f.startswith(WARNINGS)]
    # Say a thing once: the reason a value couldn't be read already explains its "unreadable" flag.
    errors += [f for f in blocking if not (f.endswith("unreadable") and f.split()[0] in explained)]
    key = None
    if values["ticket_number"] and values["supplier"]:
        key = (normal_name(values["supplier"]), values["ticket_number"].strip().upper())
    return RowResult(row=row_number, cells=[_text(v) for v in row[:MAX_COLUMNS]], values=values, flags=flags,
                     errors=errors, warnings=warnings, key=key)


def interpret_all(rows: list[list], header_index: int, mapping: dict, defaults: dict) -> list[RowResult]:
    results = []
    for index in range(header_index + 1, len(rows)):
        row = rows[index]
        if _blank(row):
            continue
        results.append(interpret(index + 1, row, mapping, defaults))
    return results


def mark_duplicates(db: Session, organization_id: int, results: list[RowResult]) -> None:
    """Same supplier and ticket number as a ticket on record in this company, or an earlier row."""
    numbers = sorted({r.key[1] for r in results if r.key})
    existing: dict[tuple[str, str], Ticket] = {}
    for start in range(0, len(numbers), 500):
        chunk = numbers[start:start + 500]
        for ticket in db.scalars(select(Ticket).where(Ticket.organization_id == organization_id,
                                                      Ticket.number_key.in_(chunk)).order_by(Ticket.id)):
            existing.setdefault((ticket.supplier_key or "", ticket.number_key or ""), ticket)
    projects = {}
    batch_ids = {t.batch_id for t in existing.values()}
    if batch_ids:
        projects = dict(db.execute(select(Batch.id, Project.code).join(Project, Project.id == Batch.project_id).where(
            Batch.id.in_(batch_ids), Batch.organization_id == organization_id, Project.organization_id == organization_id)).all())
    first_row: dict[tuple[str, str], int] = {}
    for result in results:
        if result.key is None:
            continue
        on_record = existing.get(result.key)
        if on_record is not None:
            result.duplicate = {"kind": "existing", "ticket_id": on_record.id, "batch_id": on_record.batch_id,
                                "status": on_record.status, "project_code": projects.get(on_record.batch_id),
                                "message": f"{result.values['supplier']} ticket {result.values['ticket_number']} is already on record "
                                           f"(batch {on_record.batch_id}, {_STATUS.get(on_record.status, on_record.status)})."}
        elif result.key in first_row:
            result.duplicate = {"kind": "file", "row": first_row[result.key],
                                "message": f"Same supplier and ticket number as row {first_row[result.key]} of this file."}
        first_row.setdefault(result.key, result.row)


_STATUS = {"verified": "approved", "needs_review": "waiting for review", "flagged": "flagged", "skipped": "skipped",
           "failed": "not read"}


# ------------------------------------------------------------------ the record kept for each imported row

_FONTS: dict[int, ImageFont.FreeTypeFont] = {}
_GLYPHS: dict[tuple[int, str], tuple[Image.Image, float]] = {}
_MAX_GLYPHS = 4096


def _write(image: Image.Image, x: int, y: int, text: str, shade: int, size: int) -> None:
    """Text drawn from a cache of single-character pictures. Drawing every line with
    FreeType took about 1 ms a line, which made a 2,000-row import take 25 s; pasting
    cached characters gives the same letters in a fraction of that. Only letter shapes
    are cached, never anything from a file."""
    font = _FONTS.get(size)
    if font is None:
        font = _FONTS[size] = ImageFont.load_default(size=size)
    ascent, descent = font.getmetrics()
    position = float(x)
    for ch in text:
        found = _GLYPHS.get((size, ch))
        if found is None:
            mask = Image.new("L", (max(1, round(font.getlength(ch))) + 4, ascent + descent + 2), 0)
            ImageDraw.Draw(mask).text((0, 0), ch, fill=255, font=font)
            found = (mask, font.getlength(ch))
            if len(_GLYPHS) < _MAX_GLYPHS:
                _GLYPHS[(size, ch)] = found
        mask, advance = found
        left = round(position)
        image.paste(shade, (left, y, left + mask.width, y + mask.height), mask)
        position += advance

def row_image(original_name: str, file_sha256: str, row_number: int, columns: list[dict], cells: list[str]) -> tuple[bytes, list[int]]:
    """A picture of the row as the file has it, shown where a scan would be.

    Its bytes are the ticket's crop, so its SHA-256 is the ticket's crop_sha256: the file's
    hash and the row number are drawn on it, which makes it different for every row of
    every file, and the same only for the same row of the same file imported again.
    """
    lines = [f"{(c['header'] or 'Column ' + str(c['index'] + 1))[:40]}: {cells[c['index']][:90]}"
             for c in columns if c["index"] < len(cells) and cells[c["index"]]][:40]
    height = 70 + 22 * max(len(lines), 1) + 16
    image = Image.new("L", (820, height), 255)
    _write(image, 16, 12, f"Imported from {original_name[:80]}, row {row_number}", 0, 15)
    _write(image, 16, 36, f"Spreadsheet SHA-256 {file_sha256}", 90, 12)
    ImageDraw.Draw(image).line((16, 58, 804, 58), fill=170)
    for n, line in enumerate(lines or ["(no values)"]):
        _write(image, 16, 68 + 22 * n, line, 20, 15)
    stream = io.BytesIO()
    image.save(stream, format="PNG", compress_level=6)
    return stream.getvalue(), [0, 0, image.width, image.height]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
