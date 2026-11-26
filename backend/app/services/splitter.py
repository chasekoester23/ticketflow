"""Render PDF pages and split a page image into individual ticket crops.

Pure functions, no database: easy to unit test.
Boxes are (left, top, right, bottom) in pixels of the rendered page image.
"""
import io
import math
from dataclasses import dataclass

import pymupdf as fitz  # PyMuPDF
from PIL import Image, ImageOps

Box = tuple[int, int, int, int]


class PdfError(Exception):
    """Raised for unreadable, corrupted, empty or password-protected PDFs."""


LAYOUTS = ("single", "grid_2x2", "auto")


@dataclass
class Crop:
    position: int
    box: Box
    image: Image.Image
    # Auto layout only: why a person should check this region (segmentation.py), or None.
    review: str | None = None


def fit_scale(width: float, height: float, max_pixels: int) -> float:
    """The factor (at most 1) that brings width x height within max_pixels."""
    area = width * height
    return 1.0 if area <= max_pixels else math.sqrt(max_pixels / area) * 0.999


def page_zoom(page, dpi: int, max_pixels: int) -> float:
    width, height = page.rect.width * dpi / 72, page.rect.height * dpi / 72
    if not (width >= 1 and height >= 1):
        raise PdfError("PDF page has no size. Check the scan and try again.")
    return dpi / 72 * fit_scale(width, height, max_pixels)


def render_page(page, dpi: int, max_pixels: int) -> Image.Image:
    """Render one page at ``dpi``, or at a lower resolution if that would exceed
    ``max_pixels``. The size is decided before anything is allocated.

    A phone photo saved as a PDF is usually a page of one point per pixel: a 12 MP
    photo becomes a 42 x 56 inch page, which at 200 dpi would be 94 MP. Rendering
    it to fit the limit keeps all the detail the photo has, instead of refusing it.
    """
    zoom = page_zoom(page, dpi, max_pixels)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    if pix.width * pix.height > max_pixels:
        raise PdfError("PDF renders above the safe image limit. Rescan at a lower resolution.")
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


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
            zoom = page_zoom(page, dpi, max_page_pixels)
            if total_pixels + math.ceil(page.rect.width * zoom) * math.ceil(page.rect.height * zoom) > max_total_pixels:
                raise PdfError("PDF is too large to process safely. Split it into smaller files.")
            image = render_page(page, dpi, max_page_pixels)
            if total_pixels + image.width * image.height > max_total_pixels:
                raise PdfError("PDF is too large to process safely. Split it into smaller files.")
            pages.append(image)
            total_pixels += image.width * image.height
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
        return render_page(doc[page_no - 1], dpi, max_page_pixels)
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("Could not render PDF page.") from exc
    finally:
        doc.close()


def load_image(data: bytes, *, max_pixels: int = 14_000_000, max_source_pixels: int = 50_000_000) -> Image.Image:
    """Decode an uploaded photo or scan, upright and within ``max_pixels``.

    Phones save photos sideways with an EXIF note saying which way is up; that note
    is applied, so a portrait ticket isn't read, split or shown on its side. A photo
    larger than ``max_pixels`` (a 24 or 48 MP phone camera) is reduced to fit rather
    than refused. Anything above ``max_source_pixels`` is refused from its header,
    before decoding, as a possible decompression bomb.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width <= 0 or height <= 0 or width * height > max_source_pixels:
                raise PdfError("Image dimensions are too large. Resize the scan before uploading.")
            scale = fit_scale(width, height, max_pixels)
            if scale < 1 and img.format == "JPEG":
                # Decode a JPEG at a reduced scale directly, never at full size.
                img.draft("RGB", (math.ceil(width * scale), math.ceil(height * scale)))
            img.load()
            image = ImageOps.exif_transpose(img).convert("RGB")
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("Could not read image. Check the file and try again.") from exc
    scale = fit_scale(image.width, image.height, max_pixels)
    if scale < 1:
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                             Image.Resampling.LANCZOS, reducing_gap=3.0)
    return image


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
    """The crops of one page. "auto" finds the tickets wherever they are (segmentation.py):
    every mark on the page ends up in some crop, a tilted ticket's crop is straightened,
    and a page it can't make sense of is one whole-page crop marked for review."""
    w, h = image.size
    if layout == "auto":
        from . import segmentation

        regions = segmentation.segment_page(image).regions
        return [Crop(i, r.box, segmentation.crop_region(image, r), r.review) for i, r in enumerate(regions)]
    if layout == "single":
        boxes: list[Box] = [(0, 0, w, h)]
    elif layout == "grid_2x2":
        boxes = grid_boxes(w, h, rows=2, cols=2, margin=margin)
    else:
        raise ValueError(f"Unsupported layout: {layout}")
    return [Crop(i, b, image.crop(b)) for i, b in enumerate(boxes)]


def clamp_box(box, width: int, height: int) -> Box | None:
    """A drawn box as whole pixels inside the page, or None if nothing of it is left."""
    left, top, right, bottom = (int(round(float(v))) for v in box)
    left, right = sorted((max(0, min(width, left)), max(0, min(width, right))))
    top, bottom = sorted((max(0, min(height, top)), max(0, min(height, bottom))))
    return (left, top, right, bottom) if right > left and bottom > top else None


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
