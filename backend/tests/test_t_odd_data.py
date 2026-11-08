"""Unusual but legitimate data, and data that is simply wrong, each with a known answer.

Dates at the edges of the calendar, the same ticket number from two suppliers, names
one letter apart, rates that clash, faint and turned scans, PDFs that aren't what they
claim. All fictional. Passing says nothing about what real paperwork will contain.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pymupdf
import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models import Batch, Project, ScanSlot, Ticket, UploadedFile

from .sample_data import TICKETS, make_page, make_pdf
from .test_query_budgets import _session

A, B, C, D = TICKETS


def _catalog(client, rates: dict[tuple[str, str], str]) -> None:
    suppliers, materials = {}, {}
    for supplier, material in rates:
        if supplier not in suppliers:
            suppliers[supplier] = client.post("/api/suppliers", json={"name": supplier}).json()["id"]
        if material not in materials:
            materials[material] = client.post("/api/materials", json={"name": material}).json()["id"]
    for (supplier, material), price in rates.items():
        assert client.put("/api/rates", json={"supplier_id": suppliers[supplier], "material_id": materials[material],
                                              "unit_price": price}).status_code == 200


def _deliveries(rows: list[dict], *, project_code: str = "P1") -> list[int]:
    """Unapproved delivery records, each its own scan. Returns their ids."""
    with _session() as db:
        project = db.scalar(select(Project).where(Project.code == project_code))
        if project is None:
            project = Project(organization_id=1, code=project_code, name=f"Fictional {project_code}")
            db.add(project)
            db.flush()
        batch = Batch(organization_id=1, project_id=project.id, layout="single", status="ready_for_review", intake_mode="manual")
        db.add(batch)
        db.flush()
        file = UploadedFile(batch_id=batch.id, original_name="f.pdf", storage_path="x", sha256="0" * 64, status="ready_for_review")
        db.add(file)
        db.flush()
        ids = []
        for n, row in enumerate(rows):
            ticket = Ticket(batch_id=batch.id, file_id=file.id, source_page=1, position=n, crop_box=[0, 0, 1, 1], crop_path="x",
                            crop_sha256=row.pop("sha", f"{batch.id:08d}{n:056d}"), job_location="Lot", flags=[], status="needs_review",
                            **{"supplier": "Example Aggregates", "material_type": "Sand", **row})
            db.add(ticket)
            db.flush()
            ids.append(ticket.id)
        db.commit()
    return ids


def _project_id(code: str) -> int:
    with _session() as db:
        return db.scalar(select(Project.id).where(Project.code == code))


def _line(client, **fields):
    body = {"project_id": _project_id("P1"), "invoice_number": "I-1", "supplier_name": "Example Aggregates", "material_name": "Sand",
            "date_from": "2026-09-01", "date_to": "2026-09-30", "billed_tons": "20", "billed_unit_price": "10", "billed_subtotal": "200",
            **fields}
    return client.post("/api/invoice-comparisons", json=body)


# ------------------------------------------------------------ the calendar's edges

def test_a_leap_day_belongs_to_february_and_only_february(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    leap, first = _deliveries([dict(ticket_number="L1", delivery_date=date(2024, 2, 29), tonnage=Decimal("20.000")),
                               dict(ticket_number="L2", delivery_date=date(2024, 3, 1), tonnage=Decimal("15.000"))])
    for ticket in (leap, first):
        assert client.post(f"/api/tickets/{ticket}/verify").status_code == 200
    february = _line(client, date_from="2024-02-01", date_to="2024-02-29", billed_tons="20", billed_subtotal="200").json()
    march = _line(client, invoice_number="I-2", date_from="2024-03-01", date_to="2024-03-31", billed_tons="15", billed_subtotal="150").json()
    assert (february["status"], Decimal(february["verified_tons"])) == ("matches", Decimal("20.000"))
    assert (march["status"], Decimal(march["verified_tons"])) == ("matches", Decimal("15.000"))
    months = {m["month"]: Decimal(m["verified_tons"]) for m in client.get(f"/api/reports/project/{_project_id('P1')}").json()["by_month"]}
    assert months == {"2024-02": Decimal("20.000"), "2024-03": Decimal("15.000")}


@pytest.mark.parametrize("value", ["2026-02-29", "2025-02-29", "2100-02-29", "2026-13-01", "2026-04-31", "26-10-01", ""])
def test_a_date_that_doesnt_exist_is_refused_and_changes_nothing(client, value):
    [ticket] = _deliveries([dict(ticket_number="X1", delivery_date=date(2026, 9, 1), tonnage=Decimal("20.000"))])
    response = client.patch(f"/api/tickets/{ticket}", json={"delivery_date": value or None})
    if value == "":
        assert response.status_code == 200 and response.json()["delivery_date"] is None
        return
    assert response.status_code == 422
    bulk = client.post("/api/tickets/bulk-edit", json={"ticket_ids": [ticket], "field": "delivery_date", "value": value})
    assert bulk.status_code == 422
    assert client.get(f"/api/batches/{client.get('/api/batches').json()[0]['id']}/tickets").json()[0]["delivery_date"] == "2026-09-01"


def test_a_year_end_invoice_takes_december_and_leaves_january(client):
    _catalog(client, {("Example Aggregates", "Sand"): "12.5"})
    december, january = _deliveries([dict(ticket_number="Y1", delivery_date=date(2025, 12, 31), tonnage=Decimal("18.000")),
                                      dict(ticket_number="Y2", delivery_date=date(2026, 1, 1), tonnage=Decimal("19.000"))])
    for ticket in (december, january):
        assert client.post(f"/api/tickets/{ticket}/verify").status_code == 200
    line = _line(client, date_from="2025-12-01", date_to="2025-12-31", billed_tons="37", billed_unit_price="12.5",
                 billed_subtotal="462.50", ticket_list="Y1\t18\nY2\t19").json()
    assert line["status"] == "discrepancy" and Decimal(line["verified_tons"]) == Decimal("18.000")
    rows = {row["invoice_ticket_number"]: row["result"] for row in line["ticket_check"]}
    assert rows == {"Y1": "matched", "Y2": "outside_scope"}
    assert Decimal(line["subtotal_difference"]) == Decimal("237.50")
    january_line = _line(client, invoice_number="I-2", date_from="2026-01-01", date_to="2026-01-31", billed_tons="19",
                         billed_unit_price="12.5", billed_subtotal="237.50").json()
    assert january_line["status"] == "matches"


def test_a_far_future_date_can_be_typed_but_not_approved(client):
    [ticket] = _deliveries([dict(ticket_number="F1", delivery_date=date(2026, 9, 1), tonnage=Decimal("20.000"))])
    for value in ("2099-12-31", "9999-12-31"):
        assert client.patch(f"/api/tickets/{ticket}", json={"delivery_date": value}).status_code == 200
        refused = client.post(f"/api/tickets/{ticket}/verify")
        assert refused.status_code == 422 and "future" in refused.json()["detail"]
        assert client.get("/api/records/export.csv").status_code == 200
        assert client.get("/api/records/search", params={"sort": "date", "direction": "desc"}).json()["rows"][0]["delivery_date"] == value
    assert client.get("/api/dashboard").json()["verified"] == 0


@pytest.mark.parametrize("value", ["1900-01-01", pytest.param("1899-12-31", marks=pytest.mark.xfail(strict=True, reason=(
    "BUG: a delivery dated 1899-12-31 is written to Excel as day zero, which reads back as the time 00:00, not a date"))),
    "0001-01-01"])
def test_an_ancient_date_is_kept_exactly_in_every_export(client, value):
    """Whatever someone decides about such a date, it must come out as it went in."""
    [ticket] = _deliveries([dict(ticket_number="O1", delivery_date=date(2026, 9, 1), tonnage=Decimal("20.000"))])
    assert client.patch(f"/api/tickets/{ticket}", json={"delivery_date": value}).status_code == 200
    csv = client.get("/api/records/export.csv").text.splitlines()
    assert value in csv[1]
    sheet = load_workbook(io.BytesIO(client.get("/api/records/export.xlsx").content))["Delivery Records"]
    cell = sheet.cell(2, 2).value
    assert getattr(cell, "date", lambda: cell)() == date.fromisoformat(value), cell
    months = [m["month"] for m in client.get(f"/api/reports/project/{_project_id('P1')}").json()["by_month"]]
    assert months == [value[:7]]


@pytest.mark.xfail(strict=True, reason="BUG: compute_flags has a check for future dates but none for implausibly old ones, so a ticket dated 1900-01-01 (a spreadsheet's empty date) or 0001-01-01 is approved with no warning")
@pytest.mark.parametrize("value", ["1900-01-01", "0001-01-01"])
def test_an_implausibly_old_delivery_date_needs_a_second_look_before_approval(client, value):
    [ticket] = _deliveries([dict(ticket_number="O2", delivery_date=date.fromisoformat(value), tonnage=Decimal("20.000"))])
    response = client.post(f"/api/tickets/{ticket}/verify")
    assert response.status_code == 422 or any("date" in flag.lower() for flag in response.json()["flags"])


# ------------------------------------------------------------ one ticket number, many places

def test_one_ticket_number_from_two_suppliers_is_two_deliveries(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10", ("Sample Stone Co", "Sand"): "11"})
    ea, ss = _deliveries([dict(ticket_number="10482", delivery_date=date(2026, 9, 3), tonnage=Decimal("20.000")),
                          dict(ticket_number="10482", delivery_date=date(2026, 9, 3), tonnage=Decimal("22.000"), supplier="Sample Stone Co")])
    assert client.post(f"/api/tickets/{ea}/verify").status_code == 200
    second = client.post(f"/api/tickets/{ss}/verify")
    assert second.status_code == 200, "a different supplier's ticket isn't a duplicate"
    assert Decimal(second.json()["unit_price"]) == 11
    line = _line(client, ticket_list="10482\t20", billed_tons="20", billed_subtotal="200").json()
    assert line["status"] == "matches" and Decimal(line["verified_tons"]) == Decimal("20.000")
    assert [row["ticket"]["ticket_id"] for row in line["ticket_check"]] == [ea]
    assert Decimal(client.get("/api/dashboard").json()["total_cost"]) == Decimal("442.00")


def test_one_ticket_number_on_two_projects_needs_a_person_to_say_both_are_real(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    [first] = _deliveries([dict(ticket_number="20001", delivery_date=date(2026, 9, 5), tonnage=Decimal("20.000"))])
    [second] = _deliveries([dict(ticket_number="20001", delivery_date=date(2026, 9, 5), tonnage=Decimal("20.000"))], project_code="P2")
    assert client.post(f"/api/tickets/{first}/verify").status_code == 200
    refused = client.post(f"/api/tickets/{second}/verify")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "possible_duplicate"
    assert Decimal(client.get("/api/dashboard").json()["total_tons"]) == Decimal("20.000")
    assert client.post(f"/api/tickets/{second}/verify", json={"confirm_duplicate": True}).status_code == 200
    # Each project's invoice sees its own delivery, never the other one.
    p1 = _line(client, ticket_list="20001\t20").json()
    p2 = _line(client, project_id=_project_id("P2"), ticket_list="20001\t20").json()
    for line, mine in ((p1, first), (p2, second)):
        assert line["status"] == "discrepancy" or line["status"] == "matches"
        [row] = [r for r in line["ticket_check"] if r["line"] is not None]
        assert row["ticket"]["ticket_id"] == mine and row["result"] == "matched"
    p1 = client.get(f"/api/invoice-comparisons/{p1['id']}").json()
    assert [r for r in p1["ticket_check"] if "on_another_invoice" in r["flags"]], "the same number billed twice is pointed out"
    assert Decimal(client.get("/api/dashboard").json()["total_tons"]) == Decimal("40.000")


def test_suppliers_one_letter_apart_keep_their_own_rates_and_records(client):
    names = ("Sample Stone Co", "Sample Stones Co", "Sample Stone Company")
    _catalog(client, {(name, "Sand"): price for name, price in zip(names, ("10", "20", "30"))})
    ids = _deliveries([dict(ticket_number=f"S{n}", delivery_date=date(2026, 9, 4), tonnage=Decimal("10.000"), supplier=name)
                       for n, name in enumerate(names)])
    prices = [client.post(f"/api/tickets/{ticket}/verify").json()["unit_price"] for ticket in ids]
    assert [Decimal(p) for p in prices] == [10, 20, 30]
    by_supplier = {row["name"]: Decimal(row["cost"]) for row in client.get("/api/dashboard").json()["by_supplier"]}
    assert by_supplier == {"Sample Stone Co": Decimal("100.00"), "Sample Stone Company": Decimal("300.00"), "Sample Stones Co": Decimal("200.00")}
    line = _line(client, supplier_name="Sample Stones Co", billed_tons="10", billed_unit_price="20", billed_subtotal="200").json()
    assert line["status"] == "matches" and line["verified_count"] == 1
    variants = {row["value"]: row["state"] for row in client.get("/api/records/name-variants", params={"field": "supplier"}).json()["rows"]}
    assert set(variants.values()) == {"catalog"}, "nothing suggests merging three real suppliers"


@pytest.mark.parametrize("first, second", [("Sand", "SAND"), ("Sand", "Sand."), ("No. 57 Stone", "No 57 Stone")])
def test_two_catalog_names_that_match_the_same_tickets_cant_both_be_saved(client, first, second):
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/materials", json={"name": first})
    assert client.post("/api/materials", json={"name": second}).status_code == 409


def test_a_rate_sheet_that_contradicts_itself_saves_nothing(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    before = client.get("/api/rates").json()
    for text in ("Example Aggregates,Sand,12\nEXAMPLE AGGREGATES,sand,13", "Example Aggregates,Sand,12\nExample Aggregates,Sand,12"):
        response = client.post("/api/rates/import", json={"text": text, "apply": True})
        assert response.status_code == 422 and "Nothing was saved" in response.json()["detail"], response.text
    assert client.get("/api/rates").json() == before
    assert [e["action"] for e in client.get("/api/pricing/history").json()].count("rate_changed") == 0


def test_an_approved_price_never_follows_a_later_rate(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    first, second = _deliveries([dict(ticket_number=f"R{n}", delivery_date=date(2026, 9, 6), tonnage=Decimal("10.000")) for n in range(2)])
    assert Decimal(client.post(f"/api/tickets/{first}/verify").json()["cost"]) == 100
    client.post("/api/rates/import", json={"text": "Example Aggregates,Sand,15", "apply": True})
    assert Decimal(client.post(f"/api/tickets/{second}/verify").json()["cost"]) == 150
    line = _line(client, billed_tons="20", billed_unit_price="15", billed_subtotal="300").json()
    assert line["status"] == "discrepancy" and line["rate_mismatch_count"] == 1
    money = line["diagnosis"]["money"]
    assert (Decimal(money["start"]), Decimal(money["rate_effect"]), Decimal(money["billed"])) == (Decimal("250.00"), Decimal("50.00"), Decimal("300.00"))
    assert Decimal(client.get("/api/dashboard").json()["total_cost"]) == Decimal("250.00")


# ------------------------------------------------------------ faint and turned scans

def _faint_batch(client) -> int:
    project = _project_id("P1") or client.post("/api/projects", json={"code": "P1", "name": "Fictional"}).json()["id"]
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(project)},
                           files=[("files", ("faint.pdf", make_pdf([make_page(["faint", A, None, None])]), "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _faint_slot(batch_id: int) -> int:
    with _session() as db:
        return db.scalar(select(ScanSlot.id).where(ScanSlot.batch_id == batch_id, ScanSlot.detection == "faint"))


def test_the_same_faint_ticket_in_two_batches_counts_once_however_it_is_turned(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    values = dict(ticket_number="FAINT-1", delivery_date="2026-09-07", material_type="Sand", tonnage="20.000",
                  supplier="Example Aggregates", job_location="Lot")
    found = []
    for turn in (0, 90):
        batch_id = _faint_batch(client)
        assert client.get(f"/api/batches/{batch_id}").json()["status"] != "ready_for_review" or _faint_slot(batch_id)
        slot = _faint_slot(batch_id)
        assert slot is not None, "a faint ticket is kept for a person, not dropped"
        client.post(f"/api/scan-slots/{slot}/mark-ticket")
        if turn:
            assert client.post(f"/api/scan-slots/{slot}/rotate", json={"rotation": turn}).status_code == 200
        ticket = next(t for t in client.get(f"/api/batches/{batch_id}/tickets").json() if t["position"] == 0)
        assert client.patch(f"/api/tickets/{ticket['id']}", json=values).status_code == 200
        found.append(ticket["id"])
    assert client.post(f"/api/tickets/{found[0]}/verify").status_code == 200
    second = client.post(f"/api/tickets/{found[1]}/verify", json={"confirm_duplicate": True})
    assert second.status_code == 409 and "Identical scanned ticket" in second.json()["detail"], "turning a copy doesn't make it a new ticket"
    assert Decimal(client.get("/api/dashboard").json()["total_tons"]) == Decimal("20.000")


def test_a_turned_scan_can_be_turned_back_and_an_approved_one_cant_be_turned(client):
    batch_id = _faint_batch(client)
    slot = _faint_slot(batch_id)
    client.post(f"/api/scan-slots/{slot}/mark-ticket")
    for turn in (90, 180, 270, 0):
        assert client.post(f"/api/scan-slots/{slot}/rotate", json={"rotation": turn}).status_code == 200
    for wrong in (45, 360, -90):
        assert client.post(f"/api/scan-slots/{slot}/rotate", json={"rotation": wrong}).status_code == 422
    ticket = next(t for t in client.get(f"/api/batches/{batch_id}/tickets").json() if t["position"] == 0)
    client.patch(f"/api/tickets/{ticket['id']}", json=dict(ticket_number="T1", delivery_date="2026-09-07", material_type="Sand",
                                                            tonnage="20.000", supplier="S", job_location="L"))
    assert client.post(f"/api/tickets/{ticket['id']}/verify").status_code == 200
    assert client.post(f"/api/scan-slots/{slot}/rotate", json={"rotation": 90}).status_code == 409


# ------------------------------------------------------------ PDFs that aren't what they claim

def _encrypted() -> bytes:
    with pymupdf.open(stream=make_pdf([make_page(TICKETS)]), filetype="pdf") as doc:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="fictional", owner_pw="fictional-owner")


ZERO_PAGES = (b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
              b"2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n")


def _huge() -> bytes:
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(14400, 14400), invariant=1)  # 200 inches square
    pdf.drawString(100, 100, "Ticket 1")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _too_many_pages() -> bytes:
    with pymupdf.open() as doc:
        for _ in range(21):
            doc.new_page()
        return doc.tobytes()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# name, data, what the scan upload must say (400 at upload, or the failed file's error)
BAD_SCANS = [
    ("encrypted.pdf", _encrypted, "password protected"),
    ("empty.pdf", lambda: ZERO_PAGES, "no pages"),
    ("huge.pdf", _huge, "too large"),
    ("long.pdf", _too_many_pages, "limit is 20"),
    ("image.pdf", lambda: PNG, 400),
    ("scan.png", lambda: make_pdf([make_page(TICKETS)]), 400),
    ("photo.jpg", lambda: b"\xff\xd8\xff" + b"\x00" * 64, "Could not read image"),
    ("photo.png", lambda: PNG, "Could not read image"),
]


@pytest.mark.parametrize("name, make, expected", BAD_SCANS, ids=[row[0] for row in BAD_SCANS])
def test_a_scan_that_isnt_what_it_claims_is_refused_or_fails_by_name(client, name, make, expected):
    project = client.post("/api/projects", json={"code": "BAD", "name": "Fictional"}).json()["id"]
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(project)},
                           files=[("files", (name, make(), "application/pdf"))])
    if expected == 400:
        assert response.status_code == 400, response.text
        assert client.get("/api/batches").json() == []
        return
    assert response.status_code == 200, response.text
    batch = client.get(f"/api/batches/{response.json()['id']}").json()
    assert batch["status"] == "failed" and expected in batch["files"][0]["error"], batch
    assert client.get(f"/api/batches/{batch['id']}/tickets").json() == []


@pytest.mark.parametrize("name, make, status, words", [
    ("encrypted.pdf", _encrypted, 400, "password protected"),
    ("long.pdf", _too_many_pages, 413, "at most 20 pages"),
    ("image.pdf", lambda: PNG, 400, "Upload a PDF"),
    ("invoice.png", lambda: make_pdf([make_page(TICKETS)]), 400, "Upload a PDF"),
    ("broken.pdf", lambda: b"%PDF-1.7\n" + b"\x00" * 200, 400, "can't be opened"),
])
def test_an_invoice_that_isnt_what_it_claims_is_refused_by_name(client, name, make, status, words):
    response = client.post("/api/invoice-documents", files={"file": (name, make(), "application/pdf")})
    assert response.status_code == status and words in response.json()["detail"], response.text
    assert client.get("/api/invoice-documents").json() == []


def test_a_huge_invoice_page_is_kept_but_never_rendered_whole(client):
    document = client.post("/api/invoice-documents", files={"file": ("huge.pdf", _huge(), "application/pdf")}).json()
    preview = client.get(f"/api/invoice-documents/{document['id']}/pages/1/image")
    assert preview.status_code == 404 and "not available" in preview.json()["detail"]


def test_an_invoice_with_no_pages_is_refused_for_having_no_pages(client):
    response = client.post("/api/invoice-documents", files={"file": ("empty.pdf", ZERO_PAGES, "application/pdf")})
    assert response.status_code == 400 and "no pages" in response.json()["detail"], response.text


# ------------------------------------------------------------ invoice lines that are simply wrong

@pytest.mark.parametrize("fields, words", [
    ({"billed_tons": "20.0001"}, "decimal"), ({"billed_tons": "-5"}, "greater than 0"), ({"billed_tons": "0"}, "greater than 0"),
    ({"billed_unit_price": "0"}, "greater than 0"), ({"billed_subtotal": "-1"}, "greater than or equal"),
    ({"date_from": "2026-09-30", "date_to": "2026-09-01"}, "on or before"), ({"ticket_list": "10000\tabc"}, "not a number of tons"),
    ({"ticket_list": "10000\t24,50"}, "not a number of tons"), ({"ticket_list": "10000\t20\t5"}, "two columns"), ({"ticket_list": "1.0E+5\t20"}, "scientific notation"),
    ({"ticket_list": "10000\t-2"}, "positive"), ({"invoice_number": "   "}, "nonempty"), ({"supplier_name": "!!!"}, "nonempty"),
])
def test_an_impossible_invoice_line_is_refused_and_saves_nothing(client, fields, words):
    _deliveries([dict(ticket_number="10000", delivery_date=date(2026, 9, 1), tonnage=Decimal("20.000"))])
    response = _line(client, **fields)
    assert response.status_code == 422 and words in str(response.json()["detail"]), response.text
    assert client.get("/api/invoice-comparisons").json() == []


def test_the_same_line_typed_twice_is_refused_the_second_time(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    [ticket] = _deliveries([dict(ticket_number="10000", delivery_date=date(2026, 9, 1), tonnage=Decimal("20.000"))])
    client.post(f"/api/tickets/{ticket}/verify")
    assert _line(client).status_code == 201
    again = _line(client, invoice_number="I-1-COPY")
    assert again.status_code == 409 and "already covers" in again.json()["detail"]
    narrower = _line(client, invoice_number="I-1-COPY", date_from="2026-09-15", date_to="2026-09-15")
    assert narrower.status_code == 409, "any overlap is refused, not only an exact copy"
    assert len(client.get("/api/invoice-comparisons").json()) == 1


def test_a_period_with_nothing_delivered_is_never_called_a_match(client):
    _catalog(client, {("Example Aggregates", "Sand"): "10"})
    [ticket] = _deliveries([dict(ticket_number="10000", delivery_date=date(2026, 9, 1), tonnage=Decimal("20.000"))])
    client.post(f"/api/tickets/{ticket}/verify")
    line = _line(client, date_from="2026-08-01", date_to="2026-08-31").json()
    assert line["status"] == "no_verified_tickets" and line["verified_subtotal"] is None
    assert line["diagnosis"]["money"] is None
