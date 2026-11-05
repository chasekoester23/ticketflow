"""Finding weak points on purpose: hostile input, odd order, and many views of one truth."""
import io
import random
import threading
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.auth import current_user
from app.main import app
from app.models import AuditEvent, Base, Batch, Organization, Project, Ticket, UploadedFile, User
from app.permissions import PERMISSIONS
from app.route_inventory import MUTATING, SELF_SERVICE, all_routes

from .sample_data import TICKETS, make_page, make_pdf
from .synthetic_dataset import INVOICE_CASES, build, plant_invoices
from .test_query_budgets import _seed, _session


def _as(role, user_id=1, organization_id=1):
    app.dependency_overrides[current_user] = lambda: User(
        id=user_id, organization_id=organization_id, username="t", password_hash="x", active=True, role=role, must_change_password=False)


# ------------------------------------------------------------ authorization, by inventory

@pytest.mark.parametrize("role", ["viewer", "clerk", "manager"])
def test_every_mutating_route_refuses_a_role_without_its_permission(client, role):
    """Generated from the live route list, so a new route can't be forgotten."""
    _as(role)
    wrong = []
    checked = 0
    for route in all_routes():
        if route.method not in MUTATING or route.path in SELF_SERVICE:
            continue
        needed = [p for p in route.permissions if role not in PERMISSIONS[p]]
        if not needed:
            continue
        checked += 1
        url = route.path
        for token in ("{ticket_id}", "{batch_id}", "{line_id}", "{link_id}", "{document_id}", "{line}", "{sid}", "{mid}", "{rid}", "{pid}", "{alias_id}", "{view_id}", "{slot_id}", "{member_id}", "{user_id}"):
            url = url.replace(token, "1")
        import re
        url = re.sub(r"{[^}]+}", "1", url)
        response = client.request(route.method, url, json={})
        if response.status_code != 403:
            wrong.append(f"{route.method} {route.path} -> {response.status_code}")
    assert wrong == []
    if role != "manager":
        assert checked > 15
    else:
        assert checked >= 1


def _other_company_resources():
    with _session() as db:
        db.add(Organization(id=2, name="Another Company"))
        db.flush()
        db.add(Project(id=20, organization_id=2, code="X", name="Theirs"))
        db.flush()
        db.add(Batch(id=20, organization_id=2, project_id=20, layout="single", status="ready_for_review", intake_mode="manual"))
        db.flush()
        db.add(UploadedFile(id=20, batch_id=20, original_name="theirs.pdf", storage_path="x", sha256="9" * 64))
        db.flush()
        db.add(Ticket(id=20, batch_id=20, file_id=20, source_page=1, position=0, crop_box=[0, 0, 1, 1], crop_path="x", crop_sha256="9" * 64,
                      ticket_number="THEIRS", flags=[], status="needs_review"))
        db.commit()


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/api/batches/20", None), ("GET", "/api/batches/20/tickets", None), ("GET", "/api/batches/20/manifest", None),
    ("GET", "/api/batches/20/export.xlsx", None), ("PATCH", "/api/batches/20/project", {"project_id": 1}),
    ("GET", "/api/tickets/20/image", None), ("GET", "/api/tickets/20/audit", None),
    ("PATCH", "/api/tickets/20", {"tonnage": "1"}), ("POST", "/api/tickets/20/verify", None), ("POST", "/api/tickets/20/flag", None),
    ("POST", "/api/tickets/20/skip", None), ("POST", "/api/tickets/bulk-edit", {"ticket_ids": [20], "field": "supplier", "value": "x"}),
    ("GET", "/api/reports/project/20", None), ("GET", "/api/reports/pilot?project_id=20", None),
])
def test_another_companys_records_look_like_they_dont_exist(client, method, path, body):
    _seed(1)
    _other_company_resources()
    response = client.request(method, path, json=body) if body is not None else client.request(method, path)
    assert response.status_code == 404, f"{method} {path} -> {response.status_code}"
    with _session() as db:
        assert db.get(Ticket, 20).status == "needs_review" and db.get(Ticket, 20).tonnage is None


def test_rename_can_never_reach_another_company(client):
    _seed(1)
    _other_company_resources()
    with _session() as db:
        db.get(Ticket, 20).supplier = "Shared Name"
        db.commit()
    client.post("/api/suppliers", json={"name": "Catalog Name"})
    result = client.post("/api/records/rename", json={"field": "supplier", "from_value": "Shared Name", "to_value": "Catalog Name"}).json()
    assert result["changed"] == 0
    with _session() as db:
        assert db.get(Ticket, 20).supplier == "Shared Name"


# ------------------------------------------------------------ hostile input

@pytest.mark.parametrize("tons", ["NaN", "Infinity", "-Infinity", "-1", "1e400", "abc", "12,5", "0x10", "١٢"])
def test_unusable_tons_are_refused_and_change_nothing(client, tons):
    _seed(1)
    response = client.patch("/api/tickets/1", json={"tonnage": tons})
    if response.status_code == 200:
        # Pydantic accepts some numeric spellings. Whatever is stored must be a finite, non-negative number.
        stored = Decimal(response.json()["tonnage"])
        assert stored.is_finite() and stored >= 0
    else:
        assert response.status_code == 422
        assert Decimal(client.get("/api/batches/1/tickets").json()[0]["tonnage"]) == Decimal("15.000")


@pytest.mark.parametrize("value", [
    "<script>alert(1)</script>", "'; DROP TABLE tickets; --", "=cmd|'/c calc'!A1", "+1+1", "@SUM(1)", "‮evil", "数量🚚", "a" * 100,
])
def test_hostile_text_is_stored_as_text_and_inert_in_exports(client, value):
    _seed(1)
    response = client.patch("/api/tickets/1", json={"ticket_number": value, "supplier": value})
    assert response.status_code == 200
    assert response.json()["ticket_number"] == value
    assert client.get("/api/batches/1/tickets").status_code == 200
    assert _session().query(Ticket).count() == 1  # The table is still here.
    sheet = load_workbook(io.BytesIO(client.get("/api/records/export.xlsx").content))["Delivery Records"]
    cell = sheet.cell(2, 1)
    assert cell.data_type != "f"
    if value.startswith(("=", "+", "@")):
        assert cell.value.startswith("'")
    text = client.get("/api/records/export.csv").text
    assert "\n=" not in text and ",=" not in text


def test_overlong_values_are_refused_or_kept_whole_never_cut_silently(client):
    _seed(1)
    response = client.patch("/api/tickets/1", json={"ticket_number": "x" * 5000})
    if response.status_code == 200:
        assert response.json()["ticket_number"] == "x" * 5000
    else:
        assert response.status_code == 422


@pytest.mark.parametrize("name, payload", [
    ("../../../etc/passwd.pdf", b"%PDF-1.4 not really"),
    ("evil.pdf", b"MZ\x90\x00 an executable"),
    ("ticket.png", b"%PDF-1.4 wrong type"),
    ("empty.pdf", b""),
    ("script.html", b"<script>alert(1)</script>"),
])
def test_bad_uploads_never_store_outside_or_create_records(client, tmp_path, name, payload):
    from app.config import settings
    project = client.post("/api/projects", json={"code": "S", "name": "Fictional"}).json()
    response = client.post("/api/batches", data={"layout": "single", "intake_mode": "manual", "project_id": project["id"]},
                           files=[("files", (name, payload, "application/pdf"))])
    if response.status_code == 200:
        # A corrupt PDF with a valid signature is accepted, then reported as failed. Never zero tickets silently.
        batch = client.get(f"/api/batches/{response.json()['id']}").json()
        assert batch["status"] == "failed"
        stored = [p for p in settings.storage_dir.rglob("*") if p.is_file() and p.name not in ("worker.lock",)]
        assert all(str(p).startswith(str(settings.storage_dir)) for p in stored)
        assert all("passwd" not in p.name for p in stored)
    else:
        assert response.status_code == 400
        assert client.get("/api/batches").json() == []


def test_one_bad_file_rejects_the_whole_upload(client):
    project = client.post("/api/projects", json={"code": "S", "name": "Fictional"}).json()
    good = make_pdf([make_page(TICKETS)])
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": project["id"]},
                           files=[("files", ("good.pdf", good, "application/pdf")), ("files", ("bad.exe", b"MZ", "application/pdf"))])
    assert response.status_code == 400
    assert client.get("/api/batches").json() == []


def test_uploading_the_same_scan_twice_cant_be_approved_twice(client):
    project = client.post("/api/projects", json={"code": "S", "name": "Fictional"}).json()
    pdf = make_pdf([make_page(TICKETS)])
    for _ in range(2):
        client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": project["id"]},
                    files=[("files", ("scan.pdf", pdf, "application/pdf"))])
    tickets = [t for b in client.get("/api/batches").json() for t in client.get(f"/api/batches/{b['id']}/tickets").json()]
    assert len(tickets) == 8
    first, second = tickets[0], next(t for t in tickets[1:] if t["position"] == tickets[0]["position"] and t["batch_id"] != tickets[0]["batch_id"])
    values = dict(ticket_number="1", delivery_date="2026-10-15", material_type="Sand", tonnage="10", supplier="S", job_location="J")
    client.patch(f"/api/tickets/{first['id']}", json=values)
    client.patch(f"/api/tickets/{second['id']}", json=values)
    assert client.post(f"/api/tickets/{first['id']}/verify").status_code == 200
    assert client.post(f"/api/tickets/{second['id']}/verify").status_code == 409
    assert Decimal(client.get("/api/dashboard").json()["total_tons"]) == 10


# ------------------------------------------------------------ odd order

def test_approving_twice_doesnt_reprice_or_double_count(client):
    _seed(1)
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/materials", json={"name": "Sand"})
    rate = lambda value: client.put("/api/rates", json={  # noqa: E731
        "supplier_id": client.get("/api/suppliers").json()[0]["id"], "material_id": client.get("/api/materials").json()[0]["id"], "unit_price": value})
    rate("10")
    first = client.post("/api/tickets/1/verify").json()
    rate("99")
    second = client.post("/api/tickets/1/verify").json()
    assert first["unit_price"] == second["unit_price"] and Decimal(second["unit_price"]) == 10
    with _session() as db:
        assert db.query(AuditEvent).filter(AuditEvent.ticket_id == 1, AuditEvent.new_value == "verified").count() == 1


def test_real_threads_racing_to_approve_lock_one_price_and_one_audit_entry(tmp_path):
    """Double-clicks and two managers, on a real file database with real threads."""
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with Session() as db:
        db.add(Organization(id=1, name="Fictional"))
        db.flush()
        db.add(User(id=1, organization_id=1, username="a", password_hash="x", role="owner"))
        db.add(Batch(id=1, organization_id=1, layout="single", status="ready_for_review", intake_mode="manual"))
        db.flush()
        db.add(UploadedFile(id=1, batch_id=1, original_name="f.pdf", storage_path="x", sha256="0" * 64))
        db.flush()
        db.add(Ticket(id=1, batch_id=1, file_id=1, source_page=1, position=0, crop_box=[0, 0, 1, 1], crop_path="x", crop_sha256="0" * 64,
                      ticket_number="1", delivery_date=date(2026, 1, 1), material_type="Sand", supplier="S", job_location="J",
                      tonnage=Decimal("10"), flags=[], status="needs_review"))
        db.commit()
    from fastapi.testclient import TestClient

    def override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[db_module.get_db] = override
    app.dependency_overrides[current_user] = lambda: User(id=1, organization_id=1, username="a", password_hash="x", active=True, role="owner", must_change_password=False)
    client = TestClient(app, headers={"X-TicketFlow-Request": "1"})
    revision = client.get("/api/batches/1/tickets").json()[0]["revision"]
    results = []

    def approve():
        results.append(client.post("/api/tickets/1/verify", headers={"If-Match": revision}).status_code)

    threads = [threading.Thread(target=approve) for _ in range(8)]
    try:
        [t.start() for t in threads]
        [t.join() for t in threads]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
    assert 200 in results and set(results) <= {200, 409}
    with Session() as db:
        ticket = db.get(Ticket, 1)
        assert ticket.status == "verified"
        approvals = db.query(AuditEvent).filter(AuditEvent.ticket_id == 1, AuditEvent.field == "status", AuditEvent.new_value == "verified").count()
    assert approvals == results.count(200) and approvals >= 1


def test_batch_moved_while_invoice_exists_updates_the_comparison_honestly(client):
    _seed(2)
    client.post("/api/tickets/1/verify")
    client.post("/api/tickets/2/verify")
    line = client.post("/api/invoice-comparisons", json={
        "project_id": 1, "invoice_number": "I", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-30", "billed_tons": "30.01", "billed_unit_price": "1", "billed_subtotal": "30.01"}).json()
    assert Decimal(line["verified_tons"]) == Decimal("30.01")
    with _session() as db:
        db.add(Project(id=7, organization_id=1, code="P7", name="Other"))
        db.commit()
    assert client.patch("/api/batches/1/project", json={"project_id": 7}).status_code == 200
    after = client.get(f"/api/invoice-comparisons/{line['id']}").json()
    assert Decimal(after["verified_tons"]) == 0
    assert after["status"] == "no_verified_tickets"


# ------------------------------------------------------------ one truth, many views

def _views_agree(client):
    tickets = client.get("/api/records", params={"status": "verified"}).json()
    verified = [t for t in tickets if t["tonnage_confirmed"]]
    tons = sum((Decimal(t["tonnage"]) for t in verified if t["tonnage"] is not None), Decimal(0))
    cost = sum((Decimal(t["cost"]) for t in verified if t["cost"] is not None), Decimal(0))
    dashboard = client.get("/api/dashboard").json()
    assert Decimal(dashboard["total_tons"]) == tons
    assert Decimal(dashboard["total_cost"]) == cost
    summary = client.get("/api/records/search", params={"page_size": 1}).json()["summary"]
    assert Decimal(summary["verified_tons"]) == tons
    assert Decimal(summary["verified_cost"]) == cost
    report = client.get("/api/reports/project/1").json()["totals"]
    assert Decimal(report["verified_tons"]) == tons
    assert Decimal(report["cost"]) == cost
    pilot = client.get("/api/reports/pilot").json()
    assert Decimal(pilot["review"]["verified_tons"]) == tons
    sheet = load_workbook(io.BytesIO(client.get("/api/records/export.xlsx").content))["Summary"]
    values = {row[0].value: row[1].value for row in sheet.iter_rows() if row[0].value}
    assert Decimal(str(values["Verified tons"])).quantize(Decimal("0.001")) == tons.quantize(Decimal("0.001"))
    assert Decimal(str(values["Total calculated material cost"])).quantize(Decimal("0.01")) == cost.quantize(Decimal("0.01"))
    # Every approval is attributable and has a price state, whatever happened on the way.
    for ticket in verified:
        assert ticket["verified_by"] == "Test Owner"
        assert ticket["price_snapshot_state"] in ("priced", "unpriced")


@pytest.mark.parametrize("seed", range(12))
def test_every_total_agrees_after_any_sequence_of_actions(client, seed):
    """Random people doing random things in random order. Every report must agree
    with the others and with the records underneath."""
    rng = random.Random(seed)
    _seed(25)
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/materials", json={"name": "Sand"})
    supplier = client.get("/api/suppliers").json()[0]["id"]
    material = client.get("/api/materials").json()[0]["id"]
    client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "10"})

    locked: dict[int, str] = {}
    for _ in range(150):
        ticket_id = rng.randint(1, 25)
        action = rng.choice(["verify", "verify", "verify", "flag", "skip", "edit_tons", "edit_name", "rate", "bulk", "override"])
        if action in ("verify", "flag", "skip"):
            response = client.post(f"/api/tickets/{ticket_id}/{action}")
            assert response.status_code in (200, 409, 422), response.text
            if action == "verify" and response.status_code == 200:
                locked[ticket_id] = response.json()["unit_price"]
            else:
                locked.pop(ticket_id, None) if response.status_code == 200 else None
        elif action == "edit_tons":
            client.patch(f"/api/tickets/{ticket_id}", json={"tonnage": str(Decimal(rng.randint(1000, 3000)) / 100)})
            locked.pop(ticket_id, None)
        elif action == "edit_name":
            client.patch(f"/api/tickets/{ticket_id}", json={"supplier": rng.choice(["Example Aggregates", "Other Supplier"])})
            locked.pop(ticket_id, None)
        elif action == "override":
            client.patch(f"/api/tickets/{ticket_id}", json={"unit_price_override": str(rng.randint(5, 50))})
            locked.pop(ticket_id, None)
        elif action == "rate":
            client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": str(rng.randint(5, 50))})
        else:
            client.post("/api/tickets/bulk-edit", json={"ticket_ids": [ticket_id, rng.randint(1, 25)], "field": "job_location", "value": "Bulk"})
    _views_agree(client)
    # Approved tickets kept the price they locked, through every later rate change.
    current = {t["id"]: t for t in client.get("/api/batches/1/tickets").json()}
    for ticket_id, price in locked.items():
        if current[ticket_id]["status"] == "verified":
            assert current[ticket_id]["unit_price"] == price


# ------------------------------------------------------------ planted mistakes

@pytest.fixture()
def planted(client):
    with _session() as db:
        key = build(db, 1, tickets=600, per_batch=100)
        plant_invoices(db, 1, key)
    return key


def test_every_planted_invoice_mistake_gets_the_right_result(client, planted):
    lines = {line["id"]: line for line in client.get("/api/invoice-comparisons").json()}
    wrong = {}
    for line_id, case in planted.invoices.items():
        if case.startswith("_"):
            continue
        if lines[line_id]["status"] != INVOICE_CASES[case]:
            wrong[case] = lines[line_id]["status"]
    assert wrong == {}
    assert set(INVOICE_CASES) == {c for c in planted.invoices.values() if not c.startswith("_")}


def test_every_planted_mistake_is_explained_and_adds_up(client, planted):
    expected_kind = {
        "ticket_never_delivered": "not_found", "tons_inflated": "tons_differ", "ticket_billed_twice": "repeated",
        "delivery_left_off": "not_on_invoice", "leading_zeros_stripped": "not_found", "ticket_from_other_project": "outside_scope",
        "delivery_still_in_review": "unverified",
    }
    lines = {line["id"]: line for line in client.get("/api/invoice-comparisons").json()}
    for line_id, case in planted.invoices.items():
        bridge = lines[line_id]["diagnosis"]["tons"]
        if bridge:
            total = Decimal(bridge["start"]) + sum(Decimal(i["tons"]) for i in bridge["items"]) + Decimal(bridge["unexplained"])
            assert total == Decimal(bridge["billed"]), case
        if case in expected_kind:
            assert expected_kind[case] in {i["kind"] for i in bridge["items"]}, case
    by_case = {case: lines[line_id] for line_id, case in planted.invoices.items()}
    rate = by_case["rate_too_high"]["diagnosis"]["money"]
    assert Decimal(rate["rate_effect"]) == Decimal("63.75") and Decimal(rate["quantity_effect"]) == 0
    math = by_case["bad_multiplication"]["diagnosis"]["money"]
    assert Decimal(math["arithmetic_effect"]) == Decimal("25.00")
    assert "weren't checked" in by_case["totals_only_clean"]["diagnosis"]["headline"]


def test_the_earlier_invoice_shows_up_as_a_billing_pattern(client, planted):
    found = client.get("/api/invoice-comparisons/overview").json()["exceptions"]
    assert any(e["kind"] == "ticket_on_two_invoices" and "PL-earlier" in e["detail"] for e in found)


def test_misspelled_suppliers_are_found_by_name_cleanup(client, planted):
    assert planted.misspelled_tickets > 0
    rows = client.get("/api/records/name-variants", params={"field": "supplier"}).json()["rows"]
    typos = {r["value"]: r for r in rows}
    assert typos["Examle Aggregates"]["state"] == "unknown"
    assert typos["Examle Aggregates"]["target"] == "Example Aggregates"
    assert typos["EXAMPLE AGGREGATES"]["state"] == "same_name"


def test_a_large_company_stays_consistent_across_views(client, planted):
    dashboard = client.get("/api/dashboard").json()
    summary = client.get("/api/records/search").json()["summary"]
    assert Decimal(dashboard["total_tons"]) == Decimal(summary["verified_tons"])
    assert Decimal(dashboard["total_cost"]) == Decimal(summary["verified_cost"])


def test_seeded_data_is_repeatable(tmp_path):
    def counts(seed):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            db.add(Organization(id=1, name="F"))
            db.commit()
            key = build(db, 1, tickets=100, seed=seed, per_batch=50)
            return key.misspelled_tickets, db.query(Ticket).filter(Ticket.status == "verified").count()
    assert counts(5) == counts(5)
    assert counts(5) != counts(6)
