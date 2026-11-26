"""Digital PDF tickets are read from their own text, with no reader call. All data fictional.

A supplier's system that prints tickets straight to PDF leaves the words as text. When
every field is labelled and nothing is in doubt, Quarvera reads them from that text,
makes no AI call and counts nothing against the monthly limit. A scan (no text layer),
a scan carrying OCR text, hidden text, or any doubt goes to the reader as before.
"""
import io
from decimal import Decimal

import pymupdf
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import func, select

from app.config import settings
from app.models import ExtractionUsage, Organization, Ticket
from app.routers import api as api_module
from app.services import ticket_text
from .conftest import FixtureExtractor
from .sample_data import TICKETS, make_scan_pdf, make_ticket_image

TONS = ["Example Aggregates", "Delivery Ticket", "Ticket No.: 10482", "Date: 10/15/2026",
        "Supplier: Example Aggregates", "Material: No. 53 Crushed Stone", "Net Tons: 24.50",
        "Truck: T-114", "Job: Project A", "Job No: 2026-114"]
POUNDS = ["Sample Materials", "Scale Ticket", "Ticket #: SM-77120", "Date: 2026-10-16",
          "Supplier: Sample Materials", "Material: Sand", "Gross: 70,000 lb", "Tare: 21,000 lb",
          "Net: 49,000 lb", "Truck No: 22", "Job: Project A"]
YARDS = ["Example Ready Mix", "Ticket No: RM-501", "Date: 10/16/2026", "Supplier: Example Ready Mix",
         "Material: 4000 PSI Concrete", "Quantity: 9.5 CY", "Truck: M-7", "Job: Project B"]
COLUMNS = ["Example Aggregates", "Ticket No.:|10483", "Date:|10/15/2026", "Supplier:|Example Aggregates",
           "Material:|No. 8 Stone", "Gross Weight:|35.10 ton", "Tare Weight:|13.00 ton", "Net Tons:|22.10",
           "Job:|Project A"]


def digital_pdf(tickets: list[list[str] | None], *, hidden: str | None = None) -> bytes:
    """Up to four tickets on a letter page, 2x2, drawn as real text."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, invariant=1)
    width, height = letter
    for i, lines in enumerate(tickets):
        if lines is None:
            continue
        left, top = (i % 2) * width / 2 + 24, height - (i // 2) * height / 2 - 30
        pdf.rect(left - 8, top - 330, width / 2 - 34, 345)
        pdf.setFont("Helvetica", 11)
        for n, line in enumerate(lines):
            label, _, value = line.partition("|")
            pdf.drawString(left, top - 10 - n * 22, label)
            if value:  # a label and its value in separate columns
                pdf.drawString(left + 120, top - 10 - n * 22, value)
        if hidden and i == 0:
            pdf.setFillColorRGB(1, 1, 1)  # white on white: on the page, but nobody can see it
            pdf.drawString(left, top - 300, hidden)
            pdf.setFillColorRGB(0, 0, 0)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class CountingReader(FixtureExtractor):
    """Stands in for the paid reader. Counts every call."""


def _upload(client, monkeypatch, data: bytes, reader=None):
    reader = reader or CountingReader()
    monkeypatch.setattr(api_module, "get_extractor", lambda: reader)
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "extract"},
                        files=[("files", ("digital.pdf", data, "application/pdf"))])
    assert batch.status_code == 200, batch.text
    tickets = client.get(f"/api/batches/{batch.json()['id']}/tickets").json()
    return reader, tickets


def _usage_rows() -> int:
    with api_module.SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(ExtractionUsage))


def _raw(ticket_id: int) -> dict:
    with api_module.SessionLocal() as db:
        return db.get(Ticket, ticket_id).raw_extraction


def test_four_digital_tickets_are_read_from_text_with_no_reader_call(client, monkeypatch):
    reader, tickets = _upload(client, monkeypatch, digital_pdf([TONS, POUNDS, YARDS, COLUMNS]))
    assert reader.calls == 0
    assert _usage_rows() == 0  # nothing counted against the monthly limit
    assert len(tickets) == 4
    tons, pounds, yards, columns = tickets

    assert (tons["ticket_number"], tons["delivery_date"], tons["supplier"], tons["material_type"]) == (
        "10482", "2026-10-15", "Example Aggregates", "No. 53 Crushed Stone")
    assert Decimal(tons["tonnage"]) == Decimal("24.50") and tons["unit"] == "ton"
    assert tons["truck_number"] == "T-114" and tons["job_location"] == "Project A"
    assert tons["flags"] == [] and tons["status"] == "needs_review" and tons["tonnage_confirmed"] is False
    raw = _raw(tons["id"])
    assert raw["source"] == "pdf_text" and raw["job_number"] == "2026-114"

    # Pounds: saved as tons by the same rule as a person's entry, keeping the pounds.
    assert pounds["ticket_number"] == "SM-77120"
    assert Decimal(pounds["tonnage"]) == Decimal("24.500") and pounds["unit"] == "ton"
    assert (Decimal(pounds["source_quantity"]), pounds["source_unit"]) == (Decimal("49000"), "lb")
    assert (Decimal(pounds["gross_weight"]), Decimal(pounds["tare_weight"]), pounds["weight_unit"]) == (
        Decimal("70000"), Decimal("21000"), "lb")
    assert pounds["truck_number"] == "22" and pounds["flags"] == []
    assert _raw(pounds["id"])["unit"] == "lb"  # as printed: never converted in what was read

    assert Decimal(yards["tonnage"]) == Decimal("9.5") and yards["unit"] == "cy" and yards["source_unit"] is None

    # A label and its value in separate columns; weights in tons agree with the net tons.
    assert columns["ticket_number"] == "10483" and columns["material_type"] == "No. 8 Stone"
    assert Decimal(columns["tonnage"]) == Decimal("22.10") and columns["weight_unit"] == "ton"
    assert columns["flags"] == []


def test_a_scanned_pdf_has_no_text_layer_and_is_still_read_by_the_reader(client, monkeypatch):
    reader, tickets = _upload(client, monkeypatch, make_scan_pdf())
    assert reader.calls == 4
    assert _usage_rows() == 4
    assert [t["ticket_number"] for t in tickets] == [t["ticket_number"] for t in TICKETS]
    assert {_raw(t["id"])["source"] for t in tickets} == {"reader"}


def test_only_the_ticket_the_text_leaves_in_doubt_goes_to_the_reader(client, monkeypatch):
    no_job = [line for line in TONS if not line.startswith("Job:")]
    two_numbers = [*YARDS, "Ticket No: RM-502"]
    unstated_unit = [line if not line.startswith("Net Tons") else "Quantity: 24.50" for line in TONS]
    reader = CountingReader([TICKETS[0]])
    reader, tickets = _upload(client, monkeypatch, digital_pdf([no_job, POUNDS, two_numbers, unstated_unit]), reader)
    assert reader.calls == 3
    assert _usage_rows() == 3
    assert [_raw(t["id"])["source"] for t in tickets] == ["reader", "pdf_text", "reader", "reader"]


def test_text_reads_continue_past_the_monthly_limit_and_scans_stop(client, monkeypatch):
    monkeypatch.setattr(settings, "monthly_ticket_reads_per_organization", 1)
    with api_module.SessionLocal() as db:
        db.add(ExtractionUsage(organization_id=1, model="fixture", attempts=1, outcome="ok"))
        db.commit()
    reader, tickets = _upload(client, monkeypatch, digital_pdf([TONS, POUNDS, None, YARDS]))
    assert reader.calls == 0
    assert [t["status"] for t in tickets] == ["needs_review"] * 3
    assert _usage_rows() == 1

    reader, tickets = _upload(client, monkeypatch, make_scan_pdf())
    assert reader.calls == 0
    assert {t["status"] for t in tickets} == {"failed"}  # over the limit: no call, and it says so


def test_hidden_text_is_never_read_as_the_ticket(client, monkeypatch):
    reader, tickets = _upload(client, monkeypatch, digital_pdf([TONS, None, None, None], hidden="Net Tons: 99.00"))
    assert reader.calls == 1
    assert _raw(tickets[0]["id"])["source"] == "reader"


def test_a_scan_with_an_ocr_text_layer_goes_to_the_reader(client, monkeypatch):
    """A scanner's OCR puts invisible text over the picture. The picture is the record."""
    image = io.BytesIO()
    make_ticket_image(TICKETS[0]).save(image, format="PNG")
    with pymupdf.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_image(pymupdf.Rect(20, 20, 296, 376), stream=image.getvalue())
        for n, line in enumerate(TONS):
            page.insert_text((40, 60 + n * 22), line, fontsize=11, render_mode=3)
        data = doc.tobytes()
    reader, tickets = _upload(client, monkeypatch, data, CountingReader([TICKETS[0]]))
    assert reader.calls == 1
    assert _raw(tickets[0]["id"])["source"] == "reader"


def test_the_same_ticket_number_twice_in_a_batch_is_flagged_when_read(client, monkeypatch):
    reader, tickets = _upload(client, monkeypatch, digital_pdf([TONS, POUNDS, TONS, None]))
    assert reader.calls == 0
    first, _, second = tickets
    assert not any("Same ticket number" in f for f in first["flags"])
    assert any(f.startswith("Same ticket number as another ticket in this batch (page 1, ticket 1)")
               for f in second["flags"])


def test_parser_refuses_disagreeing_net_weight_and_odd_units():
    base = ["Ticket No: 1", "Date: 10/15/2026", "Supplier: Example Aggregates", "Material: Sand", "Job: Lot 4"]
    assert ticket_text.parse_ticket(_split(base + ["Net Tons: 24.50"])) is not None
    assert ticket_text.parse_ticket(_split(base + ["Net Tons: 24.50", "Net: 52,000 lb"])) is None
    assert ticket_text.parse_ticket(_split(base + ["Quantity: 24.50 kg"])) is None
    assert ticket_text.parse_ticket(_split(base + ["Net Tons: 1O.5"])) is None  # letter O: never a number
    assert ticket_text.parse_ticket(_split(base[1:] + ["Net Tons: 24.50"])) is None  # no ticket number
    assert ticket_text.parse_ticket(_split(base + ["Net Tons: 24.50", "Ticket No: 2"])) is None  # two numbers


def _split(lines):
    """Words laid out the way a PDF gives them: one per word, on a line each."""
    out = []
    for n, line in enumerate(lines):
        x = 10.0
        for part in line.split():
            out.append(ticket_text.Word(x, 10 + 20 * n, x + 5 * len(part), 20 + 20 * n, part))
            x += 5 * len(part) + 3
    return out


def test_letterhead_supplier_needs_exactly_one_catalog_name():
    lines = ["Example Aggregates", "Ticket No: 9", "Date: 10/15/2026", "Material: Sand", "Net Tons: 3.2", "Job: Lot"]
    assert ticket_text.parse_ticket(_split(lines)) is None  # no catalog: no supplier
    found = ticket_text.parse_ticket(_split(lines), ("Example Aggregates", "Sample Materials"))
    assert found is not None and found[0].supplier == "Example Aggregates"


def test_company_b_text_read_uses_only_its_own_catalog(client, monkeypatch):
    """The letterhead match reads the uploading company's catalog, never another's."""
    assert client.post("/api/suppliers", json={"name": "Example Aggregates"}).status_code in (200, 201)
    letterhead = [line for line in TONS if not line.startswith("Supplier:")]
    reader, tickets = _upload(client, monkeypatch, digital_pdf([letterhead, None, None, None]))
    assert reader.calls == 0 and tickets[0]["supplier"] == "Example Aggregates"  # company 1's own catalog
    from app.auth import current_user
    from app.main import app
    from app.models import User
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        db.flush()
        db.add(User(id=2, organization_id=2, username="other", password_hash="fixture", role="owner"))
        db.commit()
    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=2, username="other", password_hash="fixture", active=True,
        role="owner", must_change_password=False)
    reader, _ = _upload(client, monkeypatch, digital_pdf([letterhead, None, None, None]), CountingReader([TICKETS[0]]))
    assert reader.calls == 1  # company 2 has no such supplier, so the text can't name it
