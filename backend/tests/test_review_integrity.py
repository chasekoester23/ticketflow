"""Nobody approves values they weren't looking at, and people can find their work."""
from decimal import Decimal

import pytest

from .test_query_budgets import _seed, _session
from app.models import AuditEvent, Batch, ScanSlot, Ticket


def _ticket(client, ticket_id=1):
    return next(t for t in client.get("/api/batches/1/tickets").json() if t["id"] == ticket_id)


def test_stale_approval_is_refused_and_changes_nothing(client):
    _seed(2)
    seen = _ticket(client)
    assert client.patch("/api/tickets/1", json={"tonnage": "22.000"}).status_code == 200

    stale = client.post("/api/tickets/1/verify", headers={"If-Match": seen["revision"]})

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "ticket_changed"
    current = _ticket(client)
    assert current["status"] == "needs_review"
    assert current["tonnage_confirmed"] is False
    assert current["verified_by"] is None


def test_current_revision_is_accepted(client):
    _seed(2)
    ticket = _ticket(client)
    response = client.post("/api/tickets/1/verify", headers={"If-Match": ticket["revision"]})
    assert response.status_code == 200
    assert response.json()["status"] == "verified"
    assert response.json()["revision"] != ticket["revision"]


def test_save_then_approve_uses_the_revision_from_the_save(client):
    _seed(1)
    ticket = _ticket(client)
    saved = client.patch("/api/tickets/1", json={"tonnage": "24.5"}, headers={"If-Match": ticket["revision"]})
    assert saved.status_code == 200
    assert client.post("/api/tickets/1/verify", headers={"If-Match": saved.json()["revision"]}).status_code == 200


def test_revision_matches_between_a_write_and_a_later_read(client):
    """Decimal precision must not make a fresh read look like a change."""
    _seed(1)
    saved = client.patch("/api/tickets/1", json={"tonnage": "24.5", "unit_price_override": "30.1"}).json()
    assert _ticket(client)["revision"] == saved["revision"]


@pytest.mark.parametrize("action", ["flag", "skip"])
def test_stale_flag_and_skip_are_refused(client, action):
    _seed(1)
    seen = _ticket(client)
    client.patch("/api/tickets/1", json={"job_location": "Lot B"})
    assert client.post(f"/api/tickets/1/{action}", headers={"If-Match": seen["revision"]}).status_code == 409
    assert _ticket(client)["status"] == "needs_review"


def test_stale_edit_does_not_overwrite_another_persons_correction(client):
    _seed(1)
    seen = _ticket(client)
    client.patch("/api/tickets/1", json={"tonnage": "22.000"})
    response = client.patch("/api/tickets/1", json={"tonnage": "99.000"}, headers={"If-Match": seen["revision"]})
    assert response.status_code == 409
    assert Decimal(_ticket(client)["tonnage"]) == Decimal("22.000")


def test_requests_without_the_header_still_work_for_scripts(client):
    _seed(1)
    assert client.post("/api/tickets/1/verify").status_code == 200


def test_flag_note_is_kept_audited_and_settled_by_approval(client):
    _seed(1)
    flagged = client.post("/api/tickets/1/flag", json={"note": "  Tons smudged, ask the driver  "})
    assert flagged.status_code == 200
    assert flagged.json()["flag_note"] == "Tons smudged, ask the driver"
    assert flagged.json()["status"] == "flagged"
    history = client.get("/api/tickets/1/audit").json()
    assert any(e["field"] == "flag_note" and e["new"] == "Tons smudged, ask the driver" for e in history)

    verified = client.post("/api/tickets/1/verify").json()
    assert verified["flag_note"] is None
    assert any(e["field"] == "flag_note" and e["new"] is None for e in client.get("/api/tickets/1/audit").json())


def test_flag_without_a_body_keeps_working(client):
    _seed(1)
    response = client.post("/api/tickets/1/flag")
    assert response.status_code == 200 and response.json()["flag_note"] is None


def test_flag_note_length_is_limited(client):
    _seed(1)
    assert client.post("/api/tickets/1/flag", json={"note": "x" * 301}).status_code == 422
    assert _ticket(client)["status"] == "needs_review"


def test_queue_lists_open_batches_with_counts(client):
    _seed(4)
    client.post("/api/tickets/1/verify")
    client.post("/api/tickets/2/flag")
    rows = client.get("/api/batches/queue").json()
    assert len(rows) == 1
    row = rows[0]
    assert (row["tickets"], row["needs_review"], row["flagged"], row["verified"]) == (4, 2, 1, 1)
    assert row["project_code"] == "P1"
    assert row["files"] == 1 and row["failed_files"] == 0


def test_queue_hides_finished_batches_unless_asked(client):
    _seed(1)
    client.post("/api/tickets/1/verify")
    assert client.get("/api/batches/queue").json() == []
    assert [r["id"] for r in client.get("/api/batches/queue", params={"open_only": False}).json()] == [1]


def test_queue_keeps_a_finished_batch_open_for_an_undecided_faint_slot(client):
    _seed(1)
    client.post("/api/tickets/1/verify")
    with _session() as db:
        db.add(ScanSlot(batch_id=1, file_id=1, page=99, position=0, crop_box=[0, 0, 1, 1], crop_path="x",
                        crop_sha256="f" * 64, ink_fraction=0.001, detection="faint"))
        db.commit()
    rows = client.get("/api/batches/queue").json()
    assert [r["faint_slots"] for r in rows] == [1]


def test_queue_is_scoped_to_the_company_and_filterable(client):
    from app.models import Organization
    with _session() as db:
        db.add(Organization(id=2, name="Another Company"))
        db.commit()
    _seed(1)
    _seed(1, organization_id=2, batch_id=2, start=5)
    assert [r["id"] for r in client.get("/api/batches/queue").json()] == [1]
    assert client.get("/api/batches/queue", params={"project_id": 2}).json() == []


def test_queue_query_count_does_not_grow_with_batches(client):
    from sqlalchemy import event
    engine = _session().get_bind()
    count = {"n": 0}
    listener = lambda *_: count.__setitem__("n", count["n"] + 1)  # noqa: E731
    _seed(2)
    event.listen(engine, "before_cursor_execute", listener)
    client.get("/api/batches/queue")
    few = count["n"]
    for batch in range(2, 12):
        event.remove(engine, "before_cursor_execute", listener)
        _seed(2, batch_id=batch, start=batch * 10)
        event.listen(engine, "before_cursor_execute", listener)
    count["n"] = 0
    assert len(client.get("/api/batches/queue").json()) == 11
    assert count["n"] == few
    event.remove(engine, "before_cursor_execute", listener)
