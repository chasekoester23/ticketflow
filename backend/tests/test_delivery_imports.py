"""Supplier delivery spreadsheets into the normal review: upload, map, preview, import.

Fictional data only. An imported row is an ordinary unapproved ticket; approving it
prices and locks it exactly like a scanned one (docs/engineering/delivery-imports.md).
"""
from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.auth import current_user
from app.main import app
from app.models import AuditEvent, DeliveryImport, Organization, Ticket, User
from app.services import delivery_import as svc

from .test_query_budgets import _session

HEADER = "Ticket #,Date,Product,Net Tons,Truck,Job\n"


def _csv(*lines: str, header: str = HEADER, title: bool = True) -> bytes:
    top = "Example Aggregates - delivery report (fictional)\n,,,\n" if title else ""
    return (top + header + "".join(line + "\n" for line in lines)).encode()


def _xlsx(rows: list[list]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def _upload(client, data: bytes, name: str = "deliveries.csv"):
    return client.post("/api/delivery-imports", files={"file": (name, data, "application/octet-stream")})


def _project(client, code="JOB-7") -> int:
    found = [p for p in client.get("/api/projects").json() if p["code"] == code]
    if found:
        return found[0]["id"]
    return client.post("/api/projects", json={"code": code, "name": "Fictional Sitework", "job_location": "Fictional Sitework"}).json()["id"]


def _map(client, imp: dict, *, project=None, supplier="Example Aggregates", unit="ton", job="Fictional Sitework", columns=None, **extra):
    body = {"columns": columns if columns is not None else imp["suggested"]["mapping"], "supplier": supplier, "unit": unit,
            "job_location": job, **({"project_id": project} if project else {}), **extra}
    response = client.put(f"/api/delivery-imports/{imp['id']}/mapping", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _import(client, data: bytes, name="deliveries.csv", **mapping):
    uploaded = _upload(client, data, name)
    assert uploaded.status_code == 201, uploaded.text
    return _map(client, uploaded.json(), **mapping)


def _commit(client, imp: dict, **body):
    return client.post(f"/api/delivery-imports/{imp['id']}/commit", json={"revision": imp["revision"], **body})


def _tickets(client, batch_id: int) -> list[dict]:
    return client.get(f"/api/batches/{batch_id}/tickets").json()


@contextmanager
def _as_company_two():
    with _session() as db:
        if db.get(Organization, 2) is None:
            db.add(Organization(id=2, name="Second Fictional Company"))
            db.flush()
            db.add(User(id=2, organization_id=2, username="owner-two", display_name="Owner Two", password_hash="x", role="owner"))
            db.commit()
    before = app.dependency_overrides[current_user]
    app.dependency_overrides[current_user] = lambda: User(id=2, organization_id=2, username="owner-two", display_name="Owner Two",
                                                          password_hash="x", active=True, role="owner", must_change_password=False)
    try:
        yield
    finally:
        app.dependency_overrides[current_user] = before


# ------------------------------------------------------------------ reading the file

def test_a_csv_export_finds_its_header_below_a_title_and_suggests_the_columns(client):
    response = _upload(client, _csv("10482,11/03/2026,No. 57 Stone,22.15,T-12,Fictional Sitework"))
    assert response.status_code == 201, response.text
    imp = response.json()
    assert imp["header_row"] == 3 and imp["rows"] == 1 and imp["file_kind"] == "csv" and imp["status"] == "uploaded"
    assert [c["header"] for c in imp["columns"]] == ["Ticket #", "Date", "Product", "Net Tons", "Truck", "Job"]
    assert imp["suggested"]["mapping"] == {"ticket_number": 0, "delivery_date": 1, "material_type": 2, "quantity": 3,
                                           "truck_number": 4, "job_location": 5}
    assert imp["suggested"]["unit"] == "ton"
    assert imp["sample"] == [["10482", "11/03/2026", "No. 57 Stone", "22.15", "T-12", "Fictional Sitework"]]


@pytest.mark.parametrize("headers, expected", [
    (["Ticket", "Ticket Date", "Material", "Qty", "UOM", "Supplier"],
     {"ticket_number": 0, "delivery_date": 1, "material_type": 2, "quantity": 3, "unit": 4, "supplier": 5}),
    (["Scale Ticket No.", "Ship Date", "Product Description", "Gross Tons", "Net Tons", "Truck No", "Job Name"],
     {"ticket_number": 0, "delivery_date": 1, "material_type": 2, "quantity": 4, "truck_number": 5, "job_location": 6}),
])
def test_common_supplier_headers_are_recognised(headers, expected):
    columns = [{"index": i, "header": h} for i, h in enumerate(headers)]
    assert svc.suggest(columns)[0] == expected


def test_an_excel_export_keeps_real_dates_and_whole_ticket_numbers(client):
    data = _xlsx([["Ticket", "Date", "Material", "Net Tons", "Job"],
                  [10482, datetime(2026, 11, 3), "No. 57 Stone", 22.15, "Fictional Sitework"],
                  [10483, datetime(2026, 11, 4), "Washed Sand", 18.0, "Fictional Sitework"]])
    imp = _import(client, data, "deliveries.xlsx")
    assert imp["file_kind"] == "xlsx" and imp["header_row"] == 1
    rows = imp["preview"]["rows"]
    assert [(r["ticket_number"], r["delivery_date"], r["quantity"], r["unit"]) for r in rows] == [
        ("10482", "2026-11-03", "22.150", "ton"), ("10483", "2026-11-04", "18.000", "ton")]
    assert all(r["action"] == "import" and r["errors"] == [] for r in rows)
    assert imp["preview"]["counts"]["ready"] == 2


def test_pounds_are_converted_to_tons_and_the_pounds_kept(client):
    imp = _upload(client, _csv("20001,11/03/2026,No. 57 Stone,44300", header="Ticket #,Date,Product,Net Lbs\n")).json()
    assert imp["suggested"]["unit"] == "lb"
    imp = _map(client, imp, unit="lb")
    row = imp["preview"]["rows"][0]
    assert (row["quantity"], row["unit"], row["source_quantity"], row["source_unit"]) == ("22.150", "ton", "44300.000", "lb")
    done = _commit(client, imp).json()
    ticket = _tickets(client, done["batch_id"])[0]
    assert (ticket["tonnage"], ticket["unit"], ticket["source_quantity"], ticket["source_unit"]) == ("22.150", "ton", "44300.000", "lb")


def test_a_unit_column_in_cubic_yards_is_kept_as_yards_never_tons(client):
    data = _csv("30001,11/03/2026,Topsoil,12,CY", "30002,11/03/2026,Topsoil,8 ,yds", "30003,11/03/2026,Topsoil,3,bags",
                header="Ticket #,Date,Product,Qty,UOM\n")
    imp = _import(client, data, unit=None)
    assert imp["mapping"]["unit"] == 4
    rows = {r["ticket_number"]: r for r in imp["preview"]["rows"]}
    assert (rows["30001"]["quantity"], rows["30001"]["unit"]) == ("12.000", "cy")
    assert (rows["30002"]["quantity"], rows["30002"]["unit"]) == ("8.000", "cy")
    assert rows["30003"]["action"] == "problem" and "Unit 'bags'" in rows["30003"]["errors"][0]
    assert rows["30003"]["quantity"] is None  # never recorded as tons
    done = _commit(client, imp).json()
    assert done["result"] == {"imported": 2, "skipped_duplicates": 0, "error_rows": 1, "batch_id": done["batch_id"]}
    assert {t["unit"] for t in _tickets(client, done["batch_id"])} == {"cy"}


def test_a_quantity_with_its_unit_written_beside_it_is_read_both_ways(client):
    imp = _import(client, _csv("30101,11/03/2026,Topsoil,12.5 cy", header="Ticket #,Date,Product,Qty\n"), unit="ton")
    row = imp["preview"]["rows"][0]
    assert (row["quantity"], row["unit"]) == ("12.500", "cy")


def test_rows_with_problems_say_why_and_are_left_out_unless_chosen(client):
    future = (date.today() + timedelta(days=30)).strftime("%m/%d/%Y")
    data = _csv("40001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework",
                "40002,13/45/2026,No. 57 Stone,20,T-1,Fictional Sitework",
                "40003,11/03/2026,No. 57 Stone,abc,T-1,Fictional Sitework",
                ",11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework",
                f"40005,{future},No. 57 Stone,20,T-1,Fictional Sitework",
                "40006,11/03/2026,No. 57 Stone,9000,T-1,Fictional Sitework",
                ",,,,,",
                "Total,,,9100,,")
    imp = _import(client, data)
    rows = {r["row"]: r for r in imp["preview"]["rows"]}
    assert len(rows) == 7  # the blank row isn't a delivery
    assert rows[4]["action"] == "import"
    assert rows[5]["errors"] == ["Date '13/45/2026' isn't a date Quarvera can read (month/day/year, or year-month-day)."]
    assert rows[6]["errors"] == ["Quantity 'abc' isn't a number."]
    assert rows[7]["errors"] == ["Ticket number missing or unreadable"]
    assert rows[8]["errors"] == ["Date is in the future"]
    assert rows[9]["action"] == "import" and rows[9]["warnings"][0].startswith("Tonnage unusually large")  # a warning, as on review
    assert rows[11]["action"] == "problem"  # a totals line
    assert imp["preview"]["counts"] == {"rows": 7, "ready": 2, "problems": 5, "duplicates_in_file": 0, "duplicates_existing": 0,
                                        "warnings": 2}
    done = _commit(client, imp).json()
    assert done["result"]["imported"] == 2 and done["result"]["error_rows"] == 5
    # The same file again, this time with the problem rows brought in for review.
    again = _import(client, data, name="deliveries-again.csv")
    done = _commit(client, again, include_problem_rows=True, duplicates={"4": "import", "9": "import"}).json()
    assert done["result"]["imported"] == 7
    tickets = {t["source_page"]: t for t in _tickets(client, done["batch_id"])}
    assert all(t["status"] == "needs_review" for t in tickets.values())
    assert "Date unreadable" in tickets[5]["flags"] and tickets[5]["delivery_date"] is None
    assert "Tonnage unreadable" in tickets[6]["flags"] and tickets[6]["tonnage"] is None
    refused = client.post(f"/api/tickets/{tickets[6]['id']}/verify")
    assert refused.status_code == 422  # the review form's own rule: fix it before approval


def test_a_file_needs_the_ticket_and_quantity_columns_and_a_unit(client):
    imp = _upload(client, _csv("1,11/03/2026,Sand,2,T,J")).json()
    without = {k: v for k, v in imp["suggested"]["mapping"].items() if k != "quantity"}
    response = client.put(f"/api/delivery-imports/{imp['id']}/mapping", json={"columns": without, "unit": "ton"})
    assert response.status_code == 422 and "Quantity" in response.json()["detail"]
    response = client.put(f"/api/delivery-imports/{imp['id']}/mapping", json={"columns": imp["suggested"]["mapping"]})
    assert response.status_code == 422 and "unit" in response.json()["detail"]
    twice = {**imp["suggested"]["mapping"], "supplier": 0}
    response = client.put(f"/api/delivery-imports/{imp['id']}/mapping", json={"columns": twice, "unit": "ton"})
    assert response.status_code == 422 and "one thing only" in response.json()["detail"]
    response = client.put(f"/api/delivery-imports/{imp['id']}/mapping", json={"columns": {**imp["suggested"]["mapping"], "unit": 40},
                                                                               "unit": "ton"})
    assert response.status_code == 422


# ------------------------------------------------------------------ duplicates

def test_duplicates_in_the_file_and_already_on_record_are_skipped_unless_chosen(client):
    first = _import(client, _csv("50001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework"), name="week-1.csv")
    assert _commit(client, first).status_code == 200
    data = _csv("50001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework",   # already on record (not yet approved)
                "50002,11/04/2026,No. 57 Stone,21,T-1,Fictional Sitework",
                "50002,11/04/2026,No. 57 Stone,21,T-1,Fictional Sitework",   # repeated in this file
                "050003,11/04/2026,No. 57 Stone,19,T-1,Fictional Sitework")
    imp = _import(client, data, name="week-2.csv")
    rows = {r["row"]: r for r in imp["preview"]["rows"]}
    assert rows[4]["action"] == "duplicate" and rows[4]["duplicate"]["kind"] == "existing"
    assert rows[4]["duplicate"]["status"] == "needs_review" and "already on record" in rows[4]["duplicate"]["message"]
    assert rows[5]["action"] == "import"
    assert rows[6]["duplicate"] == {"kind": "file", "row": 5, "message": "Same supplier and ticket number as row 5 of this file."}
    assert imp["preview"]["counts"]["duplicates_existing"] == 1 and imp["preview"]["counts"]["duplicates_in_file"] == 1
    done = _commit(client, imp).json()  # skip is the default
    assert done["result"]["imported"] == 2 and done["result"]["skipped_duplicates"] == 2
    assert sorted(t["ticket_number"] for t in _tickets(client, done["batch_id"])) == ["050003", "50002"]


def test_a_duplicate_imported_anyway_is_on_its_tickets_history_and_still_needs_a_person(client):
    first = _commit(client, _import(client, _csv("51001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework"))).json()
    original = _tickets(client, first["batch_id"])[0]
    verified = client.post(f"/api/tickets/{original['id']}/verify")
    assert verified.status_code == 200
    imp = _import(client, _csv("51001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework"), name="resent.csv")
    assert imp["preview"]["rows"][0]["duplicate"]["status"] == "verified"
    done = _commit(client, imp, duplicates={"4": "import"}).json()
    copy = _tickets(client, done["batch_id"])[0]
    assert copy["status"] == "needs_review"
    history = client.get(f"/api/tickets/{copy['id']}/audit").json()
    assert {"imported_from", "duplicate_import"} <= {e["field"] for e in history}
    assert any(e["new"] == f"imported alongside ticket {original['id']}" for e in history)
    # Approval asks a person about the same supplier ticket number, exactly as for scans.
    refused = client.post(f"/api/tickets/{copy['id']}/verify")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "possible_duplicate"


def test_another_companys_tickets_are_never_called_duplicates(client):
    _commit(client, _import(client, _csv("52001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework")))
    with _as_company_two():
        imp = _import(client, _csv("52001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework"))
        assert imp["preview"]["rows"][0]["duplicate"] is None and imp["preview"]["rows"][0]["action"] == "import"


# ------------------------------------------------------------------ companies

def test_another_company_cant_see_change_import_or_download_an_import(client):
    imp = _import(client, _csv("53001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework"))
    with _as_company_two():
        assert client.get("/api/delivery-imports").json() == []
        for method, path, body in (("GET", f"/api/delivery-imports/{imp['id']}", None),
                                   ("GET", f"/api/delivery-imports/{imp['id']}/file", None),
                                   ("PUT", f"/api/delivery-imports/{imp['id']}/mapping", {"columns": imp["mapping"], "unit": "ton"}),
                                   ("POST", f"/api/delivery-imports/{imp['id']}/commit", {})):
            response = client.request(method, path, json=body)
            assert response.status_code == 404 and response.json()["detail"] == "Import not found", (method, path)
        own_project = _project(client, "B-1")
    # Company 1 can't file its import under company 2's project either.
    response = client.put(f"/api/delivery-imports/{imp['id']}/mapping",
                          json={"columns": imp["mapping"], "unit": "ton", "project_id": own_project})
    assert response.status_code == 404 and response.json()["detail"] == "Project not found"
    assert _commit(client, imp).status_code == 200


# ------------------------------------------------------------------ formulas are text

def test_a_formula_cell_is_text_and_is_never_calculated(client):
    data = _xlsx([["Ticket", "Date", "Material", "Net Tons", "Job"],
                  [60001, datetime(2026, 11, 3), "No. 57 Stone", "=2*10", "Fictional Sitework"],
                  [60002, datetime(2026, 11, 3), '=HYPERLINK("http://example.invalid","Stone")', 20, "Fictional Sitework"],
                  [60003, datetime(2026, 11, 3), "No. 57 Stone", 20, "Fictional Sitework"]])
    imp = _import(client, data, "formulas.xlsx")
    rows = {r["ticket_number"]: r for r in imp["preview"]["rows"]}
    assert rows["60001"]["quantity"] is None and "formula (=2*10)" in rows["60001"]["errors"][0]
    assert rows["60002"]["material_type"] is None and "Material is a formula" in rows["60002"]["errors"][0]
    assert rows["60003"]["action"] == "import"
    done = _commit(client, imp, include_problem_rows=True).json()
    tickets = {t["ticket_number"]: t for t in _tickets(client, done["batch_id"])}
    assert tickets["60001"]["tonnage"] is None and tickets["60002"]["material_type"] is None
    assert all("=" not in (t["material_type"] or "") for t in tickets.values())


def test_a_csv_cell_that_starts_with_equals_is_treated_as_a_formula_too(client):
    imp = _import(client, _csv("61001,11/03/2026,=1+1,20,T-1,Fictional Sitework"))
    row = imp["preview"]["rows"][0]
    assert row["material_type"] is None and "formula" in row["errors"][0]


# ------------------------------------------------------------------ the normal workflow after import

def _catalog(client, price="20.00", unit=None):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    material = client.post("/api/materials", json={"name": "No. 57 Stone"}).json()["id"]
    response = client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": price,
                                              **({"unit": unit} if unit else {})})
    assert response.status_code == 200, response.text


def test_approving_an_imported_ticket_prices_and_locks_it_like_any_other(client):
    _catalog(client)
    project = _project(client)
    imp = _import(client, _csv("70001,11/03/2026,No. 57 Stone,22.15,T-12,Fictional Sitework",
                               "70002,11/03/2026,No. 57 Stone,18,T-12,Fictional Sitework"), project=project)
    done = _commit(client, imp).json()
    assert done["status"] == "imported" and done["project_id"] == project
    batch = client.get(f"/api/batches/{done['batch_id']}").json()
    assert batch["intake_mode"] == "import" and batch["project_id"] == project and batch["status"] == "ready_for_review"
    first, second = _tickets(client, done["batch_id"])
    assert first["status"] == "needs_review" and first["price_snapshot_state"] == "pending" and not first["tonnage_confirmed"]
    assert first["unit_price"] == "20.0000" and first["cost"] is None  # no cost until a person approves it
    for ticket in (first, second):  # two rows of one file: each its own record, never "the same scan"
        approved = client.post(f"/api/tickets/{ticket['id']}/verify", headers={"If-Match": ticket["revision"]})
        assert approved.status_code == 200, approved.text
    approved = approved.json()
    assert approved["status"] == "verified" and approved["price_snapshot_state"] == "priced" and approved["tonnage_confirmed"]
    with _session() as db:
        locked = db.get(Ticket, first["id"])
        assert (locked.applied_unit_price, locked.approved_cost) == (Decimal("20.0000"), Decimal("443.00"))
        assert locked.crop_sha256 != db.get(Ticket, second["id"]).crop_sha256
    # The database itself refuses to change an approved quantity.
    with _session() as db:
        with pytest.raises((DBAPIError, IntegrityError)):
            db.execute(text("UPDATE tickets SET tonnage = 99 WHERE id = :id"), {"id": first["id"]})
            db.commit()
    # A later rate change never reprices it.
    _catalog_change = client.get("/api/rates").json()[0]
    client.put("/api/rates", json={"supplier_id": _catalog_change["supplier_id"], "material_id": _catalog_change["material_id"],
                                   "unit_price": "25.00"})
    assert _tickets(client, done["batch_id"])[0]["cost"] == "443.00"


def test_an_imported_ticket_opens_in_review_with_its_row_where_the_scan_would_be(client):
    done = _commit(client, _import(client, _csv("71001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework"))).json()
    ticket = _tickets(client, done["batch_id"])[0]
    assert (ticket["source_page"], ticket["position"]) == (4, 0)
    image = client.get(f"/api/tickets/{ticket['id']}/image")
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    manifest = client.get(f"/api/batches/{done['batch_id']}/manifest").json()
    assert manifest["blockers"] == [] and manifest["status"] == "count_matches" and manifest["counts"]["legacy_files"] == 0
    history = client.get(f"/api/tickets/{ticket['id']}/audit").json()
    assert history[0]["field"] == "imported_from" and history[0]["new"] == "deliveries.csv, row 4"
    # The ticket's own review edits work as for any ticket.
    edited = client.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": "20.5"})
    assert edited.status_code == 200 and edited.json()["tonnage"] == "20.500"


def test_the_same_row_of_the_same_file_is_the_same_record(client):
    """Each row's picture carries the file's hash and its row number, so its crop hash differs
    for every row, and matches only the same row of the same file imported again."""
    columns = [{"index": 0, "header": "Ticket"}]
    one, _ = svc.row_image("a.csv", "f" * 64, 4, columns, ["1"])
    again, _ = svc.row_image("a.csv", "f" * 64, 4, columns, ["1"])
    other_row, _ = svc.row_image("a.csv", "f" * 64, 5, columns, ["1"])
    other_file, _ = svc.row_image("a.csv", "e" * 64, 4, columns, ["1"])
    assert svc.sha256_hex(one) == svc.sha256_hex(again)
    assert len({svc.sha256_hex(x) for x in (one, other_row, other_file)}) == 3


# ------------------------------------------------------------------ history

def test_history_lists_each_import_with_its_counts_and_serves_the_original(client):
    data = _csv("80001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework", "80001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework",
                "80003,bad,No. 57 Stone,20,T-1,Fictional Sitework")
    done = _commit(client, _import(client, data, name="october.csv")).json()
    pending = _upload(client, _csv("80009,11/03/2026,Sand,1,T,J"), name="not-yet.csv").json()
    history = client.get("/api/delivery-imports").json()
    assert [h["original_name"] for h in history] == ["not-yet.csv", "october.csv"]
    october = history[1]
    assert (october["rows"], october["imported"], october["skipped_duplicates"], october["error_rows"]) == (3, 1, 1, 1)
    assert october["batch_id"] == done["batch_id"] and october["created_by"] == "Test Owner" and october["status"] == "imported"
    assert history[0]["status"] == "uploaded" and history[0]["id"] == pending["id"]
    original = client.get(f"/api/delivery-imports/{done['id']}/file")
    assert original.status_code == 200 and original.content == data
    assert original.headers["content-disposition"] == 'attachment; filename="october.csv"'


def test_an_import_happens_once(client):
    imp = _import(client, _csv("81001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework"))
    assert _commit(client, imp).status_code == 200
    again = _commit(client, imp)
    assert again.status_code == 409 and "already been imported" in again.json()["detail"]
    remap = client.put(f"/api/delivery-imports/{imp['id']}/mapping", json={"columns": imp["mapping"], "unit": "ton"})
    assert remap.status_code == 409


def test_an_import_applies_only_the_preview_the_person_saw(client):
    imp = _import(client, _csv("82001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework"))
    _map(client, imp, unit="cy")
    stale = _commit(client, imp)
    assert stale.status_code == 409 and "changed since this preview" in stale.json()["detail"]


def test_an_unmapped_import_cant_be_committed(client):
    imp = _upload(client, _csv("83001,11/03/2026,No. 57 Stone,20,T-1,Fictional Sitework")).json()
    response = client.post(f"/api/delivery-imports/{imp['id']}/commit", json={})
    assert response.status_code == 409


# ------------------------------------------------------------------ files that can't be used

@pytest.mark.parametrize("name, data, message", [
    ("old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100, "Old Excel files"),
    ("fake.xlsx", b"Ticket,Date\n1,2\n", "isn't an Excel workbook"),
    ("scan.pdf", b"%PDF-1.4", "CSV or Excel"),
    ("empty.csv", b"", "empty"),
    ("binary.csv", b"PK\x03\x04rest", "isn't text"),
    ("broken.xlsx", b"PK\x03\x04" + b"\x00" * 64, "damaged"),
])
def test_files_that_arent_delivery_spreadsheets_are_refused(client, name, data, message):
    response = _upload(client, data, name)
    assert response.status_code == 400 and message in response.json()["detail"]
    with _session() as db:
        assert db.scalar(select(DeliveryImport.id)) is None


def test_size_and_row_limits(client, monkeypatch):
    monkeypatch.setattr(svc, "MAX_ROWS", 5)
    lines = [f"9{n:04d},11/03/2026,Sand,1,T,J" for n in range(30)]
    response = _upload(client, _csv(*lines))
    assert response.status_code == 400 and "more than 5 rows" in response.json()["detail"]
    monkeypatch.setattr(svc, "MAX_BYTES", 100)
    response = _upload(client, _csv(*lines[:3]))
    assert response.status_code == 400 and "MB" in response.json()["detail"]


def test_a_very_wide_sheet_is_refused():
    with pytest.raises(svc.SheetError, match="more than 60 columns"):
        svc.read_rows("csv", (",".join(f"c{n}" for n in range(70)) + "\n").encode())


def test_a_header_with_no_rows_under_it_is_refused(client):
    response = _upload(client, HEADER.encode())
    assert response.status_code == 400 and "no delivery rows" in response.json()["detail"]


def test_a_semicolon_csv_from_european_excel_is_read(client):
    data = "Ticket;Date;Material;Qty;Job\n84001;11/03/2026;Sand;12,5;Fictional\n".encode("cp1252")
    imp = _upload(client, data).json()
    assert [c["header"] for c in imp["columns"]] == ["Ticket", "Date", "Material", "Qty", "Job"]


# ------------------------------------------------------------------ the migration

def test_the_migration_adds_imports_to_a_database_from_before_them(tmp_path):
    import sqlite3

    from alembic import command

    from .migration_helpers import alembic_config

    path = tmp_path / "before-imports.db"
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "20261127_33")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO batches (id, organization_id, layout, intake_mode, status, created_at) "
                     "VALUES (1, 1, 'single', 'manual', 'ready_for_review', '2026-11-01')")
        assert "delivery_imports" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id, intake_mode FROM batches").fetchall() == [(1, "manual")]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(delivery_imports)")}
        assert {"organization_id", "storage_path", "sha256", "mapping", "batch_id", "imported", "skipped_duplicates",
                "error_rows"} <= columns
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("20261128_34",)]
    with pytest.raises(RuntimeError, match="Downgrades are not supported"):
        command.downgrade(cfg, "20261127_33")
