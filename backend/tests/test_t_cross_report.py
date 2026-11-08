"""Random sequences of what people do, checked against a model kept by the test.

The test keeps its own record of every ticket, rate and invoice line, predicts each
response (allowed or refused, and the price an approval locks), and after each
sequence checks that every place showing approved tons and cost shows exactly the
model's figures: dashboard, register, project report, pilot report, Excel and CSV
exports, and each invoice line. A ticket may never be counted twice anywhere.

Fixed seeds, so a failure always replays. Fictional data only.
"""
from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.models import Batch, InvoiceTicketLink, Ticket, UploadedFile
from app.services.pricing import norm, to_cents

from .test_query_budgets import _session

SUPPLIERS = ("Example Aggregates", "Sample Stone Co")
MATERIALS = ("Sand", "Base Rock")
START = date(2026, 9, 1)


@dataclass
class Delivery:
    id: int
    batch: int
    number: str
    supplier: str
    material: str
    day: date
    tons: Decimal
    sha: str
    status: str = "needs_review"
    price: Decimal | None = None
    cost: Decimal | None = None

    @property
    def counted(self) -> bool:
        return self.status == "verified"


@dataclass
class Line:
    id: int
    project: int
    supplier: str
    material: str
    start: date
    end: date
    revision: str


@dataclass
class Model:
    projects: dict[str, int]
    batches: dict[int, int]  # batch -> project
    tickets: dict[int, Delivery]
    rates: dict[tuple[str, str], Decimal | None]
    ids: dict[str, int] = field(default_factory=dict)  # catalog names -> ids
    lines: dict[int, Line] = field(default_factory=dict)

    def project_of(self, ticket: Delivery) -> int:
        return self.batches[ticket.batch]

    def in_line(self, ticket: Delivery, line: Line) -> bool:
        return (self.project_of(ticket) == line.project and norm(ticket.supplier) == norm(line.supplier)
                and norm(ticket.material) == norm(line.material) and line.start <= ticket.day <= line.end)

    def overlaps(self, project: int, supplier: str, material: str, start: date, end: date, *, ignore: int | None = None) -> bool:
        return any(l.id != ignore and l.project == project and norm(l.supplier) == norm(supplier) and norm(l.material) == norm(material)
                   and l.start <= end and l.end >= start for l in self.lines.values())


def _build(client, rng: random.Random) -> Model:
    projects = {code: client.post("/api/projects", json={"code": code, "name": f"Fictional {code}"}).json()["id"] for code in ("P1", "P2")}
    model = Model(projects=projects, batches={}, tickets={}, rates={})
    for name in SUPPLIERS:
        model.ids[name] = client.post("/api/suppliers", json={"name": name}).json()["id"]
    for name in MATERIALS:
        model.ids[name] = client.post("/api/materials", json={"name": name}).json()["id"]
    for supplier in SUPPLIERS:
        for material in MATERIALS:
            # One pair starts with no rate, so some approvals lock no price.
            price = None if (supplier, material) == (SUPPLIERS[1], MATERIALS[1]) else Decimal(rng.randint(1000, 4000)) / 100
            model.rates[(supplier, material)] = price
            if price is not None:
                client.put("/api/rates", json={"supplier_id": model.ids[supplier], "material_id": model.ids[material], "unit_price": str(price)})
    with _session() as db:
        serial = 0
        for batch_no, code in enumerate(("P1", "P1", "P2", "P2")):
            batch = Batch(organization_id=1, project_id=projects[code], layout="single", status="ready_for_review", intake_mode="manual")
            db.add(batch)
            db.flush()
            model.batches[batch.id] = projects[code]
            file = UploadedFile(batch_id=batch.id, original_name="f.pdf", storage_path="x", sha256="0" * 64, status="ready_for_review")
            db.add(file)
            db.flush()
            for n in range(8):
                serial += 1
                values = dict(number=f"T{serial:03d}", supplier=rng.choice(SUPPLIERS), material=rng.choice(MATERIALS),
                              day=START + timedelta(days=rng.randrange(28)), tons=Decimal(rng.randint(5000, 30000)) / 1000,
                              sha=f"{serial:064d}")
                if serial in (31, 32):  # Copies of the first two scans, uploaded again in another project.
                    values["sha"] = f"{serial - 30:064d}"
                if serial == 30:  # The same supplier ticket number as ticket 1, on a different paper.
                    values["number"], values["supplier"] = "T001", "SUPPLIER-OF-T001"
                ticket = Ticket(batch_id=batch.id, file_id=file.id, source_page=1, position=n, crop_box=[0, 0, 1, 1], crop_path="x",
                                crop_sha256=values["sha"], ticket_number=values["number"], delivery_date=values["day"],
                                material_type=values["material"], supplier=values["supplier"], tonnage=values["tons"],
                                job_location="Lot", flags=[], status="needs_review")
                db.add(ticket)
                db.flush()
                model.tickets[ticket.id] = Delivery(ticket.id, batch.id, **values)
        db.commit()
    first, thirtieth = model.tickets[min(model.tickets)], model.tickets[min(model.tickets) + 29]
    thirtieth.supplier = first.supplier
    with _session() as db:
        db.get(Ticket, thirtieth.id).supplier = first.supplier
        db.commit()
    return model


def _approve(client, model: Model, ticket: Delivery, rng: random.Random) -> None:
    confirm = rng.random() < 0.5
    response = client.post(f"/api/tickets/{ticket.id}/verify", json={"confirm_duplicate": confirm})
    if ticket.counted:
        assert response.status_code == 200 and Decimal(response.json()["unit_price"] or 0) == (ticket.price or 0)
        return
    others = [t for t in model.tickets.values() if t.id != ticket.id and t.counted]
    if any(t.sha == ticket.sha for t in others):
        assert response.status_code == 409 and "Identical scanned ticket" in response.json()["detail"], response.text
        return
    if any(t.number.upper() == ticket.number.upper() and norm(t.supplier) == norm(ticket.supplier) for t in others) and not confirm:
        assert response.status_code == 409 and response.json()["detail"]["code"] == "possible_duplicate", response.text
        return
    assert response.status_code == 200, response.text
    ticket.status = "verified"
    ticket.price = model.rates[(ticket.supplier, ticket.material)]
    ticket.cost = to_cents(ticket.tons * ticket.price) if ticket.price is not None else None
    body = response.json()
    assert (Decimal(body["unit_price"]) if body["unit_price"] else None) == ticket.price
    assert (Decimal(body["cost"]) if body["cost"] else None) == ticket.cost


def _withdraw(ticket: Delivery, status: str) -> None:
    ticket.status, ticket.price, ticket.cost = status, None, None


def _edit(client, model: Model, ticket: Delivery, rng: random.Random) -> None:
    if rng.random() < 0.5:
        ticket_tons = Decimal(rng.randint(5000, 30000)) / 1000
        response = client.patch(f"/api/tickets/{ticket.id}", json={"tonnage": str(ticket_tons)})
        changed = ticket_tons != ticket.tons
        ticket.tons = ticket_tons
    else:
        supplier = rng.choice(SUPPLIERS)
        response = client.patch(f"/api/tickets/{ticket.id}", json={"supplier": supplier})
        changed = supplier != ticket.supplier
        ticket.supplier = supplier
    assert response.status_code == 200, response.text
    if changed and ticket.status == "verified":
        _withdraw(ticket, "needs_review")
    elif changed:
        ticket.price = ticket.cost = None


def _add_line(client, model: Model, rng: random.Random) -> None:
    project = rng.choice(list(model.projects.values()))
    supplier, material = rng.choice(SUPPLIERS), rng.choice(MATERIALS)
    start = START + timedelta(days=rng.randrange(28))
    end = start + timedelta(days=rng.randrange(10))
    scope = [t for t in model.tickets.values() if model.project_of(t) == project and t.supplier == supplier and t.material == material
             and start <= t.day <= end and t.status != "skipped"]
    listed = rng.sample(scope, k=min(len(scope), rng.randint(0, 4)))
    tons = sum((t.tons for t in listed), Decimal(0)) or Decimal("10")
    body = {"project_id": project, "invoice_number": f"INV-{rng.randrange(10**6)}", "supplier_name": supplier, "material_name": material,
            "date_from": start.isoformat(), "date_to": end.isoformat(), "billed_tons": str(tons), "billed_unit_price": "25",
            "billed_subtotal": str(to_cents(tons * 25)), "ticket_list": "\n".join(f"{t.number}\t{t.tons}" for t in listed)}
    response = client.post("/api/invoice-comparisons", json=body)
    if model.overlaps(project, supplier, material, start, end):
        assert response.status_code == 409, response.text
        return
    assert response.status_code == 201, response.text
    model.lines[response.json()["id"]] = Line(response.json()["id"], project, supplier, material, start, end, response.json()["revision"])


def _move_line(client, model: Model, rng: random.Random) -> None:
    if not model.lines:
        return
    line = model.lines[rng.choice(list(model.lines))]
    start = START + timedelta(days=rng.randrange(28))
    end = start + timedelta(days=rng.randrange(10))
    current = client.get(f"/api/invoice-comparisons/{line.id}").json()
    body = {name: current[name] for name in ("project_id", "invoice_number", "supplier_name", "material_name", "billed_tons",
                                              "billed_unit_price", "billed_subtotal")}
    body.update(date_from=start.isoformat(), date_to=end.isoformat(), ticket_list="")
    response = client.put(f"/api/invoice-comparisons/{line.id}", json=body, headers={"If-Match": current["revision"]})
    if model.overlaps(line.project, line.supplier, line.material, start, end, ignore=line.id):
        assert response.status_code == 409, response.text
        return
    assert response.status_code == 200, response.text
    line.start, line.end, line.revision = start, end, response.json()["revision"]


def _link(client, model: Model, rng: random.Random) -> None:
    if not model.lines:
        return
    line_id = rng.choice(list(model.lines))
    current = client.get(f"/api/invoice-comparisons/{line_id}").json()
    clean = [row["ticket"]["ticket_id"] for row in current["ticket_check"] if row["evidence"] == "candidate"]
    if clean:
        assert client.post(f"/api/invoice-comparisons/{line_id}/ticket-links", json={"ticket_ids": clean}).status_code in (200, 409)
    links = [link for link in current["ticket_links"]]
    if links and rng.random() < 0.3:
        link = rng.choice(links)
        assert client.post(f"/api/invoice-comparisons/{line_id}/ticket-links/{link['id']}/release").status_code == 200


def _move_batch(client, model: Model, rng: random.Random) -> None:
    batch = rng.choice(list(model.batches))
    target = rng.choice(list(model.projects.values()))
    with _session() as db:
        linked = db.scalar(select(func.count()).select_from(InvoiceTicketLink).join(Ticket, Ticket.id == InvoiceTicketLink.ticket_id).where(
            Ticket.batch_id == batch, InvoiceTicketLink.released_at.is_(None)))
    response = client.patch(f"/api/batches/{batch}/project", json={"project_id": target})
    if linked and target != model.batches[batch]:
        assert response.status_code == 409, response.text
        return
    assert response.status_code == 200, response.text
    model.batches[batch] = target


def _step(client, model: Model, rng: random.Random) -> None:
    ticket = model.tickets[rng.choice(list(model.tickets))]
    action = rng.choices(["approve", "edit", "flag", "skip", "rate", "line", "move_line", "link", "move_batch", "bulk"],
                         weights=[30, 10, 4, 2, 5, 8, 3, 6, 2, 3])[0]
    if action == "approve":
        _approve(client, model, ticket, rng)
    elif action == "edit":
        _edit(client, model, ticket, rng)
    elif action == "flag":
        assert client.post(f"/api/tickets/{ticket.id}/flag").status_code == 200
        _withdraw(ticket, "flagged")
    elif action == "skip":
        assert client.post(f"/api/tickets/{ticket.id}/skip").status_code == 200
        _withdraw(ticket, "skipped")
    elif action == "rate":
        pair = (rng.choice(SUPPLIERS), rng.choice(MATERIALS))
        price = Decimal(rng.randint(1000, 4000)) / 100
        assert client.put("/api/rates", json={"supplier_id": model.ids[pair[0]], "material_id": model.ids[pair[1]],
                                              "unit_price": str(price)}).status_code == 200
        model.rates[pair] = price
    elif action == "line":
        _add_line(client, model, rng)
    elif action == "move_line":
        _move_line(client, model, rng)
    elif action == "link":
        _link(client, model, rng)
    elif action == "move_batch":
        _move_batch(client, model, rng)
    else:
        ids = rng.sample(list(model.tickets), 3)
        assert client.post("/api/tickets/bulk-edit", json={"ticket_ids": ids, "field": "job_location", "value": "Bulk"}).status_code == 200


# ------------------------------------------------------------ every view against the model

def _expected(model: Model, tickets) -> tuple[Decimal, Decimal, int]:
    counted = [t for t in tickets if t.counted]
    return (sum((t.tons for t in counted), Decimal(0)), sum((t.cost for t in counted if t.cost is not None), Decimal(0)),
            len(counted))


def _register(client) -> tuple[Decimal, Decimal, int]:
    tons = cost = Decimal(0)
    count = 0
    page = 1
    while True:
        body = client.get("/api/records/search", params={"status": "verified", "page": page, "page_size": 200}).json()
        for row in body["rows"]:
            tons += Decimal(row["tonnage"])
            cost += Decimal(row["cost"]) if row["cost"] else 0
            count += 1
        if page * 200 >= body["total"]:
            return tons, cost, count
        page += 1


def _csv(client) -> tuple[Decimal, Decimal, int]:
    rows = list(csv.DictReader(io.StringIO(client.get("/api/records/export.csv").text)))
    verified = [r for r in rows if r["Verification Status"] == "Verified"]
    return (sum((Decimal(r["Tonnage"]) for r in verified), Decimal(0)),
            sum((Decimal(r["Calculated Cost"]) for r in verified if r["Calculated Cost"]), Decimal(0)), len(verified))


def _excel(client) -> tuple[Decimal, Decimal, int, Decimal, Decimal]:
    book = load_workbook(io.BytesIO(client.get("/api/records/export.xlsx").content))
    sheet = book["Delivery Records"]
    header = [cell.value for cell in sheet[1]]
    rows = [dict(zip(header, (cell.value for cell in row))) for row in sheet.iter_rows(min_row=2)]
    verified = [r for r in rows if r["Verification Status"] == "Verified"]
    summary = {row[0].value: row[1].value for row in book["Summary"].iter_rows() if row[0].value and len(row) > 1}
    to3 = lambda v: Decimal(str(v)).quantize(Decimal("0.001"))  # noqa: E731
    to2 = lambda v: Decimal(str(v)).quantize(Decimal("0.01"))  # noqa: E731
    return (sum((to3(r["Tonnage"]) for r in verified), Decimal(0)), sum((to2(r["Calculated Cost"]) for r in verified if r["Calculated Cost"] is not None), Decimal(0)),
            len(verified), to3(summary["Verified tons"]), to2(summary["Total calculated material cost"]))


def _views_agree_with_the_model(client, model: Model) -> None:
    tons, cost, count = _expected(model, model.tickets.values())
    dashboard = client.get("/api/dashboard").json()
    assert (Decimal(dashboard["total_tons"]), Decimal(dashboard["total_cost"]), dashboard["verified"]) == (tons, cost, count), "dashboard"
    unpriced = sum(1 for t in model.tickets.values() if t.counted and t.cost is None)
    assert dashboard["unpriced_tickets"] == unpriced
    summary = client.get("/api/records/search", params={"page_size": 1}).json()["summary"]
    assert (Decimal(summary["verified_tons"]), Decimal(summary["verified_cost"])) == (tons, cost), "register summary"
    assert _register(client) == (tons, cost, count), "register rows"
    assert _csv(client) == (tons, cost, count), "CSV export"
    excel_tons, excel_cost, excel_count, summary_tons, summary_cost = _excel(client)
    assert (excel_tons, excel_cost, excel_count) == (tons, cost, count), "Excel rows"
    assert (summary_tons, summary_cost) == (tons, cost), "Excel summary"
    pilot = client.get("/api/reports/pilot").json()
    assert Decimal(pilot["review"]["verified_tons"]) == tons and pilot["review"]["verified"] == count, "pilot report"
    reconciliation = pilot["reconciliation"]
    assert reconciliation["verified_tickets_linked"] + reconciliation["verified_tickets_not_linked"] == count
    assert Decimal(reconciliation["tons_linked"]) + Decimal(reconciliation["tons_not_linked"]) == tons
    project_tons = project_cost = Decimal(0)
    for project in model.projects.values():
        mine = [t for t in model.tickets.values() if model.project_of(t) == project]
        expected = _expected(model, mine)
        report = client.get(f"/api/reports/project/{project}").json()["totals"]
        assert (Decimal(report["verified_tons"]), Decimal(report["cost"]), report["verified_tickets"]) == expected, f"project {project} report"
        assert Decimal(client.get("/api/reports/pilot", params={"project_id": project}).json()["review"]["verified_tons"]) == expected[0]
        assert Decimal(client.get("/api/dashboard", params={"project_id": project}).json()["total_tons"]) == expected[0]
        project_tons += expected[0]
        project_cost += expected[1]
    assert (project_tons, project_cost) == (tons, cost), "projects add up to the company"

    # Invoice lines: each shows exactly its own deliveries, and none is shown by two lines.
    seen: dict[int, int] = {}
    for line in client.get("/api/invoice-comparisons").json():
        mine = model.lines[line["id"]]
        scope = [t for t in model.tickets.values() if t.counted and model.in_line(t, mine)]
        assert Decimal(line["verified_tons"]) == sum((t.tons for t in scope), Decimal(0)), f"line {line['id']}"
        assert line["verified_count"] == len(scope)
        expected_cost = None if not scope or any(t.cost is None for t in scope) else sum((t.cost for t in scope), Decimal(0))
        assert (Decimal(line["verified_subtotal"]) if line["verified_subtotal"] is not None else None) == expected_cost
        for ticket in line["tickets"]:
            if ticket["review_status"] == "verified" and ticket["scope_match"] == "exact":
                assert ticket["ticket_id"] not in seen, f"ticket {ticket['ticket_id']} counted on lines {seen.get(ticket['ticket_id'])} and {line['id']}"
                seen[ticket["ticket_id"]] = line["id"]
    with _session() as db:
        active = db.execute(select(InvoiceTicketLink.ticket_id, func.count()).where(InvoiceTicketLink.released_at.is_(None))
                            .group_by(InvoiceTicketLink.ticket_id)).all()
        assert all(n == 1 for _, n in active), "a delivery is linked to two invoice lines"
        # No scan is approved twice, whatever the order of events.
        approved = db.scalars(select(Ticket.crop_sha256).where(Ticket.status == "verified")).all()
        assert len(approved) == len(set(approved))


@pytest.mark.parametrize("seed", range(10))
def test_every_report_agrees_with_the_model_after_random_work(client, seed):
    rng = random.Random(seed)
    model = _build(client, rng)
    for step in range(90):
        _step(client, model, rng)
        if step in (30, 60):
            _views_agree_with_the_model(client, model)
    _views_agree_with_the_model(client, model)
    assert any(t.counted for t in model.tickets.values()), "the sequence approved something, so the checks had work to do"
