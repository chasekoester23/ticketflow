"""Printable, self-written practice tickets for a SYNTHETIC handwriting benchmark.

Prints blank, clearly fictional delivery tickets four per letter page (the
same 2x2 layout TicketFlow splits), plus a matching answer-key CSV. People
fill the tickets by hand; the person who wrote each one records what is
actually legible in the key; ``labels_from_answer_key`` converts the key into
the evaluation manifest used by ``scripts/evaluate_extraction.py``.

Results from these tickets are a best case (few writers, clean paper, our own
layouts). They are never evidence of accuracy on real supplier tickets.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from .evaluation import DatasetError

# Slot letters follow the splitter's grid_2x2 order: 0 top-left, 1 top-right,
# 2 bottom-left, 3 bottom-right.
SLOTS = "ABCD"
MAX_SHEETS = 25  # 100 tickets; far more than one person should hand-write

# Three deliberately different layouts, like tickets from different suppliers.
# Distractors (weights in pounds, truck, signature) exist on real tickets and
# test that the reader picks NET TONS rather than another number.
VARIANTS: dict[str, dict] = {
    "1": {
        "title": "SCALE TICKET",
        "rows": [
            ("Ticket No.", "ticket_number"), ("Date", "delivery_date"),
            ("Customer / Job", "job_location"), ("Product", "material_type"),
            ("Gross (lb)", None), ("Tare (lb)", None), ("Net Tons", "tonnage"),
            ("Truck No.", None), ("Hauler / Supplier", "supplier"),
        ],
    },
    "2": {
        "title": "DELIVERY RECEIPT",
        "rows": [
            ("Ticket #", "ticket_number"), ("Ship Date", "delivery_date"),
            ("Vendor", "supplier"), ("Material", "material_type"),
            ("Quantity (tons)", "tonnage"), ("Deliver To", "job_location"),
            ("Driver", None), ("Received By", None),
        ],
    },
    "3": {
        "title": "MATERIAL TICKET",
        "rows": [
            ("No.", "ticket_number"), ("Date", "delivery_date"), ("Supplier", "supplier"),
            ("Job Site", "job_location"), ("Item", "material_type"),
            ("Net Wt (T)", "tonnage"), ("Truck", None), ("Signature", None),
        ],
    },
}

KEY_COLUMNS = [
    "practice_id", "sheet", "slot", "layout",
    "ticket_number", "delivery_date", "material", "net_tons", "supplier", "job_location",
    "notes",
]
# Answer-key column -> benchmark field.
KEY_TO_FIELD = {
    "ticket_number": "ticket_number", "delivery_date": "delivery_date",
    "material": "material_type", "net_tons": "tonnage",
    "supplier": "supplier", "job_location": "job_location",
}
UNREADABLE = "UNREADABLE"


def practice_id(sheet: int, slot: str) -> str:
    return f"S{sheet:02d}-{slot}"


def variant_for(sheet: int, slot_index: int) -> str:
    return str((sheet + slot_index) % len(VARIANTS) + 1)


def _check_sheet_count(sheets: int) -> None:
    if type(sheets) is not int or not 1 <= sheets <= MAX_SHEETS:
        raise ValueError(f"Choose between 1 and {MAX_SHEETS} sheets")


def _draw_ticket(pdf: canvas.Canvas, x: float, y: float, w: float, h: float,
                 sheet: int, slot: str, variant: str) -> None:
    """One blank ticket with its bottom-left corner at (x, y)."""
    spec = VARIANTS[variant]
    pdf.setStrokeColorRGB(0, 0, 0)
    pdf.setLineWidth(1.2)
    pdf.rect(x, y, w, h)

    top = y + h
    pdf.setFont("Helvetica", 6.5)
    pdf.setFillColorRGB(0.45, 0.1, 0.05)
    pdf.drawString(x + 8, top - 11, "SYNTHETIC PRACTICE TICKET - NOT A REAL DELIVERY")
    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(x + 8, top - 27, spec["title"])
    pdf.setLineWidth(0.8)
    pdf.line(x, top - 34, x + w, top - 34)

    rows = spec["rows"]
    body_top, body_bottom = top - 40, y + 22
    step = (body_top - body_bottom) / len(rows)
    label_w = 76
    for i, (label, _field) in enumerate(rows):
        baseline = body_top - (i + 1) * step + 6
        pdf.setFont("Helvetica", 8)
        pdf.setFillColorRGB(0.15, 0.15, 0.15)
        pdf.drawString(x + 8, baseline + 2, label)
        pdf.setStrokeColorRGB(0.55, 0.55, 0.55)
        pdf.setLineWidth(0.5)
        pdf.line(x + 8 + label_w, baseline, x + w - 8, baseline)

    pdf.setStrokeColorRGB(0, 0, 0)
    pdf.setLineWidth(0.5)
    pdf.line(x, y + 16, x + w, y + 16)
    pdf.setFont("Helvetica", 6.5)
    pdf.setFillColorRGB(0.3, 0.3, 0.3)
    pdf.drawString(x + 8, y + 6, f"Practice ID {practice_id(sheet, slot)}  (not a ticket number)")
    pdf.drawRightString(x + w - 8, y + 6, f"Layout {variant}")
    pdf.setFillColorRGB(0, 0, 0)


def build_practice_pdf(sheets: int) -> bytes:
    """Letter-size pages, four tickets each, inset well inside each quadrant
    so TicketFlow's 2x2 split never cuts a ticket or catches a neighbor."""
    _check_sheet_count(sheets)
    buffer = io.BytesIO()
    page_w, page_h = letter
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setTitle("TicketFlow synthetic practice tickets")
    pdf.setAuthor("TicketFlow")
    quad_w, quad_h = page_w / 2, page_h / 2
    inset = 20  # points (~0.28 in); the splitter trims only 1% of the page
    for sheet in range(1, sheets + 1):
        for index, slot in enumerate(SLOTS):
            col, row = index % 2, index // 2  # row 0 is the TOP of the page
            x = col * quad_w + inset
            y = page_h - (row + 1) * quad_h + inset
            _draw_ticket(pdf, x, y, quad_w - 2 * inset, quad_h - 2 * inset,
                         sheet, slot, variant_for(sheet, index))
        pdf.setFont("Helvetica", 7)
        pdf.setFillColorRGB(0.4, 0.4, 0.4)
        pdf.drawCentredString(page_w / 2, page_h / 2 - 3, f"Practice sheet {sheet}")
        pdf.setFillColorRGB(0, 0, 0)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def answer_key_csv(sheets: int) -> str:
    """Pre-numbered key with one row per printed ticket; fields left blank."""
    _check_sheet_count(sheets)
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(KEY_COLUMNS)
    for sheet in range(1, sheets + 1):
        for index, slot in enumerate(SLOTS):
            writer.writerow([practice_id(sheet, slot), sheet, slot, variant_for(sheet, index)]
                            + [""] * (len(KEY_COLUMNS) - 4))
    return out.getvalue()


_US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")


def _key_date(value: str) -> str:
    """Accept YYYY-MM-DD or M/D/YYYY (M/D/YY = 20YY); return ISO."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        match = _US_DATE.match(value)
        if match:
            month, day, year = (int(g) for g in match.groups())
            if year < 100:
                year += 2000
            return date(year, month, day).isoformat()
    except ValueError:
        pass
    raise ValueError("date must be YYYY-MM-DD or M/D/YYYY (the date as written on the ticket)")


def _key_tons(value: str) -> str:
    try:
        tons = Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise ValueError("net tons must be a number in TONS such as 24.5 (no units; never pounds)") from None
    if not tons.is_finite() or tons <= 0 or tons.as_tuple().exponent < -3 or tons >= 1000:
        raise ValueError("net tons must be a positive number of tons with at most 3 decimals")
    return str(tons)


def labels_from_answer_key(csv_text: str, scan_name: str, scan_pages: int) -> tuple[dict, int]:
    """Convert a filled answer key into a version-1 evaluation manifest.

    Rows where all six answer fields are blank are treated as unused tickets
    and omitted (their count is returned). A partially filled row is an
    error: a forgotten field must never become a silent "unreadable".
    Write UNREADABLE for a field a person genuinely cannot read.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
    missing_columns = [c for c in KEY_COLUMNS if c not in (reader.fieldnames or [])]
    if missing_columns:
        raise DatasetError("Answer key is missing column(s): " + ", ".join(missing_columns))

    tickets, problems, unused, seen = [], [], 0, set()
    for line, row in enumerate(reader, start=2):
        pid = (row.get("practice_id") or "").strip()
        answers = {col: (row.get(col) or "").strip() for col in KEY_TO_FIELD}
        if not any(answers.values()):
            unused += 1
            continue
        try:
            sheet = int((row.get("sheet") or "").strip())
            slot = (row.get("slot") or "").strip().upper()
            if slot not in SLOTS or sheet < 1 or pid != practice_id(sheet, slot):
                raise ValueError
        except ValueError:
            problems.append(f"line {line}: practice_id/sheet/slot were changed; restore them from the printed ticket")
            continue
        if pid in seen:
            problems.append(f"line {line}: {pid} appears twice")
            continue
        seen.add(pid)
        if sheet > scan_pages:
            problems.append(f"line {line}: {pid} is on sheet {sheet}, but the scan has only {scan_pages} page(s)")
            continue
        expected: dict[str, str | None] = {}
        for col, field in KEY_TO_FIELD.items():
            value = answers[col]
            if not value:
                problems.append(f"line {line} ({pid}): '{col}' is blank; fill it or write {UNREADABLE}")
                continue
            if value.upper() == UNREADABLE:
                expected[field] = None
                continue
            try:
                if field == "delivery_date":
                    value = _key_date(value)
                elif field == "tonnage":
                    value = _key_tons(value)
            except ValueError as exc:
                problems.append(f"line {line} ({pid}): {exc}")
                continue
            expected[field] = value
        if len(expected) == len(KEY_TO_FIELD):
            tickets.append({
                "id": f"practice-{pid}", "source": scan_name, "layout": "grid_2x2",
                "page": sheet, "position": SLOTS.index(slot), "expected": expected,
            })
    if problems:
        shown = problems[:20]
        more = f" (and {len(problems) - 20} more)" if len(problems) > 20 else ""
        raise DatasetError("Answer key needs fixes:\n  " + "\n  ".join(shown) + more)
    if not tickets:
        raise DatasetError("No filled-in tickets found in the answer key")
    return {"version": 1, "synthetic_practice": True, "tickets": tickets}, unused
