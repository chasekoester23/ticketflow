"""The review queue: one list of what's waiting for a person, across tickets, failed files,
truck shifts and drafted invoice lines. Nothing is approved from it. Fictional data."""
from app.models import Batch, UploadedFile

from .sample_data import make_scan_pdf
from .test_query_budgets import _session
from .test_trucking_api import _approve, _shift, yard  # noqa: F401  (fixture)


def _batch(client, project):
    response = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(project)},
                           files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_queue_lists_open_tickets_by_batch_and_shifts_to_approve(client, yard):
    batch = _batch(client, yard["project"])
    tickets = client.get(f"/api/batches/{batch}/tickets").json()
    client.post(f"/api/tickets/{tickets[0]['id']}/verify")
    client.post(f"/api/tickets/{tickets[1]['id']}/flag", json={"note": "Tons smudged"})
    waiting = _shift(client, yard)
    _approve(client, _shift(client, yard, truck=1), work_hours="9", standby_hours="0")

    queue = client.get("/api/review-queue").json()
    by_kind = {(i["kind"], i["id"]): i for i in queue["items"]}
    item = by_kind[("tickets", batch)]
    assert item["count"] == len(tickets) - 1 and "1 flagged" in item["detail"] and item["attention"]
    assert item["href"] == f"/upload?batch={batch}" and item["project"]["code"] == "P1"
    assert ("truck_shifts", waiting["id"]) in by_kind  # the approved one isn't waiting
    assert queue["summary"]["truck_shifts"] == 1 and queue["summary"]["tickets"] == len(tickets) - 1
    assert queue["summary"]["total"] == len(tickets) - 1 + 1


def test_queue_follows_the_project_and_kind_filters(client, yard):
    _batch(client, yard["project"])
    other = _batch(client, yard["other"])
    only_other = client.get("/api/review-queue", params={"project_id": yard["other"]}).json()
    assert {i["id"] for i in only_other["items"] if i["kind"] == "tickets"} == {other}
    shifts_only = client.get("/api/review-queue", params={"kind": "truck_shifts"}).json()
    assert all(i["kind"] == "truck_shifts" for i in shifts_only["items"])
    assert client.get("/api/review-queue", params={"kind": "anything"}).status_code == 422
    assert client.get("/api/review-queue", params={"project_id": 99999}).status_code == 404


def test_a_file_that_failed_is_listed_with_its_reason(client, yard):
    batch = _batch(client, yard["project"])
    with _session() as db:
        scan = db.query(UploadedFile).filter(UploadedFile.batch_id == batch).first()
        scan.status, scan.error = "failed", "The PDF is password protected."
        db.commit()
        assert db.get(Batch, batch) is not None
    failed = client.get("/api/review-queue", params={"kind": "failed_files"}).json()
    assert failed["summary"]["failed_files"] == 1
    assert failed["items"][0]["detail"] == "The PDF is password protected." and failed["items"][0]["href"] == f"/upload?batch={batch}"
