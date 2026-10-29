"""Private, OFFLINE scan splitting preflight for an authorized TicketFlow pilot.

Run from backend/:
  python scripts/preview_scans.py --scans evaluation_data --layout grid_2x2

Uses the same rendering DPI, 2x2 crop coordinates, and blank heuristic as the
real worker. Writes a contact sheet per scanned page, full-resolution crops,
and a human reconciliation CSV. Never calls an AI provider, network or DB.

These files contain the original ticket information. Keep ALL inputs and
outputs inside the git-ignored backend/evaluation_data/ folder.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402
from app.services import splitter  # noqa: E402

IMAGE_EXT = {".png", ".jpg", ".jpeg"}
EXT = IMAGE_EXT | {".pdf"}
LAYOUTS = {"single", "grid_2x2"}
INDEX_COLUMNS = [
    "source", "source_page", "position", "crop_file", "page_preview_file",
    "looks_blank", "human_ticket_present", "review_note",
]


class PreviewError(ValueError):
    pass


def _sources(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise PreviewError("Scans directory does not exist")
    paths = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXT),
        key=lambda p: p.name.casefold(),
    )
    if not paths:
        raise PreviewError("No PDF/JPG/PNG scans found in the selected folder")
    for p in paths:
        if p.is_symlink() or not p.resolve().is_relative_to(folder.resolve()):
            raise PreviewError("Do not use links to sources outside the scan folder")
    return paths


def _pages(path: Path):
    if path.suffix.lower() != ".pdf":
        try:
            yield splitter.load_image(path.read_bytes())
        except (OSError, splitter.PdfError) as exc:
            raise PreviewError(f"Unreadable image: {path.name}") from exc
        return

    try:
        with pymupdf.open(str(path)) as doc:
            if doc.needs_pass or doc.page_count == 0:
                raise PreviewError(f"Empty or password-protected PDF: {path.name}")
            for page in doc:
                pix = page.get_pixmap(dpi=settings.render_dpi, alpha=False)
                yield Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    except PreviewError:
        raise
    except Exception as exc:
        raise PreviewError(f"Unreadable PDF: {path.name}") from exc


def _contact_sheet(crops, flags: list[bool], label: str, layout: str) -> Image.Image:
    """Local-only contact sheet; blank-looking crops remain visible for humans."""
    columns = 1 if layout == "single" else 2
    rows = 1 if layout == "single" else 2
    w, h, pad, header = 565, 650, 20, 92
    sheet = Image.new("RGB", (columns * w + pad * 2, rows * h + header), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=18)
    draw.text((pad, 17), label[:90], fill="black", font=font)
    draw.text(
        (pad, 51), "Check EVERY crop against the original scan. BLANK? is only an estimate.",
        fill="#444444", font=font,
    )
    for idx, (crop, blank) in enumerate(zip(crops, flags)):
        x, y = pad + (idx % columns) * w, header + (idx // columns) * h
        draw.rectangle((x, y, x + w - 15, y + h - 15), outline="#999999", width=2)
        status = "BLANK? REVIEW" if blank else "NONBLANK? REVIEW"
        draw.text(
            (x + 14, y + 9),
            f"Position {crop.position}  |  {status}",
            fill="#a12d20" if blank else "#20572d", font=font,
        )
        thumb = crop.image.copy().convert("RGB")
        thumb.thumbnail((w - 45, h - 85))
        sheet.paste(thumb, (x + (w - 15 - thumb.width) // 2, y + 54))
    return sheet


def preview_scan_folder(scans: Path, layout: str, output: Path | None = None) -> dict:
    """Create new, never-overwritten local previews; returns counts not extracted data."""
    if layout not in LAYOUTS:
        raise PreviewError("layout must be single or grid_2x2")
    scans = scans.resolve()
    sources = _sources(scans)
    output = (output or scans / "scan_preview").resolve()

    # Reject accidental writes to a public repo, desktop or external folder.
    if not output.is_relative_to(scans) or output == scans:
        raise PreviewError("Preview output must be a new subfolder inside the scan folder")
    if output.exists():
        raise PreviewError("Preview folder already exists; refusing to overwrite prior review")
    if output.parent != scans:
        raise PreviewError("Put preview output directly inside the scan folder")

    output.mkdir(mode=0o700)
    (output / "crops").mkdir(mode=0o700)
    (output / "pages").mkdir(mode=0o700)
    rows: list[dict] = []
    page_total = 0
    looks_blank = 0
    for source in sources:
        for page_no, image in enumerate(_pages(source), start=1):
            page_total += 1
            crops = splitter.split_page(image, layout)
            flags = [splitter.is_blank(c.image) for c in crops]
            preview_name = f"pages/page-{page_total:05d}.jpg"
            sheet = _contact_sheet(crops, flags, f"Source: {source.name} | page {page_no}", layout)
            sheet.save(output / preview_name, format="JPEG", quality=94)
            for crop, blank in zip(crops, flags):
                crop_name = f"crops/crop-{len(rows)+1:05d}.png"
                crop.image.save(output / crop_name, format="PNG")
                rows.append({
                    "source": source.name,
                    "source_page": page_no,
                    "position": crop.position,
                    "crop_file": crop_name,
                    "page_preview_file": preview_name,
                    "looks_blank": "yes" if blank else "no",
                    "human_ticket_present": "",
                    "review_note": "",
                })
                looks_blank += int(blank)

    with (output / "reconciliation.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    return {
        "source_files": len(sources),
        "pages": page_total,
        "crop_slots": len(rows),
        "looks_blank": looks_blank,
        "looks_nonblank": len(rows) - looks_blank,
        "output": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview ticket crop quality locally; no OCR, no keys, no network"
    )
    parser.add_argument("--scans", required=True, type=Path)
    parser.add_argument("--layout", required=True, choices=sorted(LAYOUTS))
    parser.add_argument(
        "--output", type=Path,
        help="New direct subfolder of --scans (default: scan_preview)",
    )
    args = parser.parse_args()
    try:
        stats = preview_scan_folder(args.scans, args.layout, args.output)
    except (PreviewError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"OFFLINE scan preview: {stats['source_files']} source(s), {stats['pages']} page(s), "
        f"{stats['crop_slots']} crop slot(s); {stats['looks_blank']} look blank."
    )
    print(f"Open page previews in {stats['output']}/pages/")
    print("Examine EVERY slot (including blank-looking) against original scans.")
    print("Fill human_ticket_present (yes/no/uncertain) in reconciliation.csv.")
    print("This does not establish completeness or handwriting recognition accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
