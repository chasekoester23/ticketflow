"""Every route, generated from the live inventory: another company's records must look
exactly like records that don't exist, and every role is refused what it can't do.

Ids are tried in the path, in query parameters, in JSON bodies and in upload forms.
A new route, or a new id-shaped parameter on an old one, fails here until it's
covered, so nothing can be added unchecked. Two companies with identical fictional
data are built through the API, each marked with its own word, so a leak shows.
"""
from __future__ import annotations

import io
import re
import threading
import zipfile
from decimal import Decimal

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.auth import current_user
from app.config import settings
from app.main import app
from app.models import InvoiceTicketLink, Organization, ScanSlot, User
from app.permissions import PERMISSIONS, ROLES
from app.route_inventory import ROUTERS, SELF_SERVICE

from .sample_data import TICKETS, make_page, make_pdf
from .sample_invoices import letterhead_supplier
from .test_concurrency import race  # noqa: F401  (a fixture: real file database, real threads)
from .test_query_budgets import _session

NOWHERE = 987654  # An id no company has.
MARK = {1: "ALPHA-MARK", 2: "BRAVO-MARK"}
SELF = SELF_SERVICE | {"/api/auth/me"}
# Path parameter -> which record it names. A new parameter must be added here.
PATH_PARAMS = {"batch_id": "batch", "ticket_id": "ticket", "link_id": "link", "document_id": "document", "file_id": "file",
               "slot_id": "slot", "view_id": "view", "member_id": "member", "sid": "supplier", "mid": "material", "rid": "rate",
               "pid": "project", "alias_id": "alias", "project_id": "project"}
# Query, body and form fields that name a record. Anything else ending in _id or _ids must be added.
FIELDS = {"project_id": "project", "ticket_id": "ticket", "ticket_ids": "ticket", "supplier_id": "supplier",
          "material_id": "material", "target_id": "supplier", "view_id": "view"}


def _record(param: str, path: str) -> str:
    if param == "line_id":
        return "draft" if path.startswith("/api/invoice-documents") else "line"
    return PATH_PARAMS[param]


def _routes() -> list[tuple[str, APIRoute]]:
    found = []
    for router in ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute):
                found += [(method, route) for method in sorted(route.methods - {"HEAD", "OPTIONS"}) if route.path not in SELF]
    return found


def _id_fields(route: APIRoute) -> list[tuple[str, str]]:
    """(where, name) for every id-shaped query, body or form field on a route."""
    fields = [("query", p.name) for p in route.dependant.query_params]
    for param in route.dependant.body_params:
        model = getattr(param.field_info.annotation, "model_fields", None)
        if model is not None:
            fields += [("json", name) for name in model]
        elif type(param.field_info).__name__ == "Form":
            fields.append(("form", param.name))
    return [(where, name) for where, name in fields if name.endswith(("_id", "_ids"))]


def _identity(request: Request) -> User:
    """Which person is asking, from a test-only header, so two companies can ask at once."""
    org, role = request.headers.get("X-Test-As", "1:owner").split(":")
    org = int(org)
    return User(id=org * 10 + ROLES.index(role), organization_id=org, username=f"{role}-{org}", display_name=f"{role.title()} {org}",
                password_hash="x", active=True, role=role, must_change_password=False)


def _as(client, org: int, role: str = "owner") -> None:
    client.headers["X-Test-As"] = f"{org}:{role}"


SCAN = make_pdf([make_page(TICKETS)])
INVOICE = letterhead_supplier()


def _company(client, org: int) -> dict:
    """The same fictional records for each company, made the way people make them."""
    _as(client, org)
    ids = {"project": client.post("/api/projects", json={"code": "JOB-1", "name": f"Fictional job {MARK[org]}"}).json()["id"]}
    ids["supplier"] = client.post("/api/suppliers", json={"name": "Example Aggregates"}).json()["id"]
    ids["material"] = client.post("/api/materials", json={"name": "Sand"}).json()["id"]
    ids["rate"] = client.put("/api/rates", json={"supplier_id": ids["supplier"], "material_id": ids["material"], "unit_price": "10"}).json()["id"]
    ids["alias"] = client.post("/api/name-aliases", json={"kind": "supplier", "alias": "Exmple Aggregates", "target_id": ids["supplier"]}).json()["id"]
    ids["view"] = client.post("/api/saved-views", json={"name": "Approved", "filters": {"status": "verified"}}).status_code and \
        client.get("/api/saved-views").json()[0]["id"]
    batch = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "manual", "project_id": str(ids["project"])},
                        files=[("files", ("scan.pdf", SCAN, "application/pdf"))]).json()
    ids["batch"], ids["file"] = batch["id"], batch["files"][0]["id"]
    tickets = client.get(f"/api/batches/{batch['id']}/tickets").json()
    ids["ticket"], ids["ticket2"] = tickets[0]["id"], tickets[1]["id"]
    for ticket, number in ((tickets[0], "10482"), (tickets[1], "10483")):
        client.patch(f"/api/tickets/{ticket['id']}", json=dict(ticket_number=number, delivery_date="2026-09-15", material_type="Sand",
                                                                tonnage="20.000", supplier="Example Aggregates", job_location=MARK[org]))
    assert client.post(f"/api/tickets/{ids['ticket']}/verify").status_code == 200
    with _session() as db:
        ids["slot"] = db.scalar(select(ScanSlot.id).where(ScanSlot.batch_id == batch["id"], ScanSlot.position == 3))
    line = client.post("/api/invoice-comparisons", json={
        "project_id": ids["project"], "invoice_number": f"INV-{MARK[org]}", "supplier_name": "Example Aggregates", "material_name": "Sand",
        "date_from": "2026-09-01", "date_to": "2026-09-30", "billed_tons": "20", "billed_unit_price": "10", "billed_subtotal": "200",
        "ticket_list": "10482\t20"}).json()
    ids["line"] = line["id"]
    linked = client.post(f"/api/invoice-comparisons/{line['id']}/ticket-links", json={"ticket_ids": [ids["ticket"]]}).json()
    ids["link"] = linked["ticket_links"][0]["id"]
    document = client.post("/api/invoice-documents", data={"project_id": str(ids["project"])},
                           files={"file": ("invoice.pdf", INVOICE, "application/pdf")}).json()
    ids["document"], ids["draft"] = document["id"], document["lines"][0]["id"]
    ids["member"] = org * 10 + ROLES.index("clerk")
    return ids


@pytest.fixture()
def world(client, monkeypatch):
    monkeypatch.setattr(settings, "review_timing_enabled", True)
    with _session() as db:
        db.add(Organization(id=2, name="Second Fictional Company"))
        db.flush()
        for org in (1, 2):
            for role in ROLES:
                if org * 10 + ROLES.index(role) != 1 and db.get(User, org * 10 + ROLES.index(role)) is None:
                    db.add(User(id=org * 10 + ROLES.index(role), organization_id=org, username=f"{role}-{org}",
                                display_name=f"{role.title()} {org}", password_hash="x", role=role))
        db.commit()
    # The fixture's owner is user 1; this test's owner of company 1 is user 10.
    with _session() as db:
        if db.get(User, 10) is None:
            db.add(User(id=10, organization_id=1, username="owner-1", display_name="Owner 1", password_hash="x", role="owner"))
            db.commit()
    app.dependency_overrides[current_user] = _identity
    ids = {org: _company(client, org) for org in (1, 2)}
    _as(client, 1)
    return ids


def _body(method: str, path: str, ids: dict):
    """A valid request for each route, so the answer is about whose record it is, never
    about a malformed body. Returns (json, data, files, params)."""
    line = {"project_id": ids["project"], "invoice_number": "INV-SWEEP", "supplier_name": "Example Aggregates", "material_name": "Sand",
            "date_from": "2026-10-01", "date_to": "2026-10-31", "billed_tons": "5", "billed_unit_price": "10", "billed_subtotal": "50",
            "ticket_list": ""}
    bodies = {
        ("POST", "/api/batches"): (None, {"layout": "single", "intake_mode": "manual", "project_id": str(ids["project"])},
                                   [("files", ("scan.pdf", SCAN, "application/pdf"))], None),
        ("PATCH", "/api/batches/{batch_id}/project"): {"project_id": ids["project"]},
        ("PATCH", "/api/tickets/{ticket_id}"): {"job_location": "Swept"},
        ("POST", "/api/tickets/{ticket_id}/verify"): {},
        ("POST", "/api/tickets/{ticket_id}/flag"): {"note": "swept"},
        ("POST", "/api/suppliers"): {"name": "Sweep Supplier"},
        ("POST", "/api/materials"): {"name": "Sweep Stone"},
        ("POST", "/api/name-aliases"): {"kind": "supplier", "alias": "Sweep Alias", "target_id": ids["supplier"]},
        ("PUT", "/api/rates"): {"supplier_id": ids["supplier"], "material_id": ids["material"], "unit_price": "12"},
        ("POST", "/api/rates/import"): {"text": "Example Aggregates,Sand,12", "apply": False},
        ("POST", "/api/invoice-comparisons"): line,
        ("PUT", "/api/invoice-comparisons/{line_id}"): {**line, "invoice_number": f"INV-{MARK[1]}", "date_from": "2026-09-01", "date_to": "2026-09-30"},
        ("POST", "/api/invoice-comparisons/{line_id}/ticket-links"): {"ticket_ids": [ids["ticket"]]},
        ("POST", "/api/projects"): {"code": "SWEEP", "name": "Sweep"},
        ("PATCH", "/api/projects/{pid}"): {"name": "Renamed"},
        ("POST", "/api/review-timings"): {"ticket_id": ids["ticket"], "seconds": "5"},
        ("POST", "/api/tickets/bulk-edit"): {"ticket_ids": [ids["ticket2"]], "field": "job_location", "value": "Bulk"},
        ("POST", "/api/records/rename"): {"field": "supplier", "from_value": "Example Aggregates", "to_value": "Example Aggregates",
                                          "project_id": ids["project"]},
        ("POST", "/api/saved-views"): {"name": "Sweep view", "filters": {}},
        ("POST", "/api/team/members"): {"username": "sweep-person", "role": "clerk"},
        ("PATCH", "/api/team/members/{member_id}"): {"display_name": "Swept"},
        ("PUT", "/api/batches/{batch_id}/expected-count"): {"expected_ticket_count": 4},
        ("POST", "/api/scan-slots/{slot_id}/rotate"): {"rotation": 90},
        ("POST", "/api/invoice-documents"): (None, {"project_id": str(ids["project"])}, {"file": ("i.pdf", INVOICE, "application/pdf")}, None),
        ("POST", "/api/invoice-documents/{document_id}/lines"): (None, None, None, {"page": 1}),
        ("PATCH", "/api/invoice-documents/{document_id}/lines/{line_id}"): {"billed_quantity": "7"},
    }
    found = bodies.get((method, path))
    if isinstance(found, tuple):
        return found
    if found is None and method != "GET" and route_has_body(method, path):
        raise AssertionError(f"Add a valid body for {method} {path} to this sweep")
    params = {"project_id": ids["project"]} if method == "GET" and path in QUERY_PROJECT else None
    if path == "/api/records/name-variants":
        params = {"field": "supplier", "project_id": ids["project"]}
    return found, None, None, params


QUERY_PROJECT = {"/api/batches/queue", "/api/records", "/api/records/export.xlsx", "/api/dashboard", "/api/reports/pilot",
                 "/api/reports/pilot.csv", "/api/records/search", "/api/records/export.csv"}


def route_has_body(method: str, path: str) -> bool:
    route = next(r for m, r in _routes() if m == method and r.path == path)
    return bool(route.dependant.body_params) and not all(
        getattr(p.field_info, "default", None) is None and type(p.field_info).__name__ == "Body" for p in route.dependant.body_params)


def _url(path: str, ids: dict, swap: dict | None = None) -> str:
    def fill(match):
        name = match.group(1)
        if name == "page_no":
            return "1"
        if swap and name in swap:
            return str(swap[name])
        return str(ids[_record(name, path)])
    return re.sub(r"{([^}]+)}", fill, path)


def _send(client, method: str, path: str, ids: dict, *, path_swap=None, field_swap=None):
    json, data, files, params = _body(method, path, ids)
    if field_swap:
        where, name, value = field_swap
        if where == "json":
            json = {**json, name: [value] if name.endswith("_ids") else value}
        elif where == "form":
            data = {**data, name: str(value)}
        else:
            params = {**(params or {}), name: value}
    kwargs = {"params": params}
    if files is not None:
        kwargs.update(data=data, files=files)
    elif json is not None:
        kwargs["json"] = json
    return client.request(method, _url(path, ids, path_swap), **kwargs)


def _answer(response, asked: int | None = None) -> tuple:
    """Status and message, with the id that was asked about blanked out: a refusal may
    quote it back, and that says nothing about whose it is."""
    try:
        detail = response.json().get("detail") if isinstance(response.json(), dict) else response.json()
    except ValueError:
        detail = response.content[:200]
    text = str(detail)
    return response.status_code, text.replace(f"#{asked}:", "#ID:") if asked is not None else text


def _companys_view(client, org: int) -> list:
    """Everything a company can read about itself. Must be unchanged by the other's attempts."""
    _as(client, org)
    views = [client.get(path).json() for path in ("/api/projects", "/api/batches", "/api/records", "/api/suppliers", "/api/materials",
                                                   "/api/rates", "/api/name-aliases", "/api/saved-views", "/api/invoice-comparisons",
                                                   "/api/invoice-documents", "/api/team/members", "/api/dashboard")]
    _as(client, 1)
    return views


def test_every_route_and_id_parameter_is_covered_by_this_sweep():
    """Fails when a route or parameter appears that the sweeps below don't understand."""
    unknown = []
    for method, route in _routes():
        for name in re.findall(r"{([^}]+)}", route.path):
            if name not in PATH_PARAMS and name not in ("line_id", "page_no"):
                unknown.append(f"{method} {route.path}: path {name}")
        for where, name in _id_fields(route):
            if name not in FIELDS:
                unknown.append(f"{method} {route.path}: {where} {name}")
    assert unknown == []
    assert len(_routes()) > 80


def test_another_companys_ids_in_the_path_look_like_ids_that_dont_exist(client, world):
    mine, theirs = world[1], world[2]
    before = _companys_view(client, 2)
    checked = 0
    for method, route in _routes():
        names = re.findall(r"{([^}]+)}", route.path)
        names = [n for n in names if n != "page_no"]
        if not names:
            continue
        # All of theirs, then each one of theirs among my own.
        for chosen in [names] + ([[n] for n in names] if len(names) > 1 else []):
            other = {n: theirs[_record(n, route.path)] for n in chosen}
            missing = {n: NOWHERE for n in chosen}
            leaked = _answer(_send(client, method, route.path, mine, path_swap=other))
            absent = _answer(_send(client, method, route.path, mine, path_swap=missing))
            assert leaked == absent, f"{method} {route.path} with company 2's {chosen}: {leaked} but a missing id gives {absent}"
            assert leaked[0] == 404, (method, route.path, leaked)
            checked += 1
    assert checked > 50
    assert _companys_view(client, 2) == before


def test_another_companys_ids_in_queries_bodies_and_forms_look_like_ids_that_dont_exist(client, world):
    mine, theirs = world[1], world[2]
    before = _companys_view(client, 2)
    checked = 0
    for method, route in _routes():
        for where, name in _id_fields(route):
            if (where, name) == ("query", "project_id") and route.path not in QUERY_PROJECT:
                continue
            leaked = _send(client, method, route.path, mine, field_swap=(where, name, theirs[FIELDS[name]]))
            absent = _send(client, method, route.path, mine, field_swap=(where, name, NOWHERE))
            theirs_answer, nowhere_answer = _answer(leaked, theirs[FIELDS[name]]), _answer(absent, NOWHERE)
            assert theirs_answer[0] == nowhere_answer[0], f"{method} {route.path} {where} {name}: {theirs_answer} vs {nowhere_answer}"
            if leaked.status_code >= 400:
                assert theirs_answer == nowhere_answer, (method, route.path, name)
            else:
                assert MARK[2] not in leaked.text, f"{method} {route.path} {where} {name} showed company 2's records"
            checked += 1
    assert checked >= 20
    assert _companys_view(client, 2) == before


def _text_of(response) -> str:
    if response.headers.get("content-type", "").startswith("application/vnd.openxmlformats"):
        with zipfile.ZipFile(io.BytesIO(response.content)) as book:
            return " ".join(book.read(name).decode("utf-8", "replace") for name in book.namelist())
    return response.content.decode("utf-8", "replace")


@pytest.mark.parametrize("me, other", [(1, 2), (2, 1)])
def test_nothing_any_read_returns_mentions_the_other_company(client, world, me, other):
    """Every read, file and export, with my own ids. The other's word must never appear."""
    _as(client, me)
    read = 0
    for method, route in _routes():
        if method != "GET":
            continue
        response = _send(client, method, route.path, world[me])
        assert response.status_code == 200, (route.path, response.status_code, response.text[:200])
        assert MARK[other] not in _text_of(response), f"{route.path} showed company {other}'s records"
        assert MARK[me] in _text_of(response) or not response.headers.get("content-type", "").startswith(("application/json", "text/csv")) \
            or route.path in NOT_SHOWING_MARKS, route.path
        read += 1
    assert read > 35


# Reads that legitimately show nothing with a company's word in it.
NOT_SHOWING_MARKS = {"/api/batches", "/api/batches/queue", "/api/batches/{batch_id}", "/api/suppliers", "/api/materials", "/api/rates",
                     "/api/name-aliases", "/api/pricing/history", "/api/saved-views", "/api/onboarding", "/api/system/status",
                     "/api/team/members", "/api/team/events", "/api/batches/{batch_id}/manifest", "/api/batches/{batch_id}/scan-events",
                     "/api/tickets/{ticket_id}/audit", "/api/invoice-comparisons/{line_id}/audit", "/api/invoice-documents",
                     "/api/invoice-documents/{document_id}", "/api/invoice-documents/{document_id}/history",
                     "/api/invoice-comparisons/overview", "/api/reports/pilot", "/api/reports/pilot.csv", "/api/records/name-variants",
                     "/api/dashboard", "/api/setup/status",
                     # Counts of reads and tokens, never a record.
                     "/api/system/usage",
                     "/api/invoice-comparisons/discrepancies.csv",
                     # Only a header when every line matches and every delivery is billed.
                     "/api/reports/project/{project_id}/unbilled.csv"}


@pytest.mark.parametrize("role", [r for r in ROLES if r != "owner"])
def test_every_role_is_refused_exactly_what_it_lacks(client, world, role):
    """Refusals first, on every route, then proof that nothing changed. Then the reads a
    role is allowed must not be refused by the permission guard."""
    before = [_companys_view(client, org) for org in (1, 2)]
    _as(client, 1, role)
    refused = allowed = 0
    for method, route in _routes():
        needed = [p for p in _route_permissions(route)]
        lacks = [p for p in needed if role not in PERMISSIONS[p]]
        response = _send(client, method, route.path, world[1]) if lacks or method == "GET" else None
        if lacks:
            assert response.status_code == 403, (role, method, route.path, response.status_code)
            assert "does not allow this action" in str(response.json()["detail"]), (role, method, route.path)
            refused += 1
        elif response is not None:
            assert response.status_code != 403, (role, method, route.path, response.text)
            allowed += 1
    _as(client, 1)
    assert refused >= {"manager": 1, "clerk": 15, "viewer": 30}[role] and allowed > 30
    assert [_companys_view(client, org) for org in (1, 2)] == before


def _route_permissions(route: APIRoute) -> list[str]:
    found = []

    def walk(dependant):
        for dep in dependant.dependencies:
            if getattr(dep.call, "permission", None):
                found.append(dep.call.permission)
            walk(dep)
    walk(route.dependant)
    return sorted(set(found))


def test_a_price_override_needs_a_manager_even_through_an_edit(client, world):
    _as(client, 1, "clerk")
    response = client.patch(f"/api/tickets/{world[1]['ticket2']}", json={"unit_price_override": "1"})
    assert response.status_code == 403
    _as(client, 1)
    assert client.get(f"/api/batches/{world[1]['batch']}/tickets").json()[1]["unit_price_override"] is None


def test_a_link_to_another_companys_ticket_cant_be_made_or_released(client, world):
    mine, theirs = world[1], world[2]
    refused = client.post(f"/api/invoice-comparisons/{mine['line']}/ticket-links", json={"ticket_ids": [theirs["ticket"]]})
    assert refused.status_code == 409 and f"#{theirs['ticket']}: not listed" in refused.json()["detail"]
    with _session() as db:
        links = db.scalars(select(InvoiceTicketLink).where(InvoiceTicketLink.released_at.is_(None))).all()
        assert sorted((link.comparison_id, link.ticket_id) for link in links) == sorted(
            [(mine["line"], mine["ticket"]), (theirs["line"], theirs["ticket"])])


# ------------------------------------------------------------ both companies at once

def test_two_companies_with_identical_data_working_at_the_same_moment_never_cross(race, monkeypatch):
    """Real threads, real connections. Each company approves, reads, exports and bills
    its own copy of the same paperwork; neither may see or change the other's."""
    from app.routers import api as api_module
    client = race.client
    # Uploads are processed straight after the request, as the client fixture does.
    monkeypatch.setattr(api_module, "SessionLocal", race.Session)
    monkeypatch.setattr(api_module, "_notify_worker", lambda background, _job: background.add_task(api_module._drain_job_for_tests))
    with race.Session() as db:
        db.add(Organization(id=2, name="Second Fictional Company"))
        db.flush()
        for org in (1, 2):
            for role in ROLES:
                if db.get(User, org * 10 + ROLES.index(role)) is None:
                    db.add(User(id=org * 10 + ROLES.index(role), organization_id=org, username=f"{role}-{org}", password_hash="x", role=role))
        db.commit()
    app.dependency_overrides[current_user] = _identity
    worlds = {}
    for org in (1, 2):
        client.headers["X-Test-As"] = f"{org}:owner"
        worlds[org] = _company(client, org)
    del client.headers["X-Test-As"]
    results: dict[int, list] = {1: [], 2: []}

    def work(org: int):
        headers = {"X-Test-As": f"{org}:owner"}
        ids = worlds[org]
        seen = results[org]
        for round_ in range(6):
            seen.append(client.patch(f"/api/tickets/{ids['ticket2']}", json={"tonnage": f"{21 + round_}.000"}, headers=headers))
            seen.append(client.post(f"/api/tickets/{ids['ticket2']}/verify", headers=headers))
            seen.append(client.get("/api/dashboard", headers=headers))
            seen.append(client.get("/api/records/export.csv", headers=headers))
            seen.append(client.get("/api/invoice-comparisons", headers=headers))
            seen.append(client.get(f"/api/reports/project/{ids['project']}", headers=headers))
            seen.append(client.get("/api/records/search", params={"q": "MARK"}, headers=headers))

    threads = [threading.Thread(target=work, args=(org,)) for org in (1, 2)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert not any(t.is_alive() for t in threads)
    for org, other in ((1, 2), (2, 1)):
        for response in results[org]:
            assert response.status_code in (200, 409, 503), response.text
            assert MARK[other] not in response.text
    totals = {}
    for org in (1, 2):
        dashboard = client.get("/api/dashboard", headers={"X-Test-As": f"{org}:owner"}).json()
        totals[org] = (Decimal(dashboard["total_tons"]), Decimal(dashboard["total_cost"]), dashboard["verified"])
        assert MARK[3 - org] not in client.get("/api/records/export.csv", headers={"X-Test-As": f"{org}:owner"}).text
    # Same paperwork, same work, same answer: 20 tons, plus the last tonnage approved.
    assert totals[1] == totals[2] == (Decimal("46.000"), Decimal("460.00"), 2), totals
