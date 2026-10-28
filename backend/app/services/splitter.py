"""Render PDF pages and split a page image into individual ticket crops.

Pure functions, no database: easy to unit test.
Boxes are (left, top, right, bottom) in pixels of the rendered page image.
"""
import io
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


def render_pdf_pages(data: bytes, dpi: int = 200) -> list[Image.Image]:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PdfError(f"Could not open PDF: {exc}") from exc
    try:
        if doc.needs_pass:
            raise PdfError("PDF is password protected")
        if doc.page_count == 0:
            raise PdfError("PDF has no pages")
        pages = []
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
        return pages
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"Could not render PDF: {exc}") from exc
    finally:
        doc.close()


def load_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception as exc:
        raise PdfError(f"Could not read image: {exc}") from exc


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
