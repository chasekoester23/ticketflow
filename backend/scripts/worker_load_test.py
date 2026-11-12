"""Several companies uploading at once, read by several workers, then proof nothing slipped.

Usage (from backend/):
    python scripts/worker_load_test.py                                   # SQLite, 3 companies, 2 workers
    python scripts/worker_load_test.py --companies 4 --uploads 5 --workers 2 --kill-one \\
        --database-url postgresql://u:p@localhost/ticketflow_perf --json workers.json

Starts a real API server and real database-coordinated worker processes on a new
database (SQLite unless a PostgreSQL URL is given, whose database name must contain
"perf" or "load"). Each company's owner signs in and uploads four-ticket scans at the
same time as the others. Workers read them with a stand-in reader that waits like a
slow model and reports token counts; no AI provider is called. With --kill-one, one
worker is killed with SIGKILL partway, and another must take its job over once the
lease lapses.

Then it checks, from the database and from each company's own view through the API:
every ticket exists exactly once, every ticket belongs to its batch's company, no
company can see or open another's batches, every job finished, no company ever ran
more jobs at once than its limit, and the usage report adds up. Fictional data only.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))
PASSWORD = "fictional-load-password"
FIRST_PORT = 8302  # Never the developer's own 3000 or 8000.
TICKETS_PER_SCAN = 4


# ---------------------------------------------------------------- the worker process

def _as_worker(reader_seconds: float) -> int:
    """A real worker, with a stand-in reader in place of the AI provider."""
    import run_worker
    from app.services import job_log
    from app.services.validation import ExtractedTicket

    count, lock = [0], threading.Lock()

    class StandIn:
        model = "stand-in-model"

        def extract(self, _image):
            time.sleep(reader_seconds)
            with lock:
                count[0] += 1
                number = f"W{os.getpid()}-{count[0]}"
            raw = {"ticket_number": number, "delivery_date": "2026-09-15", "material_type": "Sand", "tonnage": "20.000",
                   "supplier": "Example Aggregates", "job_location": "Fictional Site"}
            return ExtractedTicket.model_validate(raw), {**raw, "_ticketflow": {
                "model": self.model, "attempts": 1, "seconds": reader_seconds,
                "prompt_tokens": 1200, "output_tokens": 150, "thinking_tokens": 300}}

    run_worker.get_extractor = StandIn
    job_log.configure()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stopping.set())
    return run_worker.run(once=False, poll_seconds=0.25, stopping=stopping)


# ---------------------------------------------------------------- setting up

def _free_port() -> int:
    for port in range(FIRST_PORT, FIRST_PORT + 200):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free port from 8302 up.")


def _seed(companies: int) -> None:
    from sqlalchemy import delete

    from app.auth import password_hash
    from app.db import SessionLocal
    from app.models import Organization, User
    digest = password_hash(PASSWORD)
    with SessionLocal() as db:
        db.execute(delete(Organization))  # The migrations' placeholder company.
        db.commit()
        for n in range(1, companies + 1):
            db.add(Organization(id=n, name=f"Fictional Company {n}"))
            db.flush()
            db.add(User(id=n, organization_id=n, username=f"owner-{n}", display_name=f"Owner {n}", password_hash=digest,
                        role="owner", active=True, must_change_password=False))
        db.commit()


def _signed_in(port: int, company: int):
    import httpx
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-TicketFlow-Request": "1"}, timeout=120)
    response = client.post("/api/auth/login", json={"username": f"owner-{company}", "password": PASSWORD})
    response.raise_for_status()
    return client


# ---------------------------------------------------------------- the run

def run(*, companies: int, uploads: int, workers: int, url: str | None, reader_seconds: float, limit: int,
        kill_one: bool, timeout: float) -> dict:
    from load_test import _empty, _environment

    work = Path(tempfile.mkdtemp(prefix="ticketflow-workers-"))
    env = {**_environment(work, url), "WORKER_COORDINATION": "database", "WORKER_LEASE_SECONDS": "15",
           "WORKER_HEARTBEAT_SECONDS": "1", "MAX_CONCURRENT_JOBS_PER_ORGANIZATION": str(limit), "EXTRACTION_CONCURRENCY": "4"}
    # Before anything imports the app, so its engine opens this run's database and nothing else.
    os.environ.update({key: env[key] for key in ("DATABASE_URL", "STORAGE_DIR")})
    if url:
        _empty(url)
    from tests.sample_data import make_scan_pdf
    migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, capture_output=True, text=True)
    if migrated.returncode:
        raise SystemExit(migrated.stderr[-2000:])
    _seed(companies)
    port = _free_port()
    processes: list[subprocess.Popen] = []
    problems: list[str] = []
    peak: Counter = Counter()
    try:
        log = open(work / "server.log", "wb")
        processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level",
                                           "warning", "--no-access-log"], cwd=BACKEND, env=env, stdout=log, stderr=log))
        for n in range(workers):
            worker_log = open(work / f"worker-{n}.log", "wb")
            processes.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--as-worker",
                                               "--reader-seconds", str(reader_seconds)],
                                              cwd=BACKEND, env=env, stdout=worker_log, stderr=worker_log))
        import httpx
        for _ in range(240):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.25)

        scan = make_scan_pdf()
        uploaded: dict[int, list[int]] = {}
        errors: list[str] = []

        def company(n: int) -> None:
            try:
                client = _signed_in(port, n)
                ids = []
                for k in range(uploads):
                    response = client.post("/api/batches", data={"layout": "grid_2x2", "intake_mode": "extract"},
                                           files=[("files", (f"company-{n}-scan-{k}.pdf", scan, "application/pdf"))])
                    response.raise_for_status()
                    ids.append(response.json()["id"])
                uploaded[n] = ids
            except Exception as exc:  # noqa: BLE001 - reported as a problem
                errors.append(f"company {n} upload: {type(exc).__name__}")

        started = time.monotonic()
        threads = [threading.Thread(target=company, args=(n,)) for n in range(1, companies + 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        problems += errors

        from sqlalchemy import func, select

        from app.db import SessionLocal
        from app.models import Batch, ExtractionUsage, ProcessingJob, Ticket, UploadedFile, WorkerHeartbeat
        killed = None
        expected_jobs = companies * uploads
        while time.monotonic() - started < timeout:
            with SessionLocal() as db:
                running = Counter(dict(db.execute(select(Batch.organization_id, func.count()).join(
                    ProcessingJob, ProcessingJob.batch_id == Batch.id).where(ProcessingJob.status == "running")
                    .group_by(Batch.organization_id)).all()))
                finished = db.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.status == "finished"))
            for org, n in running.items():
                peak[org] = max(peak[org], n)
            if kill_one and killed is None and finished >= expected_jobs // 3 and sum(running.values()):
                killed = processes[1].pid
                os.kill(killed, signal.SIGKILL)
                processes[1].wait(10)
            if finished == expected_jobs:
                break
            time.sleep(0.2)
        elapsed = round(time.monotonic() - started, 1)

        with SessionLocal() as db:
            jobs = Counter(status for (status,) in db.execute(select(ProcessingJob.status)))
            tickets = db.execute(select(Ticket.id, Ticket.organization_id, Ticket.file_id, Ticket.source_page, Ticket.position,
                                        Ticket.status, Batch.organization_id).join(Batch, Batch.id == Ticket.batch_id)).all()
            failed_files = db.scalar(select(func.count()).select_from(UploadedFile).where(UploadedFile.status == "failed"))
            reads = Counter(dict(db.execute(select(ExtractionUsage.organization_id, func.count())
                                            .group_by(ExtractionUsage.organization_id)).all()))
            attempts = Counter(dict(db.execute(select(ProcessingJob.attempts, func.count()).group_by(ProcessingJob.attempts)).all()))
            live_workers = db.scalar(select(func.count()).select_from(WorkerHeartbeat))
        expected = expected_jobs * TICKETS_PER_SCAN
        if jobs != Counter({"finished": expected_jobs}):
            problems.append(f"jobs not all finished: {dict(jobs)}")
        if len(tickets) != expected:
            problems.append(f"{len(tickets)} tickets, expected exactly {expected}")
        if len({(t.file_id, t.source_page, t.position) for t in tickets}) != len(tickets):
            problems.append("a scan slot has more than one ticket")
        if any(t[1] != t[6] for t in tickets):
            problems.append("a ticket belongs to a different company from its batch")
        if failed_files:
            problems.append(f"{failed_files} files failed")
        if limit and max(peak.values(), default=0) > limit:
            problems.append(f"a company ran more than {limit} jobs at once: {dict(peak)}")
        per_company = Counter(t[1] for t in tickets)
        for n in range(1, companies + 1):
            if per_company[n] != uploads * TICKETS_PER_SCAN:
                problems.append(f"company {n} has {per_company[n]} tickets, expected {uploads * TICKETS_PER_SCAN}")
            if reads[n] < per_company[n]:
                problems.append(f"company {n}: {reads[n]} reads recorded for {per_company[n]} tickets")

        # Each company's own view, through the API.
        views = {}
        for n in range(1, companies + 1):
            client = _signed_in(port, n)
            mine = set(uploaded.get(n, []))
            listed = client.get("/api/batches").json()
            listed_ids = {b["id"] for b in (listed if isinstance(listed, list) else listed.get("items", listed.get("batches", [])))}
            if listed_ids != mine:
                problems.append(f"company {n} lists batches {sorted(listed_ids ^ mine)} that aren't exactly its own")
            seen = sum(len(client.get(f"/api/batches/{b}/tickets").json()) for b in mine)
            if seen != uploads * TICKETS_PER_SCAN:
                problems.append(f"company {n} sees {seen} of its tickets")
            others = [b for m, ids in uploaded.items() if m != n for b in ids]
            if others and client.get(f"/api/batches/{others[0]}/tickets").status_code != 404:
                problems.append(f"company {n} could open another company's batch")
            usage = client.get("/api/system/usage").json()
            if usage["tickets_read"] < uploads * TICKETS_PER_SCAN or usage["estimated_cost_usd"] is not None:
                problems.append(f"company {n} usage report: {usage['tickets_read']} read, cost {usage['estimated_cost_usd']}")
            views[n] = {"tickets_read": usage["tickets_read"], "prompt_tokens": usage["prompt_tokens"]}
        if killed is None and kill_one:
            problems.append("the run finished before a worker could be killed")
        return {"companies": companies, "uploads_each": uploads, "workers": workers, "per_company_limit": limit,
                "tickets": len(tickets), "expected_tickets": expected, "seconds": elapsed,
                "tickets_per_second": round(len(tickets) / elapsed, 2) if elapsed else None,
                "reads": sum(reads.values()), "re_reads_after_kill": sum(reads.values()) - len(tickets),
                "job_attempts": {str(k): v for k, v in sorted(attempts.items())}, "peak_running_per_company": dict(peak),
                "killed_worker": killed is not None, "worker_rows_left": live_workers, "usage_views": views, "problems": problems}
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in processes:
            try:
                process.wait(20)
            except subprocess.TimeoutExpired:
                process.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--companies", type=int, default=3)
    parser.add_argument("--uploads", type=int, default=3, help="Four-ticket scans each company uploads")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int, default=1, help="MAX_CONCURRENT_JOBS_PER_ORGANIZATION")
    parser.add_argument("--reader-seconds", type=float, default=0.2, help="How long the stand-in reader takes per ticket")
    parser.add_argument("--kill-one", action="store_true", help="SIGKILL one worker partway")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--database-url", help="An empty PostgreSQL database whose name contains 'perf' or 'load'. It is emptied.")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--as-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.as_worker:
        return _as_worker(args.reader_seconds)
    if args.database_url and not any(word in args.database_url.rsplit("/", 1)[-1] for word in ("perf", "load")):
        print("Refusing: the database name must contain 'perf' or 'load'.", file=sys.stderr)
        return 1
    if args.kill_one and args.workers < 2:
        parser.error("--kill-one needs at least two workers")
    result = run(companies=args.companies, uploads=args.uploads, workers=args.workers, url=args.database_url,
                 reader_seconds=args.reader_seconds, limit=args.limit, kill_one=args.kill_one, timeout=args.timeout)
    report = {"measured_at": time.strftime("%Y-%m-%d %H:%M"), "machine": f"{platform.platform()}, {os.cpu_count()} CPUs",
              "database": "PostgreSQL" if args.database_url else "SQLite", **result}
    print(json.dumps(report, indent=2))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
