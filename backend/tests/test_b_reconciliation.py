"""Delivered but not billed, and every difference on what was billed. Fictional data.

These describe; they never approve a delivery, an invoice or a payment.
"""
import csv
import io
import random
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.auth import current_user
from app.main import app
from app.models import Batch, InvoiceTicketLink, Organization, Project, Ticket, UploadedFile, User
from app.services import reconciliation

from .test_invoice_diagnosis import _line, _priced, _two_verified
from .test_query_budgets import _seed, _session


def _as(role="owner", organization_id=1, user_id=1):
    app.dependency_overrides[current_user] = lambda: User(id=user_id, organization_id=organization_id, username="u", password_hash="x",
                                                         active=True, role=role, must_change_password=False)


class Deliveries:
    """Approved deliveries written straight to the database, so a test states exactly
    what's there. Approval through the API is covered by the diagnosis tests."""

    def __init__(self, organization_id=1, project_id=1, code="P1"):
        self.organization_id, self.project_id, self.next = organization_id, project_id, 0
        with _session() as db:
            if db.get(Organization, organization_id) is None:
                db.add(Organization(id=organization_id, name=f"Company {organization_id}"))
                db.flush()
            db.add(Project(id=project_id, organization_id=organization_id, code=code, name="Fictional Job"))
            db.flush()
            db.add(Batch(id=project_id, organization_id=organization_id, project_id=project_id, layout="single",
                         status="ready_for_review", intake_mode="manual"))
            db.flush()
            db.add(UploadedFile(id=project_id, batch_id=project_id, original_name="f.pdf", storage_path="x", sha256="0" * 64, status="ready_for_review"))
            db.commit()

    def add(self, day, *, supplier="Example Aggregates", material="Sand", tons="10.000", cost="100.00", status="verified", number=None) -> int:
        self.next += 1
        with _session() as db:
            ticket = Ticket(batch_id=self.project_id, file_id=self.project_id, source_page=self.next, position=0, crop_box=[0, 0, 1, 1],
                            crop_path="x", crop_sha256=f"{self.project_id:08d}{self.next:056d}", ticket_number=number or f"T{self.project_id}-{self.next}",
                            delivery_date=date(2026, 9, day) if day else None, material_type=material, supplier=supplier, job_location="Lot",
                            tonnage=Decimal(tons), flags=[], status=status, tonnage_confirmed=status == "verified",
                            price_snapshot_state="priced" if cost and status == "verified" else ("unpriced" if status == "verified" else "pending"),
                            applied_unit_price=(Decimal(cost) / Decimal(tons)).quantize(Decimal("0.0001")) if cost and status == "verified" else None,
                            approved_cost=Decimal(cost) if cost and status == "verified" else None,
                            verified_at=datetime.now(timezone.utc) if status == "verified" else None)
            db.add(ticket)
            db.commit()
            return ticket.id


def _line_for(client, number, start, end, *, project_id=1, supplier="Example Aggregates", material="Sand", tickets=""):
    response = client.post("/api/invoice-comparisons", json={
        "project_id": project_id, "invoice_number": number, "supplier_name": supplier, "material_name": material,
        "date_from": f"2026-09-{start:02d}", "date_to": f"2026-09-{end:02d}", "billed_tons": "1", "billed_unit_price": "1",
        "billed_subtotal": "1.00", "ticket_list": tickets})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture()
def project(client):
    """One project with deliveries billed, unbilled, listed elsewhere, linked, and not approved."""
    p = Deliveries()
    ids = {
        "covered_a": p.add(1), "covered_b": p.add(3),
        "gap_one_a": p.add(8, tons="1.111", cost="11.11"), "gap_one_b": p.add(9, tons="2.222", cost="22.22"),
        "gap_two": p.add(20, tons="3.333", cost="33.33"),
        "listed_elsewhere": p.add(25, number="LISTED-1"),
        "other_material": p.add(8, supplier="Sample Stone Co", material="No. 57 Stone", tons="4.444", cost="44.44"),
        "not_approved": p.add(26, status="needs_review"),
        "no_cost": p.add(27, tons="5.555", cost=None),
        "skipped": p.add(28, status="skipped"),
        "linked": p.add(29),
        "no_date": p.add(None, tons="0.001", cost="0.01"),
    }
    Deliveries(project_id=2, code="P2")
    lines = {"first": _line_for(client, "EA-1", 1, 5), "middle": _line_for(client, "EA-2", 12, 15),
             "elsewhere": _line_for(client, "EA-3", 1, 2, project_id=2, tickets="LISTED-1")}
    with _session() as db:
        db.add(InvoiceTicketLink(comparison_id=lines["first"], ticket_id=ids["linked"], listed_number="T1-11", verified_tons=Decimal("10.000")))
        db.commit()
    # Another company, the same supplier and dates: none of it can appear here.
    other = Deliveries(organization_id=2, project_id=3, code="P1")
    other.add(8, tons="99.999", cost="999.99")
    return ids


def _unbilled(client, project_id=1):
    response = client.get(f"/api/reports/project/{project_id}/unbilled")
    assert response.status_code == 200, response.text
    return response.json()


def test_only_approved_deliveries_no_line_lists_or_covers_are_unbilled(client, project):
    report = _unbilled(client)
    found = {row["ticket_id"] for row in report["rows"]}
    assert found == {project[name] for name in ("gap_one_a", "gap_one_b", "gap_two", "other_material", "no_cost", "no_date")}
    assert report["project"] == {"id": 1, "code": "P1", "name": "Fictional Job"}
    assert report["total_tickets"] == 6 and report["truncated"] is False


def test_totals_are_exact_and_a_missing_price_is_counted_not_zeroed(client, project):
    report = _unbilled(client)
    assert report["total_tons"] == "16.666"  # 1.111 + 2.222 + 3.333 + 4.444 + 5.555 + 0.001
    assert report["total_cost"] == "111.11"
    assert report["tickets_without_cost"] == 1
    no_cost = next(row for row in report["rows"] if row["ticket_id"] == project["no_cost"])
    assert no_cost["cost"] is None and no_cost["tons"] == "5.555"


def test_rows_have_the_documented_shape_and_strings_for_decimals(client, project):
    row = next(row for row in _unbilled(client)["rows"] if row["ticket_id"] == project["gap_one_a"])
    assert row == {"ticket_id": project["gap_one_a"], "batch_id": 1, "ticket_number": "T1-3", "delivery_date": "2026-09-08",
                   "supplier": "Example Aggregates", "material": "Sand", "tons": "1.111", "cost": "11.11"}


def test_gaps_are_runs_of_dates_no_line_covers(client, project):
    gaps = _unbilled(client)["gaps"]
    assert gaps == [
        {"supplier": "Example Aggregates", "material": "Sand", "date_from": "2026-09-08", "date_to": "2026-09-09", "tickets": 2, "tons": "3.333"},
        {"supplier": "Example Aggregates", "material": "Sand", "date_from": "2026-09-20", "date_to": "2026-09-27", "tickets": 2, "tons": "8.888"},
        {"supplier": "Sample Stone Co", "material": "No. 57 Stone", "date_from": "2026-09-08", "date_to": "2026-09-08", "tickets": 1, "tons": "4.444"},
    ]


def test_it_never_says_anything_is_approved_for_payment(client, project):
    assert "not an approval to pay" in _unbilled(client)["note"]


def test_rows_stop_at_500_but_totals_cover_everything(client):
    p = Deliveries()
    with _session() as db:
        db.add_all(Ticket(batch_id=1, file_id=1, source_page=n, position=0, crop_box=[0, 0, 1, 1], crop_path="x", crop_sha256=f"{n:064d}",
                          ticket_number=f"N{n}", delivery_date=date(2026, 9, 1 + n % 28), material_type="Sand", supplier="Example Aggregates",
                          tonnage=Decimal("1.001"), flags=[], status="verified", tonnage_confirmed=True, price_snapshot_state="priced",
                          applied_unit_price=Decimal("10"), approved_cost=Decimal("10.01")) for n in range(1, 521))
        db.commit()
    assert p
    report = _unbilled(client)
    assert len(report["rows"]) == 500 and report["truncated"] is True
    assert report["total_tickets"] == 520 and report["total_tons"] == "520.520" and report["total_cost"] == "5205.20"
    lines = list(csv.reader(io.StringIO(client.get("/api/reports/project/1/unbilled.csv").text)))
    assert len(lines) == 521  # The CSV has every row.


def test_the_csv_has_every_row_with_neutralized_text(client, project):
    with _session() as db:
        db.get(Ticket, project["gap_two"]).supplier = "=HYPERLINK(\"http://example.invalid\")"
        db.commit()
    response = client.get("/api/reports/project/1/unbilled.csv")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][0] == "Ticket ID" and "not a bill" in rows[0][-1]
    assert len(rows) == 1 + 6
    hostile = next(row for row in rows[1:] if row[0] == str(project["gap_two"]))
    assert hostile[4].startswith("'=")
    assert hostile[6] == "3.333" and hostile[7] == "33.33"


def test_another_companys_project_is_not_found(client, project):
    assert client.get("/api/reports/project/3/unbilled").status_code == 404
    assert client.get("/api/reports/project/3/unbilled.csv").status_code == 404


def test_every_role_can_read_it(client, project):
    _as("viewer")
    assert client.get("/api/reports/project/1/unbilled").status_code == 200


# ------------------------------------------------------------------ discrepancies

def _discrepancies(client, **params) -> list[dict]:
    response = client.get("/api/invoice-comparisons/discrepancies.csv", params=params)
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.text)))


EFFECT = "Dollar effect (billed minus approved)"
CHECK = "What to check (not an approval to pay)"


def test_a_matching_line_has_no_rows_and_the_header_still_says_its_not_an_approval(client):
    _two_verified(client)
    _line(client, tickets="10000\t15.00\n10001\t15.01")
    response = client.get("/api/invoice-comparisons/discrepancies.csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows == [reconciliation.HEADERS]
    assert "not an approval to pay" in rows[0][-1]


def test_each_difference_is_a_row_with_its_dollar_effect(client):
    _two_verified(client)  # 10000: 15.00 t, 10001: 15.01 t, both at $10/t.
    line = _line(client, tons="35.00", tickets="10000\t15.50\n99999\t4.49")
    rows = _discrepancies(client)
    by_kind = {row["Kind"]: row for row in rows}
    assert by_kind["Tons differ"]["Ticket number"] == "10000" and by_kind["Tons differ"]["Tons difference"] == "0.500"
    assert by_kind["Tons differ"][EFFECT] == "5.00"
    assert by_kind["No delivery record"]["Ticket number"] == "99999" and by_kind["No delivery record"][EFFECT] == "44.90"
    assert by_kind["Approved delivery not on this invoice"]["Ticket number"] == "10001"
    assert by_kind["Approved delivery not on this invoice"]["Tons difference"] == "-15.010"
    assert by_kind["Approved delivery not on this invoice"][EFFECT] == "-150.10"
    assert all(row["Invoice"] == "I-1" and row["Supplier"] == "Example Aggregates" for row in rows)
    assert sum(Decimal(row[EFFECT]) for row in rows) == Decimal(line["subtotal_difference"])


def test_a_rate_difference_is_its_own_row(client):
    _two_verified(client)
    line = _line(client, tons="30.01", rate="10.50", tickets="10000\t15.00\n10001\t15.01")
    rows = _discrepancies(client)
    rate = next(row for row in rows if row["Kind"] == "Rate")
    assert rate[EFFECT] == "15.01" and rate["Approved tons"] == "30.010" and "$10" in rate[CHECK]
    assert sum(Decimal(row[EFFECT]) for row in rows) == Decimal(line["subtotal_difference"])


def test_invoice_arithmetic_is_its_own_row(client):
    _two_verified(client)
    line = _line(client, subtotal="310.10", tickets="10000\t15.00\n10001\t15.01")
    rows = _discrepancies(client)
    assert [row["Kind"] for row in rows] == ["Invoice arithmetic"] and rows[0][EFFECT] == "10.00"
    assert Decimal(line["subtotal_difference"]) == Decimal("10.00")


def test_a_totals_only_line_gets_one_quantity_row(client):
    _two_verified(client)
    line = _line(client, tons="31.01")
    [row] = _discrepancies(client)
    assert row["Kind"] == "Quantity" and row["Billed tons"] == "31.010" and row["Approved tons"] == "30.010"
    assert row["Tons difference"] == "1.000" and row[EFFECT] == "10.00" == line["subtotal_difference"]


def test_without_a_locked_price_the_dollar_effect_is_left_blank_and_says_why(client):
    _seed(2)
    client.post("/api/tickets/1/verify")  # No rate yet: approved without a price.
    _line(client, tons="20.00", tickets="10000\t16.00")
    rows = _discrepancies(client)
    assert rows and all(row[EFFECT] == "" for row in rows)
    assert any("no locked price" in row[CHECK] for row in rows)


@pytest.mark.parametrize("seed", range(25))
def test_dollar_effects_always_add_up_to_the_lines_difference(client, seed):
    """Mathematically reproducible: whatever the mix, a line's rows sum to billed minus approved."""
    rng = random.Random(seed)
    _seed(10)
    _priced(client, rate=str(Decimal(rng.randint(10000, 40000)) / 1000))
    for ticket_id in range(1, 11):
        if rng.random() < 0.8:
            client.post(f"/api/tickets/{ticket_id}/verify")
    rows = []
    for ticket_id in range(1, 11):
        roll = rng.random()
        if roll < 0.15:
            continue
        tons = Decimal(1500 + ticket_id - 1) / 100 + (Decimal(rng.randint(-300, 300)) / 1000 if roll < 0.4 else 0)
        rows.append(f"{10000 + ticket_id - 1}\t{tons}")
        if roll > 0.92:
            rows.append(f"{10000 + ticket_id - 1}\t{tons}")
    rows.append(f"77{seed}\t{Decimal(rng.randint(100, 900)) / 100}")
    billed = sum(Decimal(r.split("\t")[1]) for r in rows)
    rate = Decimal(rng.randint(10000, 40000)) / 1000
    subtotal = (billed * rate).quantize(Decimal("0.01")) + Decimal(rng.choice([0, 0, 0, 1, -1])) / 100
    line = _line(client, tons=str(billed), rate=str(rate), subtotal=str(subtotal), tickets="\n".join(rows))
    found = _discrepancies(client)
    if line["subtotal_difference"] is None:
        assert all(row[EFFECT] == "" for row in found)
    else:
        assert sum((Decimal(row[EFFECT]) for row in found if row[EFFECT]), Decimal(0)) == Decimal(line["subtotal_difference"])
        assert all(row[EFFECT] != "" for row in found if row["Kind"] != "Also on another invoice")
    tons = [Decimal(row["Tons difference"]) for row in found if row["Tons difference"]]
    assert sum(tons, Decimal(0)) == Decimal(line["quantity_difference"])


def test_filters_and_company_scoping(client):
    _two_verified(client)
    _line(client, tons="31.01")
    with _session() as db:
        db.add(Project(id=2, organization_id=1, code="P2", name="Other job"))
        db.add(Organization(id=2, name="Other company"))
        db.flush()
        db.add(Project(id=3, organization_id=2, code="P3", name="Theirs"))
        db.commit()
    assert len(_discrepancies(client, project_id="1")) == 1
    assert _discrepancies(client, project_id="2") == []
    assert len(_discrepancies(client, project_id="", supplier="")) == 1  # Blank means no filter.
    assert len(_discrepancies(client, supplier="EXAMPLE aggregates.")) == 1
    assert _discrepancies(client, supplier="Sample Stone Co") == []
    assert client.get("/api/invoice-comparisons/discrepancies.csv", params={"project_id": "3"}).status_code == 404
    assert client.get("/api/invoice-comparisons/discrepancies.csv", params={"project_id": "x"}).status_code == 422
    _as(organization_id=2, user_id=2)
    assert _discrepancies(client) == []


def test_text_from_the_invoice_is_neutralized(client):
    _two_verified(client)
    _line(client, number="=cmd|' /C calc'!A0", tons="31.01")
    [row] = _discrepancies(client)
    assert row["Invoice"].startswith("'=")


# ------------------------------------------------------------------ the rounding-only headline

def test_the_headline_says_when_the_only_money_difference_is_rounding(client):
    """0.1 t and 0.2 t at $28.1235: each ticket rounds down to 2.81 and 5.62, while the
    invoice's 0.3 t rounds its 8.43705 up to 8.44."""
    _seed(2)
    _priced(client, rate="28.1235")
    client.patch("/api/tickets/1", json={"tonnage": "0.100"})
    client.patch("/api/tickets/2", json={"tonnage": "0.200"})
    client.post("/api/tickets/1/verify")
    client.post("/api/tickets/2/verify")
    line = _line(client, tons="0.300", rate="28.1235", tickets="10000\t0.100\n10001\t0.200")
    assert line["status"] == "discrepancy"  # A rounding difference is still shown, never hidden.
    assert line["subtotal_difference"] == "0.01"
    headline = line["diagnosis"]["headline"]
    assert headline.startswith("The only money difference is rounding: $0.01") and "not an approval to pay" in headline
    [row] = _discrepancies(client)
    assert row["Kind"] == "Rounding" and row[EFFECT] == "0.01"


@pytest.mark.parametrize("change", ["rate", "tons", "arithmetic"])
def test_the_rounding_headline_isnt_used_when_anything_else_differs(client, change):
    _seed(2)
    _priced(client, rate="28.1235")
    client.patch("/api/tickets/1", json={"tonnage": "0.100"})
    client.patch("/api/tickets/2", json={"tonnage": "0.200"})
    client.post("/api/tickets/1/verify")
    client.post("/api/tickets/2/verify")
    kwargs = {"rate": {"rate": "28.2"}, "tons": {"tons": "0.310", "tickets": "10000\t0.110\n10001\t0.200"},
              "arithmetic": {"subtotal": "9.00"}}[change]
    arguments = {"tons": "0.300", "rate": "28.1235", "tickets": "10000\t0.100\n10001\t0.200", **kwargs}
    line = _line(client, **arguments)
    assert line["status"] == "discrepancy"
    assert "only money difference is rounding" not in line["diagnosis"]["headline"]
