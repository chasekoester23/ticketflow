# Pilot readiness

Status as of 2026-11-08, on `feature/c7-integration`. **Local use only, with fictional or permissioned documents.** Not ready for hosted customers. The overall assessment is in the [CTO report](2026-11-cto-report.md); branch state and open bugs are in [programme status](programme-status.md).

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
| Keyboard-first review, one screen, resume where you left off | Green | `review-flow.spec`, `f_keyboard.spec` |
| Typed values survive a browser crash or closed tab, and clear on sign-out | Green | `f_recovery.spec` |
| A failed save keeps what was typed; a double click can't approve twice | Green | `f_failures.spec`, `review-flow.spec` |
| A cut-off scan or invoice PDF is refused, not shown as a shorter one | Green on fictional files | `test_t_failures`, `test_integration_safeguards` |
| Time saved per ticket | **Red, blocked** | Unmeasured: needs real people. Turn on `REVIEW_TIMING_ENABLED` |
| Records at 1,000,000 tickets: paging, search, saved views, export columns | Amber on fictional data | [Benchmarks](2026-11-benchmarks.md): every request completes; some take seconds at 1,000,000 |
| Bulk tidying and name cleanup | Green | `test_records_at_scale` |
| Project report, on screen and in Excel | Green | `test_records_at_scale` |
| Backups: retention, off-device copy, completeness check, restore drill | Green | `test_b_backup_recovery`. A schedule is printed, not installed |
| Stored documents survive a restore to a new location | Green | `test_b_storage`, `test_b_backup_recovery` |
| A job that crashes the worker stops after 3 attempts | Green | `test_b_worker_reliability`; `test_t_failures` with a real `kill -9` |
| System health, request logs | Green | `test_operations` |
| Getting started checklist, rate sheet import | Green | `test_operations` |
| Free-form crop adjustment, perspective correction | **Red** | Not built; only 90° rotation |
| Supplier and material aliases as suggestions | Green | `test_name_aliases` |
| Two catalog names tickets can't tell apart ("Sand", "SAND") are refused | Green | `test_t_odd_data`, `test_integration_safeguards` |
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
| Planted-error invoices, PDF to final figures | Green on 15 fictional invoices | `test_t_invoice_pipeline` |
| Unbilled deliveries, billing gaps, discrepancy CSV for a supplier | Green on fictional data | `test_b_reconciliation`, `f_invoice_investigation.spec` |
| One active invoice link per ticket | Green on SQLite and PostgreSQL | `test_ticket_evidence`, `test_concurrency` with real threads |
| Partial allocation | **Red, by decision** | [Specification](invoice-ticket-evidence-spec.md) |
| Pilot report with drill-down, CSV | Green | `test_pilot_report` |
| Opt-in review timing | Green, off by default | `test_pilot_report` |
| Handwriting accuracy on real tickets | **Red, blocked** | Needs consented tickets and an independent answer key |
| API cost per ticket | **Red, blocked** | Needs a live evaluation run, with permission |
| PostgreSQL | Amber | Whole suite on PostgreSQL 16 in CI, and a verified copy tool. Not run on a production-sized server |
| Concurrent load | Amber | `load_test.py`: 1, 5 and 20 employees on one process, no errors, exact totals. One process only |
| Accessibility | Amber | `f_accessibility.spec`: axe finds nothing on 15 pages at 390 and 1366 px. No screen reader or real phone tested |
| Another company's records, through every route | Green, automated | `test_t_tenant_sweep`, generated from the live route list |
| Hosting, object storage, more than one API process | **Red** | [`SECURITY_PRELAUNCH.md`](../../SECURITY_PRELAUNCH.md), [proposal](hosted-architecture-proposal.md) |
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

All of these run in CI, with every Python package pinned in `backend/constraints.txt`. Backend tests here ran on Python 3.13, and CI uses 3.12. [Test strategy](test-strategy.md) says what each layer covers.

## Performance

See [benchmarks](2026-11-benchmarks.md) for before and after, up to 1,000,000 fictional tickets on SQLite and PostgreSQL, and the limits that remain. Reproduce with `python backend/scripts/measure_performance.py --tickets 100000`.

## Unsupported on purpose

- Automatic approval of any ticket, invoice or payment, and bulk approval.
- Bulk changes to tons or ticket numbers.
- Unit conversion: pounds and cubic yards are never turned into tons.
- Credits and negative invoice amounts.
- Tax, freight and whole-invoice reconciliation. Those rows are shown as unused.
- A totals-only comparison implying that individual tickets were checked.
- Dispatch, GPS, scale hardware, procurement, ERP posting.

## Upgrading an existing install

The steps, and how they were rehearsed on a copy of your database, are in [programme status](programme-status.md#upgrading-your-own-install). Migrations don't downgrade, on purpose: undo by putting back the copy you made first. [Operating TicketFlow](operations.md) has the full runbook.

## Next single most valuable task

Run real, permissioned tickets through intake. It tells you whether the faint-slot thresholds and reading accuracy hold up, and nothing else in this repo can answer that.
