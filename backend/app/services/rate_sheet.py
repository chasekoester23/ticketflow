"""Read a pasted rate sheet: supplier, material, price per ton. Plans; never applies.

Existing names are matched the same way tickets are, so "ABC Aggregates." doesn't
become a second supplier. Any unreadable row stops the whole sheet, with its line
number, so a typo can't silently skip a rate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_ROWS = 2000
_PRICE_LIMIT = Decimal("100000000")


class RateSheetError(ValueError):
    pass


@dataclass(frozen=True)
class SheetRow:
    line: int
    supplier: str
    material: str
    price: Decimal


def _cells(line: str) -> list[str]:
    # Excel copies tab-separated cells. Names can contain commas, so tabs win.
    parts = line.split("\t") if "\t" in line else line.split(",")
    cells = [part.strip() for part in parts]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _price(text: str) -> Decimal:
    cleaned = re.sub(r"[$,\s]", "", text)
    cleaned = re.sub(r"/(ton|tn)$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise RateSheetError(f"'{text}' is not a price per ton") from None
    if not value.is_finite() or value <= 0 or value >= _PRICE_LIMIT:
        raise RateSheetError(f"price '{text}' must be a positive number")
    if value.as_tuple().exponent < -4:
        raise RateSheetError(f"price '{text}' has more than 4 decimal places")
    return value.quantize(Decimal("0.0001"))


def parse(text: str) -> list[SheetRow]:
    rows: list[SheetRow] = []
    problems: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    first = True
    for number, raw in enumerate((text or "").splitlines(), start=1):
        cells = _cells(raw)
        if not cells:
            continue
        is_first, first = first, False
        if len(cells) != 3:
            problems.append(f"line {number}: expected three columns (supplier, material, price per ton)")
            continue
        supplier, material, price_text = cells
        if is_first and not re.search(r"\d", price_text):
            continue  # A copied header row.
        if not supplier or not material:
            problems.append(f"line {number}: supplier and material are both required")
            continue
        if len(supplier) > 200 or len(material) > 200:
            problems.append(f"line {number}: a name is longer than 200 characters")
            continue
        key = (supplier.lower(), material.lower())
        if key in seen:
            problems.append(f"line {number}: repeats line {seen[key]}, so which rate applies isn't clear")
            continue
        seen[key] = number
        try:
            rows.append(SheetRow(number, supplier, material, _price(price_text)))
        except RateSheetError as exc:
            problems.append(f"line {number}: {exc}")
    if problems:
        more = f" (and {len(problems) - 8} more)" if len(problems) > 8 else ""
        raise RateSheetError("Rate sheet needs fixes: " + "; ".join(problems[:8]) + more + ". Nothing was saved.")
    if len(rows) > MAX_ROWS:
        raise RateSheetError(f"A rate sheet can have at most {MAX_ROWS} rows.")
    return rows
