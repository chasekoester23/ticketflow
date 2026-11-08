"""A real API server and a real worker, as separate processes, on throwaway data.

For tests that need something only a real process can do: die from kill -9, hold a
file lock, hit a kernel limit, or leave a half-written file behind. Fictional data,
no AI, no network beyond this machine. Only processes started here are ever stopped.
"""
from __future__ import annotations

import os
import resource
import signal
import socket
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth import password_hash
from app.db import Base, enforce_references, engine_options, normalized_url
from app.models import AuditEvent, Material, Organization, Project, Supplier, SupplierRate, Ticket, User

BACKEND = Path(__file__).resolve().parents[1]
OWNER, PASSWORD = "stack-owner", "fictional-owner-password"
VALUES = dict(ticket_number="1", delivery_date="2026-09-15", material_type="Sand", tonnage="20.000",
              supplier="Example Aggregates", job_location="Lot")


def wait_until(condition, *, seconds: float = 60, what: str = "condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"Timed out after {seconds} s waiting for {what}")


class Stack:
    def __init__(self, work: Path, url: str, engine):
        self.work, self.url, self.engine = work, url, engine
        self.storage = work / "storage"
        self.storage.mkdir(parents=True, exist_ok=True)
        self.Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.port: int | None = None
        self.server: subprocess.Popen | None = None
        self.workers: list[subprocess.Popen] = []

    # ---------------------------------------------------------------- processes

    def environment(self, **extra) -> dict:
        return {**os.environ, "DATABASE_URL": self.url, "STORAGE_DIR": str(self.storage), "BACKUP_DIR": str(self.work / "backups"),
                "EXTRACTION_PROVIDER": "gemini", "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "",
                "PYTHONPATH": str(BACKEND), "PYTHONWARNINGS": "ignore", **extra}

    def start_server(self, *, largest_file_bytes: int | None = None) -> None:
        """``largest_file_bytes``: the kernel refuses to grow any file past this, for this
        process alone, which is how a full disk looks to the code writing the file."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        limit = (lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (largest_file_bytes, largest_file_bytes))) if largest_file_bytes else None
        log = open(self.work / "server.log", "ab")
        self.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port), "--log-level", "warning"],
            cwd=BACKEND, env=self.environment(), stdout=log, stderr=log, preexec_fn=limit)
        log.close()

        def ready():
            if self.server.poll() is not None:
                raise AssertionError(f"Server exited with {self.server.returncode}: {(self.work / 'server.log').read_text()[-1500:]}")
            try:
                return httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=2).status_code == 200
            except httpx.TransportError:
                return False
        wait_until(ready, what="the server to start")

    def kill_server(self) -> None:
        if self.server is not None and self.server.poll() is None:
            os.kill(self.server.pid, signal.SIGKILL)
            self.server.wait(10)

    def start_worker(self, *, pause_at: str | None = None, stand_in_reader: bool = False) -> subprocess.Popen:
        """The real worker, unmodified. With a pause point or the stand-in reader it's the
        same code run by ``fixtures_worker``, so a kill lands exactly where it's aimed."""
        log = open(self.work / "worker.log", "ab")
        command = [sys.executable, str(Path(__file__).with_name("fixtures_worker.py"))] if pause_at or stand_in_reader \
            else [sys.executable, "scripts/run_worker.py", "--poll-seconds", "0.25"]
        extra = {"TF_PAUSE_AT": pause_at or "", "TF_PAUSE_FILE": str(self.work / "paused"), "TF_READ_LOG": str(self.work / "reads.log")}
        worker = subprocess.Popen(command, cwd=BACKEND, env=self.environment(**extra), stdout=log, stderr=log)
        log.close()
        self.workers.append(worker)
        return worker

    def paused(self) -> bool:
        return (self.work / "paused").exists()

    def clear_pause(self) -> None:
        (self.work / "paused").unlink(missing_ok=True)

    def stop_everything(self) -> None:
        self.kill_server()
        for worker in self.workers:
            if worker.poll() is None:
                os.kill(worker.pid, signal.SIGKILL)
                worker.wait(10)

    # ---------------------------------------------------------------- talking to it

    def signed_in(self) -> httpx.Client:
        client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", headers={"X-TicketFlow-Request": "1"}, timeout=90)
        response = client.post("/api/auth/login", json={"username": OWNER, "password": PASSWORD})
        assert response.status_code == 200, response.text
        return client

    def reconnect(self, client: httpx.Client) -> httpx.Client:
        """The same browser, with the same cookie, after the server came back on a new port."""
        again = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", headers={"X-TicketFlow-Request": "1"}, timeout=90)
        again.cookies.set("ticketflow_session", client.cookies.get("ticketflow_session"))
        return again

    def batch_tickets(self, client: httpx.Client, batch_id: int) -> list[dict]:
        response = client.get(f"/api/batches/{batch_id}/tickets")
        assert response.status_code == 200, response.text
        return response.json()

    def wait_for_batch(self, client: httpx.Client, batch_id: int, status: str, seconds: float = 90) -> dict:
        def done():
            batch = client.get(f"/api/batches/{batch_id}").json()
            return batch if batch["status"] == status else None
        return wait_until(done, seconds=seconds, what=f"batch {batch_id} to be {status}")

    def approve(self, client: httpx.Client, ticket_id: int, **values) -> dict:
        """Fill in a ticket the way a person does, then approve it."""
        edited = client.patch(f"/api/tickets/{ticket_id}", json={**VALUES, "ticket_number": f"N{ticket_id}", **values})
        assert edited.status_code == 200, edited.text
        approved = client.post(f"/api/tickets/{ticket_id}/verify", headers={"If-Match": edited.json()["revision"]})
        assert approved.status_code == 200, approved.text
        return approved.json()

    def verified(self) -> dict[int, tuple]:
        """Everything about approved tickets that must never change by accident."""
        with self.Session() as db:
            return {t.id: (t.ticket_number, t.tonnage, t.applied_unit_price, t.approved_cost, t.verified_by_user_id, t.status,
                           t.price_snapshot_state, t.crop_sha256)
                    for t in db.scalars(select(Ticket).where(Ticket.status == "verified").order_by(Ticket.id))}

    def approvals_on_record(self, ticket_id: int) -> int:
        with self.Session() as db:
            return len(list(db.scalars(select(AuditEvent.id).where(
                AuditEvent.ticket_id == ticket_id, AuditEvent.field == "status", AuditEvent.new_value == "verified"))))


def seed(session) -> None:
    session.add(Organization(id=1, name="Fictional Company"))
    session.flush()
    session.add(User(id=1, organization_id=1, username=OWNER, display_name="Stack Owner", password_hash=password_hash(PASSWORD), role="owner"))
    session.add(Project(id=1, organization_id=1, code="P1", name="Fictional Job"))
    session.add(Supplier(id=1, organization_id=1, name="Example Aggregates"))
    session.add(Material(id=1, organization_id=1, name="Sand"))
    session.flush()
    session.add(SupplierRate(organization_id=1, supplier_id=1, material_id=1, unit_price=Decimal("10")))
    session.commit()


def build_stack(tmp_path: Path, postgres_engine, test_database_url: str | None, reset) -> Stack:
    if postgres_engine is not None:
        engine, url = postgres_engine, test_database_url
        reset(engine)
    else:
        url = f"sqlite:///{tmp_path / 'stack.db'}"
        engine = create_engine(normalized_url(url), **engine_options(url))
        enforce_references(engine)
        Base.metadata.create_all(engine)
    stack = Stack(tmp_path, url, engine)
    with stack.Session() as session:
        seed(session)
    return stack


def scan_batch(client: httpx.Client, files: int = 3, *, intake: str = "manual") -> int:
    """Upload ``files`` PDFs of one page of four fictional tickets each."""
    from .sample_data import TICKETS, make_page, make_pdf
    pdf = make_pdf([make_page(TICKETS)])
    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": intake, "project_id": "1"},
                           files=[("files", (f"scan-{n}.pdf", pdf, "application/pdf")) for n in range(files)])
    assert response.status_code == 200, response.text
    return response.json()["id"]


