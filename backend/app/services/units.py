"""Units of measure for material quantities and rates.

A delivery's approved quantity is in one billing unit (UNITS). A rate prices only
deliveries in its own unit; Quarvera never converts between dimensions (cubic yards to
tons, loads to weight) on its own, because the factor depends on the material and the
contract. Pounds are a source unit: they convert exactly to US short tons
(2,000 lb = 1 t), rounded half up to the 0.001 t that quantities are kept at, and the
original figure and unit stay on the ticket.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# key: (short label, long label)
UNITS: dict[str, tuple[str, str]] = {
    "ton": ("t", "tons"),
    "cy": ("CY", "cubic yards"),
    "load": ("loads", "loads"),
    "each": ("ea", "each"),
    "gal": ("gal", "gallons"),
}
SOURCE_UNITS = (*UNITS, "lb")          # what a ticket may be written in
WEIGHT_UNITS = ("lb", "ton")           # for gross and tare weights
DEFAULT = "ton"
LB_PER_TON = Decimal(2000)
THOUSANDTH = Decimal("0.001")
# Net weight and the entered quantity may differ by scale rounding; more is a question.
NET_TOLERANCE_TONS = Decimal("0.010")


# How invoices and spreadsheets write the units, to the unit they mean. Pounds and hours
# aren't billing units for a material line, so they aren't here.
ALIASES = {"ton": "ton", "tons": "ton", "tn": "ton", "tns": "ton", "t": "ton",
           "cy": "cy", "yd": "cy", "yds": "cy", "cyd": "cy", "yd3": "cy",
           "load": "load", "loads": "load", "ld": "load", "lds": "load",
           "each": "each", "ea": "each", "gal": "gal", "gals": "gal", "gallon": "gal", "gallons": "gal"}


def canonical(text: str | None) -> str | None:
    """The billing unit an invoice's unit text means, or None when it isn't one."""
    return ALIASES.get((text or "").strip().lower().rstrip("."))


def pounds_to_tons(pounds: Decimal) -> Decimal:
    return (pounds / LB_PER_TON).quantize(THOUSANDTH, rounding=ROUND_HALF_UP)


def to_tons(weight: Decimal, unit: str | None) -> Decimal:
    """A gross, tare or net weight in tons."""
    return pounds_to_tons(weight) if unit == "lb" else weight


def singular(unit: str | None) -> str:
    return {"ton": "ton", "cy": "cubic yard", "load": "load", "each": "each", "gal": "gallon"}.get(unit or DEFAULT, unit or "")


def short(unit: str | None) -> str:
    return UNITS.get(unit or DEFAULT, (unit or "", unit or ""))[0]


def long(unit: str | None) -> str:
    return UNITS.get(unit or DEFAULT, (unit or "", unit or ""))[1]


def net_check(quantity: Decimal | None, unit: str | None, gross: Decimal | None, tare: Decimal | None,
              weight_unit: str | None) -> str | None:
    """A review flag when gross minus tare disagrees with the entered tons. Never changes anything."""
    if quantity is None or gross is None or tare is None or (unit or DEFAULT) != "ton":
        return None
    if tare > gross:
        return "Weights: tare is more than gross. Check the scale ticket."
    net = to_tons(gross - tare, weight_unit)
    if abs(net - quantity) > NET_TOLERANCE_TONS:
        return f"Weights: gross minus tare is {net} t, but the quantity is {quantity} t. Check which is right."
    return None
