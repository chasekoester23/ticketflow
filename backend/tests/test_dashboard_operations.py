"""The dashboard's working sections: active jobs and recent activity, from real records only."""
from datetime import datetime, timezone

from app.models import AuditEvent, Batch, Organization, Project, Ticket, UploadedFile, User

from .sample_data import make_scan_pdf
from .test_query_budgets import _session


def _upload(client, project_id):
    files = [("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))]
    response = client.post("/api/batches", data={"layout": "grid_2x2", "project_id": str(project_id)}, files=files)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_active_projects_show_their_tickets_open_work_and_approved_tons(client):
    road = client.post("/api/projects", json={"code": "24-115", "name": "Fictional Road Job"}).json()["id"]
    idle = client.post("/api/projects", json={"code": "24-200", "name": "Fictional Idle Job"}).json()["id"]
    old = client.post("/api/projects", json={"code": "23-001", "name": "Fictional Finished Job"}).json()["id"]
    assert client.patch(f"/api/projects/{old}", json={"status": "archived"}).status_code == 200
    batch_id = _upload(client, road)
    tickets = client.get(f"/api/batches/{batch_id}/tickets").json()
    client.post(f"/api/tickets/{tickets[0]['id']}/verify")
    client.post(f"/api/tickets/{tickets[1]['id']}/skip")
    rows = {row["code"]: row for row in client.get("/api/dashboard").json()["projects"]}
    assert rows["24-115"]["tickets"] == 4 and rows["24-115"]["open"] == 2
    assert rows["24-115"]["verified_tons"] == tickets[0]["tonnage"]
    assert rows["24-115"]["last_upload"] is not None
    assert rows["24-200"] == {"id": idle, "code": "24-200", "name": "Fictional Idle Job", "tickets": 0, "open": 0,
                              "verified_tons": "0.000", "last_upload": None}
    assert "23-001" not in rows  # archived jobs aren't active
    assert list(rows)[0] == "24-115"  # most recently worked on first
    only_idle = client.get(f"/api/dashboard?project_id={idle}").json()["projects"]
    assert [row["code"] for row in only_idle] == ["24-200"]


def test_recent_activity_names_who_approved_flagged_or_skipped_what(client):
    project = client.post("/api/projects", json={"code": "24-115", "name": "Fictional Road Job"}).json()["id"]
    tickets = client.get(f"/api/batches/{_upload(client, project)}/tickets").json()
    client.post(f"/api/tickets/{tickets[0]['id']}/verify")
    client.post(f"/api/tickets/{tickets[1]['id']}/flag", json={"note": "Tons smudged"})
    client.patch(f"/api/tickets/{tickets[2]['id']}", json={"supplier": "Fictional Supplier"})
    activity = client.get("/api/dashboard").json()["activity"]
    assert [(a["action"], a["ticket_id"]) for a in activity] == [("flagged", tickets[1]["id"]), ("approved", tickets[0]["id"])]
    assert all(a["by"] and a["at"] for a in activity)  # an edit isn't activity; approvals and flags are


def test_another_companys_projects_and_activity_never_appear(client):
    client.post("/api/projects", json={"code": "24-115", "name": "Fictional Road Job"})
    with _session() as db:
        other = Organization(name="Fictional Other Co")
        db.add(other)
        db.flush()
        person = User(organization_id=other.id, username="other-person", password_hash="x", role="owner", display_name="Other Person")
        job = Project(organization_id=other.id, code="99-999", name="Other Company Job", status="active")
        db.add_all([person, job])
        db.flush()
        batch = Batch(organization_id=other.id, project_id=job.id, layout="single", intake_mode="manual", status="ready_for_review")
        db.add(batch)
        db.flush()
        scan = UploadedFile(batch_id=batch.id, original_name="other.png", storage_path="uploads/other.png", sha256="x")
        db.add(scan)
        db.flush()
        ticket = Ticket(organization_id=other.id, batch_id=batch.id, file_id=scan.id, source_page=1, position=0, crop_box=[0, 0, 10, 10], crop_path="crops/other.png", crop_sha256="0" * 64,
                        ticket_number="SECRET-1", status="verified", flags=[])
        db.add(ticket)
        db.flush()
        db.add(AuditEvent(ticket_id=ticket.id, actor_user_id=person.id, field="status", old_value="needs_review",
                          new_value="verified", created_at=datetime.now(timezone.utc)))
        db.commit()
    body = client.get("/api/dashboard").json()
    assert [row["code"] for row in body["projects"]] == ["24-115"]
    assert all(a["ticket_number"] != "SECRET-1" and a["by"] != "Other Person" for a in body["activity"])
