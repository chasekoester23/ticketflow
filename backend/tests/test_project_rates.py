"""Project pricing agreements and dated rates: which rate prices a ticket, that approval
locks it, and that nothing is guessed. The first scan ticket is Example Aggregates,
No. 53 Crushed Stone, 24.50 t, delivered 2026-10-15."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import SupplierRate
from app.routers import api as api_module
from app.services.pricing import RateBook, RateRow

from .sample_data import TICKETS, make_scan_pdf


@pytest.fixture()
def catalog(client):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    material = client.post("/api/materials", json={"name": "No. 53 Crushed Stone"}).json()["id"]
    projects = {code: client.post("/api/projects", json={"code": code, "name": f"Fictional {code}"}).json()["id"]
                for code in ("P1", "P2")}

    def rate(price, *, project=None, start=None, expect=200):
        body = {"supplier_id": supplier, "material_id": material, "unit_price": price,
                "project_id": projects[project] if project else None, "effective_from": start}
        response = client.put("/api/rates", json=body)
        assert response.status_code == expect, response.text
        return response.json()

    return rate, projects


def _first_ticket(client, project_id=None, *, number=None, **changes):
    data = {"layout": "grid_2x2", **({"project_id": str(project_id)} if project_id else {})}
    scan = make_scan_pdf([{**TICKETS[0], "ticket_number": number}, *TICKETS[1:]] if number else None)
    batch = client.post("/api/batches", data=data, files=[("files", ("scan.pdf", scan, "application/pdf"))]).json()
    ticket = client.get(f"/api/batches/{batch['id']}/tickets").json()[0]
    if number:
        changes["ticket_number"] = number  # as a person reads it off that paper
    if changes:
        ticket = client.patch(f"/api/tickets/{ticket['id']}", json=changes).json()
    return ticket


def _price(ticket):
    return (Decimal(ticket["unit_price"]) if ticket["unit_price"] is not None else None), ticket["price_source"]


# ---------------------------------------------------------------- the rule itself, without a database

def _book(*rows):
    return RateBook({("s", "m"): [RateRow(Decimal(p), project, start) for p, project, start in rows]}, {})


@pytest.mark.parametrize("project, on, expected", [
    (None, date(2026, 10, 1), ("18.00", "rate")),          # before the change: the undated rate
    (None, date(2026, 11, 1), ("19.50", "rate")),          # on the start date: the new rate
    (None, date(2027, 3, 1), ("19.50", "rate")),           # long after: still the latest
    (7, date(2026, 10, 1), ("17.25", "project_rate")),     # the project's agreement wins
    (7, date(2026, 12, 15), ("16.90", "project_rate")),    # its own dated revision
    (8, date(2026, 11, 2), ("19.50", "rate")),             # another project: company rate
])
def test_project_then_latest_start_date(project, on, expected):
    book = _book(("18.00", None, None), ("19.50", None, date(2026, 11, 1)),
                 ("17.25", 7, None), ("16.90", 7, date(2026, 12, 1)))
    found = book.match("S", "M", project, on)
    assert (str(found.price), found.source) == expected


def test_an_agreement_not_yet_started_gives_way_to_the_company_rate():
    book = _book(("18.00", None, None), ("15.00", 7, date(2026, 12, 1)))
    found = book.match("S", "M", 7, date(2026, 11, 20))
    assert (found.price, found.source) == (Decimal("18.00"), "rate")


def test_nothing_applies_before_the_first_start_date():
    book = _book(("19.50", None, date(2026, 11, 1)))
    assert book.match("S", "M", None, date(2026, 10, 31)).price is None


def test_dated_rates_and_no_delivery_date_is_unpriced_with_the_reason():
    found = _book(("18.00", None, None), ("19.50", None, date(2026, 11, 1))).match("S", "M", None, None)
    assert found.price is None and "delivery date" in found.reason


def test_an_undated_rate_needs_no_date():
    found = _book(("18.00", None, None)).match("S", "M", None, None)
    assert found.price == Decimal("18.00")


def test_two_prices_for_names_that_read_the_same_are_never_picked_between():
    found = _book(("18.00", None, None), ("18.50", None, None)).match("S", "M", None, date(2026, 11, 1))
    assert found.price is None and "duplicate" in found.reason


# ---------------------------------------------------------------- through the API, as people use it

def test_a_dated_rate_change_prices_each_delivery_by_its_own_date_and_approval_locks_it(client, catalog):
    rate, _ = catalog
    rate("18.00")
    rate("19.50", start="2026-10-16")
    before = _first_ticket(client)  # delivered 10-15: the old rate
    assert _price(before) == (Decimal("18.0000"), "rate")
    after = client.patch(f"/api/tickets/{before['id']}", json={"delivery_date": "2026-10-16"}).json()
    assert _price(after) == (Decimal("19.5000"), "rate")

    locked = client.post(f"/api/tickets/{after['id']}/verify", headers={"If-Match": after["revision"]}).json()
    assert locked["price_snapshot_state"] == "priced" and Decimal(locked["cost"]) == Decimal("477.75")
    rate("21.00", start="2026-10-16")  # the dated rate is revised after approval
    again = next(t for t in client.get(f"/api/batches/{after['batch_id']}/tickets").json() if t["id"] == after["id"])
    assert _price(again) == (Decimal("19.5000"), "rate") and Decimal(again["cost"]) == Decimal("477.75")


def test_a_project_agreement_prices_its_own_tickets_only(client, catalog):
    rate, projects = catalog
    rate("18.00")
    rate("16.75", project="P1")
    on_p1 = _first_ticket(client, projects["P1"])
    on_p2 = _first_ticket(client, projects["P2"])
    no_project = _first_ticket(client)
    assert _price(on_p1) == (Decimal("16.7500"), "project_rate")
    assert _price(on_p2) == (Decimal("18.0000"), "rate")
    assert _price(no_project) == (Decimal("18.0000"), "rate")
    locked = client.post(f"/api/tickets/{on_p1['id']}/verify", headers={"If-Match": on_p1["revision"]}).json()
    assert locked["price_source"] == "project_rate" and Decimal(locked["cost"]) == Decimal("410.38")  # 24.5 x 16.75, half up


def test_a_ticket_with_dated_rates_and_no_date_says_why_it_has_no_price(client, catalog):
    rate, _ = catalog
    rate("18.00")
    rate("19.50", start="2026-11-01")
    ticket = _first_ticket(client, delivery_date=None)
    assert ticket["unit_price"] is None
    assert any("delivery date" in note for note in ticket["pricing_notes"])


def test_changing_a_rate_in_one_scope_leaves_the_others_alone_and_history_names_the_scope(client, catalog):
    rate, projects = catalog
    company = rate("18.00")
    dated = rate("19.50", start="2026-11-01")
    project = rate("16.75", project="P1")
    assert len({company["id"], dated["id"], project["id"]}) == 3
    changed = rate("17.00", project="P1")
    assert changed["id"] == project["id"] and Decimal(changed["unit_price"]) == Decimal("17.00")
    listed = {(r["project_id"], r["effective_from"]): Decimal(r["unit_price"]) for r in client.get("/api/rates").json()}
    assert listed == {(None, None): Decimal("18.0000"), (None, "2026-11-01"): Decimal("19.5000"),
                      (projects["P1"], None): Decimal("17.0000")}
    history = client.get("/api/pricing/history").json()
    assert history[0]["action"] == "rate_changed" and history[0]["scope"] == "Project P1 (Fictional P1)"
    assert any(e["scope"] == "Company-wide, from 2026-11-01" for e in history)


def test_another_companys_project_or_a_missing_one_is_refused(client, catalog):
    rate, _ = catalog
    supplier = client.get("/api/suppliers").json()[0]["id"]
    material = client.get("/api/materials").json()[0]["id"]
    response = client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "18",
                                              "project_id": 999})
    assert response.status_code == 404
    assert client.get("/api/rates").json() == []


def test_a_rate_sheet_can_be_a_project_quote_from_a_date(client, catalog):
    rate, projects = catalog
    rate("18.00")
    sheet = "Supplier,Material,Price\nExample Aggregates,No. 53 Crushed Stone,16.40\n"
    preview = client.post("/api/rates/import", json={"text": sheet, "project_id": projects["P2"], "effective_from": "2026-10-01"}).json()
    assert preview["adds"] == 1 and preview["changes"] == 0 and preview["scope"] == "Project P2 (Fictional P2), from 2026-10-01"
    applied = client.post("/api/rates/import", json={"text": sheet, "project_id": projects["P2"], "effective_from": "2026-10-01",
                                                     "apply": True})
    assert applied.status_code == 200, applied.text
    assert _price(_first_ticket(client, projects["P2"])) == (Decimal("16.4000"), "project_rate")
    assert _price(_first_ticket(client)) == (Decimal("18.0000"), "rate")  # company rate untouched


def test_the_database_refuses_two_rates_for_one_scope_and_date(client, catalog):
    rate, projects = catalog
    rate("18.00")
    rate("16.75", project="P1", start="2026-11-01")
    first = client.get("/api/rates").json()
    with api_module.SessionLocal() as db:
        for project_id, start in ((None, None), (projects["P1"], date(2026, 11, 1))):
            db.add(SupplierRate(organization_id=1, supplier_id=first[0]["supplier_id"], material_id=first[0]["material_id"],
                                unit_price=Decimal("99"), project_id=project_id, effective_from=start))
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()


def test_an_invoice_billed_at_the_company_rate_shows_against_the_project_agreement(client, catalog):
    """The money story: the supplier bills list price, the agreement says less."""
    rate, projects = catalog
    rate("18.00")
    rate("16.75", project="P1")
    ticket = _first_ticket(client, projects["P1"])
    client.post(f"/api/tickets/{ticket['id']}/verify", headers={"If-Match": ticket["revision"]})
    line = client.post("/api/invoice-comparisons", json={
        "project_id": projects["P1"], "invoice_number": "INV-1", "supplier_name": "Example Aggregates",
        "material_name": "No. 53 Crushed Stone", "date_from": "2026-10-01", "date_to": "2026-10-31",
        "billed_tons": "24.500", "billed_unit_price": "18.00", "billed_subtotal": "441.00",
        "ticket_list": ticket["ticket_number"]}).json()
    assert line["status"] == "discrepancy" and line["rate_mismatch_count"] == 1
    assert Decimal(line["verified_subtotal"]) == Decimal("410.38")
    assert Decimal(line["subtotal_difference"]) == Decimal("30.62")  # overbilled by the agreement's difference


def test_the_export_can_say_which_rate_each_approved_price_came_from(client, catalog):
    import io

    from openpyxl import load_workbook

    rate, projects = catalog
    rate("18.00")
    rate("16.75", project="P1")
    # Different paper for each: an identical scan can't be approved twice.
    on_p1, no_project, waiting = (_first_ticket(client, projects["P1"]), _first_ticket(client, number="10490"),
                                  _first_ticket(client, number="10491"))
    for ticket in (on_p1, no_project):
        assert client.post(f"/api/tickets/{ticket['id']}/verify", headers={"If-Match": ticket["revision"]}).status_code == 200
    response = client.get("/api/records/export.xlsx", params={"columns": "ticket_number,project,unit_price,price_basis"})
    assert response.status_code == 200, response.text
    sheet = load_workbook(io.BytesIO(response.content)).worksheets[0]
    rows = [[c.value for c in row] for row in sheet.iter_rows(min_row=2, max_col=4)]
    basis = {r[0]: (r[1], r[2], r[3]) for r in rows if r[0] in ("10482", "10490", "10491")}
    assert basis == {"10482": ("P1", 16.75, "Project agreement"), "10490": (None, 18.0, "Company rate"),
                     "10491": (None, 18.0, None)}
    assert waiting["status"] != "verified"  # an unapproved row has no basis yet: nothing is locked


def test_previewing_a_rate_lists_the_unapproved_tickets_it_would_reprice_and_saves_nothing(client, catalog):
    rate, projects = catalog
    rate("18.00")
    waiting_p1 = _first_ticket(client, projects["P1"])                      # 24.50 t, 2026-10-15
    approved = _first_ticket(client, projects["P1"], number="10490")
    assert client.post(f"/api/tickets/{approved['id']}/verify", headers={"If-Match": approved["revision"]}).status_code == 200
    other_project = _first_ticket(client, projects["P2"], number="10491")
    supplier, material = client.get("/api/suppliers").json()[0]["id"], client.get("/api/materials").json()[0]["id"]

    def preview(**body):
        response = client.post("/api/rates/preview", json={"supplier_id": supplier, "material_id": material, **body})
        assert response.status_code == 200, response.text
        return response.json()

    agreement = preview(unit_price="16.75", project_id=projects["P1"])
    assert agreement["tickets"] == 1 and agreement["scope"] == "Project P1 (Fictional P1)"
    (row,) = agreement["rows"]
    assert (row["ticket_id"], row["price_now"], row["price_after"], row["cost_change"]) == (waiting_p1["id"], "18.0000", "16.7500", "-30.63")
    assert agreement["cost_change"] == "-30.63"  # the approved P1 ticket keeps its locked price, so it isn't listed

    later = preview(unit_price="19.00", effective_from="2026-11-01")
    assert later["tickets"] == 0  # every waiting delivery is before the new price starts
    company = preview(unit_price="20.00")
    assert {r["ticket_id"] for r in company["rows"]} == {waiting_p1["id"], other_project["id"]}
    assert [Decimal(r["unit_price"]) for r in client.get("/api/rates").json()] == [Decimal("18.0000")]  # nothing saved
