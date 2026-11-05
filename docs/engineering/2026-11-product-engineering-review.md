# Product and engineering review, November 2026

What a review of the whole product found, what changed, and what still stands between Ticketflow and selling it. Measured on fictional data only.

## Bottom line

The core is sound: human approval, locked prices, company isolation and the audit trail held up under everything thrown at them here, including random action sequences and a generated authorization sweep. The weaknesses were in scale, in how long the work takes, and in being able to run it. Those are much better now.

**What can't be answered from this repo:** how accurately real tickets and invoices are read, whether review is actually faster for a real project engineer, and whether a contractor would pay for it. Each needs real documents and real people. No code here substitutes for that, and no result in this document is evidence of any of them.

No comparison with Field Materials, Stratum, FortressTicket or Command Alkon was made in this work, so nothing here claims parity with them.

## Findings that changed the work

| # | Finding | Severity | Now |
|---|---|---|---|
| 1 | A manager with a stale page could approve 22 tons while looking at 20 | **Integrity** | Refused. The latest values are shown first |
| 2 | A faint ticket on low-contrast paper was classified empty: the one failure that can silently lose a delivery. Found by the new adversarial suite | **Integrity** | Marks are also compared with the slot's own background |
| 3 | Every edit and approval loaded the company's whole ticket history | Scale | Reads only possible matches: 0.44 s to 0.011 s at 20,000 |
| 4 | The records page sent and drew every row | Scale | Paged, with totals computed separately |
| 5 | Each invoice line re-read its project: 228 queries for 20 lines | Scale | 14 queries in total |
| 6 | No way back to unfinished work without a saved link | Workflow | Uploads list with each batch's next step |
| 7 | Review meant scrolling past the upload form, on every ticket | Workflow | Scan, fields and Accept fit one screen |
| 8 | A stopped worker looked like nothing happening | Operations | Says so on the upload, and on System health |
| 9 | No backup tool, on a product whose value is its approved records | Operations | Backup, verify, and restore into an empty folder only |
| 10 | A difference on an invoice meant working out why by hand | Value | Figures that add up exactly to the bill, naming the tickets |
| 11 | The invoice reader handled one fixed layout | Value | Reads columns from a table's header row |
| 12 | The invoice Result column was clipped at laptop width, and three pages overflowed on phones | Interface | Fixed, with real-stack tests holding both |

## Benchmarks

`python scripts/measure_performance.py --tickets N` from `backend/`. It always uses a new temporary database. Fictional data, SQLite, one Apple M2 laptop, single requests with no concurrent load and no network. Use these to compare, not as a promise.

| Request | Before, 20,000 | After, 20,000 | After, 50,000 | Queries, after |
|---|---|---|---|---|
| Edit one ticket | 0.442 s | **0.011 s** | 0.024 s | 10 |
| Approve one ticket | 0.498 s | **0.015 s** | 0.025 s | 12 |
| One batch's tickets | 0.583 s | **0.060 s** | 0.087 s | 8 |
| Invoice list, 20 itemized lines | 4.201 s, 228 queries | **0.286 s** | 0.782 s | 14 |
| Pilot report | 4.459 s, 236 queries | **0.553 s** | 1.767 s | 22 |
| Dashboard | 0.440 s | **0.118 s** | 0.363 s | 5 |
| Records register, one page | 1.984 s (all rows) | **0.073 s** | 0.523 s | 9 |
| Search with a sort | n/a | 0.060 s | 0.157 s | 9 |
| Uploads list | n/a | 0.013 s | 0.020 s | 6 |
| Project report | n/a | 0.529 s | 1.565 s | 16 |
| CSV export, all rows | n/a | 0.556 s | 1.776 s | 53, 128 |
| Excel export, all rows | not measured | 2.639 s | 6.800 s | 53, 128 |
| Process a 20-page 4-up PDF, no AI | 0.518 s | 0.507 s | 0.620 s | n/a |

2,000 identical loads, the worst case for duplicate checks: 23 s to 0.20 s for the records list. A ticket now lists its first 10 matches and says how many more there are.

Budget tests in `test_query_budgets.py` count statements and rows, so they catch a regression on any machine.

**Limits that remain:**

- `/api/records` still returns every row, 9.5 s at 50,000. Nothing in the interface calls it now. It stays for existing callers and should get a limit before hosting.
- Exports and project reports load whole result sets. They're fine at this size, and should move to background jobs for much larger companies.
- Search uses `contains` matching, which can't use an index. It's fine at 50,000; PostgreSQL full-text search would be the next step.
- All of this is single-user timing on SQLite. Nothing has been measured under concurrent load or on PostgreSQL.

## Testing

| Layer | Count | Covers |
|---|---|---|
| Backend | 603 | Including the suites below |
| Mocked browser | 73 | Interface behavior, every role |
| Real stack | 8 | Real API, worker and browser, nothing mocked, including 3,000 records and two people |

What the new adversarial suites do:

- **Planted mistakes.** 12 kinds of invoice error with an answer key. Each must get the right result and an explanation that adds up.
- **Random sequences.** 150 random actions per run, 12 runs. Dashboard, register, project report, pilot report and Excel export must all agree, and locked prices must hold.
- **Random invoices.** 40 mixes of mistakes. The bridge must land exactly on the billed figure.
- **Authorization sweep.** Generated from the live route list, so a new route can't be forgotten.
- **Hostile input.** Formula and script text, path traversal, wrong file types, unusable numbers, another company's ids.
- **Races.** Eight real threads approving one ticket on a real file database.
- **Mutation check.** 9 deliberate breaks across authorization, tenancy, bulk edits, the bridge, units, formula safety, restore and rate sheets. All were caught.

**Not tested:** PostgreSQL, real scanners, real invoices, load, and real browsers beyond Chromium. The new CI job for the real-stack run hasn't run yet, because it starts on the first pull request.

## Decisions worth checking

- **No bulk approve.** It would be the fastest way to review and the fastest way to approve a mistake. Approval stays per ticket, with fast keyboard flow instead.
- **Same-as-previous fills the form only.** It never saves, never touches ticket number or tons, and the date takes its own click.
- **Bulk changes skip approved tickets** and report them. Tons and ticket numbers can't be changed in bulk at all.
- **No per-supplier invoice templates yet.** Column detection from headers covers the layouts that could be tested. A template system designed without real invoices would likely be wrong, so it waits for samples.
- **A letterhead supplier is accepted only from the company's own catalog,** and only if exactly one name matches. Otherwise it's left blank for a person.
- **Billing patterns are worded as questions.** Equal amounts need the same delivery window, because fixed-size loads made the first version cry wolf.

## What it would take to sell this

In order. Earlier items change what the later ones should be.

1. **Real, permissioned tickets and invoices through the whole workflow.** It's the only way to know reading accuracy, calibrate the faint thresholds, and find invoice layouts the reader misses.
2. **A real project engineer doing a real batch, with `REVIEW_TIMING_ENABLED=true`.** The keyboard work is untested on the people it's for, and its time saving is unmeasured.
3. **PostgreSQL.** Run the whole suite on it and test concurrency there. Most hosted risk is here.
4. **Hosting, private document storage and scheduled backups**, per the [proposal](hosted-architecture-proposal.md). Needs your approval and budget.
5. **An independent security review** before any customer document goes on a hosted system.
6. **Customer basics not built:** self-service company creation, password reset by email, SSO for larger companies, billing, and terms and data agreements.
7. **An accounting export for a named system** once a customer says which one. CSV is the bridge until then.
