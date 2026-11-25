"""Cost reports: approved material and trucking by period, supplier, material and hauler.
A missing price is counted, never reported as a zero cost. Fictional data."""
from io import BytesIO

from openpyxl import load_workbook

from .sample_data import TICKETS, make_scan_pdf
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)


def _deliveries(client, project, rows):
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(project)},
                        files=[("files", ("d.pdf", make_scan_pdf([TICKETS[0]] * len(rows)), "application/pdf"))]).json()
    for ticket, (number, day, supplier, material, tons, approve) in zip(client.get(f"/api/batches/{batch['id']}/tickets").json(), rows):
        edited = client.patch(f"/api/tickets/{ticket['id']}", json={"ticket_number": number, "delivery_date": day, "supplier": supplier,
                                                                      "material_type": material, "tonnage": tons}).json()
        if approve:
            assert client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]}).status_code == 200


def test_report_adds_approved_material_and_trucking_by_week_and_counts_missing_prices(client, yard):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    material = client.post("/api/materials", json={"name": "No. 57 Stone"}).json()["id"]
    client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "18.00"})
    _deliveries(client, yard["project"], [
        ("A1", "2026-11-02", "Example Aggregates", "No. 57 Stone", "20.00", True),   # Monday, week of 11-02
        ("A2", "2026-11-04", "Example Aggregates", "No. 57 Stone", "10.50", True),
        ("A3", "2026-11-10", "Other Quarry", "Rip Rap", "15.00", True),             # approved with no rate
        ("A4", "2026-11-10", "Example Aggregates", "No. 57 Stone", "30.00", False),  # waiting: not counted
    ])
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")  # 2026-11-03, $990

    report = client.get("/api/reports/summary", params={"project_id": yard["project"]}).json()
    m = report["material"]
    assert (m["tickets"], m["tons"], m["cost"], m["unpriced"], m["waiting"]) == (3, "45.500", "549.00", 1, 1)
    assert report["costs"] == {"material": "549.00", "trucking": "990.00", "total": "1539.00", "complete": False, "unpriced": 1,
                               "hauling_rate_missing": 0}
    weeks = {p["period"]: p for p in report["by_period"]}
    assert weeks["2026-11-02"]["tons"] == "30.500" and weeks["2026-11-02"]["material"] == "549.00" and weeks["2026-11-02"]["trucking"] == "990.00"
    assert weeks["2026-11-09"]["unpriced"] == 1 and weeks["2026-11-09"]["material"] == "0.00"
    assert report["trucking"]["by_hauler"] == [{"name": "Fictional Hauling LLC", "shifts": 1, "hours": "9.00", "cost": "990.00", "loads": 0, "deliveries": 0,
                                                 "per_hour": "110.00"}]
    [job] = report["by_project"]
    assert (job["code"], job["tons"], job["material"], job["unpriced"], job["trucking"], job["total"]) == ("P1", "45.500", "549.00", 1, "990.00", "1539.00")
    # Average unit price from priced tons only: the unpriced Rip Rap has none to show.
    assert [(u["supplier"], u["material"], u["tons"], u["average"]) for u in report["unit_prices"]] == [
        ("Example Aggregates", "No. 57 Stone", "30.500", "18.00")]
    assert {r["name"]: r["unpriced"] for r in m["by_supplier"]} == {"Example Aggregates": 0, "Other Quarry": 1}

    first_week = client.get("/api/reports/summary", params={"project_id": yard["project"], "date_to": "2026-11-08", "period": "day"}).json()
    assert first_week["material"]["tons"] == "30.500" and [p["period"] for p in first_week["by_period"]] == ["2026-11-02", "2026-11-03", "2026-11-04"]


def test_report_rejects_bad_input_and_other_companies_projects(client):
    assert client.get("/api/reports/summary", params={"period": "year"}).status_code == 422
    assert client.get("/api/reports/summary", params={"project_id": 99999}).status_code == 404
    assert client.get("/api/reports/summary", params={"date_from": "2026-11-10", "date_to": "2026-11-01"}).status_code == 422


def test_report_downloads_as_a_workbook_with_one_sheet_per_report(client, yard):
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    response = client.get("/api/reports/summary.xlsx", params={"project_id": yard["project"], "period": "month"})
    assert response.status_code == 200 and "quarvera-cost-report.xlsx" in response.headers["content-disposition"]
    book = load_workbook(BytesIO(response.content))
    assert book.sheetnames == ["Summary", "Material by material", "Material by supplier", "Trucking by hauler", "By month", "Invoices"]
    assert [c.value for c in book["Trucking by hauler"][2]] == ["Fictional Hauling LLC", 1, 9.0, 0, 990.0]
