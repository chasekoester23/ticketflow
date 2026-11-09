# CTO decision report, 8 November 2026

Branch `feature/d1-audit-fixes`, from `main` at `2c01393`. Compare: https://github.com/chasekoester23/ticketflow/compare/main...feature/d1-audit-fixes

Everything below was checked against the code on `main`, not against earlier reports. No real document was used, no model was called, nothing was deployed or merged.

## 0. Correction to the existing status documents

`programme-status.md` and `2026-11-cto-report.md` say "Nothing has been merged into `main`". That is out of date. `feature/c0` to `feature/c7` are all merged (pull request #28, merge commit `2c01393`). `git branch -r --no-merged origin/main` lists only `feature/invoice-report-export-20261103` and `security/prelaunch-guardrails-20261103`. The two documents are otherwise accurate about the code. One more error: they describe the draft-line overwrite as "known but untested". It was reproducible, and it is fixed here.

## 1. Assessment of current technical quality

**The financial core is careful, and better built than most products at this stage.** Money is exact `Decimal`. Prices are locked at approval (`Ticket.applied_unit_price`, `approved_cost`). Duplicates are refused by the database, not only by the code (`uq_tickets_approved_scan`, `uq_tickets_slot`). Approvals and confirmations are atomic claims with revision checks. Every query in the routers takes the company from the signed-in user, and `test_t_tenant_sweep.py` checks every route for cross-company access. Known bugs are kept as strict-xfail tests rather than forgotten. I found no company-isolation leak and no way for an approved figure to change.

**The product is over-built relative to its evidence.** Size today: about 11,000 lines of application code, 17,000 lines of tests, 22 migrations, nine CI workflows (including load, scale and newest-packages runs), and 15 engineering documents (1,600 lines) that contradict each other on status. Meanwhile, **not one real delivery ticket or supplier invoice has gone through the system.** The load test (3,000 records, simultaneous users) and the scale benchmarks answer questions no customer has asked yet. The question that decides the company is still open: does TicketFlow beat a spreadsheet, on real paper, for one contractor?

**Verdict:** the engineering is sound enough to start real-document validation now. It is not yet ready to hold hosted customer documents or to take payment (section 6).

### Findings

| # | Severity | Finding | Evidence | Action |
|---|---|---|---|---|
| F1 | High | A PDF holding two invoices was drafted as one. Every row got the first invoice number, and ticket rows from both were merged into one quantity | `services/invoice_extraction.py`, `header.setdefault("invoice_number", …)` over the whole document; `group_ticket_rows` over all pages | **Fixed** (8bf8684) |
| F2 | Medium | An invoice upload that failed after storing its file left the file behind, with no record pointing at it. This happened on a simultaneous duplicate (at the flush) and on any database error at commit | `routers/invoice_documents.py` `upload_document`: `store.save` before `db.flush()`, and cleanup only on `IntegrityError` at commit | **Fixed** (8bf8684) |
| F3 | Medium | Saving a draft invoice line had no revision check, so one manager could silently overwrite another's unconfirmed corrections | `edit_line` took no `If-Match`, and the frontend `saveDraftLine` sent none | **Fixed** (8bf8684): revision required (428 / 409) plus a per-line lock. The forced race test showed the revision check alone isn't enough |
| F4 | Medium | A ticket dated 1900-01-01 or 0001-01-01 was approved without a warning | `services/validation.py` `compute_flags` | **Fixed** (5545cd4): dates before 2000 are held for a person to check |
| F5 | **High, commercial** | Rates are one current price per company, supplier and material. There are no project-specific quotes and no effective dates. A ticket approved after a rate change locks *today's* rate, not the rate on its delivery date. Two projects with different quotes from one supplier can't both be priced right | `models.py` `SupplierRate`, unique on `(organization_id, supplier_id, material_id)`; `services/pricing.py` `rate_table` | Not changed: the design needs a founder decision and real quotes (D4). Until then: approve promptly after rate changes, and use the per-ticket override. Invoice comparison will show the difference |
| F6 | **High, workflow** | Intake is "one ticket per page" or a fixed 2×2 grid of equal quadrants with a 1% margin. Nothing detects where a ticket actually is. A ticket placed off-quadrant, or a long ticket, is cut | `services/splitter.py` `grid_boxes`, `split_page`; `routers/api.py` accepts only `single` and `grid_2x2` | Measure first: the new evaluation reports crop completeness from labels. Build crop adjustment only if real scans need it |
| F7 | Medium | Quantities are tons only. Invoice lines in other units are blocked from confirmation, never converted (correct). Tickets have only `tonnage` | `Ticket.tonnage`; `invoice_documents._blockers` | Keep the rule. Whether this blocks the first customer depends on what they buy (D5) |
| F8 | Medium | The production extractor is `GeminiBenchmarkExtractor`, with a preview model as the default (`gemini-3.1-pro-preview`). Preview models change and retire without notice | `config.py`, `services/extraction.py` `get_extractor` | Sprint 1: pin a generally available model, and record provider and model on each extraction |
| F9 | Medium | Model calls, tokens and cost aren't recorded anywhere. Cost per ticket can only come from the provider's bill | No such columns in `models.py`; `ProcessingJob.attempts` counts jobs, not calls | Sprint 1: record calls and tokens per extraction attempt (additive migration) |
| F10 | Low | Confirming a draft line accepts a request with no `If-Match`. The UI always sends one | `confirm_line` | Sprint 1: require it, as editing now does |
| F11 | Low | Three open bugs remain, all of which fail safe (section 4) | strict-xfail tests | Fix when touched |
| F12 | Process | Fifteen engineering documents with conflicting status statements; the status pages went stale within a day | `docs/engineering/` | Keep one status page and this report. Archive the rest |
| F13 | Complexity | Load tests, scale benchmarks, PostgreSQL copy tool, flaky-test hunting on a slowed runner, and a newest-packages job: all built before the first customer | `.github/workflows`, `scripts/load_test.py`, `measure_performance.py` | Keep them running, but stop investing until a customer needs it |

## 2. The five biggest risks to commercial success

1. **No evidence that it saves time.** Review time per ticket, correction rate and missed tickets have never been measured on real paper. If a person must retype half the fields, a spreadsheet wins. *This is the risk that matters most, and only real documents can retire it.*
2. **Pricing that doesn't match how contractors buy** (F5). Civil suppliers quote per project, and asphalt and fuel adjust monthly. If rates are wrong, every cost figure and every invoice comparison is wrong too, and trust is lost on the first invoice.
3. **Intake that assumes a tidy scanner** (F6, plus no HEIC phone photos). Field staff photograph tickets on phones, and tickets come in several sizes. Every cut-off crop is a ticket someone has to find by hand.
4. **Building for scale instead of learning.** Engineering effort so far has gone mostly into scale, hardening and documentation. Each further week of that delays the only test that decides the company.
5. **Hosted-launch cost and liability before revenue.** Customer documents carry supplier pricing. A hosted service needs private storage, encrypted backups, a security review ($5k–25k by the existing estimate) and AI-vendor terms. Do that work too early and money runs out; skip it and one incident ends the company.

## 3. What was fixed

All on `feature/d1-audit-fixes`. Every fix has a regression test, and I ran each test against the unfixed code to confirm it failed there.

| Commit | Change | Tests |
|---|---|---|
| [8bf8684](https://github.com/chasekoester23/ticketflow/commit/8bf8684) | Multi-invoice PDFs are split at each new invoice number: each part gets its own number, supplier, period and tickets, and the document message names the invoices found. Uploads delete their stored file on *any* failure before commit. Draft-line saves need the current revision, and edit and confirm of a line take turns. The documents page sends the revision and reloads on conflict | 3 strict-xfails removed. New: two-invoice split, same number on continuation pages stays one invoice, stale and missing revision via the API, forced two-manager race. All 7 failed on the old code. The race also failed with the revision check but without the lock |
| [6ce4ce1](https://github.com/chasekoester23/ticketflow/commit/6ce4ce1) | `scripts/evaluate_workflow.py` and `services/workflow_evaluation.py`: whole-workflow evaluation against independent labels (section 5) | 5 new tests: the metrics on a synthetic batch with known answers, refusal of labels not taken independently, refusal of another company's batch, and the database left byte-for-byte unchanged with writes refused |
| [5545cd4](https://github.com/chasekoester23/ticketflow/commit/5545cd4) | Delivery dates before 2000 need a person's check before approval | 1 strict-xfail removed (it failed before the fix) |

Not changed: no validation, lock, permission or financial rule was loosened. No migration was added.

## 4. Test results and open bugs

| Suite | Where | Result |
|---|---|---|
| Backend, SQLite, whole suite at `5545cd4` | Local, CI's pinned packages | **1204 passed, 18 skipped, 3 xfailed**. The 18 skipped are PostgreSQL-only and run in CI. The 3 xfailed are the open bugs below. Before this branch: 1188 passed, 18 skipped, 8 xfailed |
| Backend on SQLite, backend on PostgreSQL 16, real-stack browser workflow | GitHub Actions, all three commits | Passed on `8bf8684`, `6ce4ce1` and `5545cd4`. The forced draft-line race test ran on PostgreSQL too, which uses the advisory-lock path |
| Mocked browser suite (`review-browser`) and production build | GitHub Actions, `8bf8684`, the only commit touching the frontend | Passed |
| Load, scale, dependency audit, newest packages | Not triggered by these paths; not run | Unchanged code paths |

Every number above comes from fictional data and is an engineering check only.

**Open bugs (still strict-xfail, all fail safe):**

| Severity | Test | What happens |
|---|---|---|
| Low | `test_t_failures::test_rotating_a_damaged_crop_gets_a_clear_answer` | Rotating a crop that won't decode returns 500 instead of 409 |
| Low | `test_t_odd_data::test_an_ancient_date_is_kept_exactly_in_every_export[1899-12-31]` | 1899-12-31 is written to Excel as day zero. Such a date can no longer be approved without a person's check (5545cd4) |
| Low | `test_t_invoice_pipeline::test_adjacent_unit_and_price_columns_stay_separate` | Adjacent "Unit" and "Price" headers merge, leaving the unit blank and blocking confirmation until a person fills it in |

## 5. Evaluation, and what real documents must teach us

**Tooling.** The existing `scripts/evaluate_extraction.py` scores the model on labelled crops, and it stays. The new `scripts/evaluate_workflow.py` scores what the contractor actually gets. A real batch runs through TicketFlow as normal (upload, review, approve, invoice). A person labels every position on every page from the **paper**. The tool then reports, against a read-only copy of the database:

- tickets detected and missed, and how many crops held the whole ticket;
- **extraction accuracy** per field (the model's untouched output) and **final accuracy** per field (what people approved), kept separate;
- critical errors in ticket number and tonnage, counted as *wrong* (not merely blank), before and after review;
- how many approved tickets people corrected, and whether each correction fixed or broke the field;
- review time (opt-in timings), and upload-to-last-approval time per batch;
- processing jobs, retries and failures; cost per approved ticket, taken from the bill;
- invoice lines where TicketFlow and a person agree, where TicketFlow missed a discrepancy, where only TicketFlow flagged one, and where it couldn't decide.

The tool refuses labels that don't state they were taken independently. Every report lists its limitations, including a sample-size warning below 200 tickets, and a synthetic run is stamped as an engineering check. Format and steps: `backend/EVALUATION.md`, "Whole-workflow evaluation".

**Still missing:** a baseline. Time someone entering the *same* batch into the contractor's current spreadsheet. Without that, "faster" has nothing to be compared against.

**What real documents must tell us** (one contractor, at least 200 tickets, at least 3 suppliers, at least 2 invoices):

1. The share of crops that are complete on their real scanner or phone. This decides whether F6 needs building.
2. How many confidently wrong ticket numbers and tonnages the model produces. The target for *approved* records is zero.
3. Median review seconds per ticket against the spreadsheet baseline. This is the value proposition.
4. Whether their supplier quotes are per project or dated. This decides F5.
5. Which units they buy in, and whether invoices are text PDFs or scans (scans are manual entry today).
6. Whether invoice reconciliation finds anything their current process misses. This is the money story for the sale.

**Workflow limitations, classified:**

| Limitation | Class | Why |
|---|---|---|
| Fixed 2×2 grid, no ticket detection (F6) | **A, if real crop completeness is low** | Measure first; "single" layout is a slow but safe fallback |
| Company-wide current rates only (F5) | **A (credible, unconfirmed)** | Most civil jobs have project quotes; confirm with the first contractor's paperwork |
| Unreadable handwriting and uncertain tonnage | Handled | Flags block approval, and tonnage must be confirmed |
| Inconsistent supplier and material names | Handled | Aliases, plus refusal of catalog names that only differ in case |
| Initial project and rate setup | Mostly handled | First-run setup and rate-sheet import exist; untested with a real rate sheet |
| Scanned invoices | B | Manual entry beside the original works; text PDFs are the common case for larger suppliers |
| Supplier-specific invoice layouts | B | The text reader handles common tables. Each new layout is a measurement, not a rewrite |
| Units other than tons | **B for aggregates and asphalt, A if concrete (CY) or loads matter** | D5 |
| HEIC phone photos | B | JPEG and PNG work; phones can export JPEG |
| Effort of human verification | **A: unmeasured** | The new evaluation measures it |
| Multi-server scale, SSO, RLS | C | Not needed for the first contractors |

## 6. What the first customer needs, and the path to production

**Smallest sound hosted architecture:** one application server (or one PaaS service) running the API and one worker as two processes from one image. Managed PostgreSQL with point-in-time recovery, no standby yet. One private object-storage bucket with server-side encryption and versioning. Managed TLS. Nightly encrypted backups to a second account, with a restore drill. Error alerting plus an uptime check on `/health/ready`. No microservices, queue service, Kubernetes, connection pooler or second region. The existing proposal's "two API servers and a standby" can wait until there are dozens of customers.

| When | Required |
|---|---|
| **Before hosted customer documents** | Object-storage driver beside `services/storage.py` (none exists). PostgreSQL in `TICKETFLOW_ENV=production`. TLS and secure cookies. Encrypted off-server backups with a passed restore drill. Error alerting. AI provider with no-retention terms. Written permission from the document owner. A short external security review of the real deployment |
| **Before recurring payments** | Hosted checkout and customer portal from a payment provider (no custom billing code). Terms and privacy policy. Email invitations and password reset. MFA for owners. A data export and deletion path |
| **Before dozens of customers** | Shared rate limits and login throttling across processes. More than one worker (claiming already uses `SKIP LOCKED`). Per-company AI spend caps. Row-level security as defense in depth. Support tooling |
| **Not until much later** | Standby database and multiple regions, SSO, PgBouncer, a separate document origin per customer, microservices |

**For the first customer:** the first validation should run locally and supervised: the founder's machine or the contractor's, with the contractor's written permission. No hosting is needed for that, so none of the hosted work blocks learning.

## 7. The next three sprints

**Sprint 1 (about 1 week): ready to measure real documents**
- Record provider, model, call count and token counts for each extraction attempt (additive migration after `20261106_22`). *Accepted when* the workflow report shows measured calls per approved ticket and a retried ticket shows two calls.
- Pin a generally available extraction model. *Accepted when* the default isn't a preview model, and the model name is stored on each ticket's extraction.
- Require `If-Match` on draft-line confirm. *Accepted when* a confirm without it gets 428, and a test proves it.
- Rehearse the full protocol on 50 handwritten practice tickets (`make_practice_tickets.py`): scan, review, invoice, `evaluate_workflow`. *Accepted when* a stamped synthetic report exists and every step's instructions were followed as written.
- Archive the stale status documents. *Accepted when* exactly one status page exists.

**Sprint 2 (1–2 weeks, needs D1 and D2): the first real contractor batch**
- At least 200 real tickets and 2 or more invoices from one contractor, labelled from paper, with 10% labelled by a second person. A timed spreadsheet baseline on the same batch.
- *Accepted when* the workflow report exists with sample size and limitations; there are zero wrong ticket numbers or tonnages in *approved* records (any found is a release-blocking bug); and the review-time comparison is written up, whatever it shows.
- Fix only what the data shows. Likely candidates: crop adjustment (F6), if fewer than about 98% of crops are complete, and project or dated rates (F5), if their quotes require them.

**Sprint 3 (about 2 weeks, only if Sprint 2 shows value): minimum hosted pilot**
- Object-storage driver, production PostgreSQL, encrypted backups with a restore drill, alerting, and an external review booked.
- *Accepted when* a restore from backup to an empty environment passes `doctor.py` and `check_documents.py`, documents can't be fetched without a session, and the review's findings are triaged.

## 8. Work to intentionally NOT do

- More scale, load or performance work: there is no customer volume yet.
- New intake layouts, ticket detection or unit conversion before real scans show the need. Never convert units automatically.
- AI-provider comparisons or prompt tuning before a real labelled set exists.
- Multi-region, standby databases, SSO, RLS or microservices.
- Billing code, marketing pages, redesigns or new report types.
- Anything that approves automatically, even "obvious matches".
- More engineering documents. Replace them with one status page and the evaluation report.

## 9. Founder decisions and permissions needed

- **D1. Real documents.** Which contractor, written permission from them, and where the files will live (local only).
- **D2. Paid extraction.** Authorize model calls for the pilot batch, with a spending cap, and choose the provider after reading its data-retention terms.
- **D3. The time baseline.** Agree that someone will time spreadsheet entry of the same batch.
- **D4. Rates.** After seeing the contractor's quotes: add project-specific rates, dated rates, or both (F5).
- **D5. Target customer.** Aggregates and asphalt in tons first, or concrete and loads as well (F7).
- Still open from before: keep the future-date refusal (I recommend keeping it); raise the Pillow cap to 13 (I recommend it); whether the repository should stay public. It currently publishes the engineering documents, this one included.

## 10. Go or no-go

**GO for real customer validation, locally and supervised, as soon as D1 to D3 are answered.** The financial safeguards hold, the known invoice-intake bugs are fixed, and the tooling to measure value now exists. More engineering before real documents would lower, not raise, the chance of success.

**NO-GO for hosting customer documents or taking payment** until the "before hosted documents" list in section 6 is done. **No claim** of accuracy, time saved or reconciliation value should be made to anyone until Sprint 2's report exists. Nothing measured so far is real-world evidence.
