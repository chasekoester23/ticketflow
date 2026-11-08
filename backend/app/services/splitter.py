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
        damaged = incomplete_reason(doc, data)
        if damaged:
            raise PdfError(damaged)
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


def incomplete_reason(doc, data: bytes) -> str | None:
    """Why a PDF looks cut short, or None.

    MuPDF quietly rebuilds a damaged file and shows whatever survived, so a scan
    cut off mid-download would show half its tickets with nothing to say so.
    Only a rebuilt file is checked: it's refused when it has no end marker at all
    or a page has no content. A complete file with a slightly wrong index, which
    some scanners write, or with junk after its end, is rebuilt too, but every
    page is intact, so it passes.
    """
    if not doc.is_repaired:
        return None
    message = ("This PDF can't be opened completely. It looks cut off or damaged, so pages or "
               "tickets may be missing. Upload the original file again.")
    if b"%%EOF" not in data:
        return message
    for page in doc:
        if not page.get_contents():
            return message
    return None


def render_pdf_page(data: bytes, page_no: int, dpi: int = 100, *, max_page_pixels: int = 14_000_000) -> Image.Image:
    """Render one 1-based page for preview, under the same size limit."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PdfError("Could not open PDF.") from exc
    try:
        if doc.needs_pass or not 1 <= page_no <= doc.page_count:
            raise PdfError("That page is not available.")
        page = doc[page_no - 1]
        if math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72) > max_page_pixels:
            raise PdfError("PDF page is too large to preview.")
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("Could not render PDF page.") from exc
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


@dataclass(frozen=True)
class SlotAssessment:
    ink: float  # fraction of clearly dark pixels (the is_blank measure)
    detection: str  # content | faint | empty


def assess_slot(image: Image.Image, *, ink_threshold: float = 0.002,
                faint_threshold: float = 0.0015) -> SlotAssessment:
    """Classify one slot without discarding anything.

    "content" is exactly the old not-is_blank test. "faint" means it has
    visible marks that are too light for that test: a person must decide.
    "empty" means effectively no marks. These thresholds are deliberately
    cautious and have NOT been calibrated on real scanner output.
    """
    histogram = image.convert("L").resize((200, 200)).histogram()
    total = 200 * 200
    ink = sum(histogram[:140]) / total
    if ink >= ink_threshold:
        return SlotAssessment(ink, "content")
    marks = sum(histogram[:235]) / total
    # Paper isn't always white. Also count anything clearly darker than this
    # slot's own background, so a faint ticket on grey or tinted paper can't
    # read as empty. A false alarm costs one click; a lost ticket costs far more.
    background = max(range(256), key=histogram.__getitem__)
    against_paper = sum(histogram[:max(0, background - 8)]) / total
    return SlotAssessment(ink, "faint" if max(marks, against_paper) >= faint_threshold else "empty")
