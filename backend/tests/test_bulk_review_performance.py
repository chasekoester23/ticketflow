"""Large review batches should not trigger one database scan per ticket."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import event

from app.db import get_db
from app.main import app
from app.models import Batch, Ticket, UploadedFile
from app.services.duplicates import DuplicateIndex


def _make_ticket(number, *, supplier="Example Aggregates", sha=None, material="Stone",
                 tons="24.5", status="needs_review", delivery_date=date(2026, 10, 15)):
    return SimpleNamespace(
        id=number, batch_id=1, source_page=1, position=number - 1,
        status=status, crop_sha256=sha or f"hash-{number}",
        supplier=supplier, ticket_number=f"{number}",
        delivery_date=delivery_date, tonnage=Decimal(tons),
        material_type=material,
    )


def test_duplicate_index_preserves_reason_precedence_and_excludes_skipped():
    first = _make_ticket(1, sha="same-crop")
    identical = _make_ticket(2, sha="same-crop")
    number = _make_ticket(3, supplier=" example--aggregates ", sha="unique3")
    number.ticket_number = "1"
    same_load = _make_ticket(4, supplier="EXAMPLE AGGREGATES")
    different_supplier = _make_ticket(5, supplier="Other Quarry")
    skipped = _make_ticket(6, sha="same-crop", status="skipped")
    idx = DuplicateIndex([first, identical, number, same_load, different_supplier, skipped])

    found = idx.find(first)
    assert len(found) == 3
    assert found[0].startswith("Identical scanned image")
    assert found[1].startswith("Same supplier and ticket number 1")
    assert found[2].startswith("Same supplier, date, material and tonnage")
    assert "batch 1" in found[0]
    assert not any("ticket 6" in text for text in found)
    assert idx.find(different_supplier) == []


def test_batched_duplicate_and_catalog_queries_do_not_grow_per_record(client):
    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        batch = Batch(layout="single", status="ready_for_review")
        db.add(batch)
        db.flush()
        source = UploadedFile(
            batch_id=batch.id, original_name="large-sample.pdf",
            storage_path="/not-needed.pdf", sha256="unique-file",
            status="ready_for_review",
        )
        db.add(source)
        db.flush()
        for i in range(160):
            db.add(Ticket(
                batch_id=batch.id, file_id=source.id, source_page=i + 1, position=0,
                crop_box=[0, 0, 100, 100],
                crop_path="/not-needed.png", crop_sha256=f"unique-{i}",
                ticket_number=str(100000 + i), delivery_date=date(2026, 10, 15),
                material_type="Unlisted Stone", supplier="Unlisted Quarry",
                tonnage=Decimal("24.500") + Decimal(i) / 1000, job_location="Site A",
                status="needs_review", tonnage_confirmed=False, flags=[],
            ))
        db.commit()
        batch_id = batch.id
        engine = db.get_bind()
    finally:
        gen.close()

    statements = []
    def count(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        response = client.get(f"/api/batches/{batch_id}/tickets")
        batch_queries = len(statements)
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 160
        assert all(t["status"] == "needs_review" and t["cost"] is None for t in body)
        assert all(t["duplicates"] == [] for t in body)
        assert "Unlisted Quarry" in body[0]["pricing_notes"][0]
        statements.clear()

        records = client.get("/api/records")
        records_queries = len(statements)
        assert records.status_code == 200
        assert len(records.json()) == 160
    finally:
        event.remove(engine, "before_cursor_execute", count)

    # Both lists should have a small fixed number of SQL queries, not 160+
    # repeated per-ticket scans or supplier/material catalog lookups.
    assert batch_queries <= 8, f"Batch list executed {batch_queries} SELECTs"
    assert records_queries <= 8, f"Records list executed {records_queries} SELECTs"
