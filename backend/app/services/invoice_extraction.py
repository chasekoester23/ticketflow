"""Draft invoice lines from a stored PDF. Drafts only; a person confirms them.

Providers are chosen explicitly by INVOICE_EXTRACTOR and never fall back to
another one. The default reads the PDF's own text layer on this machine: no
network, no AI. Anything it can't read with certainty stays empty or is listed
as an unread row, so nothing is dropped silently and nothing is guessed.

Not implemented: scanned invoices and AI providers. A scanned PDF has no text
layer, so people enter its lines by hand beside the original.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

import pymupdf as fitz

from ..config import settings

# Only these spellings mean tons. "T" alone is ambiguous and is not accepted.
TON_UNITS = {"TON", "TONS", "TN", "TNS"}
OTHER_UNITS = {"LB", "LBS", "CY", "YD", "YDS", "EA", "LOAD", "LOADS", "HR", "HRS"}
_NUMBER = r"-?\$?[\d,]*\d(?:\.\d+)?"
_ROW = re.compile(
    rf"^(?P<material>.+?)\s+(?P<qty>{_NUMBER})\s+(?P<unit>[A-Za-z]+)\s+(?P<rate>{_NUMBER})\s+(?P<amount>{_NUMBER})$"
)
_TICKET_ROW = re.compile(r"^(?:Ticket|Tkt)\b\s*(?:No\.?|#)?\s*:?\s*(?P<number>[A-Za-z0-9-]*\d[A-Za-z0-9-]*)(?:\s+(?P<tons>\d[\d,]*\.\d+))?\b.*$", re.IGNORECASE)
_LABELS = {
    "invoice_number": re.compile(r"^Invoice\s*(?:No\.?|Number|#)\s*:?\s*(\S+)", re.IGNORECASE),
    "supplier": re.compile(r"^(?:Supplier|Vendor|Remit\s+To)\s*:\s*(.+)$", re.IGNORECASE),
    "period": re.compile(r"^(?:Delivery\s+Period|Service\s+Period|Deliveries)\s*:\s*(\S+)\s*(?:-|–|to|through)\s*(\S+)", re.IGNORECASE),
}
_CREDIT = re.compile(r"\bcredit\s+(?:memo|note)\b", re.IGNORECASE)
_IGNORED_ROW = re.compile(r"^(?:description|material|item|product)\b.*\b(?:qty|quantity)\b", re.IGNORECASE)


class InvoiceExtractionError(Exception):
    """The document can't be drafted; the reason is shown to the person."""


@dataclass
class DraftLineData:
    page: int
    source_text: str | None = None
    invoice_number: str | None = None
    supplier_name: str | None = None
    material_name: str | None = None
    billed_quantity: Decimal | None = None
    unit: str | None = None
    billed_unit_price: Decimal | None = None
    billed_subtotal: Decimal | None = None
    date_from: date | None = None
    date_to: date | None = None
    ticket_list: str = ""


@dataclass
class DraftResult:
    lines: list[DraftLineData] = field(default_factory=list)
    unread_rows: list[str] = field(default_factory=list)
    unsupported_reason: str | None = None


class InvoiceExtractor(Protocol):
    name: str

    def extract(self, data: bytes) -> DraftResult: ...


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace("$", "").replace(",", ""))
    except InvalidOperation:
        return None


def _date(text: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _page_rows(page) -> list[str]:
    """Rebuild visual rows from word positions: table cells arrive as separate
    text blocks, so plain text extraction would scatter a row across lines."""
    words = page.get_text("words")
    rows: list[tuple[float, list[tuple[float, str]]]] = []
    for x0, y0, _x1, y1, text, *_rest in sorted(words, key=lambda w: (w[1], w[0])):
        middle = (y0 + y1) / 2
        for row in rows:
            if abs(row[0] - middle) < 3:
                row[1].append((x0, text))
                break
        else:
            rows.append((middle, [(x0, text)]))
    return [" ".join(t for _, t in sorted(cells)) for _, cells in rows]


class TextLayerExtractor:
    """Strict, offline reader for PDFs that contain real text."""
    name = "text_layer"

    def extract(self, data: bytes) -> DraftResult:
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise InvoiceExtractionError("Could not open this PDF.") from exc
        try:
            pages = [_page_rows(page) for page in doc]
        finally:
            doc.close()
        all_rows = [row for rows in pages for row in rows]
        if not any(all_rows):
            return DraftResult(unsupported_reason="This PDF has no readable text, so it looks scanned. "
                                                  "Enter its lines by hand beside the original.")
        if any(_CREDIT.search(row) for row in all_rows):
            return DraftResult(unsupported_reason="This looks like a credit memo. Credits aren't supported "
                                                  "here, so nothing was drafted.")
        header: dict[str, str] = {}
        result = DraftResult()
        tickets: list[str] = []
        period: tuple[date | None, date | None] = (None, None)
        for page_no, rows in enumerate(pages, start=1):
            for row in rows:
                labelled = False
                for name, pattern in _LABELS.items():
                    match = pattern.match(row)
                    if match:
                        labelled = True
                        if name == "period":
                            period = (_date(match.group(1)), _date(match.group(2)))
                        else:
                            header.setdefault(name, match.group(1).strip())
                        break
                if labelled or _IGNORED_ROW.match(row):
                    continue
                ticket = _TICKET_ROW.match(row)
                if ticket:
                    tickets.append(ticket.group("number") + (f"\t{ticket.group('tons')}" if ticket.group("tons") else ""))
                    continue
                line = self._material_row(row, page_no)
                if isinstance(line, DraftLineData):
                    result.lines.append(line)
                elif line == "unread":
                    result.unread_rows.append(row[:200])
        for line in result.lines:
            line.invoice_number = header.get("invoice_number")
            line.supplier_name = header.get("supplier")
            line.date_from, line.date_to = period
        # Tickets can only be tied to a line when there's exactly one line.
        if tickets and len(result.lines) == 1:
            result.lines[0].ticket_list = "\n".join(tickets)
        elif tickets:
            result.unread_rows.append(
                f"{len(tickets)} ticket reference(s) could not be tied to one line; add them to the right line by hand.")
        if any((l.billed_subtotal or 0) < 0 or (l.billed_quantity or 0) < 0 for l in result.lines):
            return DraftResult(unsupported_reason="This invoice has negative amounts. Credits aren't supported "
                                                  "here, so nothing was drafted.")
        return result

    @staticmethod
    def _material_row(row: str, page_no: int):
        """A DraftLineData, "unread" for a money row we can't use, or None for prose."""
        match = _ROW.match(row)
        if match:
            unit = match.group("unit").upper()
            if unit in TON_UNITS or unit in OTHER_UNITS:
                return DraftLineData(
                    page=page_no, source_text=row[:500], material_name=match.group("material").strip(),
                    billed_quantity=_decimal(match.group("qty")),
                    unit="ton" if unit in TON_UNITS else unit.lower(),
                    billed_unit_price=_decimal(match.group("rate")), billed_subtotal=_decimal(match.group("amount")),
                )
        # Freight, tax, totals: any other row with money on it is shown as not
        # used, never folded into a material subtotal.
        return "unread" if re.search(r"\$\s?\d|\d\.\d{2}\b", row) else None


def get_invoice_extractor() -> InvoiceExtractor | None:
    """None means manual entry only. Unknown values fail closed, with no fallback."""
    if settings.invoice_extractor == "manual":
        return None
    if settings.invoice_extractor == "text_layer":
        return TextLayerExtractor()
    raise InvoiceExtractionError(f"Invoice extractor '{settings.invoice_extractor}' is not available.")
