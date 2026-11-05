"""Synthetic scanned-ticket fixtures. Entirely made up; used for tests and local demos.

make_ticket_image draws a printed-style ticket; make_scan_pdf places up to four on one page (2x2),
mimicking the flatbed-scanner workflow, then wraps the page in a PDF.
"""
import io

from PIL import Image, ImageDraw, ImageFont

TICKETS = [
    dict(ticket_number="10482", delivery_date="2026-10-15", material_type="No. 53 Crushed Stone", tonnage="24.50", supplier="Example Aggregates", job_location="Project A"),
    dict(ticket_number="10483", delivery_date="2026-10-15", material_type="No. 8 Stone", tonnage="22.10", supplier="Example Aggregates", job_location="Project A"),
    dict(ticket_number="77120", delivery_date="2026-10-16", material_type="Sand", tonnage="19.75", supplier="Sample Materials", job_location="Project A"),
    dict(ticket_number="77121", delivery_date="2026-10-16", material_type="Sand", tonnage="20.30", supplier="Sample Materials", job_location="Project A"),
]


def make_ticket_image(t: dict, size=(850, 1100)) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default(size=34)
    d.rectangle([10, 10, size[0] - 10, size[1] - 10], outline="black", width=4)
    d.text((40, 40), t["supplier"].upper(), fill="black", font=f)
    for i, (label, key) in enumerate([("Ticket No.", "ticket_number"), ("Date", "delivery_date"), ("Material", "material_type"),
                                      ("Net Tons", "tonnage"), ("Job", "job_location")]):
        y = 200 + i * 130
        d.text((40, y), f"{label}:", fill="black", font=f)
        d.text((330, y), str(t[key]), fill=(20, 20, 140), font=f)
        d.line([330, y + 50, size[0] - 40, y + 50], fill="gray", width=2)
    return img


def make_scan_page(tickets: list[dict], page_size=(1700, 2200)) -> Image.Image:
    page = Image.new("RGB", page_size, "white")
    w, h = page_size[0] // 2, page_size[1] // 2
    for i, t in enumerate(tickets[:4]):
        tile = make_ticket_image(t).resize((w - 80, h - 80))
        page.paste(tile, ((i % 2) * w + 40, (i // 2) * h + 40))
    return page


def make_scan_pdf(tickets: list[dict] | None = None) -> bytes:
    page = make_scan_page(tickets or TICKETS)
    buf = io.BytesIO()
    page.save(buf, format="PDF", resolution=200)
    return buf.getvalue()


# ---- Scan-condition fixtures: see docs/engineering/fixtures.md. All fictional.

def make_faint_ticket_image(size=(850, 1100)) -> Image.Image:
    """A real-looking ticket printed so lightly that the dark-pixel test misses it."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default(size=44)
    light = (205, 205, 205)
    d.rectangle([10, 10, size[0] - 10, size[1] - 10], outline=light, width=4)
    for i, text in enumerate(["FAINT MATERIALS CO", "Ticket No. 55120", "Net Tons 18.40", "Sand", "North Lot"]):
        d.text((40, 60 + i * 150), text, fill=light, font=f)
    return img


def make_page(slots: list, page_size=(1700, 2200)) -> Image.Image:
    """Four slots; each is a ticket dict, "faint", or None for nothing there."""
    page = Image.new("RGB", page_size, "white")
    w, h = page_size[0] // 2, page_size[1] // 2
    for i, slot in enumerate(slots[:4]):
        if slot is None:
            continue
        source = make_faint_ticket_image() if slot == "faint" else make_ticket_image(slot)
        page.paste(source.resize((w - 80, h - 80)), ((i % 2) * w + 40, (i // 2) * h + 40))
    return page


def make_pdf(pages: list[Image.Image]) -> bytes:
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", resolution=200, save_all=True, append_images=pages[1:])
    return buf.getvalue()


def make_invoice_pdf(*, number="EA-2026-1107", supplier="Example Aggregates", period="10/01/2026 - 10/31/2026",
                     rows=(("No. 53 Crushed Stone", "46.600", "TON", "30.0000", "1,398.00"),),
                     extras=(("Freight", "120.00"), ("Sales tax", "83.88"), ("Invoice total", "1,601.88")),
                     tickets=(("10482", "24.50"), ("10483", "22.10")), title="INVOICE", pages=1) -> bytes:
    """A fictional supplier invoice with a real text layer, drawn in table cells."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    y = 740
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(50, y, title)
    pdf.setFont("Helvetica", 10)
    for text in (f"Invoice No: {number}", f"Supplier: {supplier}", f"Delivery Period: {period}"):
        y -= 18
        pdf.drawString(50, y, text)
    y -= 30
    for x, text in zip((50, 280, 340, 400, 490), ("Description", "Qty", "Unit", "Rate", "Amount")):
        pdf.drawString(x, y, text)
    for row in rows:
        y -= 16
        for x, text in zip((50, 280, 340, 400, 490), row):
            pdf.drawString(x, y, text)
    for label, amount in extras:
        y -= 16
        pdf.drawString(50, y, label)
        pdf.drawString(490, y, amount)
    if pages > 1:
        pdf.showPage()
        pdf.setFont("Helvetica", 10)
        y = 740
    y -= 30
    for number_text, tons in tickets:
        y -= 14
        pdf.drawString(50, y, f"Ticket {number_text}")
        pdf.drawString(200, y, tons)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
