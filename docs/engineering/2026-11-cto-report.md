# CTO report, November 2026

The result of the commercial development directive (phases 0 to 11), as of 8 November 2026, on `feature/c7-integration`. Nothing has been merged into `main`, deployed, or tried on a real customer document. Every figure below comes from fictional data on a 15-inch MacBook Air (M2, 8 cores, 8 GB) or on GitHub's 4-core runners.

**Verified** means a test, benchmark or measurement in this repository showed it, and the evidence is linked. **Assumed** means it follows from reading the code, or it's an engineering judgment that no test checked.

## Readiness

| Stage | Assessment | What stands in the way |
|---|---|---|
| **Local pilot**: one company, one machine, permissioned documents | **Ready to start, with conditions** | Merge the branches, upgrade with a backup first, install the backup schedule, and treat the first real tickets as the accuracy test they are |
| **Hosted pilot**: one friendly customer on a server you run | **Not ready**, roughly 3 to 6 weeks of focused work (assumed) | No deployment, no private object storage, rate limits and sign-in throttling per process, no monitoring or alerting, no independent security test, no data agreement with an AI provider |
| **Production SaaS** | **Not ready**, a quarter or more (assumed) | Everything above, plus row-level tenant isolation, billing, support and incident process, audited operations, and evidence of accuracy on real documents |

The local pilot is the right next step. It's the only thing that can answer the questions nothing in this repository can: how well tickets and invoices read in real life, and how much time a person saves.

## What was done

| Phase | Outcome | Evidence |
|---|---|---|
| 0. Repository intelligence | Ranked 15 risks before changing anything. A CI flake reproduced and its likeliest cause fixed. Baselines up to 1,000,000 tickets | [Risk assessment](2026-11-risk-assessment.md), c0, c6 |
| 1. Data integrity | 12 forced races with real threads; 10 failed on `main` and all pass now. The database itself refuses a second approval of one scan, and refuses changes to history and approved figures | `test_concurrency`, `test_db_guards` (c2) |
| 2. PostgreSQL foundation | Whole suite runs on PostgreSQL 16 in CI. Migrations no longer depend on the models. A verified SQLite-to-PostgreSQL copy tool: one transaction, row checksums, exact per-company totals, rollback on any difference | `postgres-tests.yml`, `test_b_migrate_sqlite_to_postgres` (c1, c4) |
| 3. Scale | Totals computed exactly in SQL; company-scoped indexes; streaming exports; memory flat as data grows | [Benchmarks](2026-11-benchmarks.md) (c3) |
| 4. Review experience | Crash recovery of typed values, plain failure messages with retry, inline validation, next-unreviewed keys, phone layout, axe-clean pages, 2,000-ticket batches without lag | `f_*.spec` (c5) |
| 5. Invoice reconciliation | Unbilled deliveries, billing-period gaps, a discrepancy CSV for suppliers, a rounding-only headline, and 15 planted-error invoices checked end to end. No payment approval, no accounting writeback | `test_b_reconciliation`, `test_t_invoice_pipeline` (c4, c5, c6) |
| 6. Operational reliability | Worker retry cap, `SKIP LOCKED` claiming, restorable storage, atomic writes, backups with retention and off-device copy, a restore drill, document integrity checks, `doctor.py`, recovery runbook. Failures simulated with real processes: `kill -9`, restarts, deleted and corrupted files, a full disk, database outages | `test_t_failures`, `test_b_*`, [operations](operations.md) (c4, c6) |
| 7. Security and tenancy | Production startup checks, API docs off, HSTS, rate limits, a common-password blocklist, idle timeout, values kept out of logs and errors, every router audited, and a generated sweep of every route for cross-company access | [Security pre-launch](../../SECURITY_PRELAUNCH.md), `test_b_security`, `test_t_tenant_sweep` |
| 8. Business product | First-run setup, role-aware empty states, export column names matching a team's own spreadsheet, user guide | `test_b_onboarding`, `f_first_run.spec`, [user guide](../user-guide.md) |
| 9. Adversarial quality | Pinned CI packages, a dependency audit, a randomized model test across every report, hostile dates and names, bad PDFs, and a load test | [Test strategy](test-strategy.md) |
| 10. Launch architecture | A hosted plan covering infrastructure, storage, tenancy, backups, monitoring, billing, cost ranges (estimates), retention and support. Nothing provisioned | [Hosted plan](hosted-architecture-proposal.md) |

## Pull requests

Eight stacked branches, `c0` to `c7`, listed with merge order and CI state in [programme status](programme-status.md). Pull request text with compare links is ready. The GitHub CLI isn't installed here, so they still need opening.

## Architecture changes

- **One schema, two databases.** SQLite stays the local default. PostgreSQL is supported, tested, and refuses to start unless at the current migration. Migrations start from a frozen schema and never import the models.
- **The database enforces the money rules.** Unique indexes stop a second approved copy of a scan, duplicate rates and duplicate invoice files. Triggers keep audit history append-only, and stop approved tons and prices changing. Version checks reject saves made from a stale screen, including a stale price.
- **Totals in SQL, exactly.** Tons and money are summed as integers (thousandths and cents), so totals are exact on both databases. `approved_cost` is stored at approval.
- **Storage behind one interface.** Every document goes through `services/storage.py`, with paths relative to `STORAGE_DIR` and atomic writes. A private object-storage driver can be added behind it.
- **The worker can't be killed by one file.** Each job gets at most 3 attempts, claimed with `SKIP LOCKED` on PostgreSQL.

## Bugs and risks found, and what happened to them

Of the 15 ranked risks: 12 fixed, 1 mitigated, and 2 partly open. Eleven of the fixes have a test that fails without them. Uploads blocking the API was fixed by making the handlers run in the thread pool, and no test checks it.

| Risk | Status |
|---|---|
| Two managers approving copies of one scan; approving tons nobody saw; locking a rate nobody saw | **Fixed**, forced-race tests, c2 |
| A second ticket with the same supplier and number approved with only a warning | **Fixed**: needs a deliberate, audited override, c2 |
| Overlapping invoice lines saved at once; duplicate rates | **Fixed**, forced races, c2 |
| Audit tables editable | **Mitigated**: triggers refuse it, but a database administrator can still remove a trigger, c2 |
| Never run on PostgreSQL | **Fixed**, c1 and c4 |
| Reports loading every row; pilot report failing beyond about 32,000 approved tickets | **Fixed**: pilot report works at 1,000,000, c3 |
| Half-cents rounding to even | **Fixed**, c2 |
| A crashing job retried forever | **Fixed**, c4 |
| Uploads blocking the API | **Fixed**, c2 |
| Typed values lost in a browser crash | **Fixed**, c5 |
| Backups by hand, no recovery test | **Partly open**: retention, off-device copy and a restore drill exist; the schedule is printed, not installed |
| Local-only assumptions | **Partly open**: storage paths fixed; rate limits and sign-in throttling are still per process |

Found along the way and fixed:
- **Silent data loss:**
  - A PDF cut off mid-download was shown as a shorter, complete one.
  - Absolute file paths meant a restored copy lost every scan image.
- **Money:**
  - "Sand" and "SAND" could carry different rates, and approval locked whichever came last.
  - A double click on Accept could approve twice.
  - A stale save threw away what the person had typed.
- **Reliability:**
  - A folder-creation race gave one of two simultaneous uploads a 500.
  - Database pool exhaustion gave a 500 instead of "busy, try again".
  - Half-written crop images were kept for good after a crash.
- **Leaks and access:**
  - Error responses and logs repeated submitted values, and in one case a server path.
  - Backup manifests were world-readable.
- **Test and performance defects:**
  - Two flaky browser tests had races in the tests themselves.
  - Package drift made session tests check the wrong cookie.
  - My own scale changes made two lookups read every ticket in the company: a project filter, and the check for copies of one scan, which made approval take seconds at 1,000,000 tickets. Both were caught by benchmarks and fixed with an index and a plan test.

Open: 7 bugs, all low or medium, none touching approved figures or tenancy. Each has a strict-xfail test, and they're listed in [programme status](programme-status.md#open-bugs).

## Test results

| Suite | Result |
|---|---|
| Backend, SQLite, CI's pinned packages | 1188 passed, 18 skipped (PostgreSQL only), 8 xfailed (the 7 open bugs) |
| Backend, PostgreSQL 16 | Green in CI on d68c536 |
| Mocked browser suite | 250 passed, 1 skipped |
| Real stack, whole workflow then 3,000 records with two people | 8 of 8 passed |
| Load: 1, 5, 20 simultaneous employees | No errors and exact totals on SQLite locally and PostgreSQL in CI. p99 at 20 employees: 2.4 s on SQLite, 1.7 s on PostgreSQL |
| Reader on our own 15 fictional invoices | 186 of 190 fields right. Says nothing about real invoices |

## Benchmarks

The original and final code ran one after the other, on the same quiet machine, from 1,000 to 1,000,000 fictional tickets.

**Memory** stays near the 150 MB of an idle process at every size. At 1,000,000 tickets the original reached 919 MB on the dashboard and 1.7 GB on a full Excel export.

**Up to 100,000 tickets,** nearly every request is as fast or faster.

| Request at 100,000 tickets | Original | Final |
|---|---:|---:|
| Dashboard | 0.68 s | 0.25 s |
| Register, first page | 0.42 s | 0.13 s |
| Pilot report | 1.27 s | 0.69 s |
| Full CSV export | 2.5 s | 1.3 s |

**At 1,000,000 tickets:**

| Request at 1,000,000 tickets | Original | Final |
|---|---:|---:|
| Pilot report | error | 38 s |
| Full CSV export | 47 s | 13 s |
| Edit or approve one ticket | about 1 s | under 10 ms |
| Views filtered to one project | 4.4 s | 7.8 s, slower |
| Project report | 8.4 s | 16.3 s, slower |
| Text search | about 10 s | about 10 s, unchanged |

Edit and approve are fast again only after an index this benchmark itself showed was missing. The project views are slower because exact totals now read every ticket in the project.

On PostgreSQL at 1,000,000 tickets, the project report takes 0.5 s and search 1.4 s.

These are single requests against fictional data, not a promise about production.

Full tables: [benchmarks](2026-11-benchmarks.md).

## Database migration status

- Migrations `20261106_18` to `20261106_22` are additive, and are rehearsed on a read-only copy of the development database: `20261103_07` to head, 4 tickets kept, and the API starts on the result.
- The PostgreSQL preflight on that copy was clean, and the original was unchanged by checksum.
- **The live database hasn't been migrated.** That's your step, with a backup first; the steps are in programme status.
- The copy into PostgreSQL is verified only on CI's disposable databases.

## Technical debt

- Rate limits and sign-in throttling live in each process, so they need a shared store before a second API process exists.
- At 1,000,000 tickets, company-wide totals and text search still scan the company's tickets. PostgreSQL needs a trigram index for search, and summary tables if dashboards must be instant.
- Workers have no leases: a stuck worker holds its job until restarted.
- Pillow is capped below 13, despite 39 advisories fixed there. Next.js needs a major upgrade to clear a build-time postcss advisory.
- Supplier and material aliases are company-wide; some customers will need per-supplier codes.
- Invoice drafts have no revision check while being edited; confirming does check.

## Blocked by real data

These can't be answered with fictional data, and should be the point of the pilot:

- Handwriting and print accuracy on real tickets.
- Faint-slot thresholds on real scanners.
- The invoice reader on real supplier layouts.
- Time saved per ticket. Opt-in timing exists: `REVIEW_TIMING_ENABLED`.
- AI cost per ticket.
- How often real data trips the new safeguards, such as cut-off PDFs and name clashes.

## What prevents a safe commercial launch

1. No evidence of accuracy on real documents.
2. No hosted environment: deployment, HTTPS, private encrypted document storage, managed PostgreSQL with point-in-time recovery, monitoring and alerting.
3. Limits and throttles that don't span processes.
4. No independent security assessment. Nothing here is a certification, and automated tests don't replace one.
5. No agreement covering sending customer documents to an AI provider.
6. No billing, terms, support process or incident response.

## Next 10 tasks

1. Review and merge `c0` to `c7` in order. This is your decision; nothing merges automatically.
2. Upgrade the local install from a backup, run `doctor.py`, install the backup schedule, and run the restore drill once.
3. Run permissioned real tickets through intake with review timing on: accuracy, faint thresholds and time per ticket.
4. Collect a handful of real supplier invoices, with permission, to measure the reader and decide on per-supplier templates.
5. Fix the 7 open bugs, starting with the two-invoice PDF.
6. Move rate limits and sign-in throttling into the database before running a second API process.
7. With your approval for any spend, stand up a staging PostgreSQL and rehearse the copy tool on a real-sized copy, with `pg_dump` backups and a restore drill.
8. Add a private, encrypted object-storage driver behind `services/storage.py`, then worker leases.
9. Raise the Pillow cap to 13, plan the Next.js upgrade, and decide on google-genai 3.0 with a test plan.
10. Commission an independent security review of the hosted configuration before any customer data goes on it.

## Assumed, not verified

- Effort estimates for the hosted pilot and production.
- That PostgreSQL plans seen at 100,000 fictional tickets hold for real data distributions.
- That one API process serves a small company's daily use. The load test suggests so; a real day hasn't tested it.
- That the cut-off PDF check won't refuse real scanner output. It lets through every complete-but-untidy file we could make, but real scanners haven't been tried.
- Cost ranges in the hosted plan, which are estimates.

Also worth knowing:
- The GitHub repository is publicly readable.
- The cause of the original intermittent CI failure was never confirmed from its logs. The likeliest one is fixed, and every failure now names itself.
