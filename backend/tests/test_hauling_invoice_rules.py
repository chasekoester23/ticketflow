"""Hauling invoices against approved shifts, exactly (services/hauling_invoices.py)."""
from datetime import date
from decimal import Decimal

from app.services.hauling_invoices import LineFacts, ShiftFacts, reconcile

D = Decimal
DAY = date(2026, 11, 3)


def shift(id=1, truck="t-12", status="approved", work="9", standby="0", top_up="0", hourly="110", standby_rate=None, day=DAY):
    return ShiftFacts(id=id, hauler_id=1, truck_key=truck, truck=truck.upper(), work_date=day, project_id=7, status=status,
                      work=D(work), standby=D(standby), top_up=D(top_up), hourly=D(hourly),
                      standby_rate=D(standby_rate) if standby_rate else None)


def line(no=1, kind="hours", truck="t-12", hours="9", rate="110", amount="990.00", invoice=10, day=DAY, description=None):
    return LineFacts(invoice_id=invoice, invoice_number=f"H-{invoice}", hauler_id=1, hauler="Fictional Hauling", project_id=7,
                     period_from=date(2026, 11, 1), period_to=date(2026, 11, 30), line_no=no, kind=kind, truck_key=truck,
                     truck=truck.upper() if truck else None, work_date=day, hours=D(hours) if hours else None,
                     rate=D(rate) if rate else None, amount=D(amount), description=description)


def by_key(findings):
    return {f.key: f for f in findings}


def test_a_line_that_matches_its_approved_shift_raises_nothing():
    results, findings = reconcile([line()], [shift()])
    assert [(r.result, r.expected, r.difference) for r in results] == [("matched", D("990.00"), D("0"))] and findings == []


def test_extra_hours_and_a_higher_rate_split_exactly_into_their_effects():
    # Billed 10 h at $115 = $1,150 against 9 h approved at $110 = $990: $160 over.
    _, findings = reconcile([line(hours="10", rate="115", amount="1150.00")], [shift()])
    effects = {f.category: f.amount for f in findings}
    assert effects == {"hours": D("115.00"), "rate": D("45.00")} and sum(effects.values()) == D("160.00")
    assert all(f.group == "H10:1" and f.expected == D("990.00") for f in findings)


def test_the_invoices_own_arithmetic_takes_the_rest_so_effects_add_up():
    _, findings = reconcile([line(hours="9", rate="110", amount="999.00")], [shift()])
    assert {f.category: f.amount for f in findings} == {"invoice_arithmetic": D("9.00")}


def test_billing_the_same_shift_again_is_a_repeat_counted_once():
    _, findings = reconcile([line(no=1), line(no=1, invoice=11)], [shift()])
    keys = by_key(findings)
    assert list(keys) == ["H11:1:repeat"] and keys["H11:1:repeat"].amount == D("990.00")
    assert "already billed on invoice H-10, line 1" in keys["H11:1:repeat"].explanation


def test_standby_and_a_minimum_are_checked_against_their_own_approved_hours_and_rates():
    s = shift(work="2.5", standby="0.5", top_up="1.0", hourly="110", standby_rate="85")
    _, findings = reconcile([line(no=1, hours="2.5", amount="275.00"), line(no=2, kind="standby", hours="1.5", rate="85", amount="127.50"),
                             line(no=3, kind="minimum", hours="1.0", amount="110.00")], [s])
    assert {f.key: f.amount for f in findings} == {"H10:2:hours": D("85.00")}  # one hour of standby too many


def test_fees_missing_shifts_unapproved_shifts_and_unclear_trucks_each_say_what_to_do():
    lines = [line(no=1, kind="fee", truck=None, hours=None, rate=None, amount="45.00", description="Fuel surcharge"),
             line(no=2, truck="t-99"), line(no=3, truck=None)]
    shifts = [shift(id=1), shift(id=2, truck="t-14", status="needs_review")]
    _, findings = reconcile(lines, shifts)
    keys = by_key(findings)
    assert keys["H10:1:fee"].category == "unsupported_fee" and keys["H10:1:fee"].amount == D("45.00")
    assert keys["H10:2:missing"].category == "missing_shift" and keys["H10:2:missing"].amount == D("990.00")
    assert keys["H10:3:ambiguous"].category == "needs_review" and keys["H10:3:ambiguous"].amount is None
    _, unapproved = reconcile([line(truck="t-14")], shifts)
    assert unapproved[0].key == "H10:1:unapproved" and "waiting for approval" in unapproved[0].explanation


def test_partial_billing_is_below_approved_and_unbilled_shifts_in_the_period_are_reported():
    shifts = [shift(id=1), shift(id=2, truck="t-14"), shift(id=3, truck="t-14", day=date(2026, 12, 2))]
    _, findings = reconcile([line(hours="8", amount="880.00")], shifts)
    keys = by_key(findings)
    assert keys["H10:1:hours"].amount == D("-110.00")
    assert "HS2" in keys and keys["HS2"].amount is None  # in November's period, not billed
    assert "HS3" not in keys                              # December: not this invoice's period
