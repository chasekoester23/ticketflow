# Dependency and code scanning

`.github/workflows/dependency-audit.yml` runs on every change to requirements, the
lockfile, backend code or the exceptions file, on pull requests to `main`, and weekly.

| Job | Tool | Fails the build on | Reports |
|---|---|---|---|
| `python-packages` | pip-audit 2.10.1 over `backend/constraints.txt` (every pinned package, runtime and test, PostgreSQL and backup extras) | **any** known vulnerability not excepted | every finding, as annotations |
| `browser-packages` | `npm audit` over `frontend/package-lock.json` | a **high or critical** advisory not excepted | every advisory, moderate and low included |
| `bandit` | Bandit 1.9.4 over `backend/app` and `backend/scripts` | a **high severity** finding | medium as warnings, with file and line |
| `newest-packages` | the suite on unpinned newest versions | never (an early warning) | what differs from the pins |

Python fails on any severity because pip-audit's data has no reliable severity and the
list is short enough to keep at zero. Exceptions live in `.github/audit-exceptions.json`,
each with a reason and a `review_by` date.

## Current exceptions (2026-11-10)

| Advisory | Package | Why it's accepted | Review by |
|---|---|---|---|
| GHSA-6g55-p6wh-862q | postcss 8.4.31 inside next 15 | Build time only, on our own CSS; never touches anything a visitor sends. Fixed in Next 16, a major upgrade done separately | 2027-01-31 |
| GHSA-r28c-9q8g-f849 | same | same | 2027-01-31 |

## Fixed in this round

Pillow 12.3.0 had 39 published advisories, all fixed in 13.0.0. Uploaded scans are
untrusted images decoded by Pillow, so this was a real exposure: the pin moved to
13.0.0 (`requirements-dev.txt` allows `>=12,<14`) and the whole backend suite passed on it.

## Bandit findings reviewed and accepted (medium and low)

- **B608 (SQL built from strings):** every case interpolates table or column names from
  fixed lists in our own code (`backup.py`, `backup_pg.py`, `document_audit.py`,
  `migrate_sqlite_to_postgres.py`, `check_health.py`, `system_health.py`); values are
  always bound parameters. Marked `# nosec B608` in new code.
- **B310 (urlopen):** the restore drill calls its own throwaway server on `127.0.0.1`;
  the alert webhook and health check accept only `http(s)` URLs, checked first.
- **B314 (XML parsing):** `ci_report_failures.py` parses the JUnit file the same CI job
  just wrote.
- **B104 (binding all interfaces):** the string `0.0.0.0` appears in the list of local
  addresses production refuses; nothing binds to it.
- **B404/B603 (subprocess):** fixed tool names and argument lists, never a shell.
- **B105/B106 (hardcoded passwords):** constant names such as `PASSWORD_CHANGE_REQUIRED`.

## Not done

CodeQL would add data-flow analysis for both Python and TypeScript and is free for
public repositories; it's worth enabling from the repository's Security settings
("default setup") rather than as another workflow. No container image scanning yet:
add Trivy (free) once the Docker image exists.
