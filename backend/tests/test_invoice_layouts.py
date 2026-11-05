"""The reader across fictional layouts. Layout handling only, not real-world accuracy."""
from datetime import date
from decimal import Decimal

from app.services.invoice_extraction import TextLayerExtractor
from app.services.invoice_layouts import Column, Row, Word, find_columns, parse_amount, split_cells, unit_from

from . import sample_invoices as layouts
from .test_invoice_documents import _project, _upload


def read(pdf, **kwargs):
    return TextLayerExtractor().extract(pdf, **kwargs)


def test_ticket_detail_rows_become_one_line_with_their_tickets():
    result = read(layouts.ticket_detail())
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.invoice_number == "SSC-88213" and line.supplier_name == "Sample Stone Co"
    assert line.material_name == "No. 57 Stone" and line.unit == "ton"
    assert line.billed_quantity == Decimal("67.450")
    assert line.billed_unit_price == Decimal("28.5000")
    assert line.billed_subtotal == Decimal("1922.33")
    assert (line.date_from, line.date_to) == (date(2026, 10, 6), date(2026, 10, 9))
    assert line.ticket_list.splitlines() == ["T-20011\t22.150", "T-20012\t21.900", "T-20047\t23.400"]


def test_charges_are_listed_as_not_used_and_never_join_the_subtotal():
    result = read(layouts.ticket_detail())
    assert any("Fuel surcharge" in row for row in result.unread_rows)
    assert any("Invoice total" in row for row in result.unread_rows)
    assert result.lines[0].billed_subtotal == Decimal("1922.33")


def test_a_row_whose_math_is_wrong_is_pointed_out():
    result = read(layouts.ticket_detail([
        ("10/06/2026", "T-1", "Sand", "10.000", "15.0000", "150.00"),
        ("10/06/2026", "T-2", "Sand", "10.000", "15.0000", "175.00"),
    ]))
    assert any("Ticket T-2" in row and "150.00" in row and "175.00" in row for row in result.unread_rows)
    assert result.lines[0].billed_subtotal == Decimal("325.00")  # As printed, for a person to see.


def test_other_column_names_right_alignment_and_written_dates():
    result = read(layouts.right_aligned_numbers())
    line = result.lines[0]
    assert line.invoice_number == "4471-A" and line.supplier_name == "Placeholder Paving"
    assert line.material_name == "Asphalt Mix Type B 12.5mm"
    assert line.billed_quantity == Decimal("1204.350")
    assert line.billed_unit_price == Decimal("72.0000")
    assert line.billed_subtotal == Decimal("86713.20")
    assert (line.date_from, line.date_to) == (date(2026, 10, 1), date(2026, 10, 31))
    assert line.ticket_list == ""


def test_letterhead_supplier_comes_only_from_the_companys_own_catalog():
    pdf = layouts.letterhead_supplier()
    assert read(pdf).lines[0].supplier_name is None  # Never guessed from a big bold line.
    assert read(pdf, supplier_names=("Example Aggregates", "Sample Stone Co")).lines[0].supplier_name == "Example Aggregates"


def test_two_catalog_names_on_one_invoice_means_no_supplier():
    pdf = layouts.ticket_detail(supplier="Vendor name not stated")
    assert read(pdf, supplier_names=("Sample Stone Co",)).lines[0].supplier_name == "Vendor name not stated"
    from reportlab.pdfgen import canvas
    import io
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    c.drawString(50, 750, "Example Aggregates")
    c.drawString(50, 730, "Ship via Sample Stone Co")
    c.drawString(50, 700, "Material")
    c.drawString(200, 700, "Qty")
    c.drawString(300, 700, "Amount")
    c.drawString(50, 680, "Sand")
    c.drawString(200, 680, "1.000")
    c.drawString(300, 680, "5.00")
    c.showPage()
    c.save()
    assert read(buffer.getvalue(), supplier_names=("Example Aggregates", "Sample Stone Co")).lines[0].supplier_name is None


def test_pounds_are_reported_as_pounds_never_converted_and_kept_out_of_the_ton_list():
    line = read(layouts.pounds_column()).lines[0]
    assert line.unit == "lb"
    assert line.billed_quantity == Decimal("44300")
    assert line.ticket_list == "9001"  # No tons: there aren't any.


def test_pounds_cannot_be_confirmed_as_tons(client):
    project = _project(client)
    document = _upload(client, layouts.pounds_column(), project=project).json()
    line = document["lines"][0]
    assert any("units are never converted" in blocker for blocker in line["blockers"])


def test_qty_with_no_unit_leaves_the_unit_for_a_person():
    line = read(layouts.plain_qty_no_unit()).lines[0]
    assert line.material_name == "Rip Rap Class 2"
    assert line.unit is None
    assert line.billed_quantity == Decimal("40.000")


def test_each_material_and_rate_gets_its_own_line():
    lines = read(layouts.two_materials_two_rates()).lines
    found = {(l.material_name, l.billed_unit_price): l for l in lines}
    assert set(found) == {("No. 57 Stone", Decimal("28.0000")), ("Base Rock", Decimal("18.0000")), ("No. 57 Stone", Decimal("29.5000"))}
    stone = found[("No. 57 Stone", Decimal("28.0000"))]
    assert stone.billed_quantity == Decimal("41.000")
    assert stone.ticket_list.splitlines() == ["301\t20.000", "303\t21.000"]
    assert found[("No. 57 Stone", Decimal("29.5000"))].ticket_list == "304\t19.000"


def test_a_table_continues_across_pages_without_a_repeated_header():
    result = read(layouts.continued_across_pages())
    assert len(result.lines) == 1
    assert result.lines[0].billed_quantity == Decimal("50.000")
    assert len(result.lines[0].ticket_list.splitlines()) == 5
    assert (result.lines[0].date_from, result.lines[0].date_to) == (date(2026, 10, 6), date(2026, 10, 7))


def test_credits_in_parentheses_block_the_whole_document():
    result = read(layouts.credit_in_parentheses())
    assert result.lines == [] and "negative amounts" in result.unsupported_reason


def test_extra_columns_are_ignored():
    result = read(layouts.landscape_wide())
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.invoice_number == "WIDE-3" and line.supplier_name == "Placeholder Paving"
    assert line.material_name == "Asphalt Mix" and line.billed_quantity == Decimal("38.000")
    assert line.billed_subtotal == Decimal("2660.00")
    assert line.ticket_list.splitlines() == ["A100\t18.250", "A101\t19.750"]


def test_ticket_detail_draft_confirms_through_the_ordinary_path(client):
    project = _project(client)
    document = _upload(client, layouts.ticket_detail(), project=project).json()
    line = document["lines"][0]
    assert line["blockers"] == []
    confirmed = client.post(f"/api/invoice-documents/{document['id']}/lines/{line['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    comparison = client.get("/api/invoice-comparisons").json()[0]
    assert comparison["comparison_mode"] == "itemized"
    assert [row["invoice_ticket_number"] for row in comparison["ticket_check"] if row["line"]] == ["T-20011", "T-20012", "T-20047"]
    assert comparison["source_document_id"] == document["id"]
    assert comparison["status"] == "no_verified_tickets"  # Reading an invoice proves nothing about deliveries.


def test_catalog_supplier_reaches_the_draft_through_the_api(client):
    project = _project(client)
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    document = _upload(client, layouts.letterhead_supplier(), project=project).json()
    assert document["lines"][0]["supplier_name"] == "Example Aggregates"


def _row(*pairs):
    return Row([Word(x, x + width, text) for x, width, text in pairs])


def test_a_sentence_mentioning_quantity_and_amount_is_not_a_header():
    row = _row((50, 20, "Please"), (80, 20, "check"), (110, 20, "the"), (140, 30, "quantity"), (180, 20, "and"), (210, 30, "amount"),
               (250, 20, "on"), (280, 30, "your"), (320, 30, "material"), (360, 30, "before"), (400, 30, "paying"))
    assert find_columns(row) is None


def test_a_header_needs_a_quantity_an_amount_and_something_delivered():
    assert find_columns(_row((50, 30, "Qty"), (100, 40, "Amount"))) is None
    assert find_columns(_row((50, 60, "Description"), (120, 30, "Qty"), (200, 40, "Amount"))) is not None


def test_a_number_in_a_description_stays_in_the_description():
    columns = [Column("material", 50, 110), Column("quantity", 280, 300), Column("amount", 400, 440)]
    cells = split_cells(_row((50, 15, "No."), (70, 12, "57"), (86, 30, "Stone"), (280, 25, "20.000"), (400, 30, "560.00")), columns)
    assert cells == {"material": "No. 57 Stone", "quantity": "20.000", "amount": "560.00"}


def test_a_long_description_never_spills_into_a_number():
    columns = [Column("material", 50, 110), Column("quantity", 200, 220), Column("amount", 400, 440)]
    cells = split_cells(_row((50, 60, "Washed"), (120, 60, "Crushed"), (205, 40, "Limestone"), (260, 25, "20.000"), (400, 30, "560.00")), columns)
    assert cells["material"].endswith("Limestone")


def test_amount_parsing_keeps_exact_decimals_and_marks_credits():
    assert parse_amount("$1,234.50") == Decimal("1234.50")
    assert parse_amount("(5.00)") == Decimal("-5.00")
    assert parse_amount("-5.00") == Decimal("-5.00")
    assert parse_amount("abc") is None
    assert parse_amount("NaN") is None
    assert parse_amount("") is None


def test_unit_words():
    assert unit_from("TON", None) == "ton"
    assert unit_from("tn.", None) == "ton"
    assert unit_from("LBS", None) == "lbs"
    assert unit_from("T", None) is None
    assert unit_from(None, "ton") == "ton"
    assert unit_from(None, None) is None
