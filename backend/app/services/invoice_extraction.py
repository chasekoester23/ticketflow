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
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

import pymupdf as fitz

from ..config import settings
from . import invoice_layouts as layouts
from .pricing import norm

# Only these spellings mean tons. "T" alone is ambiguous and is not accepted.
TON_UNITS = {"TON", "TONS", "TN", "TNS"}
OTHER_UNITS = {"LB", "LBS", "CY", "YD", "YDS", "EA", "LOAD", "LOADS", "HR", "HRS"}
_NUMBER = r"-?\$?[\d,]*\d(?:\.\d+)?"
_ROW = re.compile(
    rf"^(?P<material>.+?)\s+(?P<qty>{_NUMBER})\s+(?P<unit>[A-Za-z]+)\s+(?P<rate>{_NUMBER})\s+(?P<amount>{_NUMBER})$"
)
_TICKET_ROW = re.compile(r"^(?:Ticket|Tkt)\b\s*(?:No\.?|#)?\s*:?\s*(?P<number>[A-Za-z0-9-]*\d[A-Za-z0-9-]*)(?:\s+(?P<tons>\d[\d,]*\.\d+))?\b.*$", re.IGNORECASE)
_DATE_TEXT = r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}"
_LABELS = {
    "invoice_number": re.compile(
        r"\b(?:Invoice|Inv\.?)\s*(?:No\.?|Number|Num\.?|#)\s*:?\s*(\S+)|^Invoice\s*:\s*(\S+)",
        re.IGNORECASE),
    "supplier": re.compile(r"^(?:Supplier|Vendor|Remit\s+To|Sold\s+By|Bill\s+From|From)\s*:\s*(.+)$", re.IGNORECASE),
    "period": re.compile(
        rf"^(?:Delivery\s+Period|Service\s+Period|Billing\s+Period|Period|Deliveries|For\s+Deliveries|Delivery\s+Dates|Dates)\s*:?\s*(?:from\s+)?({_DATE_TEXT})\s*(?:-|–|to|through|thru)\s*({_DATE_TEXT})",
        re.IGNORECASE),
}
_CREDIT = re.compile(r"\bcredit\s+(?:memo|note)\b", re.IGNORECASE)
_PAREN_NEGATIVE = re.compile(r"\(\$?\d[\d,]*\.\d{2}\)")
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

    def extract(self, data: bytes, supplier_names: tuple[str, ...] = ()) -> DraftResult: ...


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace("$", "").replace(",", ""))
    except InvalidOperation:
        return None


class TextLayerExtractor:
    """Strict, offline reader for PDFs that contain real text."""
    name = "text_layer"

    def extract(self, data: bytes, supplier_names: tuple[str, ...] = ()) -> DraftResult:
        """``supplier_names``: the company's catalog. If the invoice doesn't label
        its supplier, a letterhead holding exactly one of these names supplies it."""
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise InvoiceExtractionError("Could not open this PDF.") from exc
        try:
            pages = [layouts.page_rows(page) for page in doc]
        finally:
            doc.close()
        all_text = [row.text for rows in pages for row in rows]
        if not any(all_text):
            return DraftResult(unsupported_reason="This PDF has no readable text, so it looks scanned. "
                                                  "Enter its lines by hand beside the original.")
        if any(_CREDIT.search(text) for text in all_text):
            return DraftResult(unsupported_reason="This looks like a credit memo. Credits aren't supported "
                                                  "here, so nothing was drafted.")
        header: dict[str, str] = {}
        result = DraftResult()
        tickets: list[str] = []
        table_rows: list[layouts.TableRow] = []
        period: tuple[date | None, date | None] = (None, None)
        columns: list[layouts.Column] | None = None
        for page_no, rows in enumerate(pages, start=1):
            for row in rows:
                labelled = False
                for segment in row.segments():
                    for name, pattern in _LABELS.items():
                        match = pattern.search(segment)
                        if match:
                            labelled = True
                            if name == "period":
                                period = (layouts.parse_date(match.group(1)), layouts.parse_date(match.group(2)))
                            else:
                                header.setdefault(name, next(g for g in match.groups() if g).strip())
                            break
                if labelled:
                    continue
                found_columns = layouts.find_columns(row)
                if found_columns:
                    columns = found_columns  # Carries to the next page, for continued tables.
                    continue
                if _IGNORED_ROW.match(row.text):
                    continue
                ticket = _TICKET_ROW.match(row.text)
                if ticket:
                    tickets.append(ticket.group("number") + (f"\t{ticket.group('tons')}" if ticket.group("tons") else ""))
                    continue
                if layouts.NOT_MATERIAL.search(row.text):
                    # Freight, tax, totals: shown as not used, never folded into a subtotal.
                    if layouts.MONEY_ON_ROW.search(row.text):
                        result.unread_rows.append(row.text[:200])
                    continue
                parsed = layouts.parse_table_row(row, columns, page_no) if columns else None
                if parsed is not None:
                    table_rows.append(parsed)
                    continue
                line = self._material_row(row.text, page_no)
                if isinstance(line, DraftLineData):
                    result.lines.append(line)
                elif line == "unread":
                    result.unread_rows.append(row.text[:200])

        for parsed in table_rows:
            if parsed.quantity < 0 or parsed.amount < 0 or _PAREN_NEGATIVE.search(parsed.text):
                return DraftResult(unsupported_reason="This invoice has negative amounts. Credits aren't supported "
                                                      "here, so nothing was drafted.")
        summary_rows = [row for row in table_rows if not row.ticket]
        ticket_rows = [row for row in table_rows if row.ticket]
        for parsed in summary_rows:
            result.lines.append(DraftLineData(
                page=parsed.page, source_text=parsed.text, material_name=parsed.material,
                billed_quantity=parsed.quantity, unit=parsed.unit, billed_unit_price=parsed.rate,
                billed_subtotal=parsed.amount,
            ))
        drafted_from_rows: list[DraftLineData] = []
        for group in layouts.group_ticket_rows(ticket_rows):
            first, last = group.date_range()
            line = DraftLineData(
                page=group.rows[0].page,
                source_text=f"{len(group.rows)} ticket row(s), first: {group.rows[0].text}"[:500],
                material_name=group.material, billed_quantity=group.quantity, unit=group.unit,
                billed_unit_price=group.rate, billed_subtotal=group.amount, ticket_list=group.ticket_list(),
                date_from=first, date_to=last,
            )
            result.lines.append(line)
            drafted_from_rows.append(line)
            for row in group.rows:
                if row.rate is not None and (row.quantity * row.rate).quantize(Decimal("0.01")) != row.amount:
                    result.unread_rows.append(
                        f"Ticket {row.ticket}: {row.quantity} x {row.rate} is "
                        f"{(row.quantity * row.rate).quantize(Decimal('0.01'))}, but this row shows {row.amount}.")

        if "supplier" not in header and supplier_names:
            texts = [" " + norm(text) + " " for text in all_text]
            named = [name for name in supplier_names if norm(name) and any(f" {norm(name)} " in text for text in texts)]
            if len(named) == 1:
                header["supplier"] = named[0]
        for line in result.lines:
            line.invoice_number = header.get("invoice_number")
            line.supplier_name = header.get("supplier")
            # A stated period wins. Ticket rows otherwise give their own dates.
            if period != (None, None) or line not in drafted_from_rows:
                line.date_from, line.date_to = period
        # Tickets can only be tied to a line when there's exactly one line.
        if tickets and len(result.lines) == 1 and not result.lines[0].ticket_list:
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
        # Any other row with money on it is shown as not used, never folded into
        # a material subtotal.
        return "unread" if layouts.MONEY_ON_ROW.search(row) else None


def get_invoice_extractor() -> InvoiceExtractor | None:
    """None means manual entry only. Unknown values fail closed, with no fallback."""
    if settings.invoice_extractor == "manual":
        return None
    if settings.invoice_extractor == "text_layer":
        return TextLayerExtractor()
    raise InvoiceExtractionError(f"Invoice extractor '{settings.invoice_extractor}' is not available.")
