"""A catalog supplier's approved history suggests; it never fills in or overrides a ticket. Fictional data."""
from datetime import date
from decimal import Decimal

from app.auth import current_user
from app.main import app
from app.models import Batch, Organization, Supplier, Ticket, UploadedFile, User
from app.routers import api as api_module
from app.services import supplier_memory


def _seed(organization_id: int, batch_id: int, tickets: list[dict]) -> None:
    with api_module.SessionLocal() as db:
        db.add(Batch(id=batch_id, organization_id=organization_id, layout="single",
                     status="ready_for_review", intake_mode="manual"))
        db.flush()
        db.add(UploadedFile(id=batch_id, batch_id=batch_id, original_name="f.pdf", storage_path="x",
                            sha256=f"{batch_id:064d}", status="ready_for_review"))
        db.flush()
        for n, values in enumerate(tickets):
            db.add(Ticket(**{
                "batch_id": batch_id, "file_id": batch_id, "source_page": n + 1, "position": 0,
                "crop_box": [0, 0, 1, 1], "crop_path": "x", "crop_sha256": f"{batch_id:08d}{n:056d}",
                "ticket_number": f"{batch_id}{n:04d}", "delivery_date": date(2026, 10, 1),
                "tonnage": Decimal("9.5"), "flags": [], "supplier": "Example Ready Mix",
                **values}))
        db.commit()


HISTORY = [dict(status="verified", material_type="4000 PSI Concrete", unit="cy", job_location="North Lot")] * 3


def _company_two() -> None:
    with api_module.SessionLocal() as db:
        db.add(Organization(id=2, name="Other Fictional Contractor"))
        db.flush()
        db.add(User(id=2, organization_id=2, username="other", password_hash="fixture", role="owner"))
        db.add(Supplier(organization_id=2, name="Example Ready Mix"))
        db.commit()


def _as_company_two() -> None:
    app.dependency_overrides[current_user] = lambda: User(
        id=2, organization_id=2, username="other", password_hash="fixture", active=True,
        role="owner", must_change_password=False)


def _listed(client, batch_id: int) -> list[dict]:
    return client.get(f"/api/batches/{batch_id}/tickets").json()


def test_consistent_history_suggests_and_changes_nothing(client):
    assert client.post("/api/suppliers", json={"name": "Example Ready Mix"}).status_code in (200, 201)
    _seed(1, 1, HISTORY)
    _seed(1, 2, [dict(status="needs_review", material_type=None, unit="ton", job_location=None,
                      raw_extraction={"unit": None})])
    ticket = _listed(client, 2)[0]
    assert ticket["suggestions"] == {"usual_material_type": "4000 PSI Concrete", "usual_unit": "cy",
                                     "usual_job_location": "North Lot"}
    # A suggestion only: the ticket is exactly as it was read.
    assert (ticket["material_type"], ticket["unit"], ticket["job_location"]) == (None, "ton", None)
    with api_module.SessionLocal() as db:
        stored = db.get(Ticket, ticket["id"])
        assert (stored.material_type, stored.unit, stored.job_location) == (None, "ton", None)


def test_a_value_read_from_the_document_is_never_overridden(client):
    client.post("/api/suppliers", json={"name": "Example Ready Mix"})
    _seed(1, 1, HISTORY)
    _seed(1, 2, [dict(status="needs_review", material_type="3000 PSI Concrete", unit="ton", job_location="South Lot",
                      raw_extraction={"unit": "ton"}),
                 dict(status="needs_review", material_type="4000 psi concrete", unit="cy", job_location="North Lot")])
    read, respelled = _listed(client, 2)
    assert read["suggestions"] == {}  # different material, location and a printed unit: the document wins
    assert respelled["suggestions"] == {"usual_material_type": "4000 PSI Concrete"}  # spelling only


def test_no_pattern_no_suggestion(client):
    client.post("/api/suppliers", json={"name": "Example Ready Mix"})
    _seed(1, 1, [*HISTORY, dict(status="verified", material_type="Flowable Fill", unit="cy", job_location="North Lot"),
                 dict(status="needs_review", material_type="Grout", unit="each", job_location="Elsewhere")])
    _seed(1, 2, [dict(status="needs_review", material_type=None, unit="ton", job_location=None)])
    # Two materials approved: no material suggestion. Unapproved tickets don't count.
    assert _listed(client, 2)[0]["suggestions"] == {"usual_unit": "cy", "usual_job_location": "North Lot"}


def test_too_little_history_or_no_catalog_supplier_suggests_nothing(client):
    _seed(1, 1, HISTORY)  # supplier not in the catalog yet
    _seed(1, 2, [dict(status="needs_review", material_type=None, unit="ton", job_location=None)])
    assert _listed(client, 2)[0]["suggestions"] == {}
    client.post("/api/suppliers", json={"name": "Example Ready Mix"})
    assert _listed(client, 2)[0]["suggestions"] != {}
    with api_module.SessionLocal() as db:
        assert supplier_memory.usual_values(db, 1, {"example ready mix"}) != {}
        db.query(Ticket).filter(Ticket.batch_id == 1, Ticket.source_page == 1).update({"status": "needs_review"})
        db.commit()
        assert supplier_memory.usual_values(db, 1, {"example ready mix"}) == {}  # two approved: not a pattern


def test_company_b_never_sees_company_a_patterns(client):
    client.post("/api/suppliers", json={"name": "Example Ready Mix"})
    _seed(1, 1, HISTORY)
    _company_two()
    _seed(2, 2, [dict(status="needs_review", material_type=None, unit="ton", job_location=None)])
    with api_module.SessionLocal() as db:
        assert supplier_memory.usual_values(db, 2, {"example ready mix"}) == {}
        assert supplier_memory.usual_values(db, 1, {"example ready mix"}) != {}
    _as_company_two()
    assert _listed(client, 2)[0]["suggestions"] == {}
    # Company B's own approvals make its own pattern, which company A never sees either.
    _seed(2, 3, [dict(status="verified", material_type="Sand", unit="ton", job_location="B Yard")] * 3)
    assert _listed(client, 2)[0]["suggestions"] == {"usual_material_type": "Sand", "usual_job_location": "B Yard"}
    app.dependency_overrides[current_user] = lambda: User(
        id=1, organization_id=1, username="test-owner", password_hash="fixture", active=True,
        role="owner", must_change_password=False)
    _seed(1, 4, [dict(status="needs_review", material_type=None, unit="ton", job_location=None)])
    assert _listed(client, 4)[0]["suggestions"] == {"usual_material_type": "4000 PSI Concrete", "usual_unit": "cy",
                                                    "usual_job_location": "North Lot"}
