# Mission 02: document intelligence, review speed and pilot readiness

Branch `feature/document-intake`, from `4eefd72` (Mission 01, PR #42). This page is the persistent record: what's done and the evidence for it, what's open, and what only real contractors can validate.

## Workstreams

| # | Workstream | Owner files | Status |
|---|---|---|---|
| W0 | Baseline benchmarks (before any change) | `scripts/benchmark_intake.py`, `docs/engineering/2026-11-intake-baseline.md` | Running |
| W1 | Automatic multi-ticket segmentation (`layout=auto`), add a missed region | `services/segmentation.py`, `splitter.py`, `pipeline.prepare_file` | Running |
| W2 | Extraction: PDF text layer with no AI, unit/weights/truck fields, supplier suggestions, in-batch duplicate numbers | `extraction.py`, `validation.py`, `pipeline.apply_extraction`, `ticket_text.py` | Running |
| W3 | CSV/Excel delivery import into the normal review flow (migration 34) | `delivery_import.py`, `routers/delivery_imports.py`, `upload/import/` | Running |
| W4 | Review speed (keyboard, focus, filters, prefetch, batch corrections) and mobile capture | `upload/page.tsx`, `ReviewForm`, `TicketQueue`, `ScanViewer` | Running |
| W5 | Financial risk investigation (periods, partial billing, split shifts, duplicates, "paid" wording, performance) | reconciliation services | Running |
| W6 | Environments (demo, pilot, production), hosted demo seeding, secret scan, onboarding, founder launch checklist | `config.py`, `seed_demo.py`, `docs/deploy/` | Running |

## Acceptance criteria (engineering)

| ID | Criterion | Status | Evidence |
|---|---|---|---|
| A1 | Baseline measured for 10, 50, 100 and 300 tickets before changes (synthetic, labelled) | Open | |
| A2 | A page with 1 to 6 tickets, irregular, slightly rotated, is split without choosing a layout; uncertain pages fall back to the whole page; no ink silently dropped | Open | |
| A3 | A missed ticket can be added as a region from the original page | Open | |
| A4 | Digital PDF tickets read from their text layer with zero AI calls | Open | |
| A5 | The reader returns unit, weights and truck; pounds converted with the source kept; nothing invented | Open | |
| A6 | Suggestions from a company's own approved history, never auto-applied, never across companies | Open | |
| A7 | CSV/XLSX import: map, preview, duplicates, then normal review/approval; history and original kept; company isolation | Open | |
| A8 | Review: measured interactions per clean ticket, shortcuts, issue filters, prefetch, batch corrections without approving | Open | |
| A9 | Mobile: photograph and upload several tickets, status survives leaving, interrupted upload recoverable | Open | |
| A10 | Financial risks: each confirmed or ruled out with a test; confirmed ones fixed | Open | |
| A11 | Demo and customer environments can't share data or unrestricted AI; hosted demo seeding is safe and repeatable | Open | |
| A12 | Founder launch checklist: exact external actions | Open | |
| A13 | Integrated: SQLite, PostgreSQL, browser, real-stack and production build green; after-benchmarks repeated | Open | |

## Only real contractors can validate

- Extraction accuracy on real handwritten and printed tickets (needs authorised documents and a paid reader run).
- Real review time per ticket for a person, not a script.
- Which suppliers send spreadsheets, PDFs or portal exports, and in what columns.
- Whether producer e-ticket APIs matter to the first customers (see `market-research.md`).
