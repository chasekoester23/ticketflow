"""An existing TicketFlow owner and project survive the organization migration.

This constructs pre-organization tables on disk, not a freshly initialized ORM
schema, so it exercises the real SQLite rebuild/backfill upgrade path.
"""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_legacy_owner_project_and_code_are_preserved_across_upgrade(tmp_path):
    db_path = tmp_path / "ticketflow-pre-organizations.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(100) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                active BOOLEAN NOT NULL
            );
            INSERT INTO users(id, username, password_hash, active)
            VALUES (7, 'owner', 'existing-password-digest', 1);
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                code VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(200) NOT NULL,
                job_location VARCHAR(200),
                description TEXT,
                status VARCHAR(20),
                created_at DATETIME
            );
            INSERT INTO projects(id, code, name, status)
            VALUES (11, 'TEST-001', 'Demo Construction Project', 'active');
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY, status VARCHAR(20)
            );
            INSERT INTO tickets (id,status) VALUES (23, 'verified');
        """)

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    with sqlite3.connect(db_path) as db:
        user = db.execute(
            "SELECT id,username,password_hash,organization_id FROM users WHERE id=7"
        ).fetchone()
        project = db.execute(
            "SELECT id,code,name,organization_id FROM projects WHERE id=11"
        ).fetchone()
        assert user == (7, "owner", "existing-password-digest", 1)
        assert project == (11, "TEST-001", "Demo Construction Project", 1)
        assert db.execute(
            "SELECT status,price_snapshot_state FROM tickets WHERE id=23"
        ).fetchone() == ("verified", None)
        assert db.execute("SELECT name FROM organizations WHERE id=1").fetchone() == ("Original workspace",)

        # The company-unique project code constraint permits reuse elsewhere.
        db.execute("INSERT INTO organizations (id,name) VALUES (2,'Second Contractor')")
        db.execute(
            "INSERT INTO projects (organization_id,code,name) VALUES (2,'TEST-001','Second Project')"
        )
        assert db.execute("SELECT COUNT(*) FROM projects WHERE code='TEST-001'").fetchone()[0] == 2

    # A repeat migration should neither delete nor duplicate preexisting rows.
    command.upgrade(cfg, "head")
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM users WHERE id=7").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM tickets WHERE id=23").fetchone()[0] == 1
