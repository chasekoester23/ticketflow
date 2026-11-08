"""Many employees at once against a real server, then proof the figures are exact.

Usage (from backend/):
    python scripts/load_test.py --users 1 5 20
    python scripts/load_test.py --users 20 --seconds 60 --tickets 20000 --json out.json
    python scripts/load_test.py --users 5 --database-url postgresql://u:p@localhost/ticketflow_perf
    python scripts/load_test.py --explain --database-url postgresql://...  # plans for a database already filled

Starts a real uvicorn server on a new temporary database (SQLite unless a PostgreSQL
URL is given, whose database name must contain "perf" or "load"), fills it with
fictional records, signs in N employees and has each one read, edit, approve and export
for a while, over real HTTP. Reports throughput, p50/p95/p99 latency and errors per
action. Then checks, from the database itself and from every report, that approved
tons and cost are exact and that nothing was approved or counted twice.

Standard library plus the backend's own packages. No AI, no network beyond this
machine. Fictional data, on whatever machine runs it: the figures compare runs, they
are not a promise about production.
"""
from __future__ import annotations

import argparse
import csv
import http.client
import io
import json
import os
import platform
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PASSWORD = "fictional-load-password"
# Most of a working day is reading. Shares of each employee's actions.
MIX = {"read": 55, "search": 12, "edit": 12, "approve": 14, "export": 7}
READS = ["/api/dashboard", "/api/batches/queue", "/api/invoice-comparisons?summary=true", "/api/records/search?page_size=50",
         "/api/reports/project/1", "/api/batches/{batch}/tickets"]


def _environment(work: Path, url: str | None) -> dict:
    return {**os.environ, "DATABASE_URL": url or f"sqlite:///{work / 'load.db'}", "STORAGE_DIR": str(work / "storage"),
            "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "PYTHONWARNINGS": "ignore",
            "PYTHONPATH": str(BACKEND),
            # This measures how much one server can do. Each simulated employee sends far more
            # requests a minute than a person does, so the per-person limits would turn the
            # measurement into a count of 429s. The limits have their own tests.
            "RATE_LIMIT_UPLOADS_PER_MINUTE": "0", "RATE_LIMIT_EXPORTS_PER_MINUTE": "0",
            "RATE_LIMIT_REPORTS_PER_MINUTE": "0"}


# ---------------------------------------------------------------- seeding, in its own process

def seed(tickets: int, users: int) -> dict:
    """The benchmark's fictional company, plus one real login per employee. Approvers and
    editors get separate pools of unapproved tickets, so the final count is predictable."""
    from sqlalchemy import select, update

    from app.auth import password_hash
    from app.db import SessionLocal
    from app.models import Batch, Ticket, User
    from sqlalchemy import delete

    from app.models import Organization
    sys.path.insert(0, str(BACKEND / "scripts"))
    import measure_performance
    with SessionLocal() as db:  # The migrations add a default company; the generator brings its own.
        db.execute(delete(Organization))
        db.commit()
    measure_performance.generate(tickets)
    digest = password_hash(PASSWORD)
    with SessionLocal() as db:
        for n in range(users):
            role = "manager" if n % 2 == 0 else "clerk"
            db.add(User(id=100 + n, organization_id=1, username=f"employee-{n}", display_name=f"Employee {n}",
                        password_hash=digest, role=role, active=True, must_change_password=False))
        db.commit()
        numbers = dict(db.execute(select(Ticket.id, Ticket.ticket_number).join(Batch, Batch.id == Ticket.batch_id).where(
            Batch.organization_id == 1, Ticket.status == "needs_review").order_by(Ticket.id)).all())
        open_ids = list(numbers)
        # Every unapproved ticket gets a job location, so nothing blocks an approval but the rules.
        db.execute(update(Ticket).where(Ticket.id.in_(open_ids)).values(job_location="Fictional Site"))
        db.commit()
        batches = list(db.scalars(select(Batch.id).where(Batch.organization_id == 1).order_by(Batch.id).limit(20)))
    half = len(open_ids) // 2
    return {"approve": open_ids[:half], "edit": open_ids[half:], "batches": batches,
            "numbers": {str(ticket): numbers[ticket] for ticket in open_ids[:half]}}


# ---------------------------------------------------------------- one employee

class Employee:
    def __init__(self, port: int, number: int, pools: dict, rng: random.Random, until: float, results: dict, lock: threading.Lock):
        self.port, self.number, self.pools, self.rng, self.until = port, number, pools, rng, until
        self.results, self.lock = results, lock
        self.manager = number % 2 == 0
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        self.cookie = ""
        self.approved: list[tuple[int, str]] = []

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, bytes, dict]:
        data = json.dumps(body).encode() if body is not None else None
        sent = {"X-TicketFlow-Request": "1", "Cookie": self.cookie, **({"Content-Type": "application/json"} if data else {}), **(headers or {})}
        for attempt in (1, 2):
            try:
                self.connection.request(method, path, body=data, headers=sent)
                response = self.connection.getresponse()
                payload = response.read()
                return response.status, payload, dict(response.getheaders())
            except (http.client.HTTPException, OSError):
                self.connection.close()
                self.connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def sign_in(self) -> None:
        status, _, headers = self.request("POST", "/api/auth/login", {"username": f"employee-{self.number}", "password": PASSWORD})
        assert status == 200, f"employee {self.number} couldn't sign in: {status}"
        self.cookie = headers["set-cookie"].split(";", 1)[0]

    def timed(self, action: str, method: str, path: str, body=None, headers=None) -> tuple[int, bytes]:
        started = time.perf_counter()
        try:
            status, payload, _ = self.request(method, path, body, headers)
        except (http.client.HTTPException, OSError):
            status, payload = 0, b""
        elapsed = time.perf_counter() - started
        with self.lock:
            row = self.results.setdefault(action, {"times": [], "status": defaultdict(int)})
            row["times"].append(elapsed)
            row["status"][status] += 1
        return status, payload

    def act(self) -> None:
        action = self.rng.choices(list(MIX), weights=list(MIX.values()))[0]
        if action == "approve" and not self.manager:
            action = "edit"
        if action == "read":
            path = self.rng.choice(READS).replace("{batch}", str(self.rng.choice(self.pools["batches"])))
            self.timed("read", "GET", path)
        elif action == "search":
            self.timed("search", "GET", f"/api/records/search?page_size=50&q={self.rng.randint(100000, 100999)}")
        elif action == "export":
            self.timed("export", "GET", f"/api/records/export.csv?project_id={self.rng.randint(2, 20)}")
        elif action == "edit":
            ticket = self.rng.choice(self.pools["edit"])
            self.timed("edit", "PATCH", f"/api/tickets/{ticket}", {"job_location": f"Lot {self.rng.randint(1, 9)}"})
        else:
            ticket = self.rng.choice(self.pools["approve"])
            # What the person sees before approving, so the approval carries its revision.
            status, payload = self.request("GET", f"/api/records/search?q={self.pools['numbers'][str(ticket)]}&page_size=5")[:2]
            rows = [r for r in json.loads(payload).get("rows", []) if r["id"] == ticket] if status == 200 else []
            revision = rows[0]["revision"] if rows else "none"
            status, payload = self.timed("approve", "POST", f"/api/tickets/{ticket}/verify", None, {"If-Match": revision})
            if status == 200:
                self.approved.append((ticket, json.loads(payload)["status"]))

    def run(self) -> None:
        self.sign_in()
        while time.monotonic() < self.until:
            self.act()


# ---------------------------------------------------------------- checking the result

def check(url: str, port: int, approver_cookie: str, approved: set[int], before: dict) -> dict:
    """From the database itself, then from every report: exact, and nothing twice."""
    from sqlalchemy import create_engine, func, select

    from app.db import engine_options, normalized_url
    from app.models import AuditEvent, Batch, Ticket
    engine = create_engine(normalized_url(url), **engine_options(url))
    cents = Decimal("0.01")
    problems = []
    with engine.connect() as db:
        rows = db.execute(select(Ticket.id, Ticket.tonnage, Ticket.applied_unit_price, Ticket.approved_cost, Ticket.crop_sha256,
                                 Ticket.tonnage_confirmed).join(Batch, Batch.id == Ticket.batch_id).where(
            Batch.organization_id == 1, Ticket.status == "verified")).all()
        events = dict(db.execute(select(AuditEvent.ticket_id, func.count()).where(
            AuditEvent.field == "status", AuditEvent.new_value == "verified").group_by(AuditEvent.ticket_id)).all())
    engine.dispose()
    tons = sum((Decimal(r.tonnage) for r in rows), Decimal(0))
    # Reports add up the cost stored at approval; exports multiply tons by the locked rate.
    # Both are computed here from the rows themselves, so each view is checked against its own rule.
    cost = sum((Decimal(r.approved_cost) for r in rows if r.approved_cost is not None), Decimal(0))
    multiplied = sum(((Decimal(r.tonnage) * Decimal(r.applied_unit_price)).quantize(cents, ROUND_HALF_UP)
                      for r in rows if r.applied_unit_price is not None), Decimal(0))
    seeded_rounding = sum(1 for r in rows if r.id not in approved and r.applied_unit_price is not None and Decimal(r.approved_cost)
                          != (Decimal(r.tonnage) * Decimal(r.applied_unit_price)).quantize(cents, ROUND_HALF_UP))
    ids = {r.id for r in rows}
    if len({r.crop_sha256 for r in rows}) != len(rows):
        problems.append("a scan is approved twice")
    if ids != before["verified"] | approved:
        problems.append(f"approved tickets: {len(ids)} on record, {len(before['verified'] | approved)} expected")
    for r in rows:
        if r.id in approved:
            if events.get(r.id) != 1:
                problems.append(f"ticket {r.id} has {events.get(r.id)} approvals on record")
            if Decimal(r.approved_cost) != (Decimal(r.tonnage) * Decimal(r.applied_unit_price)).quantize(cents, ROUND_HALF_UP):
                problems.append(f"ticket {r.id} cost isn't tons times its locked rate")
    expected_tons = before["tons"] + sum((before["open_tons"][t] for t in approved), Decimal(0))
    if tons != expected_tons:
        problems.append(f"approved tons {tons}, expected {expected_tons}")

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=600)

    def get(path: str) -> bytes:
        connection.request("GET", path, headers={"Cookie": approver_cookie})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (path, response.status)
        return body

    dashboard = json.loads(get("/api/dashboard"))
    summary = json.loads(get("/api/records/search?page_size=1"))["summary"]
    pilot = json.loads(get("/api/reports/pilot"))["review"]
    exported = list(csv.DictReader(io.StringIO(get("/api/records/export.csv").decode())))
    verified_rows = [r for r in exported if r["Verification Status"] == "Verified"]
    views = {
        "dashboard": (Decimal(dashboard["total_tons"]), Decimal(dashboard["total_cost"])),
        "register": (Decimal(summary["verified_tons"]), Decimal(summary["verified_cost"])),
        "pilot report": (Decimal(pilot["verified_tons"]), None),
        "CSV export": (sum((Decimal(r["Tonnage"]) for r in verified_rows), Decimal(0)),
                       sum((Decimal(r["Calculated Cost"]) for r in verified_rows if r["Calculated Cost"]), Decimal(0))),
    }
    for name, (view_tons, view_cost) in views.items():
        expected_cost = multiplied if name == "CSV export" else cost
        if view_tons != tons or (view_cost is not None and view_cost != expected_cost):
            problems.append(f"{name} shows {view_tons} t / {view_cost}, the database has {tons} t / {expected_cost}")
    return {"approved_tickets": len(ids), "approved_during_run": len(approved), "approved_tons": str(tons), "approved_cost": str(cost),
            "seeded_rows_rounded_half_even": seeded_rounding, "problems": problems}


def _state(url: str, pools: dict) -> dict:
    from sqlalchemy import create_engine, select

    from app.db import engine_options, normalized_url
    from app.models import Batch, Ticket
    engine = create_engine(normalized_url(url), **engine_options(url))
    with engine.connect() as db:
        verified = db.execute(select(Ticket.id, Ticket.tonnage).join(Batch, Batch.id == Ticket.batch_id).where(
            Batch.organization_id == 1, Ticket.status == "verified")).all()
        open_tons = dict(db.execute(select(Ticket.id, Ticket.tonnage).where(Ticket.id.in_(pools["approve"]))).all())
    engine.dispose()
    return {"verified": {r.id for r in verified}, "tons": sum((Decimal(r.tonnage) for r in verified), Decimal(0)),
            "open_tons": {k: Decimal(v) for k, v in open_tons.items()}}


def _percentile(sorted_times: list[float], share: float) -> float:
    if not sorted_times:
        return 0.0
    return sorted_times[min(len(sorted_times) - 1, max(0, int(round(share * len(sorted_times) + 0.5)) - 1))]


def run_once(users: int, *, seconds: float, tickets: int, url: str | None, seed_value: int) -> dict:
    work = Path(tempfile.mkdtemp(prefix="ticketflow-load-"))
    env = _environment(work, url)
    server = None
    try:
        migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, capture_output=True, text=True)
        if migrated.returncode:
            raise SystemExit(migrated.stderr[-2000:])
        seeded = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--seed-only", str(tickets), str(users)],
                                cwd=BACKEND, env=env, capture_output=True, text=True)
        if seeded.returncode:
            raise SystemExit(seeded.stderr[-2000:])
        pools = json.loads(seeded.stdout.strip().splitlines()[-1])
        os.environ.update({"DATABASE_URL": env["DATABASE_URL"]})
        before = _state(env["DATABASE_URL"], pools)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        log = open(work / "server.log", "wb")
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning",
                                   "--no-access-log"], cwd=BACKEND, env=env, stdout=log, stderr=log)
        for _ in range(240):
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                connection.request("GET", "/health")
                if connection.getresponse().status == 200:
                    break
            except OSError:
                time.sleep(0.25)
        results: dict = {}
        lock = threading.Lock()
        rng = random.Random(seed_value)
        load_before = os.getloadavg()
        started = time.monotonic()
        employees = [Employee(port, n, pools, random.Random(rng.random()), started + seconds, results, lock) for n in range(users)]
        threads = [threading.Thread(target=employee.run) for employee in employees]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        wall = time.monotonic() - started
        load_after = os.getloadavg()
        approved = {ticket for employee in employees for ticket, _ in employee.approved}
        manager = next(e for e in employees if e.manager)
        verdict = check(env["DATABASE_URL"], port, manager.cookie, approved, before)
        actions = {}
        total = errors = 0
        for name, row in sorted(results.items()):
            times = sorted(row["times"])
            failed = sum(count for status, count in row["status"].items() if status == 0 or status >= 500)
            conflicts = sum(count for status, count in row["status"].items() if status == 409)
            total += len(times)
            errors += failed
            actions[name] = {"requests": len(times), "p50_ms": round(_percentile(times, 0.50) * 1000, 1),
                             "p95_ms": round(_percentile(times, 0.95) * 1000, 1), "p99_ms": round(_percentile(times, 0.99) * 1000, 1),
                             "errors": failed, "conflicts_409": conflicts,
                             "other": {str(k): v for k, v in row["status"].items() if k not in (200, 409) and k and k < 500}}
        everything = sorted(t for row in results.values() for t in row["times"])
        return {"users": users, "seconds": round(wall, 1), "tickets": tickets, "requests": total,
                "throughput_per_s": round(total / wall, 1), "errors": errors,
                "p50_ms": round(_percentile(everything, 0.50) * 1000, 1), "p95_ms": round(_percentile(everything, 0.95) * 1000, 1),
                "p99_ms": round(_percentile(everything, 0.99) * 1000, 1), "actions": actions,
                "load_average_before": [round(x, 2) for x in load_before], "load_average_after": [round(x, 2) for x in load_after],
                **verdict}
    finally:
        if server is not None and server.poll() is None:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(20)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(work, ignore_errors=True)


def _markdown(run: dict) -> str:
    lines = [f"### {run['users']} employee(s), {run['tickets']:,} tickets, {run['seconds']} s",
             f"{run['requests']} requests, {run['throughput_per_s']}/s, {run['errors']} errors; overall p50 {run['p50_ms']} ms, "
             f"p95 {run['p95_ms']} ms, p99 {run['p99_ms']} ms. Load average before {run['load_average_before']}, after {run['load_average_after']}.",
             "", "| Action | Requests | p50 ms | p95 ms | p99 ms | Errors | 409 refused |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name, row in run["actions"].items():
        lines.append(f"| {name} | {row['requests']} | {row['p50_ms']} | {row['p95_ms']} | {row['p99_ms']} | {row['errors']} | {row['conflicts_409']} |")
    lines.append("")
    lines.append(f"Afterwards: {run['approved_tickets']} approved tickets ({run['approved_during_run']} during the run), "
                 f"{run['approved_tons']} t, ${run['approved_cost']}. "
                 + ("Every check exact; nothing approved or counted twice." if not run["problems"] else "PROBLEMS: " + "; ".join(run["problems"])))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--users", type=int, nargs="+", default=[1, 5, 20])
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--tickets", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--database-url", help="An empty PostgreSQL database whose name contains 'perf' or 'load'. Emptied between runs.")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--explain", action="store_true", help="Plans for each slow read, on an already filled PostgreSQL --database-url")
    parser.add_argument("--seed-only", nargs=2, type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(BACKEND))
    if args.seed_only:
        print(json.dumps(seed(*args.seed_only)))
        return 0
    if args.database_url and not any(word in args.database_url.rsplit("/", 1)[-1] for word in ("perf", "load")):
        print("Refusing: the database name must contain 'perf' or 'load'.", file=sys.stderr)
        return 1
    if args.explain:
        if not (args.database_url or "").startswith("postgres"):
            print("--explain needs a PostgreSQL --database-url.", file=sys.stderr)
            return 1
        os.environ["DATABASE_URL"] = args.database_url
        plans = explain(args.database_url)
        if args.json:
            args.json.write_text(json.dumps(plans, indent=2))
        return 0
    report = {"measured_at": time.strftime("%Y-%m-%d %H:%M"), "machine": f"{platform.platform()}, {os.cpu_count()} CPUs",
              "python": platform.python_version(), "database": "PostgreSQL" if args.database_url else "SQLite", "runs": []}
    print(f"Machine: {report['machine']}. Database: {report['database']}. Fictional data; compare runs, not a promise about production.")
    failed = False
    for users in args.users:
        if args.database_url:
            _empty(args.database_url)
        run = run_once(users, seconds=args.seconds, tickets=args.tickets, url=args.database_url, seed_value=args.seed)
        report["runs"].append(run)
        print("\n" + _markdown(run), flush=True)
        failed = failed or bool(run["problems"]) or run["errors"] > 0
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    return 1 if failed else 0


def explain(url: str) -> list[dict]:
    """PostgreSQL's own account of each slow read: every SELECT a request makes, run under
    EXPLAIN ANALYZE, with the indexes it used and any full scans of a large table."""
    from fastapi.testclient import TestClient
    from sqlalchemy import event

    from app.auth import current_user
    from app.db import engine
    from app.main import app
    from app.models import User
    sys.path.insert(0, str(BACKEND / "scripts"))
    from measure_performance import READS as TARGETS
    app.dependency_overrides[current_user] = lambda: User(id=1, organization_id=1, username="perf", password_hash="x", active=True,
                                                          role="owner", must_change_password=False)
    captured: list[tuple[str, object]] = []

    def grab(_conn, _cursor, statement, parameters, _context, executemany):
        if not executemany and statement.lstrip().upper().startswith(("SELECT", "WITH")):
            captured.append((statement, parameters))

    def nodes(plan: dict):
        yield plan
        for child in plan.get("Plans", []):
            yield from nodes(child)

    event.listen(engine, "before_cursor_execute", grab)
    found = []
    with TestClient(app, headers={"X-TicketFlow-Request": "1"}) as client:
        for label, path in TARGETS:
            captured.clear()
            started = time.perf_counter()
            status = client.get(path).status_code
            seconds = time.perf_counter() - started
            statements = list(captured)
            event.remove(engine, "before_cursor_execute", grab)
            indexes, scans, slowest = set(), set(), (0.0, "")
            with engine.connect() as connection:
                for statement, parameters in statements:
                    plan = connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters).scalar()
                    plan = (json.loads(plan) if isinstance(plan, str) else plan)[0]
                    if plan["Execution Time"] > slowest[0]:
                        slowest = (plan["Execution Time"], " ".join(statement.split())[:300])
                    for node in nodes(plan["Plan"]):
                        if node.get("Index Name"):
                            indexes.add(f"{node['Index Name']} ({node['Node Type']})")
                        if node["Node Type"] == "Seq Scan" and node.get("Actual Rows", 0) * node.get("Actual Loops", 1) > 1000:
                            scans.add(f"{node['Relation Name']} ({node['Actual Rows'] * node.get('Actual Loops', 1):,} rows)")
            event.listen(engine, "before_cursor_execute", grab)
            found.append({"request": label, "path": path, "status": status, "seconds": round(seconds, 3), "statements": len(statements),
                          "slowest_statement_ms": round(slowest[0], 1), "slowest_statement": slowest[1],
                          "indexes_used": sorted(indexes), "full_scans_over_1000_rows": sorted(scans)})
            print(f"{label}: {seconds:.3f} s, {len(statements)} SELECTs, slowest {slowest[0]:.1f} ms; "
                  f"indexes {', '.join(sorted(indexes)) or 'none'}; full scans {', '.join(sorted(scans)) or 'none'}", flush=True)
    return found


def _empty(url: str) -> None:
    """Drop everything in the named PostgreSQL database, so each run starts the same."""
    from sqlalchemy import create_engine, text

    from app.db import normalized_url
    engine = create_engine(normalized_url(url))
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
