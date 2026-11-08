"""Fictional invoices with planted mistakes, from PDF upload to the final figures.

Each case uploads a PDF, reads the drafts, corrects what the reader can't know,
confirms, and checks the result against an answer key worked out by hand. All of it
is invented data, so it tests the software's logic, not accuracy on real invoices.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.main import app
from app.db import get_db
from app.models import Batch, InvoiceComparison, Project, Ticket, UploadedFile
from sqlalchemy import func, select

from .fixtures_invoices import CASES, Case, Delivery, Line
from . import fixtures_invoices as fx

READ_FIELDS = ("invoice_number", "supplier_name", "material_name", "billed_quantity", "unit", "billed_unit_price",
               "billed_subtotal", "date_from", "date_to", "ticket_list")
EDITABLE_NOT_ON_PAPER = "project_id"


def _session():
    return next(app.dependency_overrides[get_db]())


def _ids(make):
    return make.__name__


def _approve_deliveries(client, case: Case, project_id: int) -> dict[tuple[str, str], int]:
    """Catalog and rates through the API, delivery records in the database, and every
    approval through the real endpoint, so prices lock the way they do for a person."""
    suppliers, materials = {}, {}
    for supplier, material in case.rates:
        if supplier not in suppliers:
            suppliers[supplier] = client.post("/api/suppliers", json={"name": supplier}).json()["id"]
        if material not in materials:
            materials[material] = client.post("/api/materials", json={"name": material}).json()["id"]
    for name in case.extra_suppliers:
        client.post("/api/suppliers", json={"name": name})
    for (supplier, material), price in case.rates.items():
        response = client.put("/api/rates", json={"supplier_id": suppliers[supplier], "material_id": materials[material], "unit_price": price})
        assert response.status_code == 200, response.text
    with _session() as db:
        batch = Batch(organization_id=1, project_id=project_id, layout="single", status="ready_for_review", intake_mode="manual")
        db.add(batch)
        db.flush()
        file = UploadedFile(batch_id=batch.id, original_name="tickets.pdf", storage_path="x", sha256="0" * 64, status="ready_for_review")
        db.add(file)
        db.flush()
        tickets = {}
        for position, delivery in enumerate(case.deliveries):
            ticket = Ticket(
                batch_id=batch.id, file_id=file.id, source_page=1, position=position, crop_box=[0, 0, 1, 1], crop_path="x",
                crop_sha256=f"{position:064d}", ticket_number=delivery.number, delivery_date=delivery.day,
                material_type=delivery.material, supplier=delivery.supplier, job_location="Fictional Site",
                tonnage=Decimal(delivery.tons), flags=[], status="needs_review")
            db.add(ticket)
            db.flush()
            tickets[(delivery.supplier, delivery.number)] = ticket.id
        db.commit()
    for delivery in case.deliveries:
        if delivery.status == "verified":
            response = client.post(f"/api/tickets/{tickets[(delivery.supplier, delivery.number)]}/verify")
            assert response.status_code == 200, response.text
    return tickets


def _project(client) -> int:
    return client.post("/api/projects", json={"code": "24-115", "name": "Fictional Road Job"}).json()["id"]


def _upload(client, pdf: bytes, name="invoice.pdf"):
    # No project at upload: which job a bill belongs to is something a person says.
    return client.post("/api/invoice-documents", files={"file": (name, pdf, "application/pdf")})


def _same(actual, expected: str | None) -> bool:
    return (actual is None) == (expected is None) and (actual is None or Decimal(actual) == Decimal(expected))


def _printed_tickets(line: Line) -> list[tuple[str, Decimal | None]]:
    return [(number, Decimal(tons) if tons else None) for number, tons in line.printed.tickets]


def _draft_tickets(text: str) -> list[tuple[str, Decimal | None]]:
    rows = []
    for row in (text or "").splitlines():
        number, _, tons = row.partition("\t")
        rows.append((number, Decimal(tons) if tons else None))
    return rows


def _expected_bridge(diagnosis, key) -> None:
    bridge = diagnosis["tons"]
    if bridge is None:
        assert not key.bridge
        return
    found = {item["kind"]: (Decimal(item["tons"]), tuple(item["tickets"])) for item in bridge["items"]}
    assert found == {kind: (Decimal(tons), tickets) for kind, (tons, tickets) in key.bridge.items()}
    assert Decimal(bridge["unexplained"]) == Decimal(key.unexplained)
    # It adds up exactly, by construction of the answer key as well as the code.
    assert Decimal(bridge["start"]) + sum(Decimal(i["tons"]) for i in bridge["items"]) + Decimal(bridge["unexplained"]) == Decimal(bridge["billed"])


def _expected_money(diagnosis, key) -> None:
    money = diagnosis["money"]
    if key.money is None:
        assert money is None
        return
    assert tuple(Decimal(money[name]) for name in ("start", "quantity_effect", "rate_effect", "arithmetic_effect", "rounding", "billed")) \
        == tuple(Decimal(value) for value in key.money)
    assert (Decimal(money["start"]) + Decimal(money["quantity_effect"]) + Decimal(money["rate_effect"])
            + Decimal(money["arithmetic_effect"]) + Decimal(money["rounding"])) == Decimal(money["billed"])


@pytest.mark.parametrize("make", CASES, ids=_ids)
def test_every_planted_mistake_gets_the_exact_result(client, make):
    case = make()
    project = _project(client)
    _approve_deliveries(client, case, project)
    saved = 0
    for number, document in enumerate(case.documents):
        uploaded = _upload(client, document.pdf, f"invoice-{number}.pdf")
        if document.upload_refused:
            assert uploaded.status_code == document.upload_refused, uploaded.text
            continue
        assert uploaded.status_code == 201, uploaded.text
        body = uploaded.json()
        assert body["status"] == "needs_review", body["message"]
        assert len(body["lines"]) == len(document.lines), [(l["material_name"], l["billed_quantity"]) for l in body["lines"]]
        for text in document.unread:
            assert any(text in row for row in body["unread_rows"]), (text, body["unread_rows"])
        # Before anyone confirms a thing, nothing counts.
        assert len(client.get("/api/invoice-comparisons").json()) == saved
        for draft, line in zip(body["lines"], document.lines):
            key = line.result
            url = f"/api/invoice-documents/{body['id']}/lines/{draft['id']}"
            corrected = client.patch(url, json={"project_id": project, **key.correct})
            assert corrected.status_code == 200, corrected.text
            assert corrected.json()["blockers"] == [], corrected.json()["blockers"]
            confirmed = client.post(url + "/confirm", headers={"If-Match": corrected.json()["revision"]})
            if key.refused:
                status, words = key.refused
                assert confirmed.status_code == status, confirmed.text
                assert words in str(confirmed.json()["detail"]), confirmed.text
                assert len(client.get("/api/invoice-comparisons").json()) == saved
                assert client.get(url.rsplit("/lines", 1)[0]).json()["lines"][body["lines"].index(draft)]["state"] == "pending_review"
                assert client.post(url + "/reject").status_code == 200
                continue
            assert confirmed.status_code == 200, confirmed.text
            saved += 1
            result = client.get(f"/api/invoice-comparisons/{confirmed.json()['comparison_id']}").json()
            assert result["status"] == key.status, (result["warnings"], result["status"])
            assert Decimal(result["verified_tons"]) == Decimal(key.verified_tons)
            assert _same(result["verified_subtotal"], key.verified_subtotal)
            assert _same(result["subtotal_difference"], key.dollar_effect)
            _expected_bridge(result["diagnosis"], key)
            _expected_money(result["diagnosis"], key)
            assert result["source_document_id"] == body["id"]
    # A ticket can't be counted more than once, whatever was billed.
    assert len(client.get("/api/invoice-comparisons").json()) == saved
    approved = [d for d in case.deliveries if d.status == "verified"]
    assert Decimal(client.get("/api/dashboard").json()["total_tons"]) == sum(Decimal(d.tons) for d in approved)


def test_the_reader_keeps_two_real_suppliers_apart_and_each_rate_with_its_own(client):
    case = fx.similar_supplier_names()
    project = _project(client)
    tickets = _approve_deliveries(client, case, project)
    locked = client.get("/api/records", params={"status": "verified"}).json()
    prices = {(row["supplier"], row["ticket_number"]): Decimal(row["unit_price"]) for row in locked}
    assert prices[("Sample Stones Co", "7001")] == 25 and prices[("Sample Stone Co", "7001")] == 26
    assert len(tickets) == 4


# ------------------------------------------------------------ how well the reader read

def _score(document, drafted) -> dict[str, int]:
    """Against what's printed. Blank where the paper is silent counts as right: inventing
    a value there would be the mistake."""
    score = {"right": 0, "blank": 0, "wrong": 0}
    misses = []
    for draft, line in zip(drafted, document.lines):
        printed = line.printed
        truth = {
            "invoice_number": document.invoice_number, "supplier_name": document.supplier, "material_name": printed.material,
            "billed_quantity": Decimal(printed.quantity), "unit": printed.unit,
            "billed_unit_price": Decimal(printed.rate) if printed.rate else None, "billed_subtotal": Decimal(printed.subtotal),
            "date_from": printed.date_from, "date_to": printed.date_to,
        }
        for name, expected in truth.items():
            value = getattr(draft, name)
            if value == expected:
                score["right"] += 1
            elif value is None:
                score["blank"] += 1
                misses.append((name, expected, None))
            else:
                score["wrong"] += 1
                misses.append((name, expected, value))
        if _draft_tickets(draft.ticket_list) == _printed_tickets(line):
            score["right"] += 1
        else:
            score["wrong"] += 1
            misses.append(("ticket_list", printed.tickets, draft.ticket_list))
    score["misses"] = misses
    return score


def test_how_many_fields_the_reader_got_right_on_our_own_fictional_layouts(capsys):
    """Measures our own drawn layouts, on a reader written with them in view. It says
    nothing about real supplier invoices. A change here means the reader changed."""
    from app.services.invoice_extraction import TextLayerExtractor
    reader = TextLayerExtractor()
    totals = {"right": 0, "blank": 0, "wrong": 0}
    misses = []
    documents = 0
    for make in CASES:
        case = make()
        catalog = tuple({supplier for supplier, _ in case.rates} | set(case.extra_suppliers))
        for document in case.documents:
            if document.upload_refused:
                continue
            documents += 1
            result = reader.extract(document.pdf, catalog)
            assert len(result.lines) == len(document.lines), case.name
            score = _score(document, result.lines)
            for name in totals:
                totals[name] += score[name]
            misses += [(case.name, *miss) for miss in score["misses"]]
    fields = sum(totals.values())
    with capsys.disabled():
        print(f"\nReader, on our own fictional layouts only (says nothing about real invoices): {documents} documents, "
              f"{totals['right']} of {fields} fields right, {totals['blank']} left blank, {totals['wrong']} wrong.")
        for miss in misses:
            print("  ", *miss)
    # Exactly what it reads today. Worse fails here; better should update this number.
    assert totals == {"right": 186, "blank": 2, "wrong": 2}, (totals, misses)


# ------------------------------------------------------------ what the reader gets wrong

def _pdf(draw) -> bytes:
    buffer, pdf = fx._canvas()
    draw(pdf)
    return fx._finish(buffer, pdf)


@pytest.mark.xfail(strict=True, reason="BUG: find_columns joins neighbouring 'Unit' and 'Price' headers into one 'unit price' column, so the unit is lost and 'TON' lands on the material name")
def test_adjacent_unit_and_price_columns_stay_separate():
    """"Qty | Unit | Price | Total" is a common table. Failing safe (a person sees a blank
    unit and a wrong material), but every such invoice needs both corrected by hand."""
    from app.services.invoice_extraction import TextLayerExtractor
    pdf = fx._summary_only("X-1", "Cedar Ridge Stone", [("Base Rock", "40.000", "TON", "20.0000", "800.00")])
    [line] = TextLayerExtractor().extract(pdf, ()).lines
    assert (line.material_name, line.unit) == ("Base Rock", "ton")


@pytest.mark.xfail(strict=True, reason="BUG: a PDF holding two invoices is drafted as one: every row gets the first invoice's number and the rows are merged, with nothing on screen saying so")
def test_a_pdf_holding_two_invoices_doesnt_pass_its_rows_off_as_one():
    """A statement or a scanned-together pair. Must be split, or refused, or flagged."""
    from app.services.invoice_extraction import TextLayerExtractor
    buffer, pdf = fx._canvas()
    xs = (50, 120, 200, 330, 400, 490)
    head = ("Date", "Ticket No", "Description", "Tons", "Rate", "Amount")
    fx._text(pdf, [(50, 750, "Invoice #: FIRST-1"), (300, 750, "Vendor: Ridgeline Aggregates")])
    fx._table(pdf, 700, xs, head, [("10/06/2026", "9001", "Sand", "10.000", "15.0000", "150.00")])
    pdf.showPage()
    fx._text(pdf, [(50, 750, "Invoice #: SECOND-2"), (300, 750, "Vendor: Ridgeline Aggregates")])
    fx._table(pdf, 700, xs, head, [("10/07/2026", "9002", "Sand", "12.000", "15.0000", "180.00")])
    result = TextLayerExtractor().extract(fx._finish(buffer, pdf), ())
    numbers = {line.invoice_number for line in result.lines}
    assert result.unsupported_reason or numbers == {"FIRST-1", "SECOND-2"} or any("SECOND-2" in row for row in result.unread_rows)


def test_a_rotated_page_still_reads_or_is_left_to_a_person():
    """Sideways on the scanner glass. Text must be read correctly, or not at all."""
    import pymupdf
    from app.services.invoice_extraction import TextLayerExtractor
    case = fx.wrong_billed_quantity()
    with pymupdf.open(stream=case.documents[0].pdf, filetype="pdf") as doc:
        doc[0].set_rotation(90)
        rotated = doc.tobytes()
    result = TextLayerExtractor().extract(rotated, ())
    for line in result.lines:
        assert line.billed_subtotal in (None, Decimal("2007.83"))
        assert line.material_name in (None, "No. 57 Stone")


def test_saved_comparisons_never_outnumber_the_lines_people_confirmed(client):
    """The same bill arriving three ways leaves one comparison behind."""
    case = fx.resent_under_a_new_number()
    project = _project(client)
    _approve_deliveries(client, case, project)
    first = _upload(client, case.documents[0].pdf).json()
    client.patch(f"/api/invoice-documents/{first['id']}/lines/{first['lines'][0]['id']}", json={"project_id": project})
    assert client.post(f"/api/invoice-documents/{first['id']}/lines/{first['lines'][0]['id']}/confirm").status_code == 200
    assert _upload(client, case.documents[1].pdf).status_code == 409
    second = _upload(client, case.documents[2].pdf).json()
    client.patch(f"/api/invoice-documents/{second['id']}/lines/{second['lines'][0]['id']}", json={"project_id": project})
    refused = client.post(f"/api/invoice-documents/{second['id']}/lines/{second['lines'][0]['id']}/confirm")
    assert refused.status_code == 409
    with _session() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceComparison)) == 1
    # Sent a third time for a later period: it's allowed, and shouts about the repeat.
    third = _upload(client, fx._iso_dates("PP-900-Z", "Placeholder Paving", [("2026-11-20", "PP-1", "Asphalt Mix", "18.000", "60.0000", "1,080.00")])).json()
    client.patch(f"/api/invoice-documents/{third['id']}/lines/{third['lines'][0]['id']}", json={"project_id": project})
    confirmed = client.post(f"/api/invoice-documents/{third['id']}/lines/{third['lines'][0]['id']}/confirm")
    assert confirmed.status_code == 200
    line = client.get(f"/api/invoice-comparisons/{confirmed.json()['comparison_id']}").json()
    assert line["status"] == "no_verified_tickets"
    row = next(r for r in line["ticket_check"] if r["invoice_ticket_number"] == "PP-1")
    assert row["result"] == "outside_scope" and "on_another_invoice" in row["flags"]
    found = client.get("/api/invoice-comparisons/overview").json()["exceptions"]
    assert any(e["kind"] == "ticket_on_two_invoices" and "PP-900" in e["detail"] for e in found), found
