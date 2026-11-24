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
| **Hourly trucking (new)** | Haulers, trucks, hourly agreements (project / truck type / dates / minimum / standby), truck shifts from time sheets, approval that locks hours, rates and cost, split across projects and cost codes to the cent, reopen with reason, delivery linking with no per-delivery charge, Overview totals | `services/trucking.py`, `routers/trucking.py`, `app/trucking/` |
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

## Not done (in priority order)

1. Hauling invoices: entering a hauler's invoice and checking billed hours and rates
   against approved shifts, as an exceptions-center category.
2. Trucking time-sheet extraction: reading time sheets with the AI reader into
   unapproved shifts (today shifts are entered by hand from the sheet).
3. Attaching the time-sheet image to a shift (document storage exists; not wired).
4. Shift dispute workflow beyond the status (the `disputed` status exists; no UI to set it).
5. Units other than tons for material (cubic yards, loads), delivery dispositions
   (rejected, returned, partial), purchase orders and three-way matching.
6. Multi-ticket photo boundary detection and a crop editor.
7. Customer demo company with trucking, invoices and planted discrepancies.

## Risks

- No real documents or real contractor workflow tested.
- PostgreSQL-specific behaviour (triggers, row-level security) is verified only in CI.
- Hours rounding to the hundredth is a choice; some contracts bill in quarter hours.
  The approving person can enter the contract's figure, but it isn't configurable.
