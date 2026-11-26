"""A new contractor's first hour: upload before anything else, and paste lists in at once.

No project, supplier or rate is needed before the first upload. A ticket approved with no
rate shows the rate as missing (cost None, counted as unpriced), never $0. Suppliers,
materials, haulers and projects can be pasted from a spreadsheet, a CSV or one per line:
previewed first, then applied, inside the signed-in company only. Fictional data only.
"""
import pytest

from app.auth import current_user
from app.main import app
from app.models import Hauler, Organization, Project, Supplier, User

from .sample_data import make_scan_pdf
from .test_query_budgets import _session


def _as(role: str, organization_id: int = 1, user_id: int = 1):
    app.dependency_overrides[current_user] = lambda: User(
        id=user_id, organization_id=organization_id, username=f"{role}-{organization_id}", display_name=role.title(),
        password_hash="fixture", active=True, role=role, must_change_password=False)


def _steps(client) -> dict[str, bool]:
    return {step["key"]: step["done"] for step in client.get("/api/onboarding").json()["steps"]}


# ------------------------------------------------------------------ upload before rates

def test_a_first_batch_goes_in_and_is_approved_before_any_project_supplier_or_rate(client):
    assert client.get("/api/projects").json() == [] and client.get("/api/rates").json() == []
    assert not any(done for key, done in _steps(client).items() if key != "company")

    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual"},
                        files=[("files", ("first-scans.pdf", make_scan_pdf(), "application/pdf"))])
    assert batch.status_code == 200, batch.text
    batch = batch.json()
    assert batch["project_id"] is None
    ticket = client.get(f"/api/batches/{batch['id']}/tickets").json()[0]
    edited = client.patch(f"/api/tickets/{ticket['id']}", json={
        "ticket_number": "70001", "delivery_date": "2026-11-02", "material_type": "No. 57 Stone", "tonnage": "21.40",
        "supplier": "Fictional Quarry Co", "job_location": "North Lot"}).json()
    assert edited["cost"] is None and edited["unit_price"] is None  # no rate yet: missing, not zero
    done = client.post(f"/api/tickets/{edited['id']}/verify", json={"confirm_duplicate": True},
                       headers={"If-Match": edited["revision"]})
    assert done.status_code == 200, done.text
    done = done.json()
    assert done["status"] == "verified" and done["price_snapshot_state"] == "unpriced"
    assert done["cost"] is None and done["cost"] != "0.00"
    assert any("No rate was available" in note for note in done["pricing_notes"])

    report = client.get("/api/reports/summary", params={"period": "month"}).json()
    assert report["costs"]["unpriced"] == 1  # shown as "approved without a rate (cost incomplete)"
    summary = client.get("/api/records/search").json()["summary"]
    assert summary["verified_unpriced"] == 1

    steps = _steps(client)
    assert steps["upload"] and steps["approve"] and not steps["catalog"] and not steps["project"]

    # The batch is filed under a project afterwards.
    project = client.post("/api/projects", json={"code": "26-001", "name": "Fictional Lot Paving"}).json()
    moved = client.patch(f"/api/batches/{batch['id']}/project", json={"project_id": project["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["project_id"] == project["id"] and _steps(client)["project"]


def test_the_checklist_reports_every_starting_step_in_order(client):
    body = client.get("/api/onboarding").json()
    assert [step["key"] for step in body["steps"]] == ["company", "project", "supplier", "catalog", "upload", "approve", "team", "invoice"]
    assert all(step["href"].startswith("/") and step["title"] and step["detail"] for step in body["steps"])
    assert body["complete"] is False
    assert _steps(client)["company"] is True  # "Test Company"
    with _session() as db:
        db.get(Organization, 1).name = "Original workspace"  # the placeholder from before companies existed
        db.commit()
    assert _steps(client)["company"] is False


def test_each_list_completes_its_own_step(client):
    client.post("/api/setup/import/suppliers", json={"text": "Fictional Quarry Co", "apply": True})
    assert _steps(client)["supplier"] and not _steps(client)["catalog"]
    client.post("/api/setup/import/projects", json={"text": "26-001\tFictional Lot Paving", "apply": True})
    assert _steps(client)["project"]


# ------------------------------------------------------------------ pasted lists

SUPPLIERS = "Supplier\tContact\nFictional Quarry Co\tdispatch@example.com\nSample Sand & Gravel\t\nfictional quarry co.\t\n\n"


def test_a_supplier_list_previews_without_saving(client):
    plan = client.post("/api/setup/import/suppliers", json={"text": SUPPLIERS})
    assert plan.status_code == 200, plan.text
    plan = plan.json()
    assert plan["applied"] is False and plan["adds"] == 2 and plan["repeated"] == 1 and plan["existing"] == 0
    assert [(r["line"], r["name"], r["action"]) for r in plan["rows"]] == [
        (2, "Fictional Quarry Co", "add"), (3, "Sample Sand & Gravel", "add"), (4, "fictional quarry co.", "repeated")]
    assert plan["rows"][0]["contact"] == "dispatch@example.com" and plan["rows"][2]["same_as_line"] == 2
    assert client.get("/api/suppliers").json() == []


def test_applying_a_supplier_list_adds_only_new_names_and_records_each(client):
    client.post("/api/suppliers", json={"name": "Sample Sand & Gravel"})
    result = client.post("/api/setup/import/suppliers", json={"text": SUPPLIERS, "apply": True}).json()
    assert result["applied"] is True and result["adds"] == 1 and result["existing"] == 1
    names = sorted(s["name"] for s in client.get("/api/suppliers").json())
    assert names == ["Fictional Quarry Co", "Sample Sand & Gravel"]
    added = [e for e in client.get("/api/pricing/history").json() if e["action"] == "supplier_added"]
    assert sorted(e["supplier"] for e in added) == ["Fictional Quarry Co", "Sample Sand & Gravel"]
    # Pasting it again changes nothing.
    again = client.post("/api/setup/import/suppliers", json={"text": SUPPLIERS, "apply": True}).json()
    assert again["adds"] == 0 and len(client.get("/api/suppliers").json()) == 2


def test_materials_one_per_line(client):
    result = client.post("/api/setup/import/materials", json={"text": "No. 57 Stone\nFill Sand\nScreened Topsoil\n", "apply": True}).json()
    assert result["adds"] == 3
    assert sorted(m["name"] for m in client.get("/api/materials").json()) == ["Fill Sand", "No. 57 Stone", "Screened Topsoil"]
    assert all("contact" not in row for row in result["rows"])


def test_haulers_from_a_csv_with_quotes(client):
    text = 'Hauler,Contact\n"Bluegrass Trucking, LLC",dispatch@example.com\nCrossroads Hauling,\n'
    result = client.post("/api/setup/import/haulers", json={"text": text, "apply": True})
    assert result.status_code == 200, result.text
    with _session() as db:
        haulers = sorted((h.name, h.contact) for h in db.query(Hauler).filter(Hauler.organization_id == 1))
    assert haulers == [("Bluegrass Trucking, LLC", "dispatch@example.com"), ("Crossroads Hauling", None)]


def test_projects_with_number_name_and_location(client):
    client.post("/api/projects", json={"code": "25-101", "name": "Route 9 Widening"})
    text = "Job #\tName\tLocation\n25-101\tRoute 9 Widening\t\n25-114\tNorthpark Sitework\tFictional Township\n"
    result = client.post("/api/setup/import/projects", json={"text": text, "apply": True}).json()
    assert [(r["code"], r["action"]) for r in result["rows"]] == [("25-101", "exists"), ("25-114", "add")]
    projects = {p["code"]: p for p in client.get("/api/projects").json()}
    assert set(projects) == {"25-101", "25-114"} and projects["25-114"]["job_location"] == "Fictional Township"


@pytest.mark.parametrize("route, text, expected", [
    ("projects", "25-200\n", "project's name"),
    ("projects", ("x" * 51) + "\tToo long\n", "project number"),
    ("suppliers", "...\n", "usable supplier name"),
    ("materials", "", "Paste at least one"),
    ("suppliers", "Name\n", "Paste at least one"),
    ("haulers", "\n".join(f"Hauler {n}" for n in range(501)), "at most 500"),
])
def test_a_list_with_a_problem_saves_nothing(client, route, text, expected):
    response = client.post(f"/api/setup/import/{route}", json={"text": text or " ", "apply": True})
    assert response.status_code == 422 and expected in response.text, response.text
    assert client.get("/api/projects").json() == [] and client.get("/api/suppliers").json() == []
    with _session() as db:
        assert db.query(Hauler).count() == 0


def test_a_name_saved_as_another_spelling_is_refused(client):
    supplier = client.post("/api/suppliers", json={"name": "Fictional Quarry Co"}).json()
    alias = client.post("/api/name-aliases", json={"kind": "supplier", "alias": "Fict. Quarry", "target_id": supplier["id"]})
    assert alias.status_code in (200, 201), alias.text
    response = client.post("/api/setup/import/suppliers", json={"text": "Fict. Quarry\nNew Supplier Co", "apply": True})
    assert response.status_code == 422 and "another spelling" in response.text
    assert len(client.get("/api/suppliers").json()) == 1


# ------------------------------------------------------------------ who may, and whose

@pytest.mark.parametrize("role, route, allowed", [
    ("clerk", "suppliers", False), ("clerk", "materials", False), ("clerk", "projects", False), ("clerk", "haulers", True),
    ("viewer", "haulers", False), ("viewer", "projects", False), ("manager", "suppliers", True), ("manager", "projects", True),
])
def test_each_list_needs_the_same_permission_as_adding_one(client, role, route, allowed):
    _as(role)
    text = "26-9\tFictional Job" if route == "projects" else "Fictional Name Co"
    response = client.post(f"/api/setup/import/{route}", json={"text": text})
    assert (response.status_code == 200) is allowed, (response.status_code, response.text)
    if not allowed:
        assert response.status_code == 403


def test_another_companys_names_are_neither_seen_nor_touched(client):
    with _session() as db:
        db.add(Organization(id=2, name="Other Fictional Co"))
        db.flush()
        db.add(Supplier(organization_id=2, name="Fictional Quarry Co"))
        db.add(Project(organization_id=2, code="26-001", name="Their Job"))
        db.add(Hauler(organization_id=2, name="Crossroads Hauling", name_key="crossroads hauling"))
        db.commit()
    for route, text in (("suppliers", "Fictional Quarry Co"), ("projects", "26-001\tOur Job"), ("haulers", "Crossroads Hauling")):
        result = client.post(f"/api/setup/import/{route}", json={"text": text, "apply": True}).json()
        assert [r["action"] for r in result["rows"]] == ["add"] and result["rows"][0]["existing"] is None
    with _session() as db:
        assert db.query(Supplier).filter(Supplier.organization_id == 2).count() == 1
        assert db.query(Project).filter(Project.organization_id == 2).one().name == "Their Job"
        assert db.query(Supplier).filter(Supplier.organization_id == 1).count() == 1
