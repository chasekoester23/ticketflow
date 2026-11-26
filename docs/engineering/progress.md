# Quarvera / TicketFlow: development progress

The one status page. Each entry says what is done and tested, what isn't, and why.
"Tested" means automated tests on fictional data; nothing here has run on a real
contractor's documents yet.

## Done and tested

| Area | What works | Where |
|---|---|---|
| Material tickets | Upload (PDF, scans, phone photos, EXIF orientation), Gemini extraction with retries and usage metering, review with keyboard flow, approval that locks price, duplicate protection | `services/pipeline.py`, `upload/` |
| Material pricing | Company and project agreements, start dates, expiry dates, pricing timeline (gaps, overlaps, expiry), preview of a rate change on waiting tickets | `services/pricing.py`, `routers/pricing.py` |
| Invoice reconciliation | Per-line comparison with exact money bridge; exceptions center (owner, status, disputes, credits recorded, append-only history, stable identity, reopens when figures change after closing) | `routers/invoices.py`, `services/exceptions.py` |
| Trucking reconciliation (branch `feature/trucking-reconciliation`) | Billing increments (quarter hours, up or nearest), shift disputes with reasons, original time sheets and hauling-invoice files (stored, served per company, audited), hauling invoices checked line by line against approved shifts (hours, rate and arithmetic effects that add to each line's difference; repeats; unsupported fees; missing shifts; unapproved/unclear shifts; unbilled approved shifts), all in the exceptions center | `services/hauling_invoices.py`, `app/trucking/HaulingInvoices.tsx` |
| **Hourly trucking (PR #37)** | Haulers, trucks, hourly agreements (project / truck type / dates / minimum / standby), truck shifts from time sheets, approval that locks hours, rates and cost, split across projects and cost codes to the cent, reopen with reason, delivery linking with no per-delivery charge, Overview totals | `services/trucking.py`, `routers/trucking.py`, `app/trucking/` |
| Simplified navigation (branch `feature/ux-simplification`) | Sidebar cut to Overview, Projects, Tickets, Trucking, Invoices and Settings; former pages kept as tabs in the top bar (permission-filtered); one Upload menu (tickets, time sheet, supplier invoice, hauling invoice); pending-review counts on Tickets and Trucking; Overview leads with tons, material, trucking and total approved cost, with exceptions as one Needs-attention row; trucking split into Shifts / Hauling invoices / Haulers & agreements tabs; navy, orange and white Quarvera brand. No page, route, permission or API removed | `app/AuthShell.tsx`, `app/page.tsx`, `app/trucking/page.tsx` |
| Cloud foundation (main, PR #35) | Object storage, worker leases, PostgreSQL row-level security policies, backup drills, deploy validation | `docs/engineering/postgres.md` |

## Hourly trucking: decisions

- A truck shift is its own record. Material deliveries link to it; the hourly cost
  belongs to the shift alone, so eight loads in a nine-hour shift cost nine hours.
  Cost per ton is shown only when every linked delivery is in tons.
- Hours on site = end - start - unpaid break, to the hundredth. Start and end are
  date-times, so shifts crossing midnight need no special case.
- A person states the hours they approve. The time sheet's figures are a suggestion
  in the form; the hauler's claimed hours are evidence and flagged when they differ.
  Approved hours can't exceed the time on site recorded.
- Agreement choice: most specific started agreement (project + truck type, project,
  truck type, any), latest start wins; expired means unpriced, never a broader
  fallback. Same start, different terms: unpriced.
- Standby is paid only at an agreed standby rate. Minimum hours top up paid hours
  (working + standby) at the hourly rate, as a separate line.
- Approved figures and allocations are locked by database triggers (SQLite and
  PostgreSQL), like approved tickets. History is append-only.
- Supersedes `billing-units.md` (hours on the ticket record) for trucking.

## Migrations (all additive)

`20261123_24` to `20261124_28` (merged, PR #36), and `20261124_29_hourly_trucking`:
six new tables and `tickets.truck_shift_id`, no table rebuilt. Rollback: restore the
pre-upgrade backup (downgrades are refused by design). PostgreSQL: runs in CI only;
not run on this machine.

## Verification status (2026-11-24)

- SQLite backend suite, browser suite (production build) and real-stack suite pass locally.
- **PostgreSQL: not verified anywhere end to end.** No PostgreSQL or Docker on the dev
  machine, and the CI "PostgreSQL tests" job has failed on `main` since at least
  2026-11-12 (`d33de3f`, before PR #36): the worker-kill tests in
  `tests/test_t_failures.py` time out on PostgreSQL (90-120 s each) and the job hits its
  time limit (exit 124) before the suite finishes. Fix that first; until then, trigger,
  row-level-security and migration behaviour on PostgreSQL is unproven.
- Linking deliveries after a shift is approved is refused (it would rewrite cost per
  ton); approved money, hours, rates and allocations are trigger-locked.

## Not done (in priority order)

1. Fix the PostgreSQL CI job (see above).
2. Trucking time-sheet extraction (AI). Plan: a `timesheet` intake mode on batches so
   uploads reuse validation, storage and the job queue; in the worker, a time-sheet
   prompt and schema (truck, driver, date, start, end, break, standby, total hours
   claimed, loads, signed by; null when unreadable, never inferred) via a generic
   `extract_json(image, prompt)` on the Gemini extractor with the same retries and
   usage metering; a `timesheet_drafts` table holding the read, its file and provenance
   when it can't yet be a shift (unknown truck, missing times), and a shift created
   with `source="extracted"` and `extraction` set when it can, always `needs_review`.
   Tests with a fixture extractor; no real model calls without authorization.
3. Drafting hauling-invoice lines from the PDF's text layer (manual entry works today).
4. Fee agreements (fuel surcharge, mobilization, standby rules per contract), so
   agreed fees stop showing as unsupported.
5. Units other than tons for material (cubic yards, loads), delivery dispositions
   (rejected, returned, partial), purchase orders and three-way matching.
6. Multi-ticket photo boundary detection and a crop editor.
7. Customer demo company with trucking, invoices and planted discrepancies.

## Risks

- No real documents or real contractor workflow tested.
- PostgreSQL-specific behaviour (triggers, row-level security) is verified only in CI.
- Hours rounding to the hundredth is a choice; some contracts bill in quarter hours.
  The approving person can enter the contract's figure, but it isn't configurable.
