"""Synthetic-only tests of local crop previews; no provider, DB or network."""
import csv

import pytest
from PIL import Image

from scripts.preview_scans import PreviewError, preview_scan_folder

from .sample_data import TICKETS, make_scan_page, make_scan_pdf


def _read_index(root):
    with (root / "reconciliation.csv").open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_four_up_preview_exports_full_resolution_crops_and_human_checklist(tmp_path, monkeypatch):
    folder = tmp_path / "private"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(make_scan_pdf())
    # An OCR key or an AI service must NOT be required or invoked.
    from app.services import extraction
    monkeypatch.setattr(
        extraction, "get_extractor",
        lambda: (_ for _ in ()).throw(AssertionError("No AI may run in offline preview")),
    )

    stats = preview_scan_folder(folder, "grid_2x2")
    out = folder / "scan_preview"
    assert stats["source_files"] == 1
    assert stats["pages"] == 1
    assert stats["crop_slots"] == 4
    assert stats["looks_blank"] == 0
    rows = _read_index(out)
    assert len(rows) == 4
    assert [row["position"] for row in rows] == ["0", "1", "2", "3"]
    assert all(row["human_ticket_present"] == "" for row in rows)
    assert all(row["looks_blank"] == "no" for row in rows)
    assert (out / rows[0]["page_preview_file"]).exists()
    with Image.open(out / rows[0]["crop_file"]) as crop:
        assert crop.width > 100 and crop.height > 100
    # Always create a new directory, never mutate existing reviewer notes.
    with pytest.raises(PreviewError, match="already exists"):
        preview_scan_folder(folder, "grid_2x2")
    assert _read_index(out) == rows


def test_three_up_scan_keeps_suspected_blank_quadrant_for_human_review(tmp_path):
    folder = tmp_path / "scans"
    folder.mkdir()
    scan = make_scan_page(TICKETS[:3])
    scan.save(folder / "three.png")
    stats = preview_scan_folder(folder, "grid_2x2")
    rows = _read_index(folder / "scan_preview")
    assert stats["crop_slots"] == 4
    assert stats["looks_blank"] == 1
    assert [r["position"] for r in rows if r["looks_blank"] == "yes"] == ["3"]
    # The suspicious fourth crop remains present; a faint real ticket may
    # otherwise be silently excluded before any AI model can read it.
    assert (folder / "scan_preview" / rows[3]["crop_file"]).is_file()


def test_single_blank_pdf_produces_a_visible_blank_slot(tmp_path):
    import pymupdf
    folder = tmp_path / "scans"
    folder.mkdir()
    with pymupdf.open() as doc:
        doc.new_page(width=400, height=500)
        (folder / "empty.pdf").write_bytes(doc.tobytes())
    stats = preview_scan_folder(folder, "single")
    assert stats["crop_slots"] == 1
    assert stats["looks_blank"] == 1
    assert _read_index(folder / "scan_preview")[0]["looks_blank"] == "yes"


def test_multipage_pdf_tracks_original_page_and_crop_position(tmp_path):
    import pymupdf
    folder = tmp_path / "scans"
    folder.mkdir()
    with pymupdf.open(stream=make_scan_pdf(), filetype="pdf") as src:
        with pymupdf.open() as two_pages:
            two_pages.insert_pdf(src)
            two_pages.insert_pdf(src)
            (folder / "multi.pdf").write_bytes(two_pages.tobytes())
    stats = preview_scan_folder(folder, "grid_2x2")
    rows = _read_index(folder / "scan_preview")
    assert stats["pages"] == 2
    assert stats["crop_slots"] == 8
    assert [row["source_page"] for row in rows] == ["1"] * 4 + ["2"] * 4


def test_invalid_output_path_and_empty_scans_folder_are_rejected(tmp_path):
    scans = tmp_path / "scans"
    scans.mkdir()
    with pytest.raises(PreviewError, match="No PDF"):
        preview_scan_folder(scans, "single")
    (scans / "scan.pdf").write_bytes(make_scan_pdf())
    with pytest.raises(PreviewError, match="subfolder"):
        preview_scan_folder(scans, "single", output=tmp_path / "outside")
    assert not (tmp_path / "outside").exists()
    with pytest.raises(PreviewError, match="layout"):
        preview_scan_folder(scans, "broken")
