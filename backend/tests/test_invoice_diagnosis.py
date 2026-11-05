"""Why an invoice line does or doesn't match: figures that add up, every time."""
import random
from decimal import Decimal

import pytest

from .test_query_budgets import _seed, _session
from app.models import InvoiceComparison, InvoiceTicketRef, Organization


LINE = {"project_id": 1, "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-30"}


def _priced(client, rate="10"):
    client.post("/api/suppliers", json={"name": "Example Aggregates"})
    client.post("/api/materials", json={"name": "Sand"})
    client.put("/api/rates", json={"supplier_id": client.get("/api/suppliers").json()[0]["id"],
                                   "material_id": client.get("/api/materials").json()[0]["id"], "unit_price": rate})


def _line(client, number="I-1", tons="30.01", rate="10", subtotal=None, tickets="", **extra):
    subtotal = subtotal or str((Decimal(tons) * Decimal(rate)).quantize(Decimal("0.01")))
    response = client.post("/api/invoice-comparisons", json={
        **LINE, "invoice_number": number, "billed_tons": tons, "billed_unit_price": rate,
        "billed_subtotal": subtotal, "ticket_list": tickets, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _holds(bridge):
    total = Decimal(bridge["start"]) + sum(Decimal(i["tons"]) for i in bridge["items"]) + Decimal(bridge["unexplained"])
    assert total == Decimal(bridge["billed"])


def _item(diagnosis, kind):
    return next((i for i in diagnosis["tons"]["items"] if i["kind"] == kind), None)


def _two_verified(client):
    # Tickets 10000 and 10001: 15.00 and 15.01 tons, delivered in September.
    _seed(2)
    _priced(client)
    client.post("/api/tickets/1/verify")
    client.post("/api/tickets/2/verify")


def test_a_clean_itemized_line_says_so_with_nothing_to_explain(client):
    _two_verified(client)
    line = _line(client, tickets="10000\t15.00\n10001\t15.01")
    diagnosis = line["diagnosis"]
    assert line["status"] == "matches"
    assert "agree" in diagnosis["headline"]
    assert diagnosis["tons"]["items"] == []
    assert Decimal(diagnosis["tons"]["unexplained"]) == 0
    _holds(diagnosis["tons"])
    money = diagnosis["money"]
    assert Decimal(money["start"]) == Decimal(money["billed"]) == Decimal("300.10")
    assert all(Decimal(money[key]) == 0 for key in ("quantity_effect", "rate_effect", "arithmetic_effect", "rounding"))


def test_matching_totals_with_a_wrong_ticket_are_explained_ticket_by_ticket(client):
    """The original failure: same total tons, different tickets."""
    _two_verified(client)
    line = _line(client, tickets="10000\t15.00\n99999\t15.01")
    diagnosis = line["diagnosis"]
    assert line["status"] == "discrepancy"
    assert Decimal(line["quantity_difference"]) == 0
    assert "individual tickets don't" in diagnosis["headline"]
    missing = _item(diagnosis, "not_found")
    assert (Decimal(missing["tons"]), missing["tickets"]) == (Decimal("15.01"), ["99999"])
    unbilled = _item(diagnosis, "not_on_invoice")
    assert (Decimal(unbilled["tons"]), unbilled["tickets"]) == (Decimal("-15.01"), ["10001"])
    _holds(diagnosis["tons"])
    assert any("99999" in step["text"] for step in diagnosis["next_steps"])
    assert any("10001" in step["text"] for step in diagnosis["next_steps"])


def test_a_ticket_billed_at_more_tons_shows_the_exact_difference(client):
    _two_verified(client)
    line = _line(client, tons="32.01", tickets="10000\t17.00\n10001\t15.01")
    differ = _item(line["diagnosis"], "tons_differ")
    assert Decimal(differ["tons"]) == Decimal("2.00") and differ["tickets"] == ["10000"]
    _holds(line["diagnosis"]["tons"])
    assert "2.00" in line["diagnosis"]["headline"] and "more" in line["diagnosis"]["headline"]


def test_a_ticket_billed_twice_on_one_line_is_counted_once_and_named(client):
    _two_verified(client)
    line = _line(client, tons="45.01", tickets="10000\t15.00\n10001\t15.01\n10000\t15.00")
    repeated = _item(line["diagnosis"], "repeated")
    assert Decimal(repeated["tons"]) == Decimal("15.00") and repeated["tickets"] == ["10000"]
    _holds(line["diagnosis"]["tons"])


def test_unapproved_delivery_is_explained_as_waiting_not_missing(client):
    _seed(2)
    _priced(client)
    client.post("/api/tickets/1/verify")
    line = _line(client, tickets="10000\t15.00\n10001\t15.01")
    diagnosis = line["diagnosis"]
    assert line["status"] == "incomplete"
    assert "Can't be settled yet" in diagnosis["headline"]
    assert _item(diagnosis, "unverified")["tickets"] == ["10001"]
    assert _item(diagnosis, "not_found") is None
    _holds(diagnosis["tons"])
    assert any(step["href"] and "needs_review" in step["href"] for step in diagnosis["next_steps"])


def test_duplicate_records_are_explained_exactly(client):
    _seed(3)
    _priced(client)
    from .test_records_at_scale import _set
    _set(2, ticket_number="10000")  # Two records, one number.
    for ticket_id in (1, 2):
        assert client.post(f"/api/tickets/{ticket_id}/verify").status_code == 200
    line = _line(client, tons="15.00", tickets="10000\t15.00")
    diagnosis = line["diagnosis"]
    assert _item(diagnosis, "ambiguous")["tickets"] == ["10000"]
    _holds(diagnosis["tons"])


def test_a_list_that_disagrees_with_its_own_line_is_unexplained(client):
    _two_verified(client)
    line = _line(client, tons="40.00", tickets="10000\t15.00\n10001\t15.01")
    bridge = line["diagnosis"]["tons"]
    assert bridge["items"] == []
    assert Decimal(bridge["unexplained"]) == Decimal("9.99")
    _holds(bridge)


def test_tickets_without_billed_tons_make_the_bridge_explicitly_incomplete(client):
    _two_verified(client)
    bridge = _line(client, tickets="10000\n10001\t15.01")["diagnosis"]["tons"]
    assert bridge["complete"] is False and bridge["rows_without_tons"] == 1
    assert bridge["unexplained"] is None


def test_totals_only_never_claims_tickets_were_checked(client):
    _two_verified(client)
    line = _line(client)
    assert line["status"] == "matches"
    diagnosis = line["diagnosis"]
    assert diagnosis["tons"] is None
    assert "individual tickets weren't checked" in diagnosis["headline"]
    assert any("Enter this invoice's ticket numbers" in step["text"] for step in diagnosis["next_steps"])


def test_rate_difference_is_separated_from_quantity(client):
    _two_verified(client)  # Locked at $10.
    line = _line(client, rate="11", tickets="10000\t15.00\n10001\t15.01")
    money = line["diagnosis"]["money"]
    assert Decimal(money["start"]) == Decimal("300.10")
    assert Decimal(money["quantity_effect"]) == 0
    assert Decimal(money["rate_effect"]) == Decimal("30.01")
    assert money["rate_groups"][0]["tickets"] == 2 and Decimal(money["rate_groups"][0]["locked_rate"]) == 10
    assert Decimal(money["billed"]) == Decimal("330.11")
    total = sum(Decimal(money[key]) for key in ("start", "quantity_effect", "rate_effect", "arithmetic_effect", "rounding"))
    assert total == Decimal(money["billed"])


def test_invoice_arithmetic_is_its_own_figure(client):
    _two_verified(client)
    line = _line(client, subtotal="305.10")
    money = line["diagnosis"]["money"]
    assert Decimal(money["arithmetic_effect"]) == Decimal("5.00")
    assert Decimal(money["quantity_effect"]) == 0 and Decimal(money["rate_effect"]) == 0


def test_unknown_cost_stays_unresolved_with_the_reason(client):
    _seed(1)
    client.post("/api/tickets/1/verify")  # No rate on file.
    line = _line(client, tons="15.00")
    assert line["diagnosis"]["money"] is None
    assert "no locked price" in line["diagnosis"]["money_unresolved_reason"]
    assert any("no locked price" in step["text"] for step in line["diagnosis"]["next_steps"])


@pytest.mark.parametrize("seed", range(40))
def test_the_bridge_always_adds_up_on_random_invoices(client, seed):
    """Whatever mix of tickets and mistakes, figures must land exactly on the bill."""
    rng = random.Random(seed)
    _seed(12)
    _priced(client)
    for ticket_id in range(1, 13):
        if rng.random() < 0.7:
            assert client.post(f"/api/tickets/{ticket_id}/verify").status_code == 200
    rows = []
    for ticket_id in range(1, 13):
        roll = rng.random()
        if roll < 0.15:
            continue  # Delivered, not on the invoice.
        tons = Decimal(1500 + ticket_id - 1) / 100
        if roll < 0.3:
            tons += Decimal(rng.randint(1, 500)) / 100  # Billed differently.
        number = str(10000 + ticket_id - 1)
        rows.append(f"{number}\t{tons}")
        if roll > 0.9:
            rows.append(f"{number}\t{tons}")  # Billed twice.
    for fake in range(rng.randint(0, 3)):
        rows.append(f"88{fake}\t{Decimal(rng.randint(100, 3000)) / 100}")  # Never delivered.
    rng.shuffle(rows)
    billed = sum(Decimal(r.split("\t")[1]) for r in rows) + Decimal(rng.randint(0, 2))
    line = _line(client, tons=str(billed), tickets="\n".join(rows) or "1\t1")
    _holds(line["diagnosis"]["tons"])
    money = line["diagnosis"]["money"]
    if money:
        total = sum(Decimal(money[key]) for key in ("start", "quantity_effect", "rate_effect", "arithmetic_effect", "rounding"))
        assert total == Decimal(money["billed"])
        assert abs(Decimal(money["rounding"])) <= Decimal("0.01") * 13


def _raw_line(number, *, supplier="Example Aggregates", tons="10", rate="1", subtotal="10", material="Sand", refs=(), day=1, organization_id=1, project_id=1):
    from datetime import date
    with _session() as db:
        item = InvoiceComparison(
            organization_id=organization_id, project_id=project_id, invoice_number=number, supplier_name=supplier,
            supplier_key=supplier.lower(), material_name=material, material_key=material.lower(),
            date_from=date(2026, 9, day), date_to=date(2026, 9, day), billed_tons=Decimal(tons),
            billed_unit_price=Decimal(rate), billed_subtotal=Decimal(subtotal))
        db.add(item)
        db.flush()
        db.add_all(InvoiceTicketRef(comparison_id=item.id, position=i + 1, ticket_number=r, ticket_key=r) for i, r in enumerate(refs))
        db.commit()
        return item.id


def test_overview_finds_a_ticket_on_two_invoices(client):
    _seed(1)
    first = _raw_line("I-1", refs=["555"], day=1)
    second = _raw_line("I-2", refs=["555", "777"], tons="12", subtotal="12", day=2)
    found = client.get("/api/invoice-comparisons/overview").json()["exceptions"]
    assert [e["kind"] for e in found] == ["ticket_on_two_invoices"]
    assert "555" in found[0]["title"] and "I-1" in found[0]["detail"] and "I-2" in found[0]["detail"]
    assert found[0]["line_ids"] == [first, second]


def test_overview_does_not_cross_suppliers(client):
    _seed(1)
    _raw_line("I-1", refs=["555"], day=1)
    _raw_line("I-2", supplier="Sample Stone Co", refs=["555"], tons="12", subtotal="12", day=2)
    assert client.get("/api/invoice-comparisons/overview").json()["exceptions"] == []


def test_overview_finds_the_same_bill_under_a_new_number_and_reused_numbers(client):
    _seed(1)
    _raw_line("I-1", day=1)
    _raw_line("I-1-RESEND", day=2)
    _raw_line("I-9", tons="5", subtotal="5", day=3)
    _raw_line("I-9", tons="6", subtotal="6", day=4)
    kinds = {e["kind"] for e in client.get("/api/invoice-comparisons/overview").json()["exceptions"]}
    assert kinds == {"same_amounts_new_number", "invoice_number_reused"}


def test_overview_never_crosses_companies(client):
    with _session() as db:
        db.add(Organization(id=2, name="Another Company"))
        db.commit()
    _seed(1)
    _seed(1, organization_id=2, batch_id=2, start=5)
    _raw_line("I-1", refs=["555"], day=1)
    _raw_line("I-1", refs=["555"], day=1, organization_id=2, project_id=2)
    result = client.get("/api/invoice-comparisons/overview").json()
    assert result["lines"] == 1 and result["exceptions"] == []


def test_overview_totals_leave_unknown_figures_out_and_say_how_many(client):
    _two_verified(client)
    _line(client, tons="30.01", subtotal="300.10")
    client.post("/api/invoice-comparisons", json={
        **LINE, "material_name": "Gravel", "invoice_number": "I-2", "billed_tons": "5", "billed_unit_price": "2", "billed_subtotal": "10"})
    result = client.get("/api/invoice-comparisons/overview").json()
    assert Decimal(result["billed"]) == Decimal("310.10")
    assert Decimal(result["verified"]) == Decimal("300.10")
    assert result["lines_unresolved"] == 1
    assert result["by_status"] == {"matches": 1, "no_verified_tickets": 1}
    assert result["by_supplier"][0]["lines"] == 2 and result["by_supplier"][0]["open"] == 1
