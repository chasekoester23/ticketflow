# Pilot readiness

Status as of 2026-11-05, on the branch stack ending at `feature/m6-architecture-proposal`. **Local use only, with fictional or permissioned documents.** Not ready for hosted customers.

Green: built and tested here. Amber: works with limits. Red: missing or blocked.

## Acceptance

| Capability | State | Evidence |
|---|---|---|
| Every scanned slot on record; faint slots need a person | Green | `test_scan_manifest`, real-stack browser run |
| Paper count reconciled against records | Green | `test_scan_manifest` |
| Failed files and open slots keep invoice comparisons incomplete | Green | `test_scan_manifest` |
| Non-destructive 90° rotation; duplicate guard preserved | Green | `test_scan_manifest` |
| Faint/empty thresholds on real scanners | **Red** | Not calibrated: needs permissioned scans |
| Free-form crop adjustment, perspective correction | **Red** | Not built; only 90° rotation |
| Supplier and material aliases as suggestions | Green | `test_name_aliases` |
| Per-supplier material codes | Amber | Aliases are company-wide |
| Non-material document types | Amber | [Design only](document-types-design.md) |
| Invoice PDF to reviewed draft, beside the original | Green on fictional layout | `test_invoice_documents`, real-stack browser run |
| Reader on real supplier invoices | **Red** | Tested only on our fictional layout |
| Scanned invoices | Amber | Flagged, then hand entry |
| AI invoice reader | **Red, blocked** | Needs budget and permission to send invoices to a provider. Not written: untestable code shouldn't ship |
| Per-ticket invoice check | Green | `test_invoice_ticket_matching` |
| One active invoice link per ticket | Green on SQLite | `test_ticket_evidence`, including real threads |
| Partial allocation | **Red, by decision** | [Specification](invoice-ticket-evidence-spec.md) |
| Pilot report with drill-down, CSV | Green | `test_pilot_report` |
| Opt-in review timing | Green, off by default | `test_pilot_report` |
| Handwriting accuracy on real tickets | **Red, blocked** | Needs consented tickets and an independent answer key |
| API cost per ticket | **Red, blocked** | Needs a live evaluation run, with permission |
| Hosting, PostgreSQL, object storage, backups | **Red** | [`SECURITY_PRELAUNCH.md`](../../SECURITY_PRELAUNCH.md), [proposal](hosted-architecture-proposal.md) |
| Independent security review | **Red** | Not done |

## Reproduce

```bash
# Backend
cd backend && python -m pytest -q

# Frontend build and mocked browser tests (Node 22)
cd frontend && npm ci && npm run build
npm install --no-save --package-lock=false @playwright/test@1.61.0 && npx playwright install chromium
npx playwright test --config tests/playwright.config.mjs

# Whole workflow, nothing mocked, temporary database
frontend/tests/e2e/run.sh

# Endpoint/role matrix and performance on fictional data
python backend/scripts/route_matrix.py
python backend/scripts/measure_performance.py --tickets 5000
```

The real-stack run isn't in CI yet. Backend tests here ran on Python 3.13 and CI uses 3.12.

## Performance

Fictional data, temporary SQLite, one Apple M2 laptop. Single requests, with no concurrent load and no network. Useful for comparison, not as a promise.

| Request | 1,000 tickets | 5,000 | 20,000 | SQL queries |
|---|---|---|---|---|
| Batch ticket list (500, 417, 400 tickets) | 0.05 s | 0.15 s | 0.45 s | 8 |
| Records list | 0.06 s | 0.29 s | 2.1 s | 6 |
| Dashboard | 0.02 s | 0.13 s | 0.44 s | 6–12 |
| Scan check for one batch | 0.02 s | 0.02 s | 0.02 s | 4 |
| Invoice list, 20 lines with 50 tickets each | 0.22 s | 0.82 s | 3.9 s | about 11 per line |
| Pilot report, same 20 lines | 0.19 s | 0.85 s | 4.4 s | about 11 per line |
| Process a 20-page 4-up PDF, no AI | 0.5 s | | | n/a |

**Known limits:**

- Each invoice line scans the project's tickets in Python. That's fine at pilot size, and should move to indexed queries before large customers.
- The duplicate check is quadratic when many tickets are identical loads: 2,000 tickets with the same supplier, date, material and tons took 23 s for the records list. Real data shouldn't look like this, but a supplier with fixed-weight loads could get closer.
- AI extraction time isn't included. It would dominate real processing, and it's unmeasured.

## Unsupported on purpose

- Automatic approval of any ticket, invoice or payment.
- Unit conversion: pounds and cubic yards are never turned into tons.
- Credits and negative invoice amounts.
- Tax, freight and whole-invoice reconciliation. Those rows are shown as unused.
- A totals-only comparison implying that individual tickets were checked.
- Dispatch, GPS, scale hardware, procurement, ERP posting.

## Upgrading an existing install

Migrations `20261104_09` through `20261105_14` are additive. They were rehearsed on a `.backup` copy of the development database (4 tickets, 1 approved, 2 users preserved). **They have not been run on the live database: that's your step.**

1. Stop the backend and worker.
2. `cp backend/ticketflow.db backend/ticketflow-before-pilot-work.db`
3. `cd backend && source .venv/bin/activate && python -m alembic upgrade head`
4. Start everything again. The backend refuses to start if step 3 was skipped.
5. Open older batches and use **Build scan check** to look for tickets the old blank test dropped.

To undo: stop everything, and put the copy from step 2 back. Migrations don't downgrade, on purpose.

## Next single most valuable task

Run real, permissioned tickets through intake. It tells you whether the faint-slot thresholds and reading accuracy hold up, and nothing else in this repo can answer that.
