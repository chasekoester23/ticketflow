"""Hourly trucking's financial rules, exactly (services/trucking.py). No database."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.services.trucking import HaulRate, ShiftError, allocate, choose_rate, hours_between, price_shift

D = Decimal


def rate(**kw):
    return HaulRate(**{"id": 1, "hauler_id": 1, "hourly_rate": D("110.00"), **kw})


def test_the_brief_example_two_trucks_nine_hours_at_110():
    shifts = [price_shift(D("9"), D("0"), rate()) for _ in range(2)]
    assert [s.total for s in shifts] == [D("990.00"), D("990.00")]
    hauling = sum(s.total for s in shifts)
    material = D("220") * D("18.00")
    assert (material, hauling, material + hauling) == (D("3960.00"), D("1980.00"), D("5940.00"))


@pytest.mark.parametrize("start, end, unpaid, hours", [
    (datetime(2026, 11, 3, 6, 30), datetime(2026, 11, 3, 16, 0), 30, D("9.00")),   # a normal day, half-hour lunch
    (datetime(2026, 11, 3, 19, 0), datetime(2026, 11, 4, 5, 30), 0, D("10.50")),   # crosses midnight
    (datetime(2026, 11, 3, 7, 0), datetime(2026, 11, 3, 14, 50), 0, D("7.83")),    # 7 h 50 m, to the hundredth
    (datetime(2026, 11, 3, 7, 0), datetime(2026, 11, 3, 7, 1), 0, D("0.02")),       # one minute rounds half up
])
def test_hours_on_site(start, end, unpaid, hours):
    assert hours_between(start, end, unpaid) == hours


@pytest.mark.parametrize("start, end, unpaid, words", [
    (datetime(2026, 11, 3, 16, 0), datetime(2026, 11, 3, 6, 0), 0, "ends before it starts"),
    (datetime(2026, 11, 3, 6, 0), datetime(2026, 11, 4, 7, 0), 0, "longer than 24 hours"),
    (datetime(2026, 11, 3, 6, 0), datetime(2026, 11, 3, 7, 0), 90, "break is longer"),
])
def test_impossible_shifts_are_refused_with_the_reason(start, end, unpaid, words):
    with pytest.raises(ShiftError, match=words):
        hours_between(start, end, unpaid)


def test_standby_is_paid_at_the_agreed_standby_rate_and_never_guessed():
    priced = price_shift(D("7.5"), D("1.5"), rate(standby_rate=D("85.00")))
    assert [(line.label, line.amount) for line in priced.lines] == [("Working hours", D("825.00")), ("Standby", D("127.50"))]
    assert priced.total == D("952.50") and priced.billable_hours == D("9.0")
    unpriced = price_shift(D("7.5"), D("1.5"), rate())
    assert unpriced.total is None and "standby" in unpriced.reason


def test_a_minimum_is_a_separate_labelled_top_up_counted_after_standby():
    short = price_shift(D("2.5"), D("0.5"), rate(minimum_hours=D("4"), standby_rate=D("85.00")))
    assert [(line.label, line.hours, line.amount) for line in short.lines] == [
        ("Working hours", D("2.5"), D("275.00")), ("Standby", D("0.5"), D("42.50")), ("Minimum charge (4 h)", D("1.0"), D("110.00"))]
    assert short.total == D("427.50") and short.billable_hours == D("4")
    long = price_shift(D("6"), D("0"), rate(minimum_hours=D("4")))
    assert len(long.lines) == 1 and long.total == D("660.00")


def test_each_line_rounds_half_up_to_the_cent():
    assert price_shift(D("7.83"), D("0"), rate(hourly_rate=D("97.50"))).total == D("763.43")  # 763.425


def test_the_most_specific_started_agreement_applies():
    rates = [rate(id=1, hourly_rate=D("100")), rate(id=2, hourly_rate=D("105"), equipment_type="quad_axle"),
             rate(id=3, hourly_rate=D("95"), project_id=7), rate(id=4, hourly_rate=D("98"), project_id=7, equipment_type="quad_axle"),
             rate(id=5, hourly_rate=D("120"), hauler_id=2)]
    pick = lambda **kw: choose_rate(rates, hauler_id=1, on=date(2026, 11, 3), **kw).rate.id  # noqa: E731
    assert pick(project_id=7, equipment_type="quad_axle") == 4
    assert pick(project_id=7, equipment_type="tandem") == 3
    assert pick(project_id=8, equipment_type="quad_axle") == 2
    assert pick(project_id=None, equipment_type=None) == 1
    assert choose_rate(rates, hauler_id=3, project_id=7, equipment_type=None, on=date(2026, 11, 3)).rate is None


def test_dated_agreements_expiry_and_no_fallback_from_an_expired_one():
    rates = [rate(id=1, hourly_rate=D("100")),
             rate(id=2, hourly_rate=D("95"), project_id=7, effective_from=date(2026, 6, 1), effective_to=date(2026, 9, 30)),
             rate(id=3, hourly_rate=D("97"), project_id=7, effective_from=date(2026, 12, 1))]
    on = lambda day: choose_rate(rates, hauler_id=1, project_id=7, equipment_type=None, on=day)  # noqa: E731
    assert on(date(2026, 5, 1)).rate.id == 1       # project agreement not started: company agreement
    assert on(date(2026, 7, 1)).rate.id == 2
    expired = on(date(2026, 11, 3))
    assert expired.rate is None and "ended on 2026-09-30" in expired.reason  # not the company's $100
    assert on(date(2026, 12, 2)).rate.id == 3


def test_two_agreements_starting_together_with_different_terms_are_never_picked_between():
    rates = [rate(id=1, hourly_rate=D("100")), rate(id=2, hourly_rate=D("105"))]
    assert choose_rate(rates, hauler_id=1, project_id=None, equipment_type=None, on=date(2026, 11, 3)).rate is None


@pytest.mark.parametrize("total, shares, parts", [
    (D("990.00"), [D("9")], [D("990.00")]),
    (D("990.00"), [D("6"), D("3")], [D("660.00"), D("330.00")]),
    (D("100.00"), [D("1"), D("1"), D("1")], [D("33.34"), D("33.33"), D("33.33")]),   # the odd cent goes first
    (D("763.43"), [D("2.5"), D("5.33")], [D("243.75"), D("519.68")]),
])
def test_allocation_adds_up_to_the_cent(total, shares, parts):
    assert allocate(total, shares) == parts and sum(parts) == total


def test_allocation_refuses_nothing_to_allocate():
    with pytest.raises(ShiftError):
        allocate(D("10"), [D("0")])
