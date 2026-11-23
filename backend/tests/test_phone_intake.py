"""Phone photos as intake: large photos and photo-PDFs are read within the memory
limit instead of refused, and a phone's "this way up" note is applied."""
import io

import pymupdf
import pytest
from PIL import Image, ImageDraw

from app.config import settings
from app.services import splitter

from .sample_data import TICKETS, make_ticket_image


def _jpeg(image: Image.Image, **kwargs) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85, **kwargs)
    return buf.getvalue()


def _photo_pdf(image: Image.Image) -> bytes:
    """How phones and "Save as PDF" store a photo: one point per pixel."""
    with pymupdf.open() as document:
        page = document.new_page(width=image.width, height=image.height)
        page.insert_image(page.rect, stream=_jpeg(image))
        return document.tobytes()


def _marked(size, corner_box) -> Image.Image:
    """White, with one black block, so position survives scaling and rotation checks."""
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle(corner_box, fill="black")
    return image


def _dark_at(image: Image.Image, fx: float, fy: float) -> bool:
    return image.convert("L").getpixel((int(image.width * fx), int(image.height * fy))) < 80


def test_a_24_megapixel_phone_photo_is_reduced_to_fit_not_refused():
    photo = _marked((5712, 4284), (0, 0, 571, 428))  # the default on recent iPhones
    image = splitter.load_image(_jpeg(photo), max_pixels=14_000_000)
    assert image.width * image.height <= 14_000_000
    assert abs(image.width / image.height - 5712 / 4284) < 0.01
    assert _dark_at(image, 0.05, 0.05) and not _dark_at(image, 0.5, 0.5)


def test_a_photo_already_within_the_limit_is_untouched():
    photo = _marked((1200, 900), (0, 0, 100, 100))
    assert splitter.load_image(_jpeg(photo)).size == (1200, 900)


def test_the_phones_orientation_note_is_applied():
    # Stored landscape with the block top-left; Orientation 6 means "turn 90° clockwise to view".
    stored = _marked((400, 300), (0, 0, 40, 30))
    exif = Image.Exif()
    exif[0x0112] = 6
    image = splitter.load_image(_jpeg(stored, exif=exif.tobytes()))
    assert image.size == (300, 400)  # upright portrait
    assert _dark_at(image, 0.95, 0.04)  # top-left, turned clockwise, is now top-right


def test_an_image_beyond_the_decode_ceiling_is_refused_from_its_header():
    photo = Image.new("RGB", (300, 200), "white")
    with pytest.raises(splitter.PdfError, match="too large"):
        splitter.load_image(_jpeg(photo), max_pixels=10_000, max_source_pixels=50_000)


def test_a_phone_photo_pdf_renders_within_the_limit_at_the_photos_own_detail():
    """The case that was refused: a 12 MP photo as a PDF is a 42 x 56 inch page,
    94 MP at 200 dpi. It now renders at about the photo's own resolution."""
    photo = _marked((3024, 4032), (0, 0, 302, 403))
    pages = splitter.render_pdf_pages(_photo_pdf(photo), dpi=200, max_page_pixels=14_000_000)
    page = pages[0]
    assert page.width * page.height <= 14_000_000
    assert page.width * page.height >= 0.9 * 3024 * 4032  # nothing meaningful lost
    assert abs(page.width / page.height - 3024 / 4032) < 0.01
    assert _dark_at(page, 0.05, 0.05) and not _dark_at(page, 0.5, 0.5)


def test_the_whole_file_limit_still_applies_to_many_large_pages():
    photo = Image.new("RGB", (1000, 1000), "white")
    with pymupdf.open() as document:
        for _ in range(3):
            page = document.new_page(width=1000, height=1000)
            page.insert_image(page.rect, stream=_jpeg(photo))
        pdf = document.tobytes()
    with pytest.raises(splitter.PdfError, match="Split it into smaller files"):
        splitter.render_pdf_pages(pdf, dpi=200, max_page_pixels=1_000_000, max_total_pixels=2_500_000)


def test_ordinary_scans_render_exactly_as_before():
    with pymupdf.open() as document:
        document.new_page(width=612, height=792)  # US Letter
        pdf = document.tobytes()
    (page,) = splitter.render_pdf_pages(pdf, dpi=200)
    assert page.size == (1700, 2200)


def test_a_phone_photo_pdf_of_four_tickets_goes_through_the_whole_intake(client, monkeypatch):
    """Upload -> split -> read -> review queue, and the page preview, for a photo-PDF
    that is larger than the render limit at 200 dpi."""
    monkeypatch.setattr(settings, "max_rendered_page_pixels", 4_000_000)
    sheet = Image.new("RGB", (1700, 2200), "white")
    for i, ticket in enumerate(TICKETS[:4]):
        cell = make_ticket_image(ticket).resize((800, 1050))
        sheet.paste(cell, ((i % 2) * 850 + 25, (i // 2) * 1100 + 25))
    pdf = _photo_pdf(sheet)  # 1700 x 2200 pt page: 23.6 inches wide, 52 MP at 200 dpi

    result = client.post("/api/batches", data={"layout": "grid_2x2"},
                         files=[("files", ("IMG_0412.pdf", pdf, "application/pdf"))])
    assert result.status_code == 200, result.text
    batch = client.get(f"/api/batches/{result.json()['id']}").json()
    assert batch["files"][0]["status"] == "ready_for_review", batch["files"][0]
    tickets = client.get(f"/api/batches/{batch['id']}/tickets").json()
    assert len(tickets) == 4
    assert all(t["status"] == "needs_review" for t in tickets)  # a person still approves each one
    preview = client.get(f"/api/files/{batch['files'][0]['id']}/pages/1/image")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"


def test_a_sideways_phone_photo_of_one_ticket_is_stored_upright(client):
    ticket = make_ticket_image(TICKETS[0])  # portrait
    stored = ticket.transpose(Image.Transpose.ROTATE_90)  # what the camera wrote: landscape
    exif = Image.Exif()
    exif[0x0112] = 6
    result = client.post("/api/batches", data={"layout": "single"},
                         files=[("files", ("IMG_0413.jpg", _jpeg(stored, exif=exif.tobytes()), "image/jpeg"))])
    assert result.status_code == 200, result.text
    (record,) = client.get(f"/api/batches/{result.json()['id']}/tickets").json()
    crop = Image.open(io.BytesIO(client.get(f"/api/tickets/{record['id']}/image").content))
    assert crop.height > crop.width  # portrait, as the person held it


def test_an_iphone_heic_photo_is_refused_with_how_to_fix_it(client):
    result = client.post("/api/batches", data={"layout": "single"},
                         files=[("files", ("IMG_0414.HEIC", b"\x00\x00\x00\x18ftypheic", "image/heic"))])
    assert result.status_code == 400 or result.status_code == 422, result.text
    assert "Export it as JPEG" in result.text and "Most Compatible" in result.text
