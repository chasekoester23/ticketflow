"""First-run setup from the browser, and saved views that name export columns."""
import csv
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.auth import COOKIE_NAME, current_user
from app.config import settings
from app.main import app
from app.models import AccountEvent, Organization, SavedView, User
from app.routers import setup as setup_router
from app.services import export as export_svc

from .test_concurrency import race  # noqa: F401 - the fixture
from .test_query_budgets import _seed, _session

BODY = {"company_name": "  Fictional Paving  Co ", "username": "First.Owner", "display_name": "First Owner",
        "password": "gravel-truck-at-dawn-9"}


@pytest.fixture()
def fresh(client):
    """No users at all, and a browser on this machine, signed in as nobody."""
    with _session() as db:
        db.delete(db.get(User, 1))
        db.commit()
    app.dependency_overrides.pop(current_user, None)
    with TestClient(app, client=("127.0.0.1", 50123), headers={"X-TicketFlow-Request": "1"}) as local:
        yield local


def _users() -> int:
    with _session() as db:
        return db.scalar(select(func.count()).select_from(User))


def test_setup_is_needed_only_while_nobody_exists(client, fresh):
    assert fresh.get("/api/setup/status").json() == {"needed": True}
    assert fresh.post("/api/setup", json=BODY).status_code == 200
    assert fresh.get("/api/setup/status").json() == {"needed": False}


def test_setup_status_says_nothing_else(client):
    response = client.get("/api/setup/status")
    assert response.json() == {"needed": False}
    assert response.headers["cache-control"] == "private, no-store"


def test_setup_creates_the_company_and_owner_and_signs_them_in(client, fresh):
    response = fresh.post("/api/setup", json=BODY)
    assert response.status_code == 200, response.text
    profile = response.json()
    assert profile["username"] == "first.owner" and profile["display_name"] == "First Owner"
    assert profile["role"] == "owner" and profile["must_change_password"] is False
    assert profile["organization_name"] == "Fictional Paving Co"
    assert "team.manage" in profile["permissions"]
    assert fresh.cookies.get(COOKIE_NAME)
    # The same body as signing in, and a working session.
    assert fresh.get("/api/auth/me").json() == profile
    assert fresh.post("/api/projects", json={"code": "26-1", "name": "First job"}).status_code == 201
    with _session() as db:
        # The one existing company is reused, not left beside a new one.
        assert db.scalar(select(func.count()).select_from(Organization)) == 1
        user = db.scalar(select(User))
        assert user.password_hash.startswith("scrypt$") and BODY["password"] not in user.password_hash
        assert [(e.action, e.actor_user_id, e.target_user_id) for e in db.scalars(select(AccountEvent))] == [("workspace_set_up", user.id, user.id)]


def test_setup_with_no_company_at_all_creates_one(client, fresh):
    with _session() as db:
        db.delete(db.get(Organization, 1))
        db.commit()
    assert fresh.post("/api/setup", json=BODY).status_code == 200
    with _session() as db:
        assert [o.name for o in db.scalars(select(Organization))] == ["Fictional Paving Co"]


def test_setup_is_refused_once_anyone_exists(client):
    with TestClient(app, client=("127.0.0.1", 50123), headers={"X-TicketFlow-Request": "1"}) as local:
        response = local.post("/api/setup", json=BODY)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "already_set_up"
    assert _users() == 1


def test_a_second_setup_is_refused(client, fresh):
    assert fresh.post("/api/setup", json=BODY).status_code == 200
    again = fresh.post("/api/setup", json={**BODY, "username": "someone-else"})
    assert again.status_code == 409 and _users() == 1


def test_setup_from_another_machine_is_refused(client):
    with _session() as db:
        db.delete(db.get(User, 1))
        db.commit()
    app.dependency_overrides.pop(current_user, None)
    response = client.post("/api/setup", json=BODY)  # The test client's address isn't this machine.
    assert response.status_code == 403 and response.json()["detail"]["code"] == "setup_local_only"
    assert _users() == 0


def test_remote_setup_can_be_allowed_deliberately(client, monkeypatch):
    with _session() as db:
        db.delete(db.get(User, 1))
        db.commit()
    app.dependency_overrides.pop(current_user, None)
    monkeypatch.setattr(settings, "allow_remote_setup", True)
    assert client.post("/api/setup", json=BODY).status_code == 200


@pytest.mark.parametrize("headers, allowed", [
    ({"X-Forwarded-For": "203.0.113.9"}, False),
    ({"X-Forwarded-For": "127.0.0.1, 203.0.113.9"}, False),
    ({"X-Forwarded-For": "203.0.113.9, 127.0.0.1"}, False),  # Whatever order a proxy appends in.
    ({"X-Real-IP": "198.51.100.4"}, False),
    ({"Forwarded": 'for="[2001:db8::1]:4711";proto=https'}, False),
    ({"Forwarded": "for=192.0.2.60;proto=http, for=127.0.0.1"}, False),
    ({"X-Forwarded-For": "not-an-address"}, False),
    ({"X-Forwarded-For": "127.0.0.1"}, True),  # The local Next.js server passing on a local browser.
    ({"X-Forwarded-For": "::1"}, True),
    ({"Forwarded": "for=127.0.0.1"}, True),
    ({}, True),
])
def test_a_proxy_on_this_machine_cant_make_a_visitor_look_local(client, fresh, headers, allowed):
    response = fresh.post("/api/setup", json=BODY, headers=headers)
    assert (response.status_code == 200) is allowed, response.text
    assert _users() == (1 if allowed else 0)


def test_setup_needs_the_request_header_like_every_change(client, fresh):
    del fresh.headers["X-TicketFlow-Request"]
    assert fresh.post("/api/setup", json=BODY).status_code == 403
    assert _users() == 0


@pytest.mark.parametrize("change, expected", [
    ({"password": "Password1234"}, "too common"),
    ({"password": "short"}, "between"),
    ({"password": "first.owner"}, "your username"),
    ({"username": "x"}, "Username"),
    ({"username": "has space"}, "Username"),
    ({"company_name": "   "}, "company name"),
    ({"company_name": "Bad\x00Name"}, "company name"),
    ({"display_name": "\t"}, "name"),
])
def test_bad_setup_details_are_refused_and_nothing_is_created(client, fresh, change, expected):
    response = fresh.post("/api/setup", json={**BODY, **change})
    assert response.status_code == 422
    assert expected in str(response.json()["detail"])
    assert _users() == 0 and not fresh.cookies.get(COOKIE_NAME)


def test_a_password_that_is_the_username_is_refused(client, fresh):
    response = fresh.post("/api/setup", json={**BODY, "username": "longusername1", "password": "LongUsername1"})
    assert response.status_code == 422 and "username" in response.json()["detail"]


def test_two_people_setting_up_at_once_get_one_owner(race, monkeypatch):  # noqa: F811 - the fixture
    """Each checks that nobody exists, then both would create an owner: only one may."""
    with race.Session() as db:
        db.delete(db.get(User, 1))
        db.commit()
    app.dependency_overrides.pop(current_user, None)
    monkeypatch.setattr(settings, "allow_remote_setup", True)

    def send(name):
        return lambda: race.client.post("/api/setup", json={**BODY, "username": name})

    with race.meeting_at(monkeypatch, setup_router, "_needed"):
        results = race.run(send("owner-one"), send("owner-two"))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [(r.status_code, r.text[:200]) for r in results]
    with race.Session() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_setup_routes_are_listed_as_deliberately_public():
    from app.route_inventory import SELF_SERVICE, all_routes
    routes = {(r.method, r.path): r.permissions for r in all_routes()}
    assert routes[("POST", "/api/setup")] == () and routes[("GET", "/api/setup/status")] == ()
    assert "/api/setup" in SELF_SERVICE


# ------------------------------------------------------------------ saved view labels

LABELS = {"ticket_number": "Tkt #", "tons": "Net Tons", "supplier": "Vendor"}


def _view(client, **body):
    response = client.post("/api/saved-views", json={"name": "Our sheet", "columns": ["supplier", "ticket_number", "tons"], **body})
    return response


def test_a_view_keeps_its_labels(client):
    created = _view(client, labels={"tons": "  Net Tons  ", "supplier": "Vendor"})
    assert created.status_code == 201, created.text
    assert created.json()["labels"] == {"tons": "Net Tons", "supplier": "Vendor"}
    assert client.get("/api/saved-views").json()[0]["labels"] == {"tons": "Net Tons", "supplier": "Vendor"}


def test_a_view_without_labels_has_none(client):
    assert _view(client).json()["labels"] == {}


@pytest.mark.parametrize("labels, expected", [
    ({"not_a_column": "X"}, "known export columns"),
    ({"tons": ""}, "1 to 100"),
    ({"tons": "   "}, "1 to 100"),
    ({"tons": "x" * 101}, "1 to 100"),
    ({"tons": "Net\nTons"}, "1 to 100"),
    ({"tons": "Net\x00Tons"}, "1 to 100"),
])
def test_bad_labels_are_refused(client, labels, expected):
    response = _view(client, labels=labels)
    assert response.status_code == 422 and expected in str(response.json()["detail"])
    assert client.get("/api/saved-views").json() == []


def _csv(client, **params):
    response = client.get("/api/records/export.csv", params=params)
    assert response.status_code == 200, response.text
    return list(csv.reader(io.StringIO(response.text)))


def test_an_export_with_a_view_uses_its_columns_and_headers(client):
    _seed(2)
    view = _view(client, labels=LABELS).json()["id"]
    rows = _csv(client, view_id=view)
    assert rows[0] == ["Vendor", "Tkt #", "Net Tons"]
    assert rows[1] == ["Example Aggregates", "10000", "15.000"]


def test_columns_in_the_request_win_but_the_views_labels_still_apply(client):
    _seed(1)
    view = _view(client, labels=LABELS).json()["id"]
    assert _csv(client, view_id=view, columns="tons,status")[0] == ["Net Tons", "Verification Status"]


def test_a_label_cant_become_a_spreadsheet_formula(client):
    _seed(1)
    view = _view(client, labels={"tons": "=SUM(A1:A9)", "supplier": "@cmd"}).json()["id"]
    header = _csv(client, view_id=view)[0]
    assert header == ["'@cmd", "Ticket Number", "'=SUM(A1:A9)"]
    workbook = load_workbook(io.BytesIO(client.get("/api/records/export.xlsx", params={"view_id": view}).content))
    cells = [cell.value for cell in next(workbook["Delivery Records"].iter_rows(max_row=1))]
    assert cells == ["'@cmd", "Ticket Number", "'=SUM(A1:A9)"]


def test_the_spreadsheet_and_batch_exports_use_the_view_too(client):
    _seed(1)
    view = _view(client, labels=LABELS).json()["id"]
    for url in ("/api/records/export.xlsx", "/api/batches/1/export.xlsx"):
        workbook = load_workbook(io.BytesIO(client.get(url, params={"view_id": view}).content))
        assert [cell.value for cell in next(workbook["Delivery Records"].iter_rows(max_row=1))] == ["Vendor", "Tkt #", "Net Tons"]


def test_without_a_view_the_default_columns_and_order_never_change(client):
    """Spreadsheets and macros out there depend on exactly these."""
    _seed(1)
    expected = ["Ticket Number", "Delivery Date", "Material Type", "Tonnage", "Supplier", "Job Location", "Unit Price",
                "Calculated Cost", "Verification Status", "Source File Reference", "Approved By", "Approved At (UTC)"]
    assert export_svc.HEADERS == expected
    assert _csv(client)[0] == expected
    workbook = load_workbook(io.BytesIO(client.get("/api/records/export.xlsx").content))
    assert [cell.value for cell in next(workbook["Delivery Records"].iter_rows(max_row=1))] == expected


def test_another_companys_view_cant_be_used(client):
    _seed(1)
    with _session() as db:
        db.add(Organization(id=2, name="Other"))
        db.flush()
        db.add(SavedView(id=50, organization_id=2, name="Theirs", filters={}, columns=["tons"], labels={"tons": "Leaked"}))
        db.commit()
    for url in ("/api/records/export.csv", "/api/records/export.xlsx", "/api/batches/1/export.xlsx"):
        assert client.get(url, params={"view_id": 50}).status_code == 404
