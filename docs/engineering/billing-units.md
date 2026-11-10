# Hourly, per-ton and per-load billing: design

Status: design only. Nothing in pricing has changed. TicketFlow still prices every ticket as tons × rate.

## Why

Trucking is often billed by the hour, and some material by the load. Today, an hourly ticket has no tons, so it can't be approved or priced. Forcing hours into the tons field would corrupt totals.

## What already happens (this branch)

- The reader now also returns `hours`, `loads` and `job_number` when a ticket shows them. They are kept in `raw_extraction` only, with no new columns, so tickets read from now on carry the evidence a later migration needs.
- A read ticket with hours and no tons is flagged "Hourly ticket (6.5 h)…", not "Tonnage missing", so a reviewer flags or skips it on purpose.

## Data model (additive)

| Table | New column | Notes |
|---|---|---|
| `supplier_rates` | `unit` (`ton`, `hour`, `load`), not null, default `ton` | Every existing rate becomes `ton`, so lookups don't change |
| `tickets` | `billing_unit` (`ton`, `hour`, `load`), not null, default `ton` | Every existing ticket becomes `ton` |
| `tickets` | `hours` numeric(8,2), null | Separate from `tonnage`; never added to tons |
| `tickets` | `loads` integer, null | |
| `tickets` | `approved_quantity` / `approved_unit` | Locked at approval beside `approved_cost`, so a later unit change can't reprice an approved ticket |

The rate key becomes (supplier, material, unit), and the unique index widens to match. Existing `ton` rates keep their index entries.

## Behaviour

- **Approval:** the quantity that must be confirmed depends on the unit: tons for `ton`, hours for `hour`, loads for `load`. The confirmation rule ("a person confirms the quantity") applies to whichever it is.
- **Cost:** quantity × rate for the ticket's unit, rounded half up as now.
- **Totals:** tons, hours and loads are reported separately, never summed together. "Verified tonnage" keeps meaning tons. Cost totals add up across units.
- **Invoice comparison:** a line compares like with like. An hourly invoice line matches hourly tickets on hours. A line whose unit differs from its tickets is reported as a difference, never converted.
- **Validation:** hours above a plausible limit (for example 16 in a day) are flagged, like tons over 60.

## Migration and compatibility plan

1. One additive migration with defaults. No existing row changes value. It must pass the test that a database built by migrations matches the models, and do so on PostgreSQL.
2. Backfill nothing automatically. A person can set a ticket to `hour` and enter its hours; `raw_extraction.hours` is offered as a suggestion.
3. Tests before any behaviour change:
   - Every existing pricing, totals, export and invoice test passes unchanged, with all rows on `ton`.
   - Exact totals per company are identical before and after the migration on a copy of a real database. `restore_drill.py` already checks exact totals.
   - New tests for hourly approval, cost, mixed-unit totals and invoice mismatches.
4. Rehearse on a copy of the live database, with the backup and restore drill first, as for `20261109_23`.
5. Only then turn on the UI: a unit selector on rates and on the review form.

## Still needs real-world input

- Which suppliers bill hourly, and what their tickets show (start and stop times, total hours, minimum charges, standby time).
- Whether hourly rates vary by truck type, which may need a truck class on the rate.
- How hourly invoices list their lines (per ticket, or per day per truck).
