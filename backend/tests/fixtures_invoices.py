"""Fictional supplier invoices with mistakes planted on purpose, and an answer key.

Every company, ticket, price and layout here is invented. Each case says what's
delivered, draws the supplier's PDF, and records by hand what the software must
conclude: result, tons bridge and dollar effect. Figures in the key are literal
numbers worked out on paper, so a mistake in the software can't also be a mistake
in the test.

This tests the software's logic and how it handles layouts we drew ourselves. It
says nothing about accuracy on real supplier invoices, which hasn't been measured.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Delivery:
    number: str
    day: date
    tons: str
    material: str
    supplier: str
    status: str = "verified"  # needs_review stays unapproved


@dataclass(frozen=True)
class Printed:
    """One material line as it is on the paper: what a perfect reader would draft."""
    material: str
    quantity: str
    rate: str | None
    subtotal: str
    date_from: date | None = None
    date_to: date | None = None
    tickets: tuple[tuple[str, str | None], ...] = ()
    unit: str | None = "ton"


@dataclass(frozen=True)
class Result:
    """What comparing this line must show. ``refused`` means confirming is refused."""
    status: str | None = None
    verified_tons: str | None = None
    verified_subtotal: str | None = None
    # kind -> (tons, ticket numbers). Anything not listed must not appear.
    bridge: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    unexplained: str = "0"
    # start, quantity, rate, arithmetic, rounding, billed. None: approved cost isn't known.
    money: tuple[str, str, str, str, str, str] | None = None
    dollar_effect: str | None = None
    refused: tuple[int, str] | None = None
    # Decided by a person before confirming, because the paper doesn't say or says it wrongly.
    correct: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Line:
    printed: Printed
    result: Result


@dataclass(frozen=True)
class Document:
    pdf: bytes
    invoice_number: str | None
    supplier: str | None  # As printed in a way the reader can tell it's the supplier.
    lines: tuple[Line, ...]
    unread: tuple[str, ...] = ()  # Text that must be listed as read but not used.
    upload_refused: int | None = None


@dataclass(frozen=True)
class Case:
    name: str
    plants: tuple[str, ...]
    rates: dict[tuple[str, str], str]
    deliveries: tuple[Delivery, ...]
    documents: tuple[Document, ...]
    # Supplier names the company has in its catalog with no rate, if any.
    extra_suppliers: tuple[str, ...] = ()


# ------------------------------------------------------------ drawing


def _canvas(landscape: bool = False):
    from reportlab.lib.pagesizes import landscape as turn, letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    # invariant: the same drawing gives the same bytes on every run, with no timestamp.
    return buffer, canvas.Canvas(buffer, pagesize=turn(letter) if landscape else letter, invariant=1)


def _text(pdf, rows, font="Helvetica", size=10):
    pdf.setFont(font, size)
    for x, y, value in rows:
        pdf.drawString(x, y, value)


def _table(pdf, y, xs, header, rows, *, right=(), size=9):
    pdf.setFont("Helvetica-Bold", size)
    for index, (x, value) in enumerate(zip(xs, header)):
        (pdf.drawRightString if index in right else pdf.drawString)(x, y, value)
    pdf.setFont("Helvetica", size)
    for row in rows:
        y -= 14
        for index, (x, value) in enumerate(zip(xs, row)):
            if value:
                (pdf.drawRightString if index in right else pdf.drawString)(x, y, value)
    return y


def _finish(buffer, pdf) -> bytes:
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _ticket_detail(number, supplier, rows, footer) -> bytes:
    """Portrait: two labels on one line, one row per ticket, totals underneath."""
    buffer, pdf = _canvas()
    _text(pdf, [(50, 750, "INVOICE")], "Helvetica-Bold", 14)
    _text(pdf, [(50, 730, f"Invoice #: {number}"), (300, 730, f"Vendor: {supplier}")])
    y = _table(pdf, 690, (50, 120, 200, 330, 400, 490), ("Date", "Ticket No", "Description", "Tons", "Price/Ton", "Amount"), rows)
    _text(pdf, [(330, y - 20 - 14 * i, label) for i, (label, _) in enumerate(footer)]
          + [(490, y - 20 - 14 * i, value) for i, (_, value) in enumerate(footer)])
    return _finish(buffer, pdf)


def _summary_and_ticket_rows(number, supplier, period, rows, tickets) -> bytes:
    """Portrait: right-aligned dollars, one summary row, tickets listed below it."""
    buffer, pdf = _canvas()
    _text(pdf, [(50, 750, f"Inv. No. {number}"), (50, 735, f"Sold By: {supplier}"), (50, 720, f"Billing Period: {period}")])
    y = _table(pdf, 680, (50, 280, 340, 440, 540), ("Product", "Net Tons", "UOM", "Unit Price", "Extension"), rows, right=(1, 3, 4))
    _text(pdf, [(50, y - 30 - 14 * i, f"Ticket {ticket} {tons}") for i, (ticket, tons) in enumerate(tickets)])
    return _finish(buffer, pdf)


def _wide_landscape(number, supplier, rows) -> bytes:
    """Landscape statement, with columns the reader should ignore."""
    buffer, pdf = _canvas(landscape=True)
    _text(pdf, [(50, 570, f"Invoice No: {number}"), (350, 570, f"Remit To: {supplier}")])
    _table(pdf, 530, (50, 130, 210, 330, 470, 540, 610, 690), ("Date", "Ticket", "Truck", "Product", "Tons", "Price", "Fuel", "Amount"), rows)
    return _finish(buffer, pdf)


def _letterhead(name, address, number, period, rows) -> bytes:
    """The supplier is only in the letterhead, with no label. Tickets are BOLs."""
    buffer, pdf = _canvas()
    _text(pdf, [(50, 750, name)], "Helvetica-Bold", 16)
    _text(pdf, [(50, 735, address)], "Helvetica", 9)
    _text(pdf, [(50, 700, f"Invoice Number {number}"), (50, 685, f"Period: {period}")])
    _table(pdf, 650, (50, 110, 180, 330, 390, 440, 510), ("Date", "BOL", "Material", "Qty", "Unit", "Rate", "Amount"), rows)
    return _finish(buffer, pdf)


def _two_pages(number, supplier, first, second, closing) -> bytes:
    """The table runs onto a second page with no repeated header."""
    buffer, pdf = _canvas()
    xs = (50, 120, 200, 330, 400, 490)
    _text(pdf, [(50, 750, f"Invoice #: {number}"), (50, 735, f"Supplier: {supplier}")])
    _table(pdf, 700, xs, ("Date", "Tkt #", "Item", "Tons", "Rate", "Ext"), first)
    pdf.showPage()
    _text(pdf, [(50, 750, "Page 2 of 2")], "Helvetica", 9)
    y = 720
    pdf.setFont("Helvetica", 9)
    for row in second:
        for x, value in zip(xs, row):
            pdf.drawString(x, y, value)
        y -= 14
    _text(pdf, [(330, y - 10, closing[0]), (490, y - 10, closing[1])])
    return _finish(buffer, pdf)


def _charges_between_rows(number, supplier, period, rows) -> bytes:
    """Freight and tax sit between the material rows, in the same columns."""
    buffer, pdf = _canvas()
    _text(pdf, [(50, 750, f"Invoice No. {number}"), (300, 750, f"Bill From: {supplier}"), (50, 735, f"Delivery Period: {period}")])
    _table(pdf, 700, (50, 120, 220, 340, 410, 490), ("Delivery Date", "Scale Ticket", "Material", "Net Tons", "Rate", "Line Total"), rows)
    return _finish(buffer, pdf)


def _service_period(number, supplier, period, rows) -> bytes:
    """Two-digit years, a service period, and other column names."""
    buffer, pdf = _canvas()
    _text(pdf, [(50, 750, f"Invoice: {number}"), (50, 735, f"From: {supplier}"), (50, 720, f"Service Period: {period}")])
    _table(pdf, 680, (50, 120, 210, 350, 420, 500), ("Load Date", "Load Ticket", "Product Description", "Tons", "$/Ton", "Total"), rows)
    return _finish(buffer, pdf)


def _summary_only(number, supplier, rows) -> bytes:
    """Totals per material, with no tickets and no dates at all."""
    buffer, pdf = _canvas()
    _text(pdf, [(50, 750, f"Invoice Number: {number}"), (50, 735, f"Supplier: {supplier}")])
    _table(pdf, 700, (50, 280, 340, 400, 490), ("Item Description", "Qty", "Unit", "Price", "Charge"), rows)
    return _finish(buffer, pdf)


def _iso_dates(number, supplier, rows) -> bytes:
    """ISO dates and a price column named for tons."""
    buffer, pdf = _canvas()
    _text(pdf, [(50, 750, f"Inv # {number}"), (300, 750, f"Vendor: {supplier}")])
    _table(pdf, 700, (50, 130, 220, 350, 420, 500), ("Ship Date", "Delivery Ticket", "Material Description", "Tonnage", "Price Per Ton", "Extended"), rows)
    return _finish(buffer, pdf)


# ------------------------------------------------------------ the cases

STONE, BASE, SAND, ASPHALT, RIPRAP = "No. 57 Stone", "Base Rock", "Concrete Sand", "Asphalt Mix", "Rip Rap Class 2"


def _d(day: int, month: int = 10) -> date:
    return date(2026, month, day)


def wrong_billed_quantity() -> Case:
    supplier = "Ridgeline Aggregates"
    pdf = _ticket_detail("RA-88213", supplier, [
        ("10/06/2026", "T-20011", STONE, "22.150", "28.5000", "631.28"),
        ("10/06/2026", "T-20012", STONE, "21.900", "28.5000", "624.15"),
        ("10/09/2026", "T-20047", STONE, "26.400", "28.5000", "752.40"),  # Delivered 23.400.
    ], [("Material subtotal", "2,007.83"), ("Fuel surcharge", "48.00"), ("Invoice total", "2,055.83")])
    return Case(
        "wrong_billed_quantity", ("wrong billed quantity on one ticket",), {(supplier, STONE): "28.5"},
        (Delivery("T-20011", _d(6), "22.150", STONE, supplier), Delivery("T-20012", _d(6), "21.900", STONE, supplier),
         Delivery("T-20047", _d(9), "23.400", STONE, supplier)),
        (Document(pdf, "RA-88213", supplier, (Line(
            Printed(STONE, "70.450", "28.5", "2007.83", _d(6), _d(9),
                    (("T-20011", "22.150"), ("T-20012", "21.900"), ("T-20047", "26.400"))),
            Result("discrepancy", "67.450", "1922.33", {"tons_differ": ("3.000", ("T-20047",))}, "0",
                   ("1922.33", "85.50", "0", "0", "0", "2007.83"), "85.50")),),
            unread=("Material subtotal", "Fuel surcharge", "Invoice total")),))


def wrong_rate() -> Case:
    supplier = "Harbor Stone Supply"
    pdf = _summary_and_ticket_rows("HS-4471", supplier, "Oct 1, 2026 through Oct 31, 2026",
                                   [(BASE, "62.500", "TON", "$19.7500", "$1,234.38")], [("7701", "30.000"), ("7702", "32.500")])
    return Case(
        "wrong_rate", ("billed above the agreed rate",), {(supplier, BASE): "18.25"},
        (Delivery("7701", _d(3), "30.000", BASE, supplier), Delivery("7702", _d(20), "32.500", BASE, supplier)),
        (Document(pdf, "HS-4471", supplier, (Line(
            Printed(BASE, "62.500", "19.75", "1234.38", _d(1), _d(31), (("7701", "30.000"), ("7702", "32.500"))),
            Result("discrepancy", "62.500", "1140.63", {}, "0",
                   ("1140.63", "0", "93.75", "0", "0", "1234.38"), "93.75")),)),))


def bad_multiplication() -> Case:
    supplier = "Kestrel Paving"
    pdf = _wide_landscape("WIDE-3", supplier, [
        ("10/12/2026", "A100", "T-7", ASPHALT, "18.250", "70.0000", "-", "1,277.50"),
        ("10/12/2026", "A101", "T-9", ASPHALT, "19.750", "70.0000", "-", "1,482.50"),  # 19.750 x 70 is 1,382.50.
    ])
    return Case(
        "bad_multiplication", ("a row's amount isn't tons times rate",), {(supplier, ASPHALT): "70"},
        (Delivery("A100", _d(12), "18.250", ASPHALT, supplier), Delivery("A101", _d(12), "19.750", ASPHALT, supplier)),
        (Document(pdf, "WIDE-3", supplier, (Line(
            Printed(ASPHALT, "38.000", "70", "2760.00", _d(12), _d(12), (("A100", "18.250"), ("A101", "19.750"))),
            Result("discrepancy", "38.000", "2660.00", {}, "0",
                   ("2660.00", "0", "0", "100.00", "0", "2760.00"), "100.00")),),
            unread=("Ticket A101: 19.750 x 70.0000 is 1382.50, but this row shows 1482.50",)),))


def ticket_billed_twice() -> Case:
    supplier = "Millbrook Sand & Gravel"
    pdf = _letterhead("MILLBROOK S&G", "100 Fictional Quarry Road", "900123", "10/01/2026 - 10/15/2026", [
        ("10/02/2026", "8001", SAND, "14.000", "TON", "15.5000", "217.00"),
        ("10/05/2026", "8002", SAND, "16.500", "TON", "15.5000", "255.75"),
        ("10/05/2026", "8002", SAND, "16.500", "TON", "15.5000", "255.75"),
    ])
    return Case(
        "ticket_billed_twice", ("one ticket on two rows", "supplier only in a logo the catalog doesn't know"), {(supplier, SAND): "15.5"},
        (Delivery("8001", _d(2), "14.000", SAND, supplier), Delivery("8002", _d(5), "16.500", SAND, supplier)),
        (Document(pdf, "900123", None, (Line(
            Printed(SAND, "47.000", "15.5", "728.50", _d(1), _d(15), (("8001", "14.000"), ("8002", "16.500"), ("8002", "16.500"))),
            Result("discrepancy", "30.500", "472.75", {"repeated": ("16.500", ("8002",))}, "0",
                   ("472.75", "255.75", "0", "0", "0", "728.50"), "255.75",
                   correct={"supplier_name": supplier})),)),))


def ticket_never_delivered() -> Case:
    supplier = "Tri-County Materials"
    sand = [(f"10/0{6 + i // 2}/2026", str(5000 + i), SAND, "10.000", "15.0000", "150.00") for i in range(5)]
    pdf = _two_pages("LONG-1", supplier, sand[:3], sand[3:] + [("10/08/2026", "6001", BASE, "10.000", "18.0000", "180.00")],
                     ("Total", "930.00"))
    return Case(
        "ticket_never_delivered", ("a ticket with no delivery", "table continues on page 2 with no header", "a clean line beside it"),
        {(supplier, SAND): "15", (supplier, BASE): "18"},
        (*(Delivery(str(5000 + i), _d(6 + i // 2), "10.000", SAND, supplier) for i in range(4)),
         Delivery("6001", _d(8), "10.000", BASE, supplier)),
        (Document(pdf, "LONG-1", supplier, (
            Line(Printed(SAND, "50.000", "15", "750.00", _d(6), _d(8),
                         tuple((str(5000 + i), "10.000") for i in range(5))),
                 Result("discrepancy", "40.000", "600.00", {"not_found": ("10.000", ("5004",))}, "0",
                        ("600.00", "150.00", "0", "0", "0", "750.00"), "150.00")),
            Line(Printed(BASE, "10.000", "18", "180.00", _d(8), _d(8), (("6001", "10.000"),)),
                 Result("matches", "10.000", "180.00", {}, "0", ("180.00", "0", "0", "0", "0", "180.00"), "0")),
        ), unread=("Total",)),))


def missing_delivery_with_charges_mixed_in() -> Case:
    supplier = "Stonebridge Quarry"
    pdf = _charges_between_rows("SB-5", supplier, "10/12/2026 - 10/14/2026", [
        ("10/12/2026", "S-1", RIPRAP, "20.000", "33.0000", "660.00"),
        ("", "", "Freight", "", "", "120.00"),
        ("10/13/2026", "S-2", RIPRAP, "21.000", "33.0000", "693.00"),
        ("", "", "Sales tax", "", "", "94.71"),
    ])
    return Case(
        "missing_delivery_with_charges_mixed_in", ("a delivery left off", "freight and tax between material rows"), {(supplier, RIPRAP): "33"},
        (Delivery("S-1", _d(12), "20.000", RIPRAP, supplier), Delivery("S-2", _d(13), "21.000", RIPRAP, supplier),
         Delivery("S-3", _d(14), "22.000", RIPRAP, supplier)),
        (Document(pdf, "SB-5", supplier, (Line(
            Printed(RIPRAP, "41.000", "33", "1353.00", _d(12), _d(14), (("S-1", "20.000"), ("S-2", "21.000"))),
            Result("discrepancy", "63.000", "2079.00", {"not_on_invoice": ("-22.000", ("S-3",))}, "0",
                   ("2079.00", "-726.00", "0", "0", "0", "1353.00"), "-726.00")),),
            unread=("Freight", "Sales tax")),))


def freight_inside_a_material_row() -> Case:
    """A row saying "incl. freight" can't be split safely, so it isn't drafted. It's
    listed for a person, who adds the material figures from the original."""
    supplier = "Stonebridge Quarry"
    pdf = _charges_between_rows("SB-6", supplier, "10/12/2026 - 10/14/2026", [
        ("10/12/2026", "S-11", RIPRAP, "20.000", "33.0000", "660.00"),
        ("10/13/2026", "S-12", "Rip Rap incl. freight", "21.000", "38.0000", "798.00"),
    ])
    return Case(
        "freight_inside_a_material_row", ("freight folded into a material row",), {(supplier, RIPRAP): "33"},
        (Delivery("S-11", _d(12), "20.000", RIPRAP, supplier), Delivery("S-12", _d(13), "21.000", RIPRAP, supplier)),
        (Document(pdf, "SB-6", supplier, (Line(
            Printed(RIPRAP, "20.000", "33", "660.00", _d(12), _d(14), (("S-11", "20.000"),)),
            Result("discrepancy", "41.000", "1353.00", {"not_on_invoice": ("-21.000", ("S-12",))}, "0",
                   ("1353.00", "-693.00", "0", "0", "0", "660.00"), "-693.00")),),
            unread=("incl. freight",)),))


def incomplete_billing_period() -> Case:
    supplier = "Northfield Aggregates"
    pdf = _service_period("N-77", supplier, "10/01/26 - 10/15/26", [
        ("10/05/26", "N-1", STONE, "20.000", "27.0000", "540.00"),
        ("10/14/26", "N-2", STONE, "25.000", "27.0000", "675.00"),
        ("10/18/26", "N-3", STONE, "18.000", "27.0000", "486.00"),  # After the period it says it covers.
    ])
    return Case(
        "incomplete_billing_period", ("a stated period that stops short of what's billed",), {(supplier, STONE): "27"},
        (Delivery("N-1", _d(5), "20.000", STONE, supplier), Delivery("N-2", _d(14), "25.000", STONE, supplier),
         Delivery("N-3", _d(18), "18.000", STONE, supplier), Delivery("N-4", _d(16), "19.000", STONE, supplier)),
        (Document(pdf, "N-77", supplier, (Line(
            Printed(STONE, "63.000", "27", "1701.00", _d(1), _d(15), (("N-1", "20.000"), ("N-2", "25.000"), ("N-3", "18.000"))),
            Result("discrepancy", "45.000", "1215.00", {"outside_scope": ("18.000", ("N-3",))}, "0",
                   ("1215.00", "486.00", "0", "0", "0", "1701.00"), "486.00")),)),))


def delivery_in_period_still_in_review() -> Case:
    supplier = "Northfield Aggregates"
    pdf = _service_period("N-78", supplier, "11/01/26 - 11/15/26", [
        ("11/02/26", "N-21", STONE, "20.000", "27.0000", "540.00"),
        ("11/03/26", "N-22", STONE, "22.000", "27.0000", "594.00"),
    ])
    return Case(
        "delivery_in_period_still_in_review", ("nobody has approved one delivery yet",), {(supplier, STONE): "27"},
        (Delivery("N-21", _d(2, 11), "20.000", STONE, supplier), Delivery("N-22", _d(3, 11), "22.000", STONE, supplier, status="needs_review")),
        (Document(pdf, "N-78", supplier, (Line(
            Printed(STONE, "42.000", "27", "1134.00", _d(1, 11), _d(15, 11), (("N-21", "20.000"), ("N-22", "22.000"))),
            Result("incomplete", "20.000", "540.00", {"unverified": ("22.000", ("N-22",))}, "0",
                   ("540.00", "594.00", "0", "0", "0", "1134.00"), "594.00")),)),))


def duplicate_lines() -> Case:
    supplier = "Cedar Ridge Stone"
    pdf = _summary_only("CR-501", supplier, [(BASE, "40.000", "TON", "20.0000", "800.00"), (BASE, "40.000", "TON", "20.0000", "800.00")])
    # The reader takes "Unit" and "Price" for one "Unit Price" column, which leaves the unit
    # blank and puts "TON" on the material. See test_adjacent_unit_and_price_columns_stay_separate.
    dates = {"date_from": "2026-10-01", "date_to": "2026-10-31", "material_name": BASE, "unit": "ton"}
    return Case(
        "duplicate_lines", ("the same material line printed twice", "no dates on the invoice"), {(supplier, BASE): "20"},
        (Delivery("CR-1", _d(3), "20.000", BASE, supplier), Delivery("CR-2", _d(9), "20.000", BASE, supplier)),
        (Document(pdf, "CR-501", supplier, (
            Line(Printed(BASE, "40.000", "20", "800.00"),
                 Result("matches", "40.000", "800.00", {}, "0", ("800.00", "0", "0", "0", "0", "800.00"), "0", correct=dates)),
            Line(Printed(BASE, "40.000", "20", "800.00"),
                 Result(refused=(409, "already has a saved line"), correct=dates)),
        )),))


def _resend(number: str) -> bytes:
    return _iso_dates(number, "Placeholder Paving", [
        ("2026-10-20", "PP-1", ASPHALT, "18.000", "60.0000", "1,080.00"),
        ("2026-10-21", "PP-2", ASPHALT, "19.000", "60.0000", "1,140.00"),
    ])


def resent_under_a_new_number() -> Case:
    supplier = "Placeholder Paving"
    printed = Printed(ASPHALT, "37.000", "60", "2220.00", _d(20), _d(21), (("PP-1", "18.000"), ("PP-2", "19.000")))
    first = Document(_resend("PP-900"), "PP-900", supplier, (Line(
        printed, Result("matches", "37.000", "2220.00", {}, "0", ("2220.00", "0", "0", "0", "0", "2220.00"), "0")),))
    return Case(
        "resent_under_a_new_number", ("the same bill sent again under a new number", "the identical file uploaded again"),
        {(supplier, ASPHALT): "60"},
        (Delivery("PP-1", _d(20), "18.000", ASPHALT, supplier), Delivery("PP-2", _d(21), "19.000", ASPHALT, supplier)),
        (first,
         Document(_resend("PP-900"), None, None, (), upload_refused=409),
         Document(_resend("PP-900-R"), "PP-900-R", supplier, (Line(
             printed, Result(refused=(409, "already covers this project, supplier, material and overlapping")),),)),))


def similar_supplier_names() -> Case:
    real, similar = "Sample Stones Co", "Sample Stone Co"
    misprint = "Sample Stone Company"
    rows = [("2026-10-06", "7001", SAND, "20.000", "25.0000", "500.00"), ("2026-10-07", "7002", SAND, "22.000", "25.0000", "550.00")]
    printed = Printed(SAND, "42.000", "25", "1050.00", _d(6), _d(7), (("7001", "20.000"), ("7002", "22.000")))
    return Case(
        "similar_supplier_names", ("two real suppliers one letter apart, with the same ticket number", "a third spelling on paper that must not merge"),
        {(real, SAND): "25", (similar, SAND): "26"},
        (Delivery("7001", _d(6), "20.000", SAND, real), Delivery("7002", _d(7), "22.000", SAND, real),
         Delivery("7001", _d(6), "30.000", SAND, similar), Delivery("7003", _d(7), "31.000", SAND, similar)),
        (Document(_iso_dates("SS-1", real, rows), "SS-1", real, (Line(
            printed, Result("matches", "42.000", "1050.00", {}, "0", ("1050.00", "0", "0", "0", "0", "1050.00"), "0")),)),
         # Its tickets are all another supplier's, or nobody's, so nothing can be compared.
         Document(_iso_dates("SS-2", misprint, rows), "SS-2", misprint, (Line(
             printed, Result("no_verified_tickets", "0.000", None, {"outside_scope": ("42.000", ("7001", "7002"))}, "0")),)),))


def rows_disagree_with_their_summary() -> Case:
    supplier = "Harbor Stone Supply"
    pdf = _summary_and_ticket_rows("HS-4480", supplier, "Oct 1, 2026 through Oct 31, 2026",
                                   [(STONE, "60.000", "TON", "$30.0000", "$1,800.00")], [("7801", "25.000"), ("7802", "30.000")])
    return Case(
        "rows_disagree_with_their_summary", ("the summary bills more tons than its tickets add up to",), {(supplier, STONE): "30"},
        (Delivery("7801", _d(3), "25.000", STONE, supplier), Delivery("7802", _d(4), "30.000", STONE, supplier)),
        (Document(pdf, "HS-4480", supplier, (Line(
            Printed(STONE, "60.000", "30", "1800.00", _d(1), _d(31), (("7801", "25.000"), ("7802", "30.000"))),
            Result("discrepancy", "55.000", "1650.00", {}, "5.000", ("1650.00", "150.00", "0", "0", "0", "1800.00"), "150.00")),)),))


def half_cents_only() -> Case:
    """Each row rounds correctly, so the rows and the approved cost agree, yet tons times
    rate differs by a cent. The bridge has to say so without inventing a difference."""
    supplier = "Ridgeline Aggregates"
    pdf = _ticket_detail("RA-90000", supplier, [
        ("11/06/2026", "T-30011", STONE, "22.150", "28.5000", "631.28"),
        ("11/06/2026", "T-30012", STONE, "22.350", "28.5000", "636.98"),
    ], [("Invoice total", "1,268.26")])
    return Case(
        "half_cents_only", ("a one-cent gap that is only rounding",), {(supplier, STONE): "28.5"},
        (Delivery("T-30011", _d(6, 11), "22.150", STONE, supplier), Delivery("T-30012", _d(6, 11), "22.350", STONE, supplier)),
        (Document(pdf, "RA-90000", supplier, (Line(
            Printed(STONE, "44.500", "28.5", "1268.26", _d(6, 11), _d(6, 11), (("T-30011", "22.150"), ("T-30012", "22.350"))),
            Result("discrepancy", "44.500", "1268.26", {}, "0", ("1268.26", "0", "0", "0.01", "-0.01", "1268.26"), "0")),),
            unread=("Invoice total",)),))


def clean_invoice() -> Case:
    """The control. If this one reports a problem, the others prove nothing."""
    supplier = "Placeholder Paving"
    pdf = _iso_dates("PP-1000", supplier, [
        ("2026-10-06", "PC-1", BASE, "12.500", "18.0000", "225.00"),
        ("2026-10-07", "PC-2", BASE, "13.250", "18.0000", "238.50"),
        ("2026-10-08", "PC-3", BASE, "14.000", "18.0000", "252.00"),
    ])
    return Case(
        "clean_invoice", ("nothing wrong",), {(supplier, BASE): "18"},
        (Delivery("PC-1", _d(6), "12.500", BASE, supplier), Delivery("PC-2", _d(7), "13.250", BASE, supplier),
         Delivery("PC-3", _d(8), "14.000", BASE, supplier)),
        (Document(pdf, "PP-1000", supplier, (Line(
            Printed(BASE, "39.750", "18", "715.50", _d(6), _d(8), (("PC-1", "12.500"), ("PC-2", "13.250"), ("PC-3", "14.000"))),
            Result("matches", "39.750", "715.50", {}, "0", ("715.50", "0", "0", "0", "0", "715.50"), "0")),)),))


CASES = (
    wrong_billed_quantity, wrong_rate, bad_multiplication, ticket_billed_twice, ticket_never_delivered,
    missing_delivery_with_charges_mixed_in, freight_inside_a_material_row, incomplete_billing_period,
    delivery_in_period_still_in_review, duplicate_lines, resent_under_a_new_number, similar_supplier_names,
    rows_disagree_with_their_summary, half_cents_only, clean_invoice,
)
