"""Prove a backup can really be recovered from, before you need it to be.

Usage (from backend/):
    python scripts/restore_drill.py backups/ticketflow-20261105-120000
    python scripts/restore_drill.py --latest --report drill.json

Restores into a brand-new temporary folder, starts the real application against that
copy on a private port, then checks counts, exact totals and that a sample document
opens through the API. Your live database and documents are never touched, and the
temporary copy is deleted afterwards. Exit code 0 means pass, 1 means fail.

To sign in to the throwaway copy, it adds one temporary owner per company to that copy
only, after the counts have been compared. Reports hold counts, totals and row ids.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# This process only reads what it's pointed at. It must never reach for the configured database.
os.environ.setdefault("DATABASE_URL", "sqlite://")

from backup import company_totals, counts, newest_backup  # noqa: E402
from check_documents import check_database  # noqa: E402
from verify_backup import restore, verify  # noqa: E402

from app.auth import COOKIE_NAME  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.system_health import expected_revision  # noqa: E402

_START_SECONDS = 60


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Drill:
    def __init__(self, folder: Path, work: Path):
        self.folder = folder
        self.work = work
        self.restored = work / "restored"
        self.checks: list[dict] = []

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})
        print(f"{'pass' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
        return passed

    @property
    def database(self) -> Path:
        return self.restored / "ticketflow.db"

    @property
    def storage(self) -> Path:
        return self.restored / "storage"

    def _environment(self) -> dict:
        """Only what's needed, stated explicitly. Keys are blanked, so even a bug in the
        drill couldn't call a paid service. It runs from an empty folder, so no .env is read."""
        kept = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR") if name in os.environ}
        return {**kept, "DATABASE_URL": f"sqlite:///{self.database}", "STORAGE_DIR": str(self.storage), "BACKUP_DIR": str(self.work / "no-backups"),
                "TICKETFLOW_ENV": "development", "ENABLE_API_DOCS": "false", "SESSION_SECURE_COOKIE": "false",
                "TRUSTED_HOSTS": '["127.0.0.1"]', "CORS_ORIGINS": '["http://127.0.0.1:1"]', "ALLOW_REMOTE_SETUP": "false",
                "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "PYTHONDONTWRITEBYTECODE": "1"}

    def _upgrade_if_behind(self) -> bool:
        connection = sqlite3.connect(self.database)
        try:
            try:
                revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            except sqlite3.OperationalError:
                return self.record("Database version", True, "Not tracked by migrations, so it can't be checked")
        finally:
            connection.close()
        expected = expected_revision()
        if revision == expected:
            return self.record("Database version", True, f"At the current version, {revision}")
        result = subprocess.run([sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"], cwd=BACKEND,
                                env={**self._environment(), "PYTHONPATH": str(BACKEND)}, capture_output=True, timeout=600)
        return self.record("Database version", result.returncode == 0,
                           f"Upgraded this copy from {revision} to {expected}, as a real recovery would need" if result.returncode == 0
                           else f"This copy, at {revision}, could not be upgraded to {expected}")

    def _temporary_owners(self) -> dict[int, str]:
        """{company id: session token}, in the throwaway copy only."""
        tokens = {}
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
        connection = sqlite3.connect(self.database)
        try:
            for (organization_id,) in connection.execute("SELECT id FROM organizations ORDER BY id").fetchall():
                cursor = connection.execute(
                    "INSERT INTO users (username, organization_id, password_hash, active, role, must_change_password) VALUES (?, ?, 'no-sign-in', 1, 'owner', 0)",
                    (f"restore-drill-{organization_id}-{secrets.token_hex(4)}", organization_id))
                token = secrets.token_urlsafe(32)
                connection.execute("INSERT INTO login_sessions (token_digest, user_id, expires_at) VALUES (?, ?, ?)",
                                   (hashlib.sha256(token.encode("ascii")).hexdigest(), cursor.lastrowid, expires))
                tokens[organization_id] = token
            connection.commit()
        finally:
            connection.close()
        return tokens

    def _sample_documents(self, connection: sqlite3.Connection) -> dict[int, list[tuple[str, str | None]]]:
        """{company id: [(API path, expected hash or None)]}: one scan, one page and one
        invoice per company, so each kind of document is opened for real."""
        samples: dict[int, list[tuple[str, str | None]]] = {}
        for sql, path, hashed in (
            ("SELECT t.organization_id, MAX(t.id) FROM tickets t GROUP BY t.organization_id", "/api/tickets/{}/image", None),
            ("SELECT b.organization_id, MAX(f.id) FROM uploaded_files f JOIN batches b ON b.id = f.batch_id GROUP BY b.organization_id", "/api/files/{}/pages/1/image", None),
            ("SELECT organization_id, MAX(id) FROM invoice_documents GROUP BY organization_id", "/api/invoice-documents/{}/original", "invoice_documents"),
        ):
            for organization_id, row_id in connection.execute(sql).fetchall():
                expected = None
                if hashed:
                    expected = connection.execute(f"SELECT sha256 FROM {hashed} WHERE id = ?", (row_id,)).fetchone()[0]
                samples.setdefault(organization_id, []).append((path.format(row_id), expected))
        return samples

    def _get(self, port: int, path: str, token: str | None = None) -> tuple[int, bytes]:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if token:
            request.add_header("Cookie", f"{COOKIE_NAME}={token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - always our own private port
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, b""

    def _wait_until_up(self, port: int, process: subprocess.Popen) -> bool:
        deadline = time.monotonic() + _START_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            try:
                if self._get(port, "/health")[0] == 200:
                    return True
            except OSError:
                pass
            time.sleep(0.2)
        return False

    def _through_the_app(self, expected_totals: dict[str, dict]) -> None:
        connection = sqlite3.connect(self.database)
        try:
            samples = self._sample_documents(connection)
        finally:
            connection.close()
        tokens = self._temporary_owners()
        port = _free_port()
        log_path = self.work / "app.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND), "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                cwd=self.work, env=self._environment(), stdout=log, stderr=subprocess.STDOUT)
        try:
            up = self.record("The application starts against the restored copy", self._wait_until_up(port, process))
            if not up:
                print("The application's own last lines:", file=sys.stderr)
                print("\n".join(log_path.read_text(errors="replace").splitlines()[-8:]), file=sys.stderr)
                return
            for organization_id, token in tokens.items():
                status, body = self._get(port, "/api/dashboard", token)
                if status != 200:
                    self.record(f"Company {organization_id}: dashboard", False, f"Answered {status}")
                    continue
                board = json.loads(body)
                want = expected_totals.get(str(organization_id), {"approved_tons": "0.000", "approved_cost": "0.00"})
                same = (Decimal(board["total_tons"]) == Decimal(want["approved_tons"]) and Decimal(board["total_cost"]) == Decimal(want["approved_cost"]))
                self.record(f"Company {organization_id}: the app's totals equal the backup's", same,
                            f"{want['approved_tons']} tons, ${want['approved_cost']}")
            for organization_id, documents in samples.items():
                for path, expected in documents:
                    status, body = self._get(port, path, tokens[organization_id])
                    good = status == 200 and bool(body) and (expected is None or hashlib.sha256(body).hexdigest() == expected)
                    self.record(f"Company {organization_id}: {path} opens through the API", good, "" if good else f"Answered {status}")
            if not any(samples.values()):
                self.record("Sample documents", True, "This backup has no documents to open")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def run(self) -> bool:
        problems = verify(self.folder)
        for problem in problems:
            self.record("The backup is complete and unchanged", False, problem)
        if problems:
            return False
        self.record("The backup is complete and unchanged", True)
        manifest = json.loads((self.folder / "manifest.json").read_text())
        restore(self.folder, self.restored)
        self.record("Restored into a brand-new folder", True)

        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            found = counts(connection)
            totals = company_totals(connection)
        finally:
            connection.close()
        for name, expected in manifest["counts"].items():
            self.record(f"Count of {name}", found.get(name) == expected, f"{found.get(name)} (backup says {expected})")
        if "totals" in manifest:
            self.record("Exact totals per company equal the backup's", totals == manifest["totals"],
                        "; ".join(f"company {k}: {v['approved_tons']} tons, ${v['approved_cost']}" for k, v in totals.items()) or "No approved tickets")

        if not self._upgrade_if_behind():
            return False
        documents = check_database(f"sqlite:///{self.database}", self.storage, inside_only=True)
        self.record("Every document is in the restored copy and matches its hash", documents.ok,
                    f"{documents.checked} checked, {documents.missing_count} missing, {documents.mismatched_count} damaged")
        self._through_the_app(totals)
        return all(check["passed"] for check in self.checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, nargs="?", help="The backup folder to test")
    parser.add_argument("--latest", action="store_true", help="Test the newest backup in BACKUP_DIR")
    parser.add_argument("--work-dir", type=Path, help="Where to make the temporary copy. It needs room for the whole backup")
    parser.add_argument("--report", type=Path, help="Write the result as JSON")
    args = parser.parse_args(argv)
    folder = newest_backup(settings.backup_dir) if args.latest else args.folder
    if folder is None:
        parser.error("Name a backup folder, or use --latest with a backup in BACKUP_DIR.")
    if not folder.is_dir():
        print(f"Backup not found: {folder}", file=sys.stderr)
        return 1
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="ticketflow-drill-", dir=args.work_dir))  # Private, and brand new.
    drill = Drill(folder, work)
    try:
        try:
            passed = drill.run()
        except Exception as exc:
            # Only its kind: an error's text could quote a record.
            passed = drill.record("The drill finished", False, type(exc).__name__)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    seconds = round(time.monotonic() - started, 1)
    print(f"\nRESTORE DRILL {'PASSED' if passed else 'FAILED'} in {seconds} seconds.")
    if args.report:
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"backup": folder.name, "passed": passed, "seconds": seconds, "at": datetime.now(timezone.utc).isoformat(), "checks": drill.checks}, stream, indent=2)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
