"""A supplier-invoice line billed in cubic yards is exported in cubic yards, never "tons";
a line billed in tons reads exactly as before. Fictional data."""
import csv
import io
import re
from types import SimpleNamespace

import pytest

from app.services.invoice_matching import TicketListError, _gaps, parse_ticket_list

from .test_trucking_api import yard  # noqa: F401  (fixture)
from .test_units_of_measure import _catalog, _line, _ticket


def _report(client, line_id) -> str:
    response = client.get(f"/api/invoice-comparisons/{line_id}/report.csv")
    assert response.status_code == 200, response.text
    return response.text


def _discrepancies(client) -> list[list[str]]:
    response = client.get("/api/invoice-comparisons/discrepancies.csv")
    assert response.status_code == 200, response.text
    return list(csv.reader(io.StringIO(response.text)))


def test_a_yard_line_report_says_cubic_yards_and_never_tons(client, yard):
    _catalog(client, price="32.00", unit="cy")
    _ticket(client, yard, number="80001", quantity="12.00", unit="cy")
    line = _line(client, yard, unit="cy", quantity="13.000", price="32.0000", subtotal="420.00", ticket_list="80001\t13.000")
    assert "cubic yards" in line["diagnosis"]["headline"] and "tons" not in line["diagnosis"]["headline"].lower()
    text = _report(client, line["id"])
    for label in ("Invoice cubic yards", "Invoice rate per cubic yard", "Billed cubic yards times rate",
                  "Verified cubic yards", "Billed minus verified cubic yards", "Itemized billed cubic yards",
                  "Ticket cubic yards (not necessarily verified)", "Cubic yards differ"):
        assert label in text, label
    assert "Billed cubic yards differ from verified ticket cubic yards." in text
    assert not re.search(r"\btons?\b", text, re.I)


def test_a_ton_line_report_reads_as_before(client, yard):
    _catalog(client)
    _ticket(client, yard, number="80001", quantity="25.00")
    line = _line(client, yard, quantity="26.000", subtotal="520.00", ticket_list="80001\t26.000")
    assert "billed 1.000 tons more than approved" in line["diagnosis"]["headline"]
    text = _report(client, line["id"])
    for label in ("Invoice tons", "Invoice rate per ton", "Billed tons times rate", "Verified tons",
                  "Billed minus verified tons", "Itemized billed tons", "Ticket tons (not necessarily verified)",
                  "Tons differ", "Billed tons differ from verified ticket tons."):
        assert label in text, label
    assert "cubic" not in text.lower()


def test_the_differences_csv_names_each_rows_unit_without_moving_columns(client, yard):
    _catalog(client)
    _catalog(client, material="Fill Dirt", price="32.00", unit="cy")
    _ticket(client, yard, number="80001", quantity="25.00")
    _ticket(client, yard, number="80002", quantity="12.00", unit="cy", material="Fill Dirt")
    _line(client, yard, quantity="26.000", subtotal="520.00", ticket_list="80001\t26.000")
    _line(client, yard, unit="cy", quantity="13.000", price="32.0000", subtotal="420.00", ticket_list="80002\t13.000",
          invoice_number="INV-2", material_name="Fill Dirt")
    header, *rows = _discrepancies(client)
    assert header[:9] == ["Invoice", "Supplier", "Material", "Kind", "Ticket number", "Billed tons", "Approved tons",
                          "Tons difference", "Dollar effect (billed minus approved)"]
    assert header[9] == "Unit" and "not an approval to pay" in header[-1]
    ton_rows = [r for r in rows if r[0] == "INV-1"]
    yard_rows = [r for r in rows if r[0] == "INV-2"]
    assert ton_rows and yard_rows
    assert all(r[9] == "tons" for r in ton_rows) and any(r[3] == "Tons differ" for r in ton_rows)
    assert all(r[9] == "cubic yards" for r in yard_rows) and any(r[3] == "Cubic yards differ" for r in yard_rows)
    assert not any(re.search(r"\btons?\b", " ".join(r), re.I) for r in yard_rows)


# ------------------------------------------------------------- pasted ticket lists and per-ticket notes

def _line_raw(client, yard, unit="ton", **extra):
    body = {"project_id": yard["project"], "invoice_number": "INV-1", "supplier_name": "Example Aggregates",
            "material_name": "No. 57 Stone", "date_from": "2026-11-01", "date_to": "2026-11-30", "billed_tons": "13.000",
            "unit": unit, "billed_unit_price": "32.0000", "billed_subtotal": "416.00", **extra}
    return client.post("/api/invoice-comparisons", json=body)


def _correct(client, yard, line_id, **changes):
    body = {"project_id": yard["project"], "invoice_number": "INV-1", "supplier_name": "Example Aggregates",
            "material_name": "No. 57 Stone", "date_from": "2026-11-01", "date_to": "2026-11-30", "billed_tons": "13.000",
            "unit": "cy", "billed_unit_price": "32.0000", "billed_subtotal": "416.00", **changes}
    response = client.put(f"/api/invoice-comparisons/{line_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("text, ton_message, yard_message", [
    ("80001\tabc", "line 1: 'abc' is not a number of tons (enter it like 24.50)",
     "line 1: 'abc' is not a number of cubic yards (enter it like 24.50)"),
    ("80001\t-3", "line 1: billed tons '-3' must be a positive number",
     "line 1: billed cubic yards '-3' must be a positive number"),
    ("80001\t1.0005", "line 1: billed tons '1.0005' has more than 3 decimal places",
     "line 1: billed cubic yards '1.0005' has more than 3 decimal places"),
    ("80001\t1\t2", "line 1: copy only two columns: ticket number, then billed tons (optional)",
     "line 1: copy only two columns: ticket number, then billed cubic yards (optional)"),
])
def test_ticket_list_problems_name_the_lines_unit_and_read_as_before_for_tons(text, ton_message, yard_message):
    for unit in (None, "ton"):
        with pytest.raises(TicketListError) as ton:
            parse_ticket_list(text, unit)
        assert str(ton.value) == f"Ticket list needs fixes — {ton_message}. Nothing was saved."
    with pytest.raises(TicketListError) as yards:
        parse_ticket_list(text, "cy")
    assert str(yards.value) == f"Ticket list needs fixes — {yard_message}. Nothing was saved."
    with pytest.raises(TicketListError, match="gallons"):
        parse_ticket_list(text, "gal")


def test_a_yard_lines_unreadable_list_is_rejected_in_yards(client, yard):
    response = _line_raw(client, yard, unit="cy", ticket_list="80001\t13 yds")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "'13 yds' is not a number of cubic yards" in detail and "Nothing was saved" in detail
    assert not re.search(r"\btons?\b", detail, re.I)
    ton = _line_raw(client, yard, ticket_list="80001\t13 tons")
    assert ton.status_code == 422 and "'13 tons' is not a number of tons (enter it like 24.50)" in ton.json()["detail"]
    assert client.get("/api/invoice-comparisons").json() == []


def test_a_yard_tickets_notes_and_warnings_say_cubic_yards(client, yard):
    _catalog(client, price="32.00", unit="cy")
    _ticket(client, yard, number="80001", quantity="12.00", unit="cy")
    _ticket(client, yard, number="80002", quantity="1.00", unit="cy")
    line = _line(client, yard, unit="cy", quantity="13.000", price="32.0000", subtotal="416.00", ticket_list="80001\n80002")
    rows = {r["invoice_ticket_number"]: r for r in line["ticket_check"]}
    assert rows["80001"]["notes"] == ["No billed cubic yards entered for this ticket; quantity not compared."]
    itemized = _correct(client, yard, line["id"], unit="cy", billed_tons="14.000", billed_subtotal="448.00",
                        ticket_list="80001\t12\n80002\t1")
    assert "Itemized ticket cubic yards add up to 13.000, but the invoice line bills 14.000 cubic yards." in itemized["warnings"]
    for result in (line, itemized):
        text = " ".join(result["warnings"] + [n for r in result["ticket_check"] for n in r["notes"]])
        assert not re.search(r"\btons?\b", text, re.I), text


def test_a_ton_tickets_notes_and_warnings_read_as_before(client, yard):
    _catalog(client)
    _ticket(client, yard, number="80001", quantity="25.00")
    _ticket(client, yard, number="80002", quantity="1.00")
    line = _line(client, yard, quantity="26.000", subtotal="520.00", ticket_list="80001\n80002")
    rows = {r["invoice_ticket_number"]: r for r in line["ticket_check"]}
    assert rows["80001"]["notes"] == ["No billed tons entered for this ticket; quantity not compared."]
    itemized = _correct(client, yard, line["id"], unit="ton", billed_tons="27.000", billed_unit_price="20.0000",
                        billed_subtotal="540.00", ticket_list="80001\t25\n80002\t1")
    assert "Itemized ticket tons add up to 26.000, but the invoice line bills 27.000 tons." in itemized["warnings"]


def test_an_incomplete_delivery_is_described_in_its_own_unit():
    def ticket(unit, **extra):
        base = dict(status="verified", tonnage_confirmed=True, supplier="Example Aggregates",
                    material_type="No. 57 Stone", delivery_date="2026-11-03", tonnage=1, unit=unit)
        return SimpleNamespace(**{**base, **extra})
    assert _gaps(ticket("ton", tonnage_confirmed=False)) == ["tons not confirmed"]
    assert _gaps(ticket(None, tonnage=None)) == ["tons missing"]
    assert _gaps(ticket("cy", tonnage_confirmed=False)) == ["cubic yards not confirmed"]
    assert _gaps(ticket("load", tonnage=None)) == ["loads missing"]


def test_a_yard_lines_corrected_list_is_recorded_in_yards(client, yard):
    _catalog(client, price="32.00", unit="cy")
    _ticket(client, yard, number="80001", quantity="12.00", unit="cy")
    line = _line(client, yard, unit="cy", quantity="12.000", price="32.0000", subtotal="384.00", ticket_list="80001\t12")
    _correct(client, yard, line["id"], billed_tons="12.000", billed_subtotal="384.00", ticket_list="80001\t12.5")
    [event] = [e for e in client.get(f"/api/invoice-comparisons/{line['id']}/audit").json() if e["field"] == "ticket_list"]
    assert (event["old"], event["new"]) == ("80001 12.000 CY", "80001 12.500 CY")
