"""A supplier-invoice line billed in cubic yards is exported in cubic yards, never "tons";
a line billed in tons reads exactly as before. Fictional data."""
import csv
import io
import re

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
