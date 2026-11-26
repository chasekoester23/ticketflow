"""The secret scanner finds key-shaped values and never prints them.

Every fake key here is assembled at run time, so this file holds nothing a scanner
(this one or any other) would mistake for a real credential.
"""
import subprocess

import pytest

from scripts import scan_secrets

A, D = "A" * 40, "7" * 40


def fake(*parts: str) -> str:
    return "".join(parts)


FAKES = {
    "AWS access key ID": fake("AKIA", "Z" * 16),
    "AWS secret access key": fake("aws_secret_access_key = ", "Ab1/" * 10),
    "Google API key (Gemini)": fake("AI", "za", "Sy", "B" * 33),
    "Anthropic API key": fake("sk-", "ant-", "api03-", A),
    "OpenAI API key": fake("sk-", "proj-", "C" * 20, "T3Blbk", "FJ", "D" * 20),
    "OpenAI project key": fake("sk-", "proj-", A, "x" * 8),
    "GitHub token": fake("gh", "p_", "E" * 36),
    "Slack token": fake("xo", "xb-", "1234567890-abcdef"),
    "Stripe live key": fake("sk", "_live_", "F" * 24),
    "Private key": fake("-----BEGIN ", "RSA ", "PRIVATE KEY-----"),
}


@pytest.mark.parametrize("kind", sorted(FAKES))
def test_each_kind_of_key_is_found_and_never_printed(kind):
    value = FAKES[kind]
    found = scan_secrets.scan_text("config/app.py", f"first line\nSETTING = '{value}'\n")
    assert kind in [f.kind for f in found]
    assert all(f.line == 2 and f.path == "config/app.py" for f in found)
    assert all(value not in str(f) and value[:12] not in str(f) for f in found)


def test_ordinary_code_and_placeholders_are_not_findings():
    text = "\n".join([
        "GEMINI_API_KEY=<gemini-api-key>",
        "S3_SECRET_ACCESS_KEY=<r2-secret-access-key>",
        "DATABASE_URL=postgresql://<db-user>:<db-password>@host:5432/db",
        "password = 'gravel-truck-at-dawn-9'",
        "sk-ant-... is what an Anthropic key starts with",
        "AIza is how a Google key starts",
        f"digest = '{D}'",
    ])
    assert scan_secrets.scan_text("docs/x.md", text) == []


def test_a_marked_fictional_example_is_allowed():
    line = f"EXAMPLE = '{FAKES['GitHub token']}'  # secret-scan: allow (fictional)"
    assert scan_secrets.scan_text("docs/x.md", line) == []


@pytest.mark.parametrize("path, forbidden", [
    (".env", True), ("backend/.env", True), ("frontend/.env.local", True), ("backend/.env.production", True),
    ("deploy/server.pem", True), ("keys/id_ed25519", True), ("backend/credentials.json", True),
    (".env.example", False), ("docs/deploy/shared.env.example", False), ("backend/app/config.py", False),
    ("docs/deploy/demo.env.example", False), ("frontend/.env.sample", False),
])
def test_committed_env_and_key_files_are_findings(path, forbidden):
    assert bool(scan_secrets.FORBIDDEN_NAME.search(path)) is forbidden


def _repository(tmp_path, files: dict[str, bytes]):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, data in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_a_repository_scan_reports_file_and_line_only(tmp_path, capsys):
    value = FAKES["Google API key (Gemini)"]
    root = _repository(tmp_path, {
        "app/settings.py": f"# settings\nKEY = '{value}'\n".encode(),
        "img/scan.png": b"\x89PNG\0\0" + value.encode(),  # binary files are skipped
        ".env": b"NAME=value\n",
        "notes.txt": b"nothing here\n",
    })
    (root / "untracked.py").write_text(f"KEY = '{value}'\n")  # git doesn't track it, so neither does the scan
    assert scan_secrets.main(["--root", str(root), "--github"]) == 1
    out = capsys.readouterr().out
    assert "app/settings.py:2: Google API key (Gemini)" in out
    assert ".env: a file of this kind must never be committed" in out
    assert "::error file=app/settings.py,line=2" in out
    assert "img/scan.png" not in out and "untracked.py" not in out
    assert value not in out and value[:10] not in out and "NAME=value" not in out


def test_a_clean_repository_passes(tmp_path, capsys):
    root = _repository(tmp_path, {"README.md": b"Fictional.\n"})
    assert scan_secrets.main(["--root", str(root)]) == 0
    assert "0 possible secret(s)" in capsys.readouterr().out


def test_this_repository_is_clean():
    root = scan_secrets.repository_root()
    findings = scan_secrets.scan(root)
    assert findings == [], [str(f) for f in findings]
