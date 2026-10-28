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
