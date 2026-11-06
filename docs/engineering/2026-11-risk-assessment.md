# Engineering risk assessment, November 2026

Written before this round's changes, at `main` commit `3ed0018`, from reading the code, running forced-race tests, and benchmarking up to 1,000,000 fictional tickets. "Proven" means a test or benchmark demonstrated it. "Reading" means it follows from the code, but nothing here triggered it.

Effort: S under a day, M a few days, L a week or more. Dependency: what has to come first.

| # | Risk | Severity | Customer impact | Evidence | Effort | Depends on |
|---|---|---|---|---|---|---|
| 1 | Two managers can both approve copies of one scan | Critical | One delivery counted and costed twice | Proven: forced race | S | none |
| 2 | A save and an approval at the same moment can approve tons nobody saw | Critical | Approved quantity no person checked | Proven: forced race | S | none |
| 3 | A manager can lock a rate they never saw, if the rate changed after the page loaded | Critical | Wrong locked price on an approved ticket | Proven: test | S | none |
| 4 | A second ticket with the same supplier and number can be approved with only a warning | High | Double counted delivery, one click away | Proven: test | S | none |
| 5 | Overlapping invoice lines can both be saved when entered at once | High | One delivery appears paid for by two invoices | Proven: forced race | S | none |
| 6 | Simultaneous rate saves create two rates for one supplier and material, and which one locks is arbitrary | High | Wrong price, with no error | Proven: forced race | S | none |
| 7 | Audit tables can be changed by anything with database access, and nothing in the database stops a locked figure changing | High | History isn't evidence if it can be edited | Reading | S | none |
| 8 | Never run on PostgreSQL. Migrations depended on today's models | High | Blocks any hosted customer | Reading | L | none |
| 9 | Reports load every row into memory. Pilot report fails outright beyond about 45,000 approved tickets | High | Slow pages, then errors, on the customers worth having | Proven: benchmark | M | 8 |
| 10 | Half-cents round to even, while suppliers round up | Medium | False one-cent differences on invoices | Proven: arithmetic | S | none |
| 11 | A job that crashes the worker is retried forever | Medium | One bad PDF stops all processing | Reading | S | none |
| 12 | Upload handlers block the whole API while parsing | Medium | One large upload freezes everyone | Reading | S | none |
| 13 | A person's typed values are lost if the browser crashes | Medium | Rework after a long day | Reading | S | none |
| 14 | Backups run by hand only, with no test of a full recovery into a separate environment | High | The business's records depend on someone remembering | Reading | M | none |
| 15 | Local-only assumptions: file paths in the database, per-process login throttling, single worker lock | High | Blocks hosting | Reading | L | 8 |

Also worth deciding: **the GitHub repository is publicly readable.** The Actions API answered without signing in. If that isn't intended, it's a free setting to change, and the source and engineering documents are currently visible to anyone, including competitors.
