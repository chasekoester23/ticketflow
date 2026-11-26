"""The reader's unit, scale weights and truck number reach the ticket; nothing is invented. Fictional data."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.routers import api as api_module
from app.services import pipeline
from app.services.extraction import PROMPT, validated_ticket
from app.services.validation import ExtractedTicket, UNIT_NOT_STATED, parse_amount, quantity_unit
from .conftest import FixtureExtractor
from .sample_data import make_page, make_pdf

BASE = dict(ticket_number="88120", delivery_date="2026-10-15", material_type="Sand",
            supplier="Example Aggregates", job_location="North Lot")


def _applied(**read):
    """A ticket as the pipeline saves a real reader's answer (every key answered)."""
    raw = {**BASE, **read}
    ticket = SimpleNamespace()
    pipeline.apply_extraction(ticket, validated_ticket(raw), raw)
    return ticket


def test_prompt_asks_for_the_new_fields_without_conversion():
    for key in ("unit", "gross_weight", "tare_weight", "weight_unit", "truck_number"):
        assert key in PROMPT
    assert "Never convert units" in PROMPT and "Do not divide by" in PROMPT


@pytest.mark.parametrize("text, unit", [("ton", "ton"), ("Tons", "ton"), ("TN", "ton"), ("lb", "lb"), ("LBS", "lb"),
                                        ("cy", "cy"), ("yds", "cy"), ("load", "load"), ("each", "each"), ("gal", "gal"),
                                        ("kg", None), ("tonnes", None), ("", None), (None, None)])
def test_units_are_known_or_none(text, unit):
    assert quantity_unit(text) == unit
    assert ExtractedTicket.model_validate({"unit": text}).unit == unit


def test_a_ticket_in_tons_is_saved_as_before():
    ticket = _applied(tonnage="24.50", unit="ton", truck_number=" T-114 ")
    assert (ticket.tonnage, ticket.unit, ticket.source_quantity, ticket.source_unit) == (Decimal("24.500"), "ton", None, None)
    assert ticket.truck_number == "T-114"
    assert ticket.flags == [] and ticket.raw_extraction["source"] == "reader"


def test_pounds_are_saved_as_tons_keeping_the_pounds():
    ticket = _applied(tonnage="49,000", unit="lb", gross_weight="70,000", tare_weight="21000", weight_unit="lb")
    assert (ticket.tonnage, ticket.unit) == (Decimal("24.500"), "ton")
    assert (ticket.source_quantity, ticket.source_unit) == (Decimal("49000.000"), "lb")
    assert (ticket.gross_weight, ticket.tare_weight, ticket.weight_unit) == (Decimal("70000.000"), Decimal("21000.000"), "lb")
    assert ticket.flags == []
    assert ticket.raw_extraction["tonnage"] == "49,000"  # what was read stays as printed


def test_pounds_written_with_the_figure_count_too():
    ticket = _applied(tonnage="49,000 lb", unit="lb")
    assert ticket.tonnage == Decimal("24.500") and ticket.source_unit == "lb"
    clash = _applied(tonnage="49,000 lb", unit="cy")  # figure and unit field disagree: never pick one
    assert clash.tonnage is None


def test_other_units_are_kept_unconverted():
    ticket = _applied(tonnage="9.5", unit="cy")
    assert (ticket.tonnage, ticket.unit, ticket.source_unit) == (Decimal("9.500"), "cy", None)
    loads = _applied(tonnage="1.5", unit="load")
    assert "Loads must be a whole number" in loads.flags


@pytest.mark.parametrize("unit", [None, "kg", "tonnes"])
def test_a_unit_the_reader_cant_name_stays_tons_with_a_flag_and_no_quantity(unit):
    ticket = _applied(tonnage="24.50", unit=unit)
    assert ticket.unit == "ton" and ticket.tonnage is None  # tons are never assumed
    assert any(f.startswith(UNIT_NOT_STATED) for f in ticket.flags)
    assert "Tonnage missing" not in ticket.flags


def test_no_quantity_and_no_unit_is_just_missing():
    ticket = _applied(tonnage=None, unit=None)
    assert ticket.flags == ["Tonnage missing"]


def test_a_reader_written_before_units_keeps_its_tons():
    """FixtureExtractor-style readers never return the unit key: their figure is tons, as it was."""
    raw = {**BASE, "tonnage": "24.50"}
    ticket = SimpleNamespace()
    pipeline.apply_extraction(ticket, ExtractedTicket.model_validate(raw), raw)
    assert (ticket.tonnage, ticket.unit, ticket.flags) == (Decimal("24.500"), "ton", [])


def test_weights_disagreeing_with_the_tons_are_flagged():
    ticket = _applied(tonnage="24.50", unit="ton", gross_weight="70000", tare_weight="20000", weight_unit="lb")
    assert any(f.startswith("Weights: gross minus tare is 25.000 t") for f in ticket.flags)


def test_weights_without_a_unit_are_not_saved():
    ticket = _applied(tonnage="24.50", unit="ton", gross_weight="70000", tare_weight="21000", weight_unit=None)
    assert (ticket.gross_weight, ticket.tare_weight, ticket.weight_unit) == (None, None, None)
    assert ticket.raw_extraction["gross_weight"] == "70000"


def test_parse_amount_is_strict():
    assert parse_amount("49,000 lb") == (Decimal("49000.000"), "lb")
    assert parse_amount("12") == (Decimal("12.000"), None)
    assert parse_amount("12 kg") == (None, None)
    assert parse_amount("1O.5") == (None, None)


def test_reader_pounds_through_the_api(client, monkeypatch):
    read = {**BASE, "tonnage": "48,600", "unit": "lb", "gross_weight": "70,000", "tare_weight": "21,400",
            "weight_unit": "lb", "truck_number": "22"}
    monkeypatch.setattr(api_module, "get_extractor", lambda: FixtureExtractor([read]))
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "extract"},
                        files=[("files", ("scan.pdf", make_pdf([make_page([read, None, None, None])]), "application/pdf"))])
    assert batch.status_code == 200, batch.text
    ticket = client.get(f"/api/batches/{batch.json()['id']}/tickets").json()[0]
    assert (Decimal(ticket["tonnage"]), ticket["unit"]) == (Decimal("24.300"), "ton")
    assert (Decimal(ticket["source_quantity"]), ticket["source_unit"]) == (Decimal("48600"), "lb")
    assert ticket["truck_number"] == "22" and ticket["flags"] == []
    assert ticket["tonnage_confirmed"] is False and ticket["status"] == "needs_review"
