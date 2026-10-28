from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services import splitter
from app.services.extraction import ExtractionError, parse_model_json
from app.services.export import ticket_cost
from app.services.validation import compute_flags, parse_date, parse_tonnage

from .sample_data import TICKETS, make_scan_page, make_scan_pdf


def test_grid_boxes_cover_four_distinct_quadrants():
    boxes = splitter.grid_boxes(1000, 2000, rows=2, cols=2, margin=0.01)
    assert len(boxes) == 4
    assert boxes[0][0] < 500 <= boxes[1][2] + 10 and boxes[0][1] < 1000
    assert boxes[3][2] <= 1000 and boxes[3][3] <= 2000
    for (l, t, r, b) in boxes:
        assert r > l and b > t


def test_split_single_returns_whole_page():
    crops = splitter.split_page(Image.new("RGB", (400, 500), "white"), "single")
    assert len(crops) == 1 and crops[0].box == (0, 0, 400, 500)


def test_unknown_layout_rejected():
    with pytest.raises(ValueError):
        splitter.split_page(Image.new("RGB", (10, 10)), "hexagon")


def test_render_pdf_and_split_2x2_gives_four_nonblank_crops():
    pages = splitter.render_pdf_pages(make_scan_pdf(), dpi=100)
    assert len(pages) == 1
    crops = splitter.split_page(pages[0], "grid_2x2")
    assert [c.position for c in crops] == [0, 1, 2, 3]
    assert not any(splitter.is_blank(c.image) for c in crops)


def test_blank_quadrant_detected_when_only_three_tickets():
    page = make_scan_page(TICKETS[:3], page_size=(850, 1100))
    crops = splitter.split_page(page, "grid_2x2")
    assert [splitter.is_blank(c.image) for c in crops] == [False, False, False, True]


def test_invalid_pdf_raises():
    with pytest.raises(splitter.PdfError):
        splitter.render_pdf_pages(b"%PDF-1.4 not really")


def test_parse_date_variants():
    assert parse_date("2026-10-15") == date(2026, 10, 15)
    assert parse_date("10/15/2026") == date(2026, 10, 15)
    assert parse_date("10/15/26") == date(2026, 10, 15)
    assert parse_date("Oct 15, 2026") == date(2026, 10, 15)
    assert parse_date("garbage") is None and parse_date(None) is None


def test_parse_tonnage():
    assert parse_tonnage("24.5 tons") == Decimal("24.500")
    assert parse_tonnage("1,024.25") == Decimal("1024.250")
    assert parse_tonnage("abc") is None and parse_tonnage(None) is None


def _flags(**kw):
    base = dict(ticket_number="1", delivery_date=date(2026, 1, 1), material_type="Sand", tonnage=Decimal("20"),
                supplier="S", job_location="J", today=date(2026, 10, 28))
    base.update(kw)
    return compute_flags(**base)


def test_clean_record_has_no_flags():
    assert _flags() == []


def test_flags_for_bad_values():
    assert "Tonnage must be positive" in _flags(tonnage=Decimal("-3"))
    assert any("unusually large" in f for f in _flags(tonnage=Decimal("500")))
    assert "Date is in the future" in _flags(delivery_date=date(2027, 1, 1))
    assert "Date unreadable" in _flags(delivery_date=None, raw_date="1O/l5")
    assert "Tonnage missing" in _flags(tonnage=None)
    assert "Ticket number missing or unreadable" in _flags(ticket_number=None)


def test_parse_model_json_handles_fence_and_rejects_garbage():
    assert parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ExtractionError):
        parse_model_json("The ticket says 24 tons")
    with pytest.raises(ExtractionError):
        parse_model_json("[1, 2]")


def test_cost_uses_exact_decimal_and_requires_confirmation():
    t = SimpleNamespace(tonnage=Decimal("24.500"), tonnage_confirmed=True)
    assert ticket_cost(t, Decimal("30")) == Decimal("735.00")
    assert ticket_cost(SimpleNamespace(tonnage=Decimal("0.1"), tonnage_confirmed=True), Decimal("0.3")) == Decimal("0.03")
    assert ticket_cost(SimpleNamespace(tonnage=Decimal("24.5"), tonnage_confirmed=False), Decimal("30")) is None
    assert ticket_cost(t, None) is None
