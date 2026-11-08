# Test strategy

What each layer of TicketFlow's tests covers, how to run it, and what nothing tests yet.
Every test uses fictional data. **A passing suite says nothing about reading accuracy
on real tickets or invoices, real-world load, or security certification.**

## The layers

| Layer | Where | What it proves | Databases |
|---|---|---|---|
| API and business rules | `backend/tests/test_*.py` with the `client` fixture | Each rule through the real routes: approval, pricing, invoices, roles, exports | SQLite in memory; PostgreSQL in CI |
| Forced races | `test_concurrency.py`, the `race` fixture | Real threads on real connections, made to meet at the dangerous moment | SQLite file; PostgreSQL in CI |
| Planted invoice mistakes, PDF to figures | `test_t_invoice_pipeline.py`, `fixtures_invoices.py` | 15 fictional invoices in 10 layouts, each with an answer key worked out by hand: result, tons bridge and dollar effect to the cent | both |
| Failures with real processes and files | `test_t_failures.py`, `fixtures_stack.py`, `fixtures_worker.py` | kill -9 of a real worker at chosen moments, a killed server, damaged and truncated files, a locked or vanishing database (clean 503), read-only storage, a full disk, and a failure at every write and every commit of 17 actions leaving every table unchanged | both; a few are one database only, and say why |
| Odd and hostile data | `test_t_odd_data.py`, `test_adversarial.py` | Calendar edges, repeated ticket numbers, near-identical names, clashing rates, faint and turned scans, PDFs that aren't what they claim, impossible invoice lines | both |
| Every report against a model | `test_t_cross_report.py` | Ten fixed seeds of 90 random actions. The test predicts every answer, then checks dashboard, register, CSV, Excel, project and pilot reports and each invoice line show exactly its tons and cost, with no ticket counted twice | both |
| Tenant and permission sweep | `test_t_tenant_sweep.py` | Generated from the live route list: another company's ids in paths, queries, bodies and forms answer exactly like ids nobody has; no read shows the other company; each role refused exactly what it lacks. A new route fails until covered | both |
| Mocked browser | `frontend/tests/browser/*.spec.mjs` | Pages, keyboard flow and conflict handling, with the API stubbed to its contract | none |
| Real stack | `frontend/tests/e2e/run.sh` | Real API, worker and production frontend in a browser on a throwaway database: the whole workflow, then 3,000 records and two people | SQLite |
| Flake hunt | `frontend/tests/e2e/flake-hunt.sh` | The browser suite repeated with retries off, the browser's CPU slowed, and the machine busy | none |
| Load | `backend/scripts/load_test.py` | N signed-in employees on a real server, then exact totals and nothing doubled. Results in `2026-11-benchmarks.md` | SQLite locally; PostgreSQL in CI |
| Scale | `backend/scripts/measure_performance.py` | Each request's time, SQL count and memory from 1,000 to 1,000,000 tickets; `load_test.py --explain` gives PostgreSQL's plans | SQLite locally; PostgreSQL in CI |
| Dependencies | `.github/workflows/dependency-audit.yml` | Known vulnerabilities in the pinned Python and npm packages, and the suite on the newest allowed packages | none |

Application bugs found by these tests and not yet fixed are tests marked
`xfail(strict=True, reason="BUG: ...")`. They fail the run the moment the bug is fixed,
so the mark has to come off with the fix. Search for `reason="BUG` to list them.

## How to run each

From `backend/` with the project's virtual environment:

```bash
python -m pytest -q                                   # everything, SQLite
python -m pytest -q tests/test_t_failures.py          # one layer
TEST_DATABASE_URL=postgresql://user:pass@localhost/ticketflow_test python -m pytest -q   # PostgreSQL; the name must contain "test"
python scripts/load_test.py --users 1 5 20 --seconds 30 --tickets 20000
python scripts/measure_performance.py --tickets 1000 10000
```

CI installs with `-c constraints.txt`, so every run gets the same package versions.
Without it you get whatever is newest, which is what the dependency audit's second job
checks on purpose.

From `frontend/`, with Node 22 and Playwright 1.61.0 installed:

```bash
npx playwright test --config tests/playwright.config.mjs              # mocked browser
FLAKE_REPEAT=5 FLAKE_THROTTLE=4 FLAKE_LOAD=4 FLAKE_NICE=10 CI=1 tests/e2e/flake-hunt.sh
tests/e2e/run.sh                                                      # real stack
```

`flake-hunt.sh` works on a generated copy of the specs, prints each test that failed
even once, and stops its busy processes however it ends. A flaky test is fixed at its
cause, in the test or the app, never by a longer timeout.

In CI, each workflow names its failures in public annotations, so a red run can be
diagnosed without access to its logs. `Load and scale on PostgreSQL` runs when the
load or benchmark scripts change, and can be started by hand with other sizes.

## What it can't tell you

- **Reading accuracy.** Nothing measures it on real documents. The invoice reader gets
  186 of 190 fields right on our own fictional layouts, drawn by people who could see
  its code; that figure says nothing about real supplier invoices. Scanned tickets are
  read by an AI model that no test calls.
- **Real scanners and real paper.** The faint-slot thresholds have never been calibrated
  on real scanner output.
- **Production load and hardware.** Load figures come from a laptop shared with other
  work, or a GitHub runner, on fictional data.
- **Hosting.** No test covers more than one server or worker process, a load balancer,
  network storage, a managed database's failover, TLS, or backups taken while people
  work. The worker lock is a local file lock.
- **Security certification.** The sweeps prove the rules the code states. They are not a
  penetration test, and nothing tests the frontend's handling of a hostile API.
- **A failure partway through a download.** A CSV export that loses its database after
  the first rows have been sent ends as a broken download; no test covers what a browser
  shows then.
- **Time zones and clocks.** Approval times are UTC; nothing tests a server or browser in
  another zone, or a clock that jumps.
- **Accessibility and real devices.** Browser tests check keyboard flow and phone width
  in Chromium only.
- **Upgrades of the OCR provider SDK.** google-genai is pinned at 2.34.0; 3.0 is untested
  against the real provider, and can't be tested without a paid call.

Features arriving in other branches need their own tests after the merge: the worker's
retry cap, the storage layer, backup, restore and copy tools, first-run setup,
`/health/ready`, unbilled reports, the discrepancy CSV, labelled saved views, and the
browser's draft recovery.
