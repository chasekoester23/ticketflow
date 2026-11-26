"""Units of measure: tons, cubic yards, loads, each and gallons are each their own
quantity. Pounds convert to tons exactly and stay on the record. Nothing converts
cubic yards or loads to weight, nothing adds them to tons, and a rate prices only its
own unit (services/units.py). Fictional data."""
import csv
import io
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy.exc import DBAPIError

from app.models import Ticket
from app.services import units

from .sample_data import TICKETS, make_scan_pdf
from .test_query_budgets import _session
from .test_trucking_api import yard  # noqa: F401  (fixture)
from .test_trucking_billing import _haul_rate


def _catalog(client, material="No. 57 Stone", price="20.00", unit=None, project=None):
    suppliers = {s["name"]: s["id"] for s in client.get("/api/suppliers").json()}
    supplier = suppliers.get("Example Aggregates") or client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    materials = {m["name"]: m["id"] for m in client.get("/api/materials").json()}
    material_id = materials.get(material) or client.post("/api/materials", json={"name": material}).json()["id"]
    body = {"supplier_id": supplier, "material_id": material_id, "unit_price": price, **({"unit": unit} if unit else {}),
            **({"project_id": project} if project else {})}
    response = client.put("/api/rates", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _ticket(client, yard, *, number="80001", quantity="25.00", unit=None, material="No. 57 Stone", approve=True, hauler=False,
            project="project", **extra):
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(yard[project])},
                        files=[("files", ("d.pdf", make_scan_pdf([{**TICKETS[0], "ticket_number": number}]), "application/pdf"))]).json()
    ticket = client.get(f"/api/batches/{batch['id']}/tickets").json()[0]
    body = {"ticket_number": number, "delivery_date": "2026-11-03", "supplier": "Example Aggregates", "material_type": material,
            "job_location": "Fictional Sitework", "tonnage": quantity, **({"unit": unit} if unit else {}),
            **({"hauler_id": yard["hauler"], "truck_number": "T-12"} if hauler else {}), **extra}
    edited = client.patch(f"/api/tickets/{ticket['id']}", json=body)
    assert edited.status_code == 200, edited.text
    edited = edited.json()
    if not approve:
        return edited
    verified = client.post(f"/api/tickets/{edited['id']}/verify", headers={"If-Match": edited["revision"]})
    assert verified.status_code == 200, verified.text
    return verified.json()


# ------------------------------------------------------------------ the arithmetic

@pytest.mark.parametrize("pounds, tons", [("44300", "22.150"), ("2000", "1.000"), ("1", "0.001"), ("3", "0.002"), ("47911", "23.956")])
def test_pounds_are_exact_short_tons_rounded_half_up_to_the_thousandth(pounds, tons):
    assert units.pounds_to_tons(Decimal(pounds)) == Decimal(tons)


def test_the_net_weight_check_flags_only_a_real_disagreement():
    ok = units.net_check(Decimal("22.150"), "ton", Decimal("80000"), Decimal("35700"), "lb")
    assert ok is None
    off = units.net_check(Decimal("24.000"), "ton", Decimal("80000"), Decimal("35700"), "lb")
    assert "22.150 t" in off and "24.000 t" in off
    assert "tare is more than gross" in units.net_check(Decimal("1"), "ton", Decimal("10"), Decimal("11"), "ton")
    assert units.net_check(Decimal("12"), "cy", Decimal("80000"), Decimal("1"), "lb") is None  # yards aren't a weight


# ------------------------------------------------------------------ entering a ticket

def test_a_ticket_written_in_pounds_is_saved_in_tons_and_keeps_its_pounds(client, yard):
    _catalog(client)
    ticket = _ticket(client, yard, quantity="44300", unit="lb")
    assert (ticket["tonnage"], ticket["unit"], ticket["source_quantity"], ticket["source_unit"]) == ("22.150", "ton", "44300.000", "lb")
    assert ticket["cost"] == "443.00"  # 22.15 t x $20.00, priced as tons
    history = client.get(f"/api/tickets/{ticket['id']}/audit").json()
    assert any(e["field"] == "source_quantity" and e["new"] == "44300.000" for e in history)


def test_retyping_the_quantity_in_tons_clears_the_pounds_it_came_from(client, yard):
    ticket = _ticket(client, yard, quantity="44300", unit="lb", approve=False)
    retyped = client.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": "22.00"}, headers={"If-Match": ticket["revision"]}).json()
    assert (retyped["tonnage"], retyped["unit"], retyped["source_quantity"], retyped["source_unit"]) == ("22.000", "ton", None, None)


def test_pounds_need_a_figure_to_convert(client, yard):
    ticket = _ticket(client, yard, approve=False)
    client.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": None})
    assert client.patch(f"/api/tickets/{ticket['id']}", json={"unit": "lb"}).status_code == 422


def test_an_unknown_unit_is_refused(client, yard):
    ticket = _ticket(client, yard, approve=False)
    assert client.patch(f"/api/tickets/{ticket['id']}", json={"unit": "tonne"}).status_code == 422
    assert client.patch(f"/api/tickets/{ticket['id']}", json={"unit": "kg"}).status_code == 422


def test_scale_weights_that_disagree_with_the_tons_are_flagged_but_a_person_may_approve(client, yard):
    _catalog(client)
    ticket = _ticket(client, yard, quantity="24.00", approve=False, gross_weight="80000", tare_weight="35700", weight_unit="lb")
    assert any(f.startswith("Weights: gross minus tare is 22.150 t") for f in ticket["flags"])
    approved = client.post(f"/api/tickets/{ticket['id']}/verify", headers={"If-Match": ticket["revision"]})
    assert approved.status_code == 200  # a warning: the person checked the scan and approved the right figure
    assert approved.json()["tonnage"] == "24.000"  # the weights never replace the quantity


def test_tare_above_gross_blocks_approval(client, yard):
    ticket = _ticket(client, yard, quantity="10.00", approve=False, gross_weight="20", tare_weight="30", weight_unit="ton")
    refused = client.post(f"/api/tickets/{ticket['id']}/verify", headers={"If-Match": ticket["revision"]})
    assert refused.status_code == 422 and "tare is more than gross" in refused.text


def test_loads_must_be_whole(client, yard):
    ticket = _ticket(client, yard, quantity="1.5", unit="load", approve=False)
    assert "Loads must be a whole number" in ticket["flags"]
    assert client.post(f"/api/tickets/{ticket['id']}/verify", headers={"If-Match": ticket["revision"]}).status_code == 422


def test_a_large_quantity_in_another_unit_isnt_called_implausible_tons(client, yard):
    ticket = _ticket(client, yard, quantity="5000", unit="gal", approve=False)
    assert not any("Tonnage" in f for f in ticket["flags"])


def test_a_missing_quantity_in_another_unit_says_quantity(client, yard):
    ticket = _ticket(client, yard, unit="cy", approve=False)
    cleared = client.patch(f"/api/tickets/{ticket['id']}", json={"tonnage": None}).json()
    assert "Quantity missing" in cleared["flags"]


# ------------------------------------------------------------------ pricing

def test_a_per_yard_rate_prices_yards_and_never_tons(client, yard):
    _catalog(client, material="Topsoil", price="32.00", unit="cy")
    yards = _ticket(client, yard, number="80001", quantity="12.00", unit="cy", material="Topsoil")
    assert (yards["unit_price"], yards["cost"]) == ("32.0000", "384.00")
    tons = _ticket(client, yard, number="80002", quantity="12.00", material="Topsoil", approve=False)
    assert tons["unit_price"] is None
    assert any("per cubic yard" in n and "measured in tons" in n for n in tons["pricing_notes"])


def test_a_unit_mismatch_never_falls_back_to_another_agreement(client, yard):
    # Company price per ton, project agreement per cubic yard: a ton ticket on the project
    # is unpriced with the reason, not quietly priced at the company's per-ton list price.
    _catalog(client, price="20.00")
    _catalog(client, price="30.00", unit="cy", project=yard["project"])
    ticket = _ticket(client, yard, approve=False)
    assert ticket["unit_price"] is None
    assert any("project agreement" in n and "per cubic yard" in n for n in ticket["pricing_notes"])


def test_changing_a_rates_unit_is_on_the_pricing_history(client, yard):
    _catalog(client)
    _catalog(client, unit="cy")
    rows = client.get("/api/pricing/history").json()
    assert any(e["action"] == "unit_changed" and e["old"] == "per ton" and e["new"] == "per cubic yard" for e in rows)
    assert client.get("/api/rates").json()[0]["unit"] == "cy"


def test_saving_a_price_without_a_unit_keeps_the_unit_it_had(client, yard):
    _catalog(client, unit="cy")
    _catalog(client, price="31.00")
    [rate] = client.get("/api/rates").json()
    assert (rate["unit"], rate["unit_price"]) == ("cy", "31.0000")


# ------------------------------------------------------------------ totals never mix units

def test_tons_are_never_added_to_yards_anywhere(client, yard):
    _catalog(client, price="20.00")
    _catalog(client, material="Topsoil", price="32.00", unit="cy")
    _ticket(client, yard, number="80001", quantity="25.00")
    _ticket(client, yard, number="80002", quantity="12.00", unit="cy", material="Topsoil")
    yards = [{"unit": "cy", "quantity": "12.000", "tickets": 1}]

    records = client.get("/api/records/search", params={"page_size": 50}).json()["summary"]
    assert (records["verified_tons"], records["verified_cost"], records["other_units"]) == ("25.000", "884.00", yards)
    dashboard = client.get("/api/dashboard").json()
    assert (dashboard["total_tons"], dashboard["total_cost"], dashboard["other_units"]) == ("25.000", "884.00", yards)
    directory = next(r for r in client.get("/api/projects/directory").json() if r["id"] == yard["project"])
    assert (directory["verified_tons"], directory["material_cost"], directory["other_units"]) == ("25.000", "884.00", yards)
    report = client.get("/api/reports/summary").json()
    assert (report["material"]["tons"], report["material"]["cost"], report["material"]["other_units"]) == ("25.000", "884.00", yards)
    [project] = report["by_project"]
    assert (project["tons"], project["other_units"]) == ("25.000", yards)
    topsoil = next(m for m in report["material"]["by_material"] if m["name"] == "Topsoil")
    assert (topsoil["tons"], topsoil["cost"], topsoil["other_units"]) == ("0.000", "384.00", yards)
    prices = {(u["material"], u["unit"]): u["average"] for u in report["unit_prices"]}
    assert prices == {("No. 57 Stone", "ton"): "20.00", ("Topsoil", "cy"): "32.00"}


def test_the_export_leaves_tonnage_blank_for_yards_and_can_show_every_unit(client, yard):
    _catalog(client, material="Topsoil", price="32.00", unit="cy")
    _catalog(client)
    _ticket(client, yard, number="80001", quantity="44300", unit="lb")
    _ticket(client, yard, number="80002", quantity="12.00", unit="cy", material="Topsoil")
    rows = list(csv.reader(io.StringIO(client.get("/api/records/export.csv", params={
        "columns": "ticket_number,tons,quantity,unit,as_written,cost"}).text)))
    assert rows[1:] == [["80001", "22.150", "22.150", "tons", "44,300 lb", "443.00"],
                        ["80002", "", "12.000", "cubic yards", "", "384.00"]]
    book = load_workbook(io.BytesIO(client.get("/api/records/export.xlsx").content))
    summary = {row[0]: row[1] for row in book["Summary"].iter_rows(values_only=True) if row and row[0]}
    assert summary["Verified tons"] == pytest.approx(22.15)
    assert summary["Verified cubic yards (not included in tons)"] == pytest.approx(12)


# ------------------------------------------------------------------ the database keeps it

def test_an_approved_tickets_unit_cant_change_while_it_stays_approved(client, yard):
    _catalog(client)
    ticket = _ticket(client, yard)
    with _session() as db:
        row = db.get(Ticket, ticket["id"])
        row.unit = "cy"
        with pytest.raises(DBAPIError):
            db.commit()
        db.rollback()
    with _session() as db:
        assert db.get(Ticket, ticket["id"]).unit == "ton"


def test_changing_the_unit_through_the_app_withdraws_approval(client, yard):
    _catalog(client)
    ticket = _ticket(client, yard)
    changed = client.patch(f"/api/tickets/{ticket['id']}", json={"unit": "cy"}, headers={"If-Match": ticket["revision"]}).json()
    assert (changed["status"], changed["tonnage_confirmed"], changed["cost"]) == ("needs_review", False, None)


# ------------------------------------------------------------------ supplier invoices

def _line(client, yard, unit="ton", quantity="25.000", price="20.0000", subtotal="500.00", **extra):
    body = {"project_id": yard["project"], "invoice_number": "INV-1", "supplier_name": "Example Aggregates",
            "material_name": "No. 57 Stone", "date_from": "2026-11-01", "date_to": "2026-11-30", "billed_tons": quantity,
            "unit": unit, "billed_unit_price": price, "billed_subtotal": subtotal, **extra}
    response = client.post("/api/invoice-comparisons", json=body)
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_a_line_billed_in_yards_is_compared_with_yards_only(client, yard):
    _catalog(client, price="32.00", unit="cy")
    _ticket(client, yard, number="80001", quantity="12.00", unit="cy")
    line = _line(client, yard, unit="cy", quantity="12.000", price="32.0000", subtotal="384.00")
    assert (line["status"], line["unit"], line["verified_tons"], line["verified_subtotal"]) == ("matches", "cy", "12.000", "384.00")


def test_yards_in_a_ton_lines_window_are_named_and_left_unbilled(client, yard):
    _catalog(client)
    _ticket(client, yard, number="80001", quantity="25.00")
    _ticket(client, yard, number="80002", quantity="12.00", unit="cy", approve=False)
    line = _line(client, yard)
    assert line["verified_tons"] == "25.000" and line["pending_count"] == 0  # the yards aren't in it
    assert any("measured in cubic yards, not tons" in w for w in line["warnings"])


def test_a_ticket_listed_on_a_ton_line_but_measured_in_yards_is_outside_it(client, yard):
    _catalog(client, price="32.00", unit="cy")
    _ticket(client, yard, number="80002", quantity="12.00", unit="cy")
    line = _line(client, yard, quantity="12.000", price="32.0000", subtotal="384.00", ticket_list="80002")
    [row] = [r for r in line["ticket_check"] if r["invoice_ticket_number"] == "80002"]
    assert row["result"] == "outside_scope" and any("measured in cubic yards, not tons" in n for n in row["notes"])
    assert line["status"] != "matches"


def test_an_approved_yard_delivery_isnt_covered_by_a_ton_line(client, yard):
    _catalog(client, price="32.00", unit="cy")
    _ticket(client, yard, number="80002", quantity="12.00", unit="cy")
    _line(client, yard)
    report = client.get(f"/api/reports/project/{yard['project']}/unbilled").json()
    assert [(r["ticket_number"], r["unit"], r["tons"]) for r in report["rows"]] == [("80002", "cy", "12.000")]
    assert report["total_tons"] == "0.000" and report["other_units"] == [{"unit": "cy", "tickets": 1, "quantity": "12.000"}]


# ------------------------------------------------------------------ hauling

def test_per_ton_hauling_never_prices_a_delivery_measured_in_yards(client, yard):
    _catalog(client, price="32.00", unit="cy")
    _haul_rate(client, yard, "per_ton", "4.00")
    ticket = _ticket(client, yard, quantity="12.00", unit="cy", hauler=True)
    assert (ticket["haul_billing"], ticket["haul_cost"]) == ("unpriced", None)


def test_a_ticket_counting_loads_is_charged_per_load_for_each(client, yard):
    _catalog(client, price="250.00", unit="load")
    _haul_rate(client, yard, "per_load", "85.00")
    ticket = _ticket(client, yard, quantity="2", unit="load", hauler=True)
    assert (ticket["cost"], ticket["haul_billing"], ticket["haul_cost"]) == ("500.00", "per_load", "170.00")
    report = client.get("/api/reports/summary").json()
    [hauler] = report["trucking"]["by_hauler"]
    assert (hauler["loads"], hauler["cost"]) == (2, "170.00")
    invoice = client.post("/api/hauling-invoices", json={
        "hauler_id": yard["hauler"], "project_id": yard["project"], "invoice_number": "HV-1", "period_from": "2026-11-01",
        "period_to": "2026-11-30", "lines": [{"kind": "loads", "tickets": "80001", "quantity": "2", "rate": "85", "amount": "170.00"}]}).json()
    assert invoice["lines"][0]["result"] == "matched"


# ------------------------------------------------------------------ existing data

def test_the_migration_makes_every_existing_quantity_and_rate_tons(tmp_path):
    import sqlite3

    from alembic import command

    from .migration_helpers import alembic_config

    path = tmp_path / "before-units.db"
    cfg = alembic_config(f"sqlite:///{path}")
    command.upgrade(cfg, "20261127_32")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO suppliers (id, organization_id, name) VALUES (1, 1, 'Example Aggregates')")
        conn.execute("INSERT INTO materials (id, organization_id, name, unit) VALUES (1, 1, 'Sand', 'ton')")
        conn.execute("INSERT INTO supplier_rates (organization_id, supplier_id, material_id, unit_price) VALUES (1, 1, 1, 12.5)")
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT unit, unit_price FROM supplier_rates").fetchall() == [("ton", 12.5)]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
        assert {"unit", "source_quantity", "source_unit", "gross_weight", "tare_weight", "weight_unit"} <= columns
        assert "tickets_unit_locked" in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
