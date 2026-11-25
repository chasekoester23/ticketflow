"""One approved activity, the same figure everywhere.

A $500 delivery and a $990 truck shift are approved once. Every surface that reports
cost (Overview, project directory, cost reports, the project report, the records list
and its Excel export) must show exactly those amounts. Receiving the matching supplier
and trucking invoices must not add a cent: invoices are compared with approved records,
never counted as expenses. Fictional data.
"""
from decimal import Decimal
from io import BytesIO

from openpyxl import load_workbook

from .sample_data import TICKETS, make_scan_pdf
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)


def _approved_delivery(client, project):
    supplier = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    material = client.post("/api/materials", json={"name": "No. 57 Stone"}).json()["id"]
    client.put("/api/rates", json={"supplier_id": supplier, "material_id": material, "unit_price": "20.00"})
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(project)},
                        files=[("files", ("d.pdf", make_scan_pdf([TICKETS[0]]), "application/pdf"))]).json()
    ticket = client.get(f"/api/batches/{batch['id']}/tickets").json()[0]
    edited = client.patch(f"/api/tickets/{ticket['id']}", json={"ticket_number": "50001", "delivery_date": "2026-11-03",
                                                                  "supplier": "Example Aggregates", "material_type": "No. 57 Stone",
                                                                  "tonnage": "25.00"}).json()
    assert client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]}).status_code == 200
    return edited["id"]


def _figures(client, project):
    dashboard = client.get("/api/dashboard", params={"project_id": project}).json()
    company = client.get("/api/dashboard").json()
    directory = next(r for r in client.get("/api/projects/directory").json() if r["id"] == project)
    report = client.get("/api/reports/summary", params={"project_id": project}).json()
    project_report = client.get(f"/api/reports/project/{project}").json()
    records = client.get("/api/records/search", params={"project_id": project, "status": "verified"}).json()["summary"]
    return {
        "material": {dashboard["total_cost"], company["total_cost"], directory["material_cost"], report["costs"]["material"],
                     str(Decimal(project_report["totals"]["cost"]).quantize(Decimal("0.01"))), str(Decimal(records["verified_cost"]).quantize(Decimal("0.01")))},
        "trucking": {dashboard["hauling"]["approved_cost"], company["hauling"]["approved_cost"], directory["trucking_cost"], report["costs"]["trucking"]},
        "total": {dashboard["hauling"]["approved_with_material"], directory["verified_cost"], report["costs"]["total"]},
    }


def test_one_approved_activity_reports_the_same_everywhere_and_invoices_add_nothing(client, yard):
    _approved_delivery(client, yard["project"])
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    before = _figures(client, yard["project"])
    assert before == {"material": {"500.00"}, "trucking": {"990.00"}, "total": {"1490.00"}}

    # The supplier bills the delivery, and the hauler bills the shift: comparisons, not new costs.
    line = client.post("/api/invoice-comparisons", json={
        "project_id": yard["project"], "invoice_number": "INV-1", "supplier_name": "Example Aggregates", "material_name": "No. 57 Stone",
        "date_from": "2026-11-03", "date_to": "2026-11-03", "billed_tons": "25.000", "billed_unit_price": "20.0000",
        "billed_subtotal": "500.00", "ticket_list": "50001 25.00"})
    assert line.status_code == 201, line.text
    hauling = client.post("/api/hauling-invoices", json={
        "hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": "H-1", "period_from": "2026-11-01",
        "period_to": "2026-11-30", "lines": [{"kind": "hours", "truck_identifier": "T-12", "work_date": "2026-11-03", "hours": "9",
                                              "rate": "110", "amount": "990.00"}]})
    assert hauling.status_code == 201, hauling.text
    assert _figures(client, yard["project"]) == before

    register = client.get("/api/invoice-comparisons/register", params={"project_id": yard["project"]}).json()
    assert {(r["kind"], r["billed"], r["compared"], r["status"]) for r in register["invoices"]} == {
        ("supplier", "500.00", "500.00", "matched"), ("trucking", "990.00", "990.00", "matched")}
    # The cost report shows invoices as their own section, beside costs, never inside them.
    report = client.get("/api/reports/summary", params={"project_id": yard["project"]}).json()
    assert report["invoices"]["billed"] == "1490.00" and report["costs"]["total"] == "1490.00"


def test_the_excel_exports_carry_the_same_amounts(client, yard):
    _approved_delivery(client, yard["project"])
    _approve(client, _shift(client, yard), work_hours="9", standby_hours="0")
    records = load_workbook(BytesIO(client.get("/api/records/export.xlsx", params={"project_id": yard["project"]}).content))
    sheet = records["Delivery Records"]
    header = [c.value for c in sheet[1]]
    assert sheet.cell(row=2, column=header.index("Calculated Cost") + 1).value == 500
    report = load_workbook(BytesIO(client.get("/api/reports/summary.xlsx", params={"project_id": yard["project"]}).content))["Summary"]
    values = {row[0].value: row[1].value for row in report.iter_rows() if row and row[0].value}
    assert (values["Material cost"], values["Trucking cost"], values["Total verified cost"]) == (500, 990, 1490)
    book = load_workbook(BytesIO(client.get(f"/api/reports/project/{yard['project']}/report.xlsx").content))
    summary = {row[0].value: row[1].value for row in book["Summary"].iter_rows() if row and row[0].value}
    assert summary["Material cost of approved tickets"] == 500 and summary["Combined approved cost (material + hauling)"] == 1490
    delivered = list(book["Approved deliveries"].iter_rows(min_row=2, values_only=True))
    assert len(delivered) == 1 and delivered[0][6] == 500
    assert [c.value for c in book["Trucking by hauler"][2]][:5] == ["Fictional Hauling LLC", 1, 9, 0, 990]


def test_unapproved_activity_and_missing_rates_never_enter_verified_totals(client, yard):
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(yard["project"])},
                        files=[("files", ("d.pdf", make_scan_pdf([TICKETS[0], TICKETS[1]]), "application/pdf"))]).json()
    waiting, unpriced = client.get(f"/api/batches/{batch['id']}/tickets").json()[:2]
    client.patch(f"/api/tickets/{waiting['id']}", json={"tonnage": "30.00", "supplier": "Example Aggregates", "material_type": "No. 57 Stone"})
    edited = client.patch(f"/api/tickets/{unpriced['id']}", json={"tonnage": "12.00", "supplier": "Nobody Quarry", "material_type": "Rip Rap",
                                                                    "delivery_date": "2026-11-04"}).json()
    assert client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]}).status_code == 200
    _shift(client, yard)  # entered, not approved
    figures = _figures(client, yard["project"])
    assert figures == {"material": {"0.00"}, "trucking": {"0.00"}, "total": {"0.00"}}
    directory = next(r for r in client.get("/api/projects/directory").json() if r["id"] == yard["project"])
    assert directory["unpriced_tickets"] == 1 and directory["verified_tons"] == "12.000"  # counted as missing, not as $0 of cost
    assert client.get("/api/reports/summary", params={"project_id": yard["project"]}).json()["costs"]["complete"] is False
