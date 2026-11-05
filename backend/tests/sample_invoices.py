"""Fictional supplier invoice layouts, drawn with real text layers.

Each imitates a common shape of invoice without copying any real company's form.
Names, numbers and amounts are invented. These test the reader's handling of
layouts: they say nothing about accuracy on real invoices.
"""
from __future__ import annotations

import io


def _canvas(landscape=False):
    from reportlab.lib.pagesizes import landscape as turn, letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    return buffer, canvas.Canvas(buffer, pagesize=turn(letter) if landscape else letter)


def _table(pdf, y, xs, header, rows, *, right=()):
    """Draw a table. Columns in ``right`` are right-aligned at their x."""
    pdf.setFont("Helvetica-Bold", 9)
    for index, (x, text) in enumerate(zip(xs, header)):
        (pdf.drawRightString if index in right else pdf.drawString)(x, y, text)
    pdf.setFont("Helvetica", 9)
    for row in rows:
        y -= 14
        for index, (x, text) in enumerate(zip(xs, row)):
            (pdf.drawRightString if index in right else pdf.drawString)(x, y, text)
    return y


def _finish(buffer, pdf) -> bytes:
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def ticket_detail(rows=None, *, number="SSC-88213", supplier="Sample Stone Co") -> bytes:
    """One row per delivery ticket, the most common aggregate supplier layout."""
    rows = rows or [
        ("10/06/2026", "T-20011", "No. 57 Stone", "22.150", "28.5000", "631.28"),
        ("10/06/2026", "T-20012", "No. 57 Stone", "21.900", "28.5000", "624.15"),
        ("10/09/2026", "T-20047", "No. 57 Stone", "23.400", "28.5000", "666.90"),
    ]
    buffer, pdf = _canvas()
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(50, 750, "INVOICE")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 730, f"Invoice #: {number}")
    pdf.drawString(300, 730, f"Vendor: {supplier}")
    y = _table(pdf, 690, (50, 120, 200, 330, 400, 490), ("Date", "Ticket No", "Description", "Tons", "Price/Ton", "Amount"), rows)
    pdf.drawString(330, y - 20, "Material subtotal")
    pdf.drawString(490, y - 20, "1,922.33")
    pdf.drawString(330, y - 34, "Fuel surcharge")
    pdf.drawString(490, y - 34, "48.00")
    pdf.drawString(330, y - 48, "Invoice total")
    pdf.drawString(490, y - 48, "1,970.33")
    return _finish(buffer, pdf)


def right_aligned_numbers() -> bytes:
    """Different column names and order, with right-aligned figures."""
    buffer, pdf = _canvas()
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 750, "Inv. No. 4471-A")
    pdf.drawString(50, 735, "Sold By: Placeholder Paving")
    pdf.drawString(50, 720, "Billing Period: Oct 1, 2026 through Oct 31, 2026")
    _table(pdf, 680, (50, 280, 340, 440, 540), ("Product", "Net Tons", "UOM", "Unit Price", "Extension"),
           [("Asphalt Mix Type B 12.5mm", "1,204.350", "TON", "$72.0000", "$86,713.20")], right=(1, 3, 4))
    return _finish(buffer, pdf)


def letterhead_supplier() -> bytes:
    """The supplier is only in the letterhead, with no label."""
    buffer, pdf = _canvas()
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(50, 750, "Example Aggregates")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(50, 735, "100 Fictional Quarry Road")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 700, "Invoice Number 900123")
    pdf.drawString(50, 685, "Period: 10/01/2026 - 10/15/2026")
    _table(pdf, 650, (50, 280, 340, 400, 490), ("Material", "Qty", "Unit", "Rate", "Amount"),
           [("Base Rock", "100.000", "TON", "20.0000", "2,000.00")])
    return _finish(buffer, pdf)


def pounds_column() -> bytes:
    """Billed in pounds. Must be reported as pounds, never converted."""
    buffer, pdf = _canvas()
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 750, "Invoice #: LB-55")
    pdf.drawString(50, 735, "Supplier: Sample Stone Co")
    _table(pdf, 700, (50, 120, 200, 330, 400, 490), ("Date", "Ticket", "Material", "Net Lbs", "Rate", "Amount"),
           [("10/06/2026", "9001", "Sand", "44300", "0.0150", "664.50")])
    return _finish(buffer, pdf)


def plain_qty_no_unit() -> bytes:
    """"Qty" with no unit column: the unit stays empty for a person to fill in."""
    buffer, pdf = _canvas()
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 750, "Invoice #: Q-1")
    pdf.drawString(50, 735, "Supplier: Sample Stone Co")
    _table(pdf, 700, (50, 300, 400, 490), ("Description", "Qty", "Rate", "Total"),
           [("Rip Rap Class 2", "40.000", "33.0000", "1,320.00")])
    return _finish(buffer, pdf)


def two_materials_two_rates() -> bytes:
    buffer, pdf = _canvas()
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 750, "Invoice #: MIX-7")
    pdf.drawString(50, 735, "Supplier: Sample Stone Co")
    _table(pdf, 700, (50, 120, 200, 330, 400, 490), ("Date", "Tkt #", "Item", "Tons", "Rate", "Ext"), [
        ("10/06/2026", "301", "No. 57 Stone", "20.000", "28.0000", "560.00"),
        ("10/07/2026", "302", "Base Rock", "10.000", "18.0000", "180.00"),
        ("10/08/2026", "303", "No. 57 Stone", "21.000", "28.0000", "588.00"),
        ("10/09/2026", "304", "No. 57 Stone", "19.000", "29.5000", "560.50"),
    ])
    return _finish(buffer, pdf)


def continued_across_pages() -> bytes:
    """The table runs onto a second page with no repeated header."""
    buffer, pdf = _canvas()
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 750, "Invoice #: LONG-1")
    pdf.drawString(50, 735, "Supplier: Sample Stone Co")
    _table(pdf, 700, (50, 120, 200, 330, 400, 490), ("Date", "Ticket No", "Description", "Tons", "Rate", "Amount"),
           [("10/06/2026", str(5000 + i), "Sand", "10.000", "15.0000", "150.00") for i in range(3)])
    pdf.showPage()
    pdf.setFont("Helvetica", 9)
    y = 720
    pdf.drawString(50, 750, "Page 2 of 2")
    for i in range(3, 5):
        for x, text in zip((50, 120, 200, 330, 400, 490), ("10/07/2026", str(5000 + i), "Sand", "10.000", "15.0000", "150.00")):
            pdf.drawString(x, y, text)
        y -= 14
    pdf.drawString(330, y - 10, "Total")
    pdf.drawString(490, y - 10, "750.00")
    return _finish(buffer, pdf)


def credit_in_parentheses() -> bytes:
    buffer, pdf = _canvas()
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 750, "Invoice #: CR-1")
    pdf.drawString(50, 735, "Supplier: Sample Stone Co")
    _table(pdf, 700, (50, 300, 360, 420, 490), ("Description", "Qty", "Unit", "Rate", "Amount"),
           [("Sand", "(10.000)", "TON", "15.0000", "(150.00)")])
    return _finish(buffer, pdf)


def landscape_wide() -> bytes:
    """A wide landscape statement with extra columns the reader should ignore."""
    buffer, pdf = _canvas(landscape=True)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, 570, "Invoice No: WIDE-3")
    pdf.drawString(350, 570, "Remit To: Placeholder Paving")
    _table(pdf, 530, (50, 130, 210, 330, 470, 540, 610, 690), ("Date", "Ticket", "Truck", "Product", "Tons", "Price", "Fuel", "Amount"), [
        ("10/12/2026", "A100", "T-7", "Asphalt Mix", "18.250", "70.0000", "—", "1,277.50"),
        ("10/12/2026", "A101", "T-9", "Asphalt Mix", "19.750", "70.0000", "—", "1,382.50"),
    ])
    return _finish(buffer, pdf)
