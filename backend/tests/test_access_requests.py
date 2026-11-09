"""The public request-access form, and turning a request into a separate company."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import rate_limit
from app.auth import COOKIE_NAME, current_user, password_valid
from app.config import settings
from app.main import app
from app.models import AccessRequest, AccountEvent, Organization, User
from app.services.workspaces import ProvisioningError, create_workspace, provision_request

from .test_query_budgets import _session

BODY = {"company_name": " Fictional  Paving ", "contact_name": "Fictional Person", "email": "Person@Fictional-Paving.example"}


@pytest.fixture()
def public(client):
    """A visitor with no session, and a limit that doesn't interfere unless a test sets one."""
    app.dependency_overrides.pop(current_user, None)
    rate_limit.reset()
    limit = settings.rate_limit_access_requests_per_minute
    settings.rate_limit_access_requests_per_minute = 0
    with TestClient(app, headers={"X-TicketFlow-Request": "1"}) as visitor:
        yield visitor
    settings.rate_limit_access_requests_per_minute = limit
    rate_limit.reset()


def _requests() -> list[AccessRequest]:
    with _session() as db:
        return list(db.scalars(select(AccessRequest).order_by(AccessRequest.id)))


def _counts() -> tuple[int, int]:
    with _session() as db:
        return db.scalar(select(func.count()).select_from(Organization)), db.scalar(select(func.count()).select_from(User))


def test_a_request_is_stored_cleaned_and_grants_nothing(public):
    before = _counts()
    response = public.post("/api/access-requests", json=BODY)
    assert response.status_code == 202 and response.json() == {"received": True}
    assert COOKIE_NAME not in response.cookies
    saved = _requests()[0]
    assert (saved.company_name, saved.email, saved.status, saved.organization_id) == ("Fictional Paving", "person@fictional-paving.example", "new", None)
    assert _counts() == before


def test_the_form_needs_the_request_header(client):
    with TestClient(app) as bare:
        assert bare.post("/api/access-requests", json=BODY).status_code == 403
    assert _requests() == []


def test_asking_twice_looks_the_same_and_stores_one(public):
    first = public.post("/api/access-requests", json=BODY)
    second = public.post("/api/access-requests", json={**BODY, "email": "PERSON@fictional-paving.example"})
    assert (first.status_code, first.json()) == (second.status_code, second.json())
    assert len(_requests()) == 1


def test_a_filled_trap_field_is_answered_but_not_stored(public):
    assert public.post("/api/access-requests", json={**BODY, "website": "http://spam.example"}).status_code == 202
    assert _requests() == []


@pytest.mark.parametrize("change", [
    {"email": "not-an-address"}, {"email": "two@@fictional.example"}, {"company_name": "   "},
    {"company_name": "Fictional\x1b[31m Paving"}, {"contact_name": "x" * 101}, {"monthly_tickets": "lots"},
    {"role": "owner"}, {"organization_id": 1},
])
def test_bad_or_extra_values_are_refused(public, change):
    assert public.post("/api/access-requests", json={**BODY, **change}).status_code == 422
    assert _requests() == []


def test_one_network_is_limited(public):
    settings.rate_limit_access_requests_per_minute = 2
    answers = [public.post("/api/access-requests", json={**BODY, "email": f"p{n}@fictional.example"}) for n in range(3)]
    assert [a.status_code for a in answers] == [202, 202, 429]
    assert int(answers[2].headers["Retry-After"]) > 0
    assert len(_requests()) == 2


def test_a_flood_cant_grow_the_list_without_end(public, monkeypatch):
    monkeypatch.setattr("app.routers.access_requests.MAX_WAITING_REQUESTS", 1)
    assert public.post("/api/access-requests", json=BODY).status_code == 202
    assert public.post("/api/access-requests", json={**BODY, "email": "other@fictional.example"}).status_code == 503
    assert len(_requests()) == 1


def test_setup_stays_closed_once_a_user_exists(public):
    setup = {"company_name": "Fictional", "username": "intruder", "display_name": "I", "password": "gravel-truck-at-dawn-9"}
    with TestClient(app, client=("127.0.0.1", 50123), headers={"X-TicketFlow-Request": "1"}) as local:
        assert local.post("/api/setup", json=setup).status_code == 409


def _waiting(email="owner@fictional.example") -> int:
    with _session() as db:
        item = AccessRequest(company_name="Fictional Paving", contact_name="Fictional Owner", email=email)
        db.add(item)
        db.commit()
        return item.id


def test_provisioning_makes_a_separate_company_whose_owner_must_change_password(client):
    request_id = _waiting()
    with _session() as db:
        existing = db.scalar(select(User).where(User.id == 1)).organization_id
        organization, owner, temporary = provision_request(db, request_id, "Fictional.Owner")
        db.commit()
        assert organization.id != existing
        assert (owner.organization_id, owner.role, owner.must_change_password, owner.display_name) == (organization.id, "owner", True, "Fictional Owner")
        assert password_valid(temporary, owner.password_hash)
        assert db.get(AccessRequest, request_id).organization_id == organization.id
        assert db.scalar(select(AccountEvent.action).where(AccountEvent.organization_id == organization.id)) == "workspace_set_up"


def test_a_request_cant_be_provisioned_twice(client):
    request_id = _waiting()
    with _session() as db:
        provision_request(db, request_id, "first-owner")
        db.commit()
        with pytest.raises(ProvisioningError):
            provision_request(db, request_id, "second-owner")


def test_a_taken_username_creates_nothing(client):
    request_id = _waiting()
    with _session() as db:
        taken = db.get(User, 1).username
        before = db.scalar(select(func.count()).select_from(Organization))
        with pytest.raises(IntegrityError):
            provision_request(db, request_id, taken)
        db.rollback()
        assert db.scalar(select(func.count()).select_from(Organization)) == before
        assert db.get(AccessRequest, request_id).status == "new"


def test_a_new_workspace_starts_empty_and_cant_see_another_company(client):
    with _session() as db:
        organization, owner = create_workspace(db, company_name="Fictional Other", owner_username="other-owner", encoded_password="x")
        db.commit()
        other = User(id=owner.id, organization_id=organization.id, username="other-owner", password_hash="x", active=True, role="owner", must_change_password=False)
    app.dependency_overrides[current_user] = lambda: other
    assert client.get("/api/projects").json() == []
    assert client.get("/api/tickets").status_code in {200, 404, 405}
    assert client.get("/api/records/search?page_size=50").json()["total"] == 0
