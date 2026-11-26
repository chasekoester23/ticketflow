"""A contractor approves a first batch before entering contract rates, then enters them.
The approved deliveries are priced once, with the rate that applies, recorded in each
ticket's history. Locked prices, overrides and other companies are never touched.
Fictional data."""
from .test_delivery_imports import _as_company_two
from .test_trucking_api import yard  # noqa: F401  (fixture)
from .test_trucking_billing import _haul_rate
from .test_units_of_measure import _catalog, _ticket


def _apply(client, apply):
    response = client.post("/api/rates/apply-to-unpriced", json={"apply": apply})
    assert response.status_code == 200, response.text
    return response.json()


def test_deliveries_approved_before_their_rate_are_priced_once_the_rate_exists(client, yard):
    approved = _ticket(client, yard, quantity="25.00")
    assert (approved["price_snapshot_state"], approved["cost"]) == ("unpriced", None)
    assert client.get("/api/dashboard").json()["unpriced_tickets"] == 1

    _catalog(client, price="20.00")
    preview = _apply(client, False)
    assert (preview["priced"], preview["cost"], preview["still_unpriced"]) == (1, "500.00", 0)
    assert client.get(f"/api/batches/{approved['batch_id']}/tickets").json()[0]["cost"] is None  # a preview changes nothing

    done = _apply(client, True)
    assert done["priced"] == 1
    [ticket] = client.get(f"/api/batches/{approved['batch_id']}/tickets").json()
    assert (ticket["status"], ticket["price_snapshot_state"], ticket["unit_price"], ticket["cost"]) == ("verified", "priced", "20.0000", "500.00")
    dashboard = client.get("/api/dashboard").json()
    assert (dashboard["total_cost"], dashboard["unpriced_tickets"]) == ("500.00", 0)
    history = client.get(f"/api/tickets/{ticket['id']}/audit").json()
    assert any(e["field"] == "verified_unit_price" and "after approval" in (e["new"] or "") for e in history)
    assert _apply(client, True)["priced"] == 0  # once only


def test_a_locked_price_and_an_override_are_never_repriced(client, yard):
    _catalog(client, price="20.00")
    locked = _ticket(client, yard, number="80001")
    _catalog(client, price="25.00")  # the rate changes after approval
    assert _apply(client, True)["priced"] == 0
    [ticket] = client.get(f"/api/batches/{locked['batch_id']}/tickets").json()
    assert ticket["cost"] == "500.00"


def test_a_ticket_still_without_a_rate_says_why(client, yard):
    _ticket(client, yard, quantity="12.00", unit="cy")
    _catalog(client, price="20.00")  # per ton: doesn't price yards
    result = _apply(client, True)
    assert (result["priced"], result["still_unpriced"]) == (0, 1)
    assert "per ton" in result["left"][0]["reason"] and "cubic yards" in result["left"][0]["reason"]


def test_a_delivered_price_after_hauling_was_charged_is_left_for_a_person(client, yard):
    _haul_rate(client, yard, "per_ton", "4.00")
    charged = _ticket(client, yard, hauler=True)
    assert charged["haul_billing"] == "per_ton"
    _catalog(client, price="24.00")
    rates = client.get("/api/rates").json()
    client.put("/api/rates", json={"supplier_id": rates[0]["supplier_id"], "material_id": rates[0]["material_id"],
                                   "unit_price": "24.00", "freight": "included"})
    result = _apply(client, True)
    assert result["priced"] == 0 and "freight" in result["left"][0]["reason"]


def test_another_company_can_neither_see_nor_price_them(client, yard):
    _ticket(client, yard)
    _catalog(client, price="20.00")
    with _as_company_two():
        assert _apply(client, True)["priced"] == 0
    assert _apply(client, False)["priced"] == 1


def test_only_managers_can_apply_rates(client, yard):
    from app.auth import current_user
    from app.main import app
    from app.models import User
    before = app.dependency_overrides[current_user]
    app.dependency_overrides[current_user] = lambda: User(id=1, organization_id=1, username="clerk", display_name="Clerk",
                                                          password_hash="x", active=True, role="clerk", must_change_password=False)
    try:
        assert client.post("/api/rates/apply-to-unpriced", json={"apply": True}).status_code == 403
    finally:
        app.dependency_overrides[current_user] = before
