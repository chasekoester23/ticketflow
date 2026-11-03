"""Render PDF pages and split a page image into individual ticket crops.

Pure functions, no database: easy to unit test.
Boxes are (left, top, right, bottom) in pixels of the rendered page image.
"""
import io
import math
from dataclasses import dataclass

import pymupdf as fitz  # PyMuPDF
from PIL import Image

Box = tuple[int, int, int, int]


class PdfError(Exception):
    """Raised for unreadable, corrupted, empty or password-protected PDFs."""


@dataclass
class Crop:
    position: int
    box: Box
    image: Image.Image


def render_pdf_pages(
    data: bytes,
    dpi: int = 200,
    *,
    max_pages: int = 20,
    max_page_pixels: int = 14_000_000,
    max_total_pixels: int = 70_000_000,
) -> list[Image.Image]:
    """Apply limits BEFORE rendering compressed PDF pages into memory."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PdfError("Could not open PDF. Check the document and try again.") from exc
    try:
        if doc.needs_pass:
            raise PdfError("PDF is password protected")
        if doc.page_count == 0:
            raise PdfError("PDF has no pages")
        if doc.page_count > max_pages:
            raise PdfError(
                f"PDF contains {doc.page_count} pages; the per-file limit is {max_pages}. "
                "Split this scan into smaller PDFs."
            )
        pages: list[Image.Image] = []
        total_pixels = 0
        for page in doc:
            # FitZ page.rect describes the page at 72dpi. Reject huge page
            # dimensions before get_pixmap allocates the bitmap.
            width = math.ceil(page.rect.width * dpi / 72)
            height = math.ceil(page.rect.height * dpi / 72)
            estimate = width * height
            if width <= 0 or height <= 0 or estimate > max_page_pixels:
                raise PdfError("PDF page is too large to render safely. Rescan at a lower resolution.")
            if total_pixels + estimate > max_total_pixels:
                raise PdfError("PDF is too large to process safely. Split it into smaller files.")
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            actual = pix.width * pix.height
            if actual > max_page_pixels or total_pixels + actual > max_total_pixels:
                raise PdfError("PDF renders above the safe image limit. Rescan at a lower resolution.")
            pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            total_pixels += actual
        return pages
    except PdfError:
        raise
    except Exception as exc:
        # Never expose parser internals or filesystem paths in API errors.
        raise PdfError("Could not render PDF. Check the scan and try again.") from exc
    finally:
        doc.close()


def load_image(data: bytes, *, max_pixels: int = 14_000_000) -> Image.Image:
    """Reject decompression bombs using dimensions before decoding image data."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise PdfError("Image dimensions are too large. Resize the scan before uploading.")
            img.load()
            return img.convert("RGB")
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("Could not read image. Check the file and try again.") from exc


def grid_boxes(width: int, height: int, rows: int, cols: int, margin: float = 0.01) -> list[Box]:
    """Equal grid cells, each shrunk by `margin` (fraction of the page size) to drop scanner edges and gaps."""
    mx, my = int(width * margin), int(height * margin)
    cw, ch = width / cols, height / rows
    boxes: list[Box] = []
    for r in range(rows):
        for c in range(cols):
            boxes.append((int(c * cw) + mx, int(r * ch) + my, int((c + 1) * cw) - mx, int((r + 1) * ch) - my))
    return boxes


def split_page(image: Image.Image, layout: str, margin: float = 0.01) -> list[Crop]:
    w, h = image.size
    if layout == "single":
        boxes: list[Box] = [(0, 0, w, h)]
    elif layout == "grid_2x2":
        boxes = grid_boxes(w, h, rows=2, cols=2, margin=margin)
    else:
        raise ValueError(f"Unsupported layout: {layout}")
    return [Crop(i, b, image.crop(b)) for i, b in enumerate(boxes)]


def is_blank(image: Image.Image, threshold: float = 0.002) -> bool:
    """True if almost no dark pixels, e.g. an empty quadrant when fewer than 4 tickets were scanned."""
    gray = image.convert("L").resize((200, 200))
    dark = sum(gray.histogram()[:140])
    return dark / (200 * 200) < threshold
