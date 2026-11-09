"""Ten fictional supplier ticket layouts, as data.

A template is its size, paper, ink, printed header and a list of field specs.
Each field spec says where the value goes ("box": x0, y0, x1, y1 in ticket
pixels at 150 dpi), the printed label and its wording, how it's filled in
(hand, print, stamp, check), how likely an optional field is to be used, and
its decoration (underline, cell). `F` only expands a compact row description
into those plain dicts; there is no drawing logic here.
"""
from __future__ import annotations

TICKET_DPI = 150
INK_BLUE, INK_BLACK, INK_CARBON = (20, 40, 120), (25, 25, 32), (78, 52, 150)


def F(field, label, x, y, w, h, *, ink="hand", deco="line", label_pos="left", label_w=None, present=1.0,
      fmt=None, size=13, options=None, core=False):
    spec = {"field": field, "label": label, "ink": ink, "present": present, "fmt": fmt, "label_size": size,
            "core": core, "deco": deco}
    if label_pos == "left" and label:
        lw = label_w if label_w is not None else int(len(label) * size * 0.58) + 10
        spec.update(label_xy=[x, y + h - 6], label_anchor="ls", box=[x + lw, y, x + w, y + h],
                    deco_box=[x + lw, y, x + w, y + h])
    elif label_pos == "above" and label:
        spec.update(label_xy=[x + 5, y + 4], label_anchor="la", box=[x + 4, y + size + 6, x + w - 4, y + h - 3],
                    deco_box=[x, y, x + w, y + h])
    else:
        spec.update(label_xy=None, box=[x, y, x + w, y + h], deco_box=[x, y, x + w, y + h])
    if options:
        spec["options"] = options
    return spec


def _core(*specs):
    """The six fields TicketFlow reads are never optional on the form itself."""
    for s in specs:
        if s["field"] in ("ticket_number", "delivery_date", "material", "net_tons", "supplier", "job_location"):
            s["core"] = True
    return list(specs)


TEMPLATES = {
    "ridgeback_weigh": {
        "kind": "weigh ticket", "size": [600, 780], "paper": [252, 252, 248], "ink": INK_BLUE,
        "header": {"title": "CERTIFIED SCALE TICKET", "name_size": 25, "rule": True},
        "fields": _core(
            F("supplier", None, 24, 18, 430, 34, ink="print", deco=None, size=25),
            F("ticket_number", "No.", 440, 92, 140, 30, ink="stamp", deco=None, size=14),
            F("delivery_date", "Date", 24, 134, 270, 38, fmt="M/D/YY"),
            F("time", "Time In", 310, 134, 270, 38, present=0.8),
            F("truck", "Truck", 24, 182, 270, 38, present=0.9),
            F("po_number", "P.O.", 310, 182, 270, 38, present=0.5),
            F("job_location", "Job / Deliver To", 24, 232, 556, 40),
            F("material", "Material", 24, 282, 556, 40),
            F("gross", "GROSS LB", 24, 340, 180, 64, ink="print", deco="cell", label_pos="above", size=12),
            F("tare", "TARE LB", 210, 340, 180, 64, ink="print", deco="cell", label_pos="above", size=12),
            F("net_weight", "NET LB", 396, 340, 184, 64, ink="print", deco="cell", label_pos="above", size=12),
            F("net_tons", "NET TONS", 300, 420, 280, 80, deco="cell", label_pos="above", size=14),
            F("driver", "Driver", 24, 520, 556, 40, present=0.85),
        ),
        "footer_y": 750,
    },
    "cobalt_haul": {
        "kind": "haul ticket", "size": [600, 760], "paper": [238, 248, 236], "ink": INK_BLACK,
        "header": {"title": "HAUL TICKET", "name_size": 23, "rule": True},
        "fields": _core(
            F("supplier", None, 24, 18, 420, 32, ink="print", deco=None, size=23),
            F("ticket_number", "Ticket #", 330, 96, 250, 40),
            F("delivery_date", "Date", 24, 96, 290, 40, fmt="MM/DD/YYYY"),
            F("hauler", "Hauled By", 24, 148, 556, 40, present=0.7),
            F("truck", "Truck No.", 24, 200, 270, 40),
            F("load_number", "Load #", 310, 200, 270, 40, present=0.6),
            F("driver", "Driver", 24, 252, 556, 40, present=0.8),
            F("material", "Material", 24, 304, 556, 40),
            F("job_location", "Deliver To", 24, 356, 556, 40),
            F("net_tons", "TONS", 24, 420, 300, 84, deco="cell", label_pos="above", size=15),
            F("po_number", "Cust. PO", 340, 420, 240, 84, deco="cell", label_pos="above", present=0.4),
        ),
        "footer_y": 730,
    },
    "tallgrass_delivery": {
        "kind": "delivery slip", "size": [610, 740], "paper": [250, 245, 228], "ink": INK_BLUE,
        "header": {"title": "DELIVERY SLIP", "name_size": 24, "rule": False, "box": True},
        "fields": _core(
            F("supplier", None, 30, 24, 420, 32, ink="print", deco=None, size=24),
            F("ticket_number", "No.", 430, 110, 160, 30, ink="stamp", deco=None, size=14),
            F("delivery_date", "Date", 30, 150, 280, 40, fmt="M-D-YY"),
            F("po_number", "Customer PO", 320, 150, 270, 40, present=0.5),
            F("material", "Product", 30, 205, 560, 40),
            F("net_tons", "Qty (Tons)", 30, 260, 300, 44),
            F("truck", "Truck", 345, 260, 245, 44, present=0.7),
            F("job_location", "Ship To", 30, 318, 560, 40),
            F("driver", "Received By", 30, 380, 560, 40, present=0.6),
        ),
        "footer_y": 712,
    },
    "ironvale_carbon": {
        "kind": "carbon copy", "size": [600, 760], "paper": [252, 236, 240], "ink": INK_CARBON, "carbon": True,
        "header": {"title": "WEIGHT TICKET - CUSTOMER COPY", "name_size": 23, "rule": True},
        "fields": _core(
            F("supplier", None, 24, 18, 430, 32, ink="print", deco=None, size=23),
            F("ticket_number", "No.", 420, 92, 160, 30, ink="stamp", deco=None, size=14),
            F("delivery_date", "Date", 24, 134, 270, 40, fmt="MM/DD/YY"),
            F("truck", "Truck", 310, 134, 270, 40, present=0.9),
            F("material", "Material", 24, 186, 556, 40),
            F("job_location", "Job", 24, 238, 556, 40),
            F("gross", "Gross", 24, 296, 180, 68, deco="cell", label_pos="above"),
            F("tare", "Tare", 210, 296, 180, 68, deco="cell", label_pos="above"),
            F("net_weight", "Net (lb)", 396, 296, 184, 68, deco="cell", label_pos="above"),
            F("net_tons", "Net Tons", 24, 382, 300, 80, deco="cell", label_pos="above", size=14),
            F("driver", "Driver", 24, 484, 556, 40, present=0.7),
        ),
        "footer_y": 730,
    },
    "bluestem_landscape": {
        "kind": "landscape ticket", "size": [780, 540], "paper": [255, 255, 255], "ink": INK_BLUE,
        "landscape": True,
        "header": {"title": "ASPHALT DELIVERY", "name_size": 24, "rule": True},
        "fields": _core(
            F("supplier", None, 24, 16, 460, 32, ink="print", deco=None, size=24),
            F("ticket_number", "Ticket", 24, 100, 150, 76, ink="print", deco="cell", label_pos="above"),
            F("delivery_date", "Date", 174, 100, 150, 76, deco="cell", label_pos="above", fmt="M/D/YY"),
            F("material", "Mix", 324, 100, 222, 76, deco="cell", label_pos="above"),
            F("gross", "Gross T", 546, 100, 70, 76, ink="print", deco="cell", label_pos="above", size=11),
            F("tare", "Tare T", 616, 100, 70, 76, ink="print", deco="cell", label_pos="above", size=11),
            F("net_weight", "Net T", 686, 100, 70, 76, ink="print", deco="cell", label_pos="above", size=11),
            F("net_tons", "Net Tons Delivered", 24, 196, 300, 80, deco="cell", label_pos="above"),
            F("job_location", "Project / Location", 324, 196, 432, 80, deco="cell", label_pos="above"),
            F("truck", "Truck", 24, 296, 230, 70, deco="cell", label_pos="above", present=0.8),
            F("load_number", "Load", 254, 296, 120, 70, deco="cell", label_pos="above", present=0.6),
            F("driver", "Driver", 374, 296, 382, 70, deco="cell", label_pos="above", present=0.6),
        ),
        "footer_y": 515,
    },
    "granite_stub": {
        "kind": "ticket with tear-off stub", "size": [560, 800], "paper": [246, 246, 240], "ink": INK_BLUE,
        "header": {"title": "MATERIAL TICKET", "name_size": 22, "rule": True},
        "perforation_y": 610,
        "fields": _core(
            F("supplier", None, 22, 16, 400, 30, ink="print", deco=None, size=22),
            F("ticket_number", "No.", 380, 88, 160, 30, ink="stamp", deco=None, size=14),
            F("delivery_date", "Date", 22, 128, 250, 40, fmt="MM/DD/YYYY"),
            F("truck", "Truck", 290, 128, 250, 40, present=0.8),
            F("material", "Material", 22, 180, 518, 40),
            F("net_tons", "Tons", 22, 232, 260, 44),
            F("load_number", "Load", 300, 232, 240, 44, present=0.5),
            F("job_location", "Job Site", 22, 288, 518, 40),
            F("driver", "Driver", 22, 340, 518, 40, present=0.6),
            F("stub_ticket_number", "STUB No.", 22, 640, 250, 30, ink="stamp", deco=None, size=12),
            F("stub_net_tons", "Tons", 290, 640, 250, 44),
        ),
        "footer_y": 775,
    },
    "prairie_plant": {
        "kind": "plant-printed ticket", "size": [600, 780], "paper": [236, 244, 252], "ink": INK_BLACK,
        "header": {"title": "HOT MIX PLANT TICKET", "name_size": 22, "rule": True},
        "fields": _core(
            F("supplier", None, 24, 18, 450, 30, ink="print", deco=None, size=22),
            F("ticket_number", "TICKET NO", 24, 100, 270, 34, ink="print", deco=None, size=13),
            F("delivery_date", "DATE", 310, 100, 270, 34, ink="print", deco=None, fmt="MM/DD/YYYY"),
            F("time", "TIME", 24, 140, 270, 34, ink="print", deco=None),
            F("material", "MIX", 24, 180, 556, 34, ink="print", deco=None),
            F("gross", "GROSS", 24, 226, 180, 34, ink="print", deco=None, size=12),
            F("tare", "TARE", 210, 226, 180, 34, ink="print", deco=None, size=12),
            F("net_weight", "NET", 396, 226, 184, 34, ink="print", deco=None, size=12),
            F("net_tons", "NET TONS", 24, 266, 300, 34, ink="print", deco=None),
            F("job_location", "JOB", 24, 330, 556, 44),
            F("truck", "TRUCK", 24, 384, 270, 44),
            F("driver", "DRIVER", 310, 384, 270, 44, present=0.7),
            F("po_number", "PO", 24, 438, 270, 44, present=0.4),
        ),
        "footer_y": 750,
    },
    "kestrel_fill": {
        "kind": "fill tally", "size": [600, 740], "paper": [250, 250, 250], "ink": INK_BLUE,
        "header": {"title": "LOAD TALLY", "name_size": 23, "rule": True},
        "fields": _core(
            F("supplier", None, 24, 18, 430, 32, ink="print", deco=None, size=23),
            F("ticket_number", "Load Ticket", 24, 96, 300, 40),
            F("delivery_date", "Date", 340, 96, 240, 40, fmt="Mon D YYYY"),
            F("material", "Material (check one)", 24, 150, 556, 150, ink="check", deco="cell", label_pos="above",
              options=["Select Fill", "Common Fill", "Structural Fill", "Screened Topsoil"]),
            F("net_tons", "Est. Tons", 24, 320, 270, 44),
            F("truck", "Truck", 310, 320, 270, 44, present=0.9),
            F("job_location", "Dump Site", 24, 376, 556, 40),
            F("driver", "Driver", 24, 428, 556, 40, present=0.7),
        ),
        "footer_y": 712,
    },
    "copperleaf_grid": {
        "kind": "boxed grid form", "size": [620, 760], "paper": [255, 253, 246], "ink": INK_BLUE,
        "header": {"title": "RECYCLED MATERIAL TICKET", "name_size": 21, "rule": False, "box": True},
        "fields": _core(
            F("supplier", None, 28, 22, 440, 30, ink="print", deco=None, size=21),
            F("ticket_number", "TICKET NUMBER", 20, 100, 290, 72, ink="stamp", deco="cell", label_pos="above",
              size=11),
            F("delivery_date", "DATE", 310, 100, 290, 72, deco="cell", label_pos="above", size=11, fmt="M/D/YYYY"),
            F("material", "MATERIAL", 20, 172, 580, 72, deco="cell", label_pos="above", size=11),
            F("gross", "GROSS (T)", 20, 244, 194, 72, deco="cell", label_pos="above", size=11),
            F("tare", "TARE (T)", 214, 244, 193, 72, deco="cell", label_pos="above", size=11),
            F("net_weight", "NET (T)", 407, 244, 193, 72, deco="cell", label_pos="above", size=11),
            F("net_tons", "NET TONS BILLED", 20, 316, 290, 72, deco="cell", label_pos="above", size=11),
            F("load_number", "LOAD", 310, 316, 290, 72, deco="cell", label_pos="above", size=11, present=0.6),
            F("job_location", "JOB / PROJECT", 20, 388, 580, 72, deco="cell", label_pos="above", size=11),
            F("truck", "TRUCK", 20, 460, 290, 72, deco="cell", label_pos="above", size=11, present=0.8),
            F("driver", "DRIVER", 310, 460, 290, 72, deco="cell", label_pos="above", size=11, present=0.6),
        ),
        "footer_y": 735,
    },
    "northfork_long": {
        "kind": "long narrow ticket", "size": [560, 960], "paper": [248, 246, 236], "ink": INK_BLACK,
        "header": {"title": "DELIVERY TICKET", "name_size": 22, "rule": True},
        "fields": _core(
            F("supplier", None, 22, 18, 420, 30, ink="print", deco=None, size=22),
            F("ticket_number", "No.", 22, 100, 300, 42),
            F("delivery_date", "Date", 22, 160, 516, 42, fmt="M/D/YY"),
            F("time", "Time", 22, 220, 516, 42, present=0.5),
            F("material", "Material", 22, 280, 516, 42),
            F("job_location", "Job", 22, 340, 516, 42),
            F("truck", "Truck", 22, 400, 516, 42, present=0.8),
            F("driver", "Driver", 22, 460, 516, 42, present=0.6),
            F("po_number", "PO #", 22, 520, 516, 42, present=0.4),
            F("gross", "Gross lb", 22, 600, 516, 42, present=0.7),
            F("tare", "Tare lb", 22, 660, 516, 42, present=0.7),
            F("net_tons", "Net Tons", 22, 740, 300, 60, deco="cell", label_pos="above", size=14),
        ),
        "footer_y": 930,
    },
}

TEMPLATE_IDS = sorted(TEMPLATES)
CORE_FIELDS = ("ticket_number", "delivery_date", "material", "net_tons", "supplier", "job_location")
# The label names TicketFlow (and evaluate_workflow) use for the six fields.
TICKETFLOW_FIELD = {"ticket_number": "ticket_number", "delivery_date": "delivery_date", "material": "material_type",
                    "net_tons": "tonnage", "supplier": "supplier", "job_location": "job_location"}
# Some forms repeat a value (a stub). They point at the record field they copy.
ALIASES = {"stub_ticket_number": "ticket_number", "stub_net_tons": "net_tons"}


def check_templates() -> list[str]:
    """Static checks: boxes inside the ticket, no two value boxes overlapping."""
    problems = []
    for tid, t in TEMPLATES.items():
        w, h = t["size"]
        boxes = []
        for f in t["fields"]:
            x0, y0, x1, y1 = f["box"]
            if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
                problems.append(f"{tid}.{f['field']}: box {f['box']} outside {w}x{h}")
            boxes.append((f["field"], f["box"]))
        for i, (a, ba) in enumerate(boxes):
            for b, bb in boxes[i + 1:]:
                if ba[0] < bb[2] and bb[0] < ba[2] and ba[1] < bb[3] and bb[1] < ba[3]:
                    problems.append(f"{tid}: {a} and {b} boxes overlap")
        if not {f["field"] for f in t["fields"]} >= set(CORE_FIELDS):
            problems.append(f"{tid}: missing a core field")
    return problems
