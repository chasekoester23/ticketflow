"""APP_ENVIRONMENT: the fictional demo and a customer's pilot can't share data or AI.

The demo can only reach a database and buckets named "demo" and never reads with AI.
A pilot or production installation can't reach anything named "demo", needs PostgreSQL,
private https object storage, secure cookies, real credentials and a monthly read cap.
Every value here is fictional.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main as main_module
from app.config import Settings, configuration_problems, environment, settings
from app.main import app
from scripts import seed_demo

DOCS = Path(__file__).resolve().parents[2] / "docs" / "deploy"

PILOT = dict(
    app_environment="pilot", database_url="postgresql://quarvera:Fictional-Pilot-Pass-1@db.internal:5432/railway",
    session_secure_cookie=True, cors_origins=["https://app.example.com"], trusted_hosts=["api.railway.internal"],
    enable_api_docs=False, storage_backend="s3", s3_endpoint_url="https://account.r2.cloudflarestorage.com",
    s3_bucket="quarvera-pilot-documents", s3_access_key_id="fictional-key-id", s3_secret_access_key="fictional-bucket-token-1",
    monthly_ticket_reads_per_organization=2000, ai_reading_enabled=True, gemini_api_key=None, anthropic_api_key=None,
    openai_api_key=None, backup_s3_bucket=None, backup_source_s3_bucket=None,
)
DEMO = {**PILOT, "app_environment": "demo", "ticketflow_env": "production", "ai_reading_enabled": False,
        "monthly_ticket_reads_per_organization": 0, "s3_bucket": "quarvera-demo-documents",
        "database_url": "postgresql://quarvera:Fictional-Demo-Pass-1@db.internal:5432/quarvera_demo"}
LOCAL_DEMO = dict(app_environment="demo", ai_reading_enabled=False, gemini_api_key=None, anthropic_api_key=None, openai_api_key=None,
                  database_url="sqlite:////srv/demo_workspace/quarvera-demo/quarvera-demo.db",
                  storage_dir=Path("/srv/demo_workspace/quarvera-demo/files"), storage_backend="local",
                  backup_s3_bucket=None, backup_source_s3_bucket=None)


def make(base: dict, **overrides) -> Settings:
    # Never backend/.env: only what the test says.
    return Settings(_env_file=None, **{**base, **overrides})


def problems(base: dict, **overrides) -> list[str]:
    return configuration_problems(make(base, **overrides))


def refused(base: dict, expected: str, **overrides) -> None:
    found = problems(base, **overrides)
    assert any(expected in problem for problem in found), found


# ------------------------------------------------------------------ pilot and production

def test_a_complete_pilot_starts_and_runs_in_production_mode():
    config = make(PILOT)
    assert configuration_problems(config) == []
    assert config.ticketflow_env == "production" and environment(config) == "pilot"


def test_production_has_the_pilots_rules():
    assert problems(PILOT, app_environment="production") == []
    refused(PILOT, "MONTHLY_TICKET_READS_PER_ORGANIZATION", app_environment="production", monthly_ticket_reads_per_organization=0)


@pytest.mark.parametrize("override, expected", [
    ({"database_url": "sqlite:///quarvera.db"}, "PostgreSQL"),
    ({"storage_backend": "local"}, "STORAGE_BACKEND=s3"),
    ({"s3_endpoint_url": "http://minio.internal:9000"}, "https://"),
    ({"session_secure_cookie": False}, "SESSION_SECURE_COOKIE"),
    ({"monthly_ticket_reads_per_organization": 0}, "MONTHLY_TICKET_READS_PER_ORGANIZATION"),
    ({"database_url": "postgresql://quarvera:<db-password>@db.internal:5432/railway"}, "real password"),
    ({"database_url": "postgresql://quarvera:postgres@db.internal:5432/railway"}, "real password"),
    ({"database_url": "postgresql://quarvera@db.internal:5432/railway"}, "real password"),
    ({"database_url": "postgresql://quarvera:local-only-password@db.internal:5432/railway"}, "real password"),
    ({"s3_secret_access_key": "<r2-secret-access-key>"}, "S3_SECRET_ACCESS_KEY"),
    ({"s3_secret_access_key": "minioadmin"}, "S3_SECRET_ACCESS_KEY"),
    ({"database_url": "postgresql://quarvera:Fictional-Pilot-Pass-1@db.internal:5432/quarvera_demo"}, "database with \"demo\""),
    ({"s3_bucket": "quarvera-demo-documents"}, "S3_BUCKET"),
    ({"backup_s3_bucket": "quarvera-demo-backups"}, "BACKUP_S3_BUCKET"),
    ({"ticketflow_env": "development"}, "TICKETFLOW_ENV=production"),
])
def test_a_pilot_refuses(override, expected):
    refused(PILOT, expected, **override)


def test_a_pilot_without_ai_reading_needs_no_read_cap():
    assert problems(PILOT, ai_reading_enabled=False, monthly_ticket_reads_per_organization=0) == []


# ------------------------------------------------------------------ the demo

def test_a_hosted_demo_starts():
    assert problems(DEMO) == []
    assert environment(make(DEMO)) == "demo"


def test_a_local_demo_on_sqlite_starts():
    assert problems(LOCAL_DEMO) == []


@pytest.mark.parametrize("override, expected", [
    ({"ai_reading_enabled": True}, "AI_READING_ENABLED=false"),
    ({"database_url": "postgresql://quarvera:Fictional-Demo-Pass-1@db.internal:5432/railway"}, "database must have \"demo\""),
    ({"database_url": "postgresql://quarvera:Fictional-Demo-Pass-1@db.internal:5432/quarvera_pilot"}, "database must have \"demo\""),
    ({"s3_bucket": "quarvera-production-documents"}, "S3_BUCKET"),
    ({"backup_s3_bucket": "quarvera-backups"}, "BACKUP_S3_BUCKET"),
    ({"backup_source_s3_bucket": "quarvera-production-documents"}, "BACKUP_SOURCE_S3_BUCKET"),
    ({"gemini_api_key": "fictional-not-a-key"}, "GEMINI_API_KEY"),
    ({"anthropic_api_key": "fictional-not-a-key"}, "ANTHROPIC_API_KEY"),
    ({"database_url": "postgresql://quarvera:<db-password>@db.internal:5432/quarvera_demo"}, "real password"),
    ({"database_url": "sqlite:///quarvera_demo.db"}, "PostgreSQL"),
])
def test_a_hosted_demo_refuses(override, expected):
    refused(DEMO, expected, **override)


@pytest.mark.parametrize("override, expected", [
    ({"ai_reading_enabled": True}, "AI_READING_ENABLED=false"),
    ({"database_url": "sqlite:////srv/backend/ticketflow.db"}, "database must have \"demo\""),
    ({"database_url": "sqlite://"}, "database must have \"demo\""),
    ({"storage_dir": Path("/srv/backend/storage")}, "STORAGE_DIR"),
])
def test_a_local_demo_refuses(override, expected):
    refused(LOCAL_DEMO, expected, **override)


def test_a_local_demo_may_sit_beside_a_key_it_never_uses():
    """backend/.env often holds a key. AI reading is off whatever keys exist; only the
    hosted demo must not hold one at all."""
    assert problems(LOCAL_DEMO, gemini_api_key="fictional-not-a-key") == []


# ------------------------------------------------------------------ declaring, and not declaring

def test_development_cant_be_declared_on_a_hosted_server():
    refused(PILOT, "APP_ENVIRONMENT=development", app_environment="development", ticketflow_env="production")


def test_an_unknown_environment_is_refused():
    with pytest.raises(ValidationError):
        make({}, app_environment="staging")
    config = Settings.model_construct(**{**Settings(_env_file=None).model_dump(), "app_environment": "staging"})
    assert any("APP_ENVIRONMENT" in problem for problem in configuration_problems(config))


def test_undeclared_keeps_the_older_behaviour():
    legacy = {key: value for key, value in PILOT.items() if key != "app_environment"}
    config = make(legacy, ticketflow_env="production", monthly_ticket_reads_per_organization=0,
                  database_url="postgresql://quarvera@db.internal:5432/quarvera")
    assert configuration_problems(config) == []
    assert environment(config) == "production" and config.app_environment is None
    assert environment(make({}, ticketflow_env="development")) == "development"


# ------------------------------------------------------------------ startup and status

def test_startup_refuses_a_demo_that_could_read_with_ai(client, monkeypatch):
    monkeypatch.setattr(settings, "app_environment", "demo")
    monkeypatch.setattr(settings, "ai_reading_enabled", True)
    with pytest.raises(RuntimeError, match="AI_READING_ENABLED=false"):
        with TestClient(app):
            pass


def test_startup_refuses_a_pilot_pointed_at_the_demo_database(client, monkeypatch):
    for name, value in {**PILOT, "ticketflow_env": "production",
                        "database_url": "postgresql://quarvera:Fictional-Pilot-Pass-1@db.internal:5432/quarvera_demo"}.items():
        monkeypatch.setattr(settings, name, value)
    with pytest.raises(RuntimeError, match="demo"):
        with TestClient(app):
            pass


def test_status_names_the_environment(client, monkeypatch):
    body = client.get("/api/status").json()
    assert body["environment"] == "development" and body["environment_declared"] is False
    monkeypatch.setattr(settings, "app_environment", "demo")
    body = client.get("/api/status").json()
    assert body["environment"] == "demo" and body["environment_declared"] is True
    assert main_module.status()["environment"] == "demo"


# ------------------------------------------------------------------ the documented variables

def _env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _documented(monkeypatch, *names: str, fill: bool = True) -> Settings:
    """The example files, as a founder would paste them, with each <placeholder> filled in."""
    import re

    for name in names:
        for key, value in _env_file(DOCS / f"{name}.env.example").items():
            monkeypatch.setenv(key, re.sub(r"<([^>]*)>", r"\1", value) if fill else value)
    return Settings(_env_file=None)


@pytest.fixture()
def clean_env(monkeypatch):
    import os

    variables = {name.upper() for name in Settings.model_fields}
    for key in list(os.environ):
        if key.upper() in variables:
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_the_documented_pilot_variables_pass(clean_env):
    config = _documented(clean_env, "shared", "api")
    assert configuration_problems(config) == [] and environment(config) == "pilot"


def test_the_documented_demo_variables_pass(clean_env):
    config = _documented(clean_env, "demo", "api")
    assert configuration_problems(config) == [] and environment(config) == "demo"
    assert config.ai_reading_enabled is False and config.gemini_api_key is None


def test_the_unfilled_examples_are_refused(clean_env):
    found = configuration_problems(_documented(clean_env, "shared", "api", fill=False))
    assert any("real password" in problem for problem in found), found


# ------------------------------------------------------------------ seeding the hosted demo

DEMO_URL = "postgresql://db-user:db-password@postgres-private-host:5432/quarvera_demo"


@pytest.fixture()
def seeder(clean_env, tmp_path):
    """A shell for the hosted seeder: no backend/.env, nothing it writes leaks into other
    tests, and no database is ever opened or seeded unless a test says so."""
    clean_env.chdir(tmp_path)
    for name in ("DATABASE_URL", "AI_READING_ENABLED", "RATE_LIMIT_UPLOADS_PER_MINUTE", "RATE_LIMIT_EXPORTS_PER_MINUTE",
                 "RATE_LIMIT_REPORTS_PER_MINUTE"):
        clean_env.setenv(name, "unset-by-test")  # recorded, so teardown restores whatever was there
        clean_env.delenv(name)
    clean_env.setattr(seed_demo, "EXPECTED_PATH", tmp_path / "expected.json")
    clean_env.setattr(seed_demo, "HOSTED_EXPECTED_PATH", tmp_path / "hosted-expected.json")

    def never(*_args, **_kwargs):
        raise AssertionError("the seeder went past its refusal")

    clean_env.setattr(seed_demo, "seed", never)
    clean_env.setattr(seed_demo, "database_has_users", never)
    return clean_env


def _demo_shell(monkeypatch, **overrides):
    _documented(monkeypatch, "demo", "api")
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


@pytest.mark.parametrize("argv, shell, expected", [
    (["--hosted", "--database-url", DEMO_URL], {"APP_ENVIRONMENT": None}, "APP_ENVIRONMENT=demo"),
    (["--hosted", "--database-url", DEMO_URL], {"APP_ENVIRONMENT": "pilot"}, "APP_ENVIRONMENT=demo"),
    (["--hosted"], {"DATABASE_URL": None}, "PostgreSQL URL"),
    (["--hosted", "--database-url", "sqlite:///quarvera-demo.db"], {"DATABASE_URL": None}, "PostgreSQL URL"),
    (["--hosted", "--database-url", "postgresql://u:p@host:5432/railway"], {"DATABASE_URL": None}, 'must contain "demo"'),
    (["--hosted", "--database-url", "postgresql://u:p@host:5432/quarvera_pilot"], {"DATABASE_URL": None}, 'must contain "demo"'),
    (["--hosted", "--database-url", DEMO_URL], {"DATABASE_URL": "postgresql://u:p@host:5432/quarvera_pilot"}, "different database"),
    (["--hosted", "--database-url", DEMO_URL], {"STORAGE_BACKEND": "local"}, "STORAGE_BACKEND=s3"),
    (["--hosted", "--database-url", DEMO_URL], {"S3_BUCKET": "quarvera-production-documents"}, "S3_BUCKET"),
])
def test_the_hosted_seeder_refuses_before_opening_anything(seeder, capsys, argv, shell, expected):
    _demo_shell(seeder, **shell)
    assert seed_demo.main(argv) == 2
    assert expected in capsys.readouterr().err


def test_the_hosted_seeder_refuses_what_the_demo_would_refuse_at_startup(seeder, capsys):
    _demo_shell(seeder, GEMINI_API_KEY="fictional-not-a-key")
    assert seed_demo.main(["--hosted", "--database-url", DEMO_URL]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_the_hosted_seeder_never_resets_or_takes_a_url_locally(seeder, capsys):
    _demo_shell(seeder)
    assert seed_demo.main(["--hosted", "--reset", "--database-url", DEMO_URL]) == 2
    assert seed_demo.main(["--database-url", DEMO_URL]) == 2
    assert "--reset" in capsys.readouterr().err


def test_the_hosted_seeder_fills_only_an_empty_database(seeder, capsys):
    _demo_shell(seeder)
    seeder.setattr(seed_demo, "database_has_users", lambda url: True)
    assert seed_demo.main(["--hosted", "--database-url", DEMO_URL]) == 1
    assert "already has people" in capsys.readouterr().err


def test_the_hosted_seeder_seeds_the_named_database_once_checks_pass(seeder, capsys):
    import os

    _demo_shell(seeder)
    seen = {}

    def has_users(url):
        seen["checked"] = url
        return False

    def seed(hosted):
        seen["seeded"] = (hosted, os.environ["DATABASE_URL"], os.environ["AI_READING_ENABLED"])
        return "fictional-one-time-pw"

    seeder.setattr(seed_demo, "database_has_users", has_users)
    seeder.setattr(seed_demo, "seed", seed)
    assert seed_demo.main(["--hosted", "--database-url", DEMO_URL]) == 0
    assert seen["seeded"] == (True, DEMO_URL, "false") and seen["checked"].endswith("/quarvera_demo")
    assert "fictional-one-time-pw" in capsys.readouterr().out
    assert seed_demo.EXPECTED_PATH == seed_demo.HOSTED_EXPECTED_PATH


def test_database_has_users(tmp_path):
    import sqlite3

    path = tmp_path / "quarvera-demo.db"
    assert seed_demo.database_has_users(f"sqlite:///{path}") is False
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE users (id INTEGER)")
    assert seed_demo.database_has_users(f"sqlite:///{path}") is False
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO users VALUES (1)")
    assert seed_demo.database_has_users(f"sqlite:///{path}") is True
