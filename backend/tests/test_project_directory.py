"""The project directory: every project with its open work, verified quantities and costs.
Missing prices are counted, never shown as a zero cost. Fictional data."""
from .sample_data import TICKETS, make_scan_pdf
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)


def _deliveries(client, project, count=2, tons="20.00"):
    papers = [{**TICKETS[0], "ticket_number": f"D{n}"} for n in range(count)]
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(project)},
                        files=[("files", ("d.pdf", make_scan_pdf(papers), "application/pdf"))]).json()
    tickets = client.get(f"/api/batches/{batch['id']}/tickets").json()
    edited = []
    for n, ticket in enumerate(tickets[:count]):
        edited.append(client.patch(f"/api/tickets/{ticket['id']}", json={
            "ticket_number": f"D{project}-{n}", "tonnage": tons, "supplier": "Example Aggregates", "material_type": "No. 57 Stone"}).json())
    return tickets, edited


def test_directory_counts_open_work_verified_tons_and_costs_apart(client, yard):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    material = client.post("/api/materials", json={"name": "No. 57 Stone"}).json()["id"]
    client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "18.00",
                                   "project_id": yard["project"]})
    tickets, edited = _deliveries(client, yard["project"])
    assert client.post(f"/api/tickets/{edited[0]['id']}/verify", headers={"If-Match": edited[0]["revision"]}).status_code == 200
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    _shift(client, yard, truck=1)  # waiting for approval

    rows = {row["code"]: row for row in client.get("/api/projects/directory").json()}
    p1 = rows["P1"]
    assert p1["verified_tickets"] == 1 and p1["verified_tons"] == "20.000"
    assert p1["material_cost"] == "360.00" and p1["unpriced_tickets"] == 0
    assert p1["trucking_cost"] == "990.00" and p1["verified_cost"] == "1350.00"
    assert p1["open_tickets"] == len(tickets) - 1 and p1["open_shifts"] == 1
    assert p1["last_activity"] is not None and p1["status"] == "active"
    assert rows["P2"]["tickets"] == 0 and rows["P2"]["verified_cost"] == "0.00" and rows["P2"]["last_activity"] is None
    assert list(rows)[0] == "P1"  # most recently worked on first


def test_an_approved_ticket_without_a_rate_is_counted_as_unpriced_not_as_free(client, yard):
    _, edited = _deliveries(client, yard["other"], count=1)
    assert client.post(f"/api/tickets/{edited[0]['id']}/verify", headers={"If-Match": edited[0]["revision"]}).status_code == 200
    row = next(r for r in client.get("/api/projects/directory").json() if r["code"] == "P2")
    assert row["verified_tons"] == "20.000" and row["unpriced_tickets"] == 1 and row["material_cost"] == "0.00"


def test_archived_projects_are_listed_after_active_ones(client, yard):
    client.patch(f"/api/projects/{yard['project']}", json={"status": "archived"})
    rows = client.get("/api/projects/directory").json()
    assert [(r["code"], r["status"]) for r in rows] == [("P2", "active"), ("P1", "archived")]
