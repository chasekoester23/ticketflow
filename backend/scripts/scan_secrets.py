"""Look for credentials in the files git tracks. No network, no accounts, no dependencies.

Finds the shapes real keys have: AWS access keys and secrets, Google (Gemini) API keys,
Anthropic and OpenAI keys, GitHub and Slack tokens, Stripe live keys, private keys, and
committed .env files or key files. It reports the file, the line and the kind of key,
never the value, so its output is safe to show anywhere (CI annotations included).

A line that is a deliberate fictional example can carry the marker `secret-scan: allow`
(in a comment). A whole file can be listed in ALLOWED_FILES below with a reason. Neither
should ever be used for a real key: revoke it at the provider, then remove it.

Usage (from the repository root or anywhere inside it):
    python backend/scripts/scan_secrets.py            # exit 1 if anything is found
    python backend/scripts/scan_secrets.py --github   # also print GitHub annotations
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MARKER = "secret-scan: allow"
MAX_BYTES = 2 * 1024 * 1024

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("AWS secret access key", re.compile(r"(?i)aws_?secret_?access_?key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    ("Google API key (Gemini)", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])")),
    ("Anthropic API key", re.compile(r"\bsk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?(?!ant-)[A-Za-z0-9_\-]{20,}T3BlbkFJ[A-Za-z0-9_\-]{20,}")),
    ("OpenAI project key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{40,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}")),
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----")),
]

# Files that must never be committed at all, whatever they hold.
FORBIDDEN_NAME = re.compile(r"(?:^|/)(?:\.env(?:\.(?!example$|sample$|template$)[^/]+)?|id_(?:rsa|dsa|ecdsa|ed25519)|"
                            r"[^/]+\.(?:pem|p12|pfx|key)|credentials\.json|service-account[^/]*\.json)$")

# Whole files allowed, with the reason. Keep this short.
ALLOWED_FILES: dict[str, str] = {
    "backend/scripts/scan_secrets.py": "the patterns themselves",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}" if self.line else f"{self.path}: {self.kind}"


def scan_text(path: str, text: str) -> list[Finding]:
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if MARKER in line:
            continue
        for kind, pattern in PATTERNS:
            if pattern.search(line):
                found.append(Finding(path, number, kind))
    return found


def scan_file(root: Path, path: str) -> list[Finding]:
    if path in ALLOWED_FILES:
        return []
    found = [Finding(path, 0, "a file of this kind must never be committed (.env, private key or credentials file)")] \
        if FORBIDDEN_NAME.search(path) else []
    full = root / path
    try:
        if not full.is_file() or full.is_symlink() or full.stat().st_size > MAX_BYTES:
            return found
        data = full.read_bytes()
    except OSError:
        return found
    if b"\0" in data[:8192]:
        return found  # binary: images, PDFs, fonts
    return found + scan_text(path, data.decode("utf-8", errors="replace"))


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [name for name in result.stdout.decode("utf-8", errors="replace").split("\0") if name]


def repository_root() -> Path:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).resolve().parent,
                            capture_output=True, text=True, check=True)
    return Path(result.stdout.strip())


def scan(root: Path, files: list[str] | None = None) -> list[Finding]:
    findings = []
    for path in files if files is not None else tracked_files(root):
        findings += scan_file(root, path)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--github", action="store_true", help="also print GitHub Actions annotations")
    parser.add_argument("--root", type=Path, help="repository to scan (default: this one)")
    args = parser.parse_args(argv)
    root = args.root or repository_root()
    files = tracked_files(root)
    findings = scan(root, files)
    for finding in findings:
        print(finding)
        if args.github:
            print(f"::error file={finding.path},line={max(finding.line, 1)},title=Possible secret::{finding.kind}. "
                  "If it is real, revoke it at the provider first, then remove it.")
    print(f"Secret scan: {len(files)} tracked files, {len(findings)} possible secret(s). Values are never printed.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
