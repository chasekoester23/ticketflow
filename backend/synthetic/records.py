"""The structured generator: fictional delivery records, the source of truth.

Everything downstream (the drawn ticket, the scan, the labels) is derived from
these records. Same seed, same records, byte for byte.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from . import RECORDS_SCHEMA
from .util import rng

# Invented companies. Phone numbers use the 555-01xx range reserved for fiction.
# Each supplier prints its own form, so supplier and template go together.
SUPPLIERS = [
    {"id": "ridgeback", "name": "Ridgeback Hollow Aggregates", "town": "Harlow Creek", "phone": "555-0141",
     "template": "ridgeback_weigh", "categories": ["aggregate"], "number": ("", 6), "scale": "lb"},
    {"id": "cobalt", "name": "Cobalt Creek Materials", "town": "Wendmere", "phone": "555-0117",
     "template": "cobalt_haul", "categories": ["fill", "aggregate"], "number": ("H", 5), "scale": None},
    {"id": "tallgrass", "name": "Tallgrass Quarry Co", "town": "Ashby Flats", "phone": "555-0163",
     "template": "tallgrass_delivery", "categories": ["aggregate"], "number": ("", 5), "scale": None},
    {"id": "ironvale", "name": "Ironvale Sand & Gravel", "town": "Port Calder", "phone": "555-0108",
     "template": "ironvale_carbon", "categories": ["sand", "aggregate"], "number": ("S", 5), "scale": "lb"},
    {"id": "bluestem", "name": "Bluestem Asphalt Supply", "town": "Millbrook Junction", "phone": "555-0152",
     "template": "bluestem_landscape", "categories": ["asphalt"], "number": ("B", 6), "scale": "ton"},
    {"id": "granite", "name": "Granite Hollow Stone", "town": "Easterly", "phone": "555-0129",
     "template": "granite_stub", "categories": ["aggregate", "riprap"], "number": ("", 6), "scale": None},
    {"id": "prairie", "name": "Prairie Line Paving Materials", "town": "Fennick", "phone": "555-0186",
     "template": "prairie_plant", "categories": ["asphalt"], "number": ("P", 5), "scale": "lb"},
    {"id": "kestrel", "name": "Kestrel Fill & Grading", "town": "Lower Brannock", "phone": "555-0174",
     "template": "kestrel_fill", "categories": ["fill"], "number": ("K", 4), "scale": None},
    {"id": "copperleaf", "name": "Copperleaf Recycled Aggregates", "town": "Sallow Point", "phone": "555-0135",
     "template": "copperleaf_grid", "categories": ["recycled", "aggregate"], "number": ("C", 5), "scale": "ton"},
    {"id": "northfork", "name": "Northfork Rock & Supply", "town": "Quillan", "phone": "555-0199",
     "template": "northfork_long", "categories": ["aggregate", "sand", "fill"], "number": ("", 5), "scale": "lb"},
]
SUPPLIER_BY_ID = {s["id"]: s for s in SUPPLIERS}

MATERIALS = {
    "aggregate": ["No. 57 Stone", "No. 8 Stone", "Crusher Run", "Dense Graded Aggregate", "3/4 Crushed Limestone",
                  "Pea Gravel", "Base Rock"],
    "sand": ["Washed Concrete Sand", "Mason Sand", "Fill Sand"],
    "asphalt": ["Surface Mix 9.5mm", "Base Mix 19mm", "Hot Mix Asphalt", "Binder Course 12.5mm"],
    "fill": ["Select Fill", "Common Fill", "Structural Fill", "Screened Topsoil"],
    "riprap": ["Rip Rap Class 2", "Rip Rap Class 1"],
    "recycled": ["Crushed Concrete RCA", "Asphalt Millings"],
}
TONS_RANGE = {"aggregate": (14, 26), "sand": (12, 24), "asphalt": (15, 24), "fill": (10, 22), "riprap": (12, 22),
              "recycled": (12, 24)}

PROJECTS = [
    ("SYN-1042", "Harlow Creek Bridge", ["Harlow Creek Bridge", "Sta 42+50 Harlow Rd", "Bridge Abutment East"]),
    ("SYN-2207", "Larkspur Road Widening", ["1200 Larkspur Rd", "Larkspur Rd & 9th", "Larkspur Phase 2"]),
    ("SYN-3315", "Fenwick Business Park", ["Lot 7 Fenwick Park", "Fenwick Park Bldg C", "4410 Fenwick Pkwy"]),
    ("SYN-4180", "Quail Run Subdivision", ["Quail Run Ph 2", "Quail Run Lot 31", "88 Covey Ln"]),
    ("SYN-5023", "Ashby Water Plant", ["Ashby Water Plant", "Plant Rd Gate 2"]),
]
DRIVERS = ["Dale M", "Rosa T", "J. Pruitt", "Ken O", "Marta V", "B. Ostrow", "Lee H", "Tomas R", "Ava K", "Gus W"]
HAULERS = ["Fieldstone Trucking", "Red Mesa Haul", "Two Pines Transport", "Own Truck"]
DATE_START = date(2026, 3, 2)
DATE_DAYS = 240  # through late October 2026: always in the past for TicketFlow's checks


def rate_sheet(seed: int) -> list[dict]:
    """A fictional price per ton for every supplier and material it sells."""
    out = []
    for supplier in SUPPLIERS:
        r = rng("rates", seed, supplier["id"])
        for category in supplier["categories"]:
            for material in MATERIALS[category]:
                price = Decimal(r.randrange(36, 196)) / 4  # $9.00 to $48.75 in quarters
                out.append({"supplier": supplier["name"], "material": material, "unit_price": f"{price:.2f}"})
    return out


def _ticket_number(supplier: dict, value: int) -> str:
    prefix, digits = supplier["number"]
    return f"{prefix}{value:0{digits}d}"  # never wraps, so numbers stay unique per supplier


def generate_records(seed: int, count: int, *, suppliers: list[str] | None = None) -> list[dict]:
    """`count` fictional delivery records. `suppliers` fixes the supplier of each
    record in turn (for stratified sets); otherwise suppliers are drawn at random."""
    r = rng("records", seed)
    rates = {(x["supplier"], x["material"]): x["unit_price"] for x in rate_sheet(seed)}
    next_number = {s["id"]: r.randrange(10 ** (s["number"][1] - 1), 9 * 10 ** (s["number"][1] - 1))
                   for s in SUPPLIERS}
    records = []
    for i in range(count):
        supplier = SUPPLIER_BY_ID[suppliers[i % len(suppliers)]] if suppliers else r.choice(SUPPLIERS)
        category = r.choice(supplier["categories"])
        material = r.choice(MATERIALS[category])
        lo, hi = TONS_RANGE[category]
        if r.random() < 0.06:
            lo, hi = 2, 8  # a part load
        net_tons = Decimal(r.randrange(lo * 100, hi * 100)) / 100
        next_number[supplier["id"]] += r.randrange(1, 40)
        code, project_name, locations = r.choice(PROJECTS)
        delivered = DATE_START + timedelta(days=r.randrange(DATE_DAYS))
        hour, minute = r.randrange(6, 17), r.randrange(60)
        rec = {
            "ticket_id": f"T{i + 1:04d}",
            "supplier_id": supplier["id"],
            "supplier": supplier["name"],
            "supplier_town": supplier["town"],
            "supplier_phone": supplier["phone"],
            "template": supplier["template"],
            "ticket_number": _ticket_number(supplier, next_number[supplier["id"]]),
            "delivery_date": delivered.isoformat(),
            "time": f"{hour:02d}:{minute:02d}",
            "material": material,
            "material_category": category,
            "net_tons": f"{net_tons:.2f}",
            "weight_unit": supplier["scale"],
            "gross": None, "tare": None, "net_weight": None,
            "truck": r.choice(["T-", "#", "", "Trk "]) + str(r.randrange(3, 480)),
            "driver": r.choice(DRIVERS),
            "hauler": r.choice(HAULERS),
            "po_number": f"PO {r.randrange(1000, 9999)}" + r.choice(["", "-A", "-B", ""]),
            "load_number": str(r.randrange(1, 14)),
            "project_code": code,
            "project_name": project_name,
            "job_location": r.choice(locations),
            "unit_price": rates[(supplier["name"], material)],
        }
        if supplier["scale"] == "lb":
            tare = r.randrange(1300, 1750) * 20  # 26,000 to 35,000 lb, in 20 lb steps
            net = int(net_tons * 2000)  # exact: net_tons has two decimals, so net is a multiple of 20 lb
            rec.update(gross=str(tare + net), tare=str(tare), net_weight=str(net))
        elif supplier["scale"] == "ton":
            tare = Decimal(r.randrange(1250, 1700)) / 100
            rec.update(gross=f"{tare + net_tons:.2f}", tare=f"{tare:.2f}", net_weight=f"{net_tons:.2f}")
        records.append(rec)
    return records


def check_weights(rec: dict) -> list[str]:
    """Problems with a record's gross/tare/net arithmetic (empty when consistent)."""
    unit, problems = rec.get("weight_unit"), []
    if unit is None:
        if any(rec.get(k) is not None for k in ("gross", "tare", "net_weight")):
            problems.append("weights present without a unit")
        return problems
    gross, tare, net, tons = (Decimal(rec[k]) for k in ("gross", "tare", "net_weight", "net_tons"))
    if gross - tare != net:
        problems.append(f"gross {gross} - tare {tare} != net {net}")
    if (net / 2000 if unit == "lb" else net) != tons:
        problems.append(f"net {net} {unit} is not {tons} tons")
    return problems


def records_document(seed: int, records: list[dict]) -> dict:
    return {"schema": RECORDS_SCHEMA, "synthetic": True, "seed": seed, "records": records,
            "rate_sheet": rate_sheet(seed)}


CSV_COLUMNS = ["ticket_id", "supplier", "ticket_number", "delivery_date", "time", "material", "net_tons",
               "weight_unit", "gross", "tare", "net_weight", "truck", "driver", "hauler", "po_number",
               "load_number", "project_code", "project_name", "job_location", "unit_price", "template"]


def records_csv(records: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for rec in records:
        writer.writerow({k: "" if rec.get(k) is None else rec[k] for k in CSV_COLUMNS})
    return buf.getvalue()
