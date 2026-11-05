# Pilot readiness

Status as of 2026-11-05, on the branch stack ending at `feature/s7-interface-pass`. **Local use only, with fictional or permissioned documents.** Not ready for hosted customers. For what's next and why, see the [engineering review](2026-11-product-engineering-review.md).

Green: built and tested here. Amber: works with limits. Red: missing or blocked.

## Acceptance

| Capability | State | Evidence |
|---|---|---|
| Every scanned slot on record; faint slots need a person | Green | `test_scan_manifest`, real-stack browser run |
| Paper count reconciled against records | Green | `test_scan_manifest` |
| Failed files and open slots keep invoice comparisons incomplete | Green | `test_scan_manifest` |
| Non-destructive 90° rotation; duplicate guard preserved | Green | `test_scan_manifest` |
| A ticket is never read as empty, across 12 fictional scan conditions | Green on fictional images | `test_scan_conditions` |
| Faint/empty thresholds on real scanners | **Red** | Not calibrated: needs permissioned scans |
| Nobody approves values they weren't looking at | Green | `test_review_integrity`, two people on the real stack |
| Keyboard-first review, one screen, resume where you left off | Green | `review-flow.spec` |
| Time saved per ticket | **Red, blocked** | Unmeasured: needs real people. Turn on `REVIEW_TIMING_ENABLED` |
| Records at 50,000 tickets: paging, search, saved views, export columns | Green on fictional data | `test_records_at_scale`, `measure_performance.py` |
| Bulk tidying and name cleanup | Green | `test_records_at_scale` |
| Project report, on screen and in Excel | Green | `test_records_at_scale` |
| Backups, verification and restore to a new folder | Green, run by hand | `test_operations`. Nothing schedules them yet |
| System health, request logs | Green | `test_operations` |
| Getting started checklist, rate sheet import | Green | `test_operations` |
| Free-form crop adjustment, perspective correction | **Red** | Not built; only 90° rotation |
| Supplier and material aliases as suggestions | Green | `test_name_aliases` |
| Per-supplier material codes | Amber | Aliases are company-wide |
| Non-material document types | Amber | [Design only](document-types-design.md) |
| Invoice PDF to reviewed draft, beside the original | Green on 10 fictional layouts | `test_invoice_layouts`, real-stack browser run |
| Reader on real supplier invoices | **Red** | Tested only on layouts we drew ourselves |
| Per-supplier invoice templates | **Red, by decision** | Waits for real samples. Column detection covers what can be tested |
| Why a line doesn't match, adding up exactly | Green | `test_invoice_diagnosis`, 40 randomized mixes |
| Billing patterns across invoices | Green | `test_invoice_diagnosis` |
| Scanned invoices | Amber | Flagged, then hand entry |
| AI invoice reader | **Red, blocked** | Needs budget and permission to send invoices to a provider. Not written: untestable code shouldn't ship |
| Per-ticket invoice check | Green | `test_invoice_ticket_matching` |
| One active invoice link per ticket | Green on SQLite | `test_ticket_evidence`, including real threads |
| Partial allocation | **Red, by decision** | [Specification](invoice-ticket-evidence-spec.md) |
| Pilot report with drill-down, CSV | Green | `test_pilot_report` |
| Opt-in review timing | Green, off by default | `test_pilot_report` |
| Handwriting accuracy on real tickets | **Red, blocked** | Needs consented tickets and an independent answer key |
| API cost per ticket | **Red, blocked** | Needs a live evaluation run, with permission |
| PostgreSQL | **Red** | Code avoids SQLite-only features, but no test has run on it |
| Concurrent load | **Red** | All timings are single requests |
| Hosting, object storage, scheduled backups | **Red** | [`SECURITY_PRELAUNCH.md`](../../SECURITY_PRELAUNCH.md), [proposal](hosted-architecture-proposal.md) |
| Independent security review | **Red** | Not done |

## Reproduce

```bash
# Backend
cd backend && python -m pytest -q

# Frontend build and mocked browser tests (Node 22)
cd frontend && npm ci && npm run build
npm install --no-save --package-lock=false @playwright/test@1.61.0 && npx playwright install chromium
npx playwright test --config tests/playwright.config.mjs

# Whole workflow, then 3,000 records and two people. Nothing mocked, temporary database
frontend/tests/e2e/run.sh

# Endpoint/role matrix and performance on fictional data
python backend/scripts/route_matrix.py
python backend/scripts/measure_performance.py --tickets 5000
```

The real-stack run is now in CI, starting with the first pull request that includes it. Backend tests here ran on Python 3.13 and CI uses 3.12.

## Performance

See the [engineering review](2026-11-product-engineering-review.md#benchmarks) for before-and-after tables at 20,000 and 50,000 tickets, and the limits that remain. Reproduce with `python backend/scripts/measure_performance.py --tickets 50000`.

## Unsupported on purpose

- Automatic approval of any ticket, invoice or payment, and bulk approval.
- Bulk changes to tons or ticket numbers.
- Unit conversion: pounds and cubic yards are never turned into tons.
- Credits and negative invoice amounts.
- Tax, freight and whole-invoice reconciliation. Those rows are shown as unused.
- A totals-only comparison implying that individual tickets were checked.
- Dispatch, GPS, scale hardware, procurement, ERP posting.

## Upgrading an existing install

Migrations `20261104_09` through `20261105_17` are additive. They were rehearsed on a `.backup` copy of the development database, from `20261103_07` to `20261105_17` (4 tickets, 1 approved, 2 users preserved, and the original's checksum unchanged). **They have not been run on the live database: that's your step.**

1. Stop the backend and worker.
2. `cp backend/ticketflow.db backend/ticketflow-before-upgrade.db`
3. `cd backend && source .venv/bin/activate && python -m alembic upgrade head`
4. Start everything again. The backend refuses to start if step 3 was skipped.
5. Open older batches and use **Build scan check** to look for tickets the old blank test dropped.
6. Take your first backup: `python scripts/backup.py`

To undo: stop everything, and put the copy from step 2 back. Migrations don't downgrade, on purpose. [Operating TicketFlow](operations.md) has the full runbook.

## Next single most valuable task

Run real, permissioned tickets through intake. It tells you whether the faint-slot thresholds and reading accuracy hold up, and nothing else in this repo can answer that.
