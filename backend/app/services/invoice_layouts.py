"""Read supplier invoice tables from their column headers.

Suppliers lay invoices out differently, but nearly all print a header row. Finding
the columns from that row means a new layout usually needs no code. Everything is
read from word positions in the PDF's own text layer: no AI and no network.

Conservative on purpose:
- A unit comes only from a unit column or a header that names one ("Net Tons").
  "Qty" alone leaves the unit empty, and a person fills it in.
- Pounds and yards are reported as such and never converted.
- Anything with money on it that isn't a material row is listed as not used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Longest phrase wins, so "Unit Price" is a rate and not a unit.
_COLUMN_PHRASES: dict[str, tuple[str, ...]] = {
    "ticket": ("scale ticket no", "scale ticket", "delivery ticket", "load ticket", "ticket number", "ticket no", "ticket #",
               "ticket#", "ticket", "tkt no", "tkt #", "tkt#", "tkt", "bol", "bol #", "bill of lading"),
    "date": ("delivery date", "ship date", "load date", "date delivered", "delivered", "date"),
    "material": ("item description", "material description", "product description", "description", "material", "product", "item"),
    "quantity": ("net tons", "tons net", "qty tons", "net weight tons", "tonnage", "tons", "tns", "net lbs", "net pounds", "lbs",
                 "pounds", "cubic yards", "yards", "cy", "quantity", "qty", "net wt", "net"),
    "unit": ("uom", "u/m", "unit"),
    "rate": ("unit price", "price per ton", "price/ton", "$/ton", "rate/ton", "rate per ton", "price", "rate"),
    "amount": ("line total", "extended price", "ext price", "extended", "extension", "amount", "ext", "total", "charge"),
}
_PHRASES = sorted(((phrase, column) for column, phrases in _COLUMN_PHRASES.items() for phrase in phrases),
                  key=lambda item: -len(item[0].split()))
_HEADER_UNITS = {"net tons": "ton", "tons net": "ton", "qty tons": "ton", "net weight tons": "ton", "tonnage": "ton",
                 "tons": "ton", "tns": "ton", "net lbs": "lb", "net pounds": "lb", "lbs": "lb", "pounds": "lb",
                 "cubic yards": "cy", "yards": "cy", "cy": "cy"}
_CELL_NUMERIC = ("ticket", "date", "quantity", "rate", "amount")
_MONEY_COLUMNS = ("quantity", "rate", "amount")

NUMBER = re.compile(r"^\(?-?\$?[\d,]*\d(?:\.\d+)?\)?$")
_DATE_TOKEN = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")
# Rows that are money but never material, wherever they sit in a table.
NOT_MATERIAL = re.compile(
    r"\b(sub\s?-?total|total|balance|amount due|freight|fuel|surcharge|delivery charge|haul|tax|fee|discount|deposit|payment)\b",
    re.IGNORECASE)
MONEY_ON_ROW = re.compile(r"\$\s?\d|\d\.\d{2}\b")
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y")
_TONS_UNITS = {"TON", "TONS", "TN", "TNS"}
_KNOWN_UNITS = {"LB", "LBS", "CY", "YD", "YDS", "EA", "LOAD", "LOADS", "HR", "HRS"}


@dataclass(frozen=True)
class Word:
    x0: float
    x1: float
    text: str


@dataclass
class Row:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    def segments(self, gap: float = 18) -> list[str]:
        """Text blocks on this row split at wide gaps, so two labels printed
        side by side ("Invoice No: 7   Vendor: Sample") read separately."""
        groups: list[list[Word]] = []
        for word in self.words:
            if groups and word.x0 - groups[-1][-1].x1 < gap:
                groups[-1].append(word)
            else:
                groups.append([word])
        return [" ".join(word.text for word in group) for group in groups]


def page_rows(page) -> list[Row]:
    """Rebuild visual rows from word positions: table cells arrive as separate
    text blocks, so plain text extraction would scatter a row across lines."""
    rows: list[tuple[float, list[Word]]] = []
    for x0, y0, x1, y1, text, *_rest in sorted(page.get_text("words"), key=lambda w: (w[1], w[0])):
        middle = (y0 + y1) / 2
        for row in rows:
            if abs(row[0] - middle) < 3:
                row[1].append(Word(x0, x1, text))
                break
        else:
            rows.append((middle, [Word(x0, x1, text)]))
    return [Row(sorted(words, key=lambda w: w.x0)) for _, words in rows]


@dataclass(frozen=True)
class Column:
    kind: str
    x0: float
    x1: float
    implied_unit: str | None = None


def _clean(text: str) -> str:
    return text.lower().strip(" :.,()").replace("(", " ").replace(")", " ")


def find_columns(row: Row) -> list[Column] | None:
    """The table's columns, if this row is a header with at least a quantity, an
    amount, and something saying what was delivered."""
    tokens = [_clean(word.text) for word in row.words]
    columns: dict[str, Column] = {}
    others: list[Column] = []
    index = 0
    while index < len(tokens):
        for phrase, kind in _PHRASES:
            parts = phrase.split()
            if tokens[index:index + len(parts)] == parts:
                if kind not in columns:
                    last = row.words[index + len(parts) - 1]
                    columns[kind] = Column(kind, row.words[index].x0, last.x1, _HEADER_UNITS.get(phrase))
                index += len(parts)
                break
        else:
            # Kept as a column so "Truck" or "Fuel" values don't run into their neighbors.
            others.append(Column("other", row.words[index].x0, row.words[index].x1))
            index += 1
    if not {"quantity", "amount"} <= set(columns) or not {"material", "ticket"} & set(columns):
        return None
    if len(others) > len(columns):  # A sentence that happens to contain these words.
        return None
    return sorted([*columns.values(), *others], key=lambda c: c.x0)


def split_cells(row: Row, columns: list[Column]) -> dict[str, str]:
    """Assign each word to a column. Left-aligned text belongs to the column whose
    span it starts in. Numbers may be right-aligned, so overlap with a numeric
    column's header decides first."""
    cells: dict[str, list[str]] = {}
    for word in row.words:
        looks_numeric = bool(NUMBER.match(word.text) or _DATE_TOKEN.match(word.text))
        column = None
        if looks_numeric:
            overlaps = [(min(word.x1, c.x1) - max(word.x0, c.x0), c) for c in columns if c.kind in _CELL_NUMERIC]
            best = max(overlaps, default=(0, None), key=lambda item: item[0])
            if best[0] > 0:
                column = best[1]
        if column is None:
            starting = [c for c in columns if c.x0 - 6 <= word.x0]
            column = starting[-1] if starting else columns[0]
            if not looks_numeric and column.kind in _MONEY_COLUMNS:
                # A word in a number column is really the end of a long description.
                text_columns = [c for c in columns if c.kind in ("material", "unit") and c.x0 <= word.x0]
                if text_columns:
                    column = text_columns[-1]
        cells.setdefault(column.kind, []).append(word.text)
    return {kind: " ".join(words) for kind, words in cells.items()}


def parse_amount(text: str | None) -> Decimal | None:
    """A decimal, negative for a credit written as -5.00 or (5.00). None if unreadable."""
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return -value if negative else value


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def unit_from(cell: str | None, implied: str | None) -> str | None:
    """"ton" only when plainly stated. A lone "T" is ambiguous and isn't accepted."""
    if cell:
        word = cell.strip().upper().rstrip(".")
        if word in _TONS_UNITS:
            return "ton"
        if word in _KNOWN_UNITS:
            return word.lower()
        return None
    return implied


@dataclass
class TableRow:
    page: int
    text: str
    material: str | None
    quantity: Decimal
    unit: str | None
    rate: Decimal | None
    amount: Decimal
    ticket: str | None = None
    delivered: date | None = None


def parse_table_row(row: Row, columns: list[Column], page: int) -> TableRow | None:
    """A material row, or None if the cells don't add up to one."""
    cells = split_cells(row, columns)
    quantity = parse_amount(cells.get("quantity"))
    amount = parse_amount(cells.get("amount"))
    ticket = cells.get("ticket")
    material = cells.get("material")
    if quantity is None or amount is None or not (ticket or material):
        return None
    if ticket and not re.search(r"\d", ticket):
        return None
    implied = next((c.implied_unit for c in columns if c.kind == "quantity"), None)
    unit = unit_from(cells.get("unit"), implied)
    if cells.get("unit") and unit is None:
        return None  # A unit we can't name, such as a lone "T", is shown rather than guessed.
    return TableRow(
        page=page, text=row.text[:500], material=material, quantity=quantity,
        unit=unit, rate=parse_amount(cells.get("rate")), amount=amount,
        ticket=ticket, delivered=parse_date(cells.get("date")),
    )


@dataclass
class GroupedTickets:
    """Ticket-by-ticket rows for one material at one rate, as one draft line."""
    material: str | None
    unit: str | None
    rate: Decimal | None
    rows: list[TableRow] = field(default_factory=list)

    @property
    def quantity(self) -> Decimal:
        return sum((row.quantity for row in self.rows), Decimal(0))

    @property
    def amount(self) -> Decimal:
        return sum((row.amount for row in self.rows), Decimal(0))

    def date_range(self) -> tuple[date | None, date | None]:
        dates = [row.delivered for row in self.rows if row.delivered]
        return (min(dates), max(dates)) if dates else (None, None)

    def ticket_list(self) -> str:
        # Billed tons go in the list only when the rows really are in tons.
        return "\n".join(
            f"{row.ticket}\t{row.quantity}" if self.unit == "ton" else str(row.ticket)
            for row in self.rows
        )


def group_ticket_rows(rows: list[TableRow]) -> list[GroupedTickets]:
    """One line per material, unit and rate: the same grain as an invoice comparison."""
    groups: dict[tuple, GroupedTickets] = {}
    for row in rows:
        key = ((row.material or "").strip().lower(), row.unit, row.rate)
        if key not in groups:
            groups[key] = GroupedTickets(row.material, row.unit, row.rate)
        groups[key].rows.append(row)
    return list(groups.values())
