"""Material, trucking and project cost reports for a date range, by day, week or month.

Only approved figures count: approved tickets at their locked price and approved truck
shifts at their locked cost, through each shift's allocations. A ticket approved without
a rate adds its tons but no cost, and is counted in ``unpriced`` so the report never
presents a missing price as a zero cost. Totals are added up by the database as whole
thousandths and cents (see ``aggregates``); days are grouped into weeks or months here.
Undated tickets can't be placed in a period and are counted apart.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from ..models import Batch, Hauler, Project, ShiftAllocation, Ticket, TruckShift
from . import aggregates

PERIODS = ("day", "week", "month")


def bucket(day: date, period: str) -> str:
    if period == "day":
        return day.isoformat()
    if period == "week":
        return (day - timedelta(days=day.weekday())).isoformat()  # the Monday it starts
    return day.strftime("%Y-%m")


def _tickets(organization_id: int, project_id: int | None, date_from: date | None, date_to: date | None, *columns):
    query = select(*columns).select_from(Ticket).where(Ticket.organization_id == organization_id, Ticket.status != "skipped")
    if project_id is not None:
        query = query.where(Ticket.batch_id.in_(select(Batch.id).where(
            Batch.organization_id == organization_id, Batch.project_id == project_id)))
    if date_from:
        query = query.where(Ticket.delivery_date >= date_from)
    if date_to:
        query = query.where(Ticket.delivery_date <= date_to)
    return query


def _counted():
    return (aggregates.counted_tickets().label("tickets"), aggregates.counted_tons().label("tons"),
            aggregates.counted_cost().label("cost"), aggregates.unpriced().label("unpriced"))


def _material_row(name, r) -> dict:
    return {"name": name, "tickets": int(r.tickets), "tons": str(aggregates.as_decimal(r.tons, 3)),
            "cost": str(aggregates.as_decimal(r.cost, 2)), "unpriced": int(r.unpriced)}


def build(db: Session, organization_id: int, project_id: int | None, date_from: date | None, date_to: date | None,
          period: str = "week") -> dict:
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")

    def grouped(column):
        label = func.coalesce(func.nullif(column, ""), "(unknown)")
        rows = db.execute(_tickets(organization_id, project_id, date_from, date_to, label.label("name"), *_counted()).group_by(label)).all()
        return sorted((_material_row(r.name, r) for r in rows if r.tickets), key=lambda r: (-Decimal(r["tons"]), r["name"]))

    totals = db.execute(_tickets(organization_id, project_id, date_from, date_to, *_counted())).one()
    waiting = db.scalar(_tickets(organization_id, project_id, date_from, date_to, func.count())
                        .where(Ticket.status.in_(("needs_review", "flagged", "failed"))))
    by_day = db.execute(_tickets(organization_id, project_id, date_from, date_to, Ticket.delivery_date.label("day"), *_counted())
                        .group_by(Ticket.delivery_date)).all()

    periods: dict[str, dict] = defaultdict(lambda: {"tickets": 0, "tons": 0, "material": 0, "unpriced": 0, "hours": 0, "trucking": 0, "loads": 0})
    undated = {"tickets": 0, "tons": 0, "cost": 0, "unpriced": 0}
    for r in by_day:
        if r.day is None:
            undated = {"tickets": int(r.tickets), "tons": int(r.tons), "cost": int(r.cost), "unpriced": int(r.unpriced)}
            continue
        if not r.tickets:
            continue
        p = periods[bucket(r.day, period)]
        p["tickets"] += int(r.tickets)
        p["tons"] += int(r.tons)          # thousandths
        p["material"] += int(r.cost)      # cents
        p["unpriced"] += int(r.unpriced)

    # Approved trucking, through allocations so a shift split across jobs counts once, in its parts.
    shifts = (select(TruckShift.work_date.label("day"), Hauler.name.label("hauler"),
                     aggregates.exact_sum(ShiftAllocation.hours, 2).label("hours"),
                     aggregates.exact_sum(ShiftAllocation.cost, 2).label("cost"))
              .select_from(ShiftAllocation).join(TruckShift, TruckShift.id == ShiftAllocation.shift_id)
              .join(Hauler, Hauler.id == TruckShift.hauler_id)
              .where(TruckShift.organization_id == organization_id, TruckShift.status == "approved"))
    loads = (select(TruckShift.work_date.label("day"), Hauler.name.label("hauler"), func.count().label("shifts"),
                    func.coalesce(func.sum(TruckShift.loads), 0).label("loads"))
             .join(Hauler, Hauler.id == TruckShift.hauler_id)
             .where(TruckShift.organization_id == organization_id, TruckShift.status == "approved"))
    if project_id is not None:
        shifts = shifts.where(ShiftAllocation.project_id == project_id)
        loads = loads.where(TruckShift.project_id == project_id)
    if date_from:
        shifts, loads = shifts.where(TruckShift.work_date >= date_from), loads.where(TruckShift.work_date >= date_from)
    if date_to:
        shifts, loads = shifts.where(TruckShift.work_date <= date_to), loads.where(TruckShift.work_date <= date_to)
    haulers: dict[str, dict] = defaultdict(lambda: {"shifts": 0, "hours": 0, "cost": 0, "loads": 0})
    for r in db.execute(shifts.group_by(TruckShift.work_date, Hauler.name)).all():
        haulers[r.hauler]["hours"] += int(r.hours)
        haulers[r.hauler]["cost"] += int(r.cost)
        p = periods[bucket(r.day, period)]
        p["hours"] += int(r.hours)
        p["trucking"] += int(r.cost)
    for r in db.execute(loads.group_by(TruckShift.work_date, Hauler.name)).all():
        haulers[r.hauler]["shifts"] += int(r.shifts)
        haulers[r.hauler]["loads"] += int(r.loads)
        periods[bucket(r.day, period)]["loads"] += int(r.loads)

    # Per project: tickets through their batch, trucking through each shift's allocations.
    projects = {p.id: p for p in db.scalars(select(Project).where(Project.organization_id == organization_id))}
    per_project: dict[int, dict] = defaultdict(lambda: {"tickets": 0, "tons": 0, "material": 0, "unpriced": 0, "hours": 0, "trucking": 0})
    for r in db.execute(_tickets(organization_id, project_id, date_from, date_to, Batch.project_id.label("pid"), *_counted())
                        .join(Batch, Batch.id == Ticket.batch_id).group_by(Batch.project_id)).all():
        if r.pid is None or not r.tickets:
            continue
        row = per_project[r.pid]
        row["tickets"], row["tons"], row["material"], row["unpriced"] = int(r.tickets), int(r.tons), int(r.cost), int(r.unpriced)
    by_alloc = (select(ShiftAllocation.project_id.label("pid"), aggregates.exact_sum(ShiftAllocation.hours, 2).label("hours"),
                       aggregates.exact_sum(ShiftAllocation.cost, 2).label("cost"))
                .join(TruckShift, TruckShift.id == ShiftAllocation.shift_id)
                .where(TruckShift.organization_id == organization_id, TruckShift.status == "approved"))
    if project_id is not None:
        by_alloc = by_alloc.where(ShiftAllocation.project_id == project_id)
    if date_from:
        by_alloc = by_alloc.where(TruckShift.work_date >= date_from)
    if date_to:
        by_alloc = by_alloc.where(TruckShift.work_date <= date_to)
    for r in db.execute(by_alloc.group_by(ShiftAllocation.project_id)).all():
        per_project[r.pid]["hours"] += int(r.hours)
        per_project[r.pid]["trucking"] += int(r.cost)

    # What each supplier charged per ton for each material: priced tons only, so a
    # delivery still waiting for its rate can't pull an average down.
    priced = and_(aggregates.COUNTED, Ticket.approved_cost.is_not(None))
    unit_prices = []
    supplier_label = func.coalesce(func.nullif(Ticket.supplier, ""), "(unknown)")
    material_label = func.coalesce(func.nullif(Ticket.material_type, ""), "(unknown)")
    for r in db.execute(_tickets(organization_id, project_id, date_from, date_to, supplier_label.label("supplier"), material_label.label("material"),
                                 aggregates.exact_sum(case((priced, Ticket.tonnage), else_=0), 3).label("tons"),
                                 aggregates.exact_sum(case((priced, Ticket.approved_cost), else_=0), 2).label("cost"),
                                 func.min(case((priced, Ticket.applied_unit_price))).label("low"),
                                 func.max(case((priced, Ticket.applied_unit_price))).label("high"))
                        .group_by(supplier_label, material_label)).all():
        if not r.tons:
            continue
        tons_d, cost_d = aggregates.as_decimal(r.tons, 3), aggregates.as_decimal(r.cost, 2)
        unit_prices.append({"supplier": r.supplier, "material": r.material, "tons": str(tons_d), "cost": str(cost_d),
                            "average": str((cost_d / tons_d).quantize(Decimal("0.01"))),
                            "low": str(Decimal(r.low).quantize(Decimal("0.01"))) if r.low is not None else None,
                            "high": str(Decimal(r.high).quantize(Decimal("0.01"))) if r.high is not None else None})
    unit_prices.sort(key=lambda u: (u["material"], Decimal(u["average"])))

    cents = lambda units: str(aggregates.as_decimal(units, 2))  # noqa: E731
    material = aggregates.as_decimal(totals.cost, 2)
    trucking = aggregates.as_decimal(sum(h["cost"] for h in haulers.values()), 2)
    return {
        "period": period, "date_from": date_from.isoformat() if date_from else None, "date_to": date_to.isoformat() if date_to else None,
        "material": {
            "tickets": int(totals.tickets), "tons": str(aggregates.as_decimal(totals.tons, 3)), "cost": str(material),
            "unpriced": int(totals.unpriced), "waiting": int(waiting or 0),
            "undated": {**undated, "tons": str(aggregates.as_decimal(undated["tons"], 3)), "cost": cents(undated["cost"])},
            "by_material": grouped(Ticket.material_type), "by_supplier": grouped(Ticket.supplier),
        },
        "trucking": {
            "shifts": sum(h["shifts"] for h in haulers.values()), "hours": cents(sum(h["hours"] for h in haulers.values())),
            "cost": str(trucking), "loads": sum(h["loads"] for h in haulers.values()),
            "by_hauler": sorted(({"name": name, "shifts": h["shifts"], "hours": cents(h["hours"]), "cost": cents(h["cost"]), "loads": h["loads"],
                                  "per_hour": str((Decimal(h["cost"]) / Decimal(h["hours"])).quantize(Decimal("0.01"))) if h["hours"] else None}
                                 for name, h in haulers.items()), key=lambda r: (-Decimal(r["cost"]), r["name"])),
            "billing_methods": [{"method": "hourly", "cost": str(trucking)}],
        },
        "unit_prices": unit_prices,
        "by_project": sorted(({"id": pid, "code": projects[pid].code, "name": projects[pid].name, "status": projects[pid].status,
                               "tickets": p["tickets"], "tons": str(aggregates.as_decimal(p["tons"], 3)), "material": cents(p["material"]),
                               "unpriced": p["unpriced"], "hours": cents(p["hours"]), "trucking": cents(p["trucking"]),
                               "total": cents(p["material"] + p["trucking"])}
                              for pid, p in per_project.items() if pid in projects), key=lambda p: (-Decimal(p["total"]), p["code"])),
        "costs": {"material": str(material), "trucking": str(trucking), "total": str(material + trucking),
                  "complete": int(totals.unpriced) == 0, "unpriced": int(totals.unpriced)},
        "by_period": [{"period": key, "tickets": p["tickets"], "tons": str(aggregates.as_decimal(p["tons"], 3)),
                       "material": cents(p["material"]), "unpriced": p["unpriced"], "hours": cents(p["hours"]),
                       "trucking": cents(p["trucking"]), "loads": p["loads"], "total": cents(p["material"] + p["trucking"])}
                      for key, p in sorted(periods.items())],
    }
