# Mission 02: document intelligence, review speed and pilot readiness

Branch `feature/document-intake`, from `4eefd72` (Mission 01, PR #42). This page is the persistent record: what's done and the evidence for it, what's open, and what only real contractors can validate.

## Workstreams

| # | Workstream | Owner files | Status |
|---|---|---|---|
| W0 | Baseline benchmarks (before any change) | `scripts/benchmark_intake.py`, `docs/engineering/2026-11-intake-baseline.md` | Integrated |
| W1 | Automatic multi-ticket segmentation (`layout=auto`), add a missed region | `services/segmentation.py`, `splitter.py`, `pipeline.prepare_file` | Integrated |
| W2 | Extraction: PDF text layer with no AI, unit/weights/truck fields, supplier suggestions, in-batch duplicate numbers | `extraction.py`, `validation.py`, `pipeline.apply_extraction`, `ticket_text.py` | Integrated |
| W3 | CSV/Excel delivery import into the normal review flow (migration 34) | `delivery_import.py`, `routers/delivery_imports.py`, `upload/import/` | Integrated |
| W4 | Review speed (keyboard, focus, filters, prefetch, batch corrections) and mobile capture | `upload/page.tsx`, `ReviewForm`, `TicketQueue`, `ScanViewer` | Integrated |
| W5 | Financial risk investigation (periods, partial billing, split shifts, duplicates, "paid" wording, performance) | reconciliation services | Integrated |
| W6 | Environments (demo, pilot, production), hosted demo seeding, secret scan, onboarding, founder launch checklist | `config.py`, `seed_demo.py`, `docs/deploy/` | Integrated |

## Acceptance criteria (engineering)

| ID | Criterion | Status | Evidence |
|---|---|---|---|
| A1 | Baseline measured for 10, 50, 100 and 300 tickets before changes (synthetic, labelled) | Done | `scripts/benchmark_intake.py`, `docs/engineering/2026-11-intake-baseline.md` (synthetic, labelled) |
| A2 | A page with 1 to 6 tickets, irregular, slightly rotated, is split without choosing a layout; uncertain pages fall back to the whole page; no ink silently dropped | Done | `services/segmentation.py`, `layout=auto` default; `tests/test_segmentation.py` (1–6 tickets, tilt, blank, stray print). Synthetic recall table not yet written up |
| A3 | A missed ticket can be added as a region from the original page | Done | `POST /api/files/{id}/pages/{n}/regions`, audited and company-checked (tests). No drawing UI yet |
| A4 | Digital PDF tickets read from their text layer with zero AI calls | Done | `services/ticket_text.py`: 76 of 80 synthetic digital tickets read with no reader call (`docs/engineering/ocr-performance.md`) |
| A5 | The reader returns unit, weights and truck; pounds converted with the source kept; nothing invented | Done | `tests/test_extraction_fields.py` |
| A6 | Suggestions from a company's own approved history, never auto-applied, never across companies | Partial | `services/supplier_memory.py` (3+ agreeing approved tickets, same company; test). Not shown in the review form yet |
| A7 | CSV/XLSX import: map, preview, duplicates, then normal review/approval; history and original kept; company isolation | Done | `/api/delivery-imports`, `/upload/import`, migration 34; `tests/test_delivery_imports.py`, `imports.spec.mjs` |
| A8 | Review: measured interactions per clean ticket, shortcuts, issue filters, prefetch, batch corrections without approving | Done | Cursor on what needs a person, warned values checked before Accept, scans fetched once (369 → 59 ms wait after Accept, scripted), filters, batch corrections; `review-speed.spec.mjs`, `docs/engineering/review-speed.md` |
| A9 | Mobile: photograph and upload several tickets, status survives leaving, interrupted upload recoverable | Partial | Camera button, recoverable upload errors (`mobile-intake.spec.mjs`). No byte-level upload progress |
| A10 | Financial risks: each confirmed or ruled out with a test; confirmed ones fixed | Done | 3 confirmed defects fixed (split-shift reconciliation, out-of-period hauler tickets, a query per invoice); payment wording removed; `docs/engineering/financial-risk-review.md`, `tests/test_financial_risks.py` |
| A11 | Demo and customer environments can't share data or unrestricted AI; hosted demo seeding is safe and repeatable | Done | `APP_ENVIRONMENT` checks, guarded `seed_demo.py --hosted`, `AI_READING_ENABLED`; `tests/test_environments.py`. Hosted seed not yet run on PostgreSQL |
| A12 | Founder launch checklist: exact external actions | Done | `docs/deploy/founder-launch-checklist.md` |
| A13 | Integrated: SQLite, PostgreSQL, browser, real-stack and production build green; after-benchmarks repeated | Partial | See Verification. After-benchmarks not repeated |

## Only real contractors can validate

- Extraction accuracy on real handwritten and printed tickets (needs authorised documents and a paid reader run).
- Real review time per ticket for a person, not a script.
- Which suppliers send spreadsheets, PDFs or portal exports, and in what columns.
- Whether producer e-ticket APIs matter to the first customers (see `market-research.md`).

## Also done in this run

- PRs #40, #41 and #42 merged into main, in order, each after its checks passed. This branch now comes from main.
- Deliveries approved before their rate existed can be priced once the rate is entered, with history (`POST /api/rates/apply-to-unpriced`, `tests/test_rates_after_approval.py`).

## Open, in priority order

1. **Intake defects found by the baseline:**
   - a PDF over the page or pixel limit is accepted, then fails in the worker (it should be refused at upload);
   - the first ticket waits until every page is drawn;
   - one failed read marks the whole batch failed;
   - an identical re-upload is read again, which costs AI reads.
2. Show supplier-memory suggestions in the review form; a drawing tool for adding a region.
3. Run the hosted demo seed against PostgreSQL; repeat the benchmarks on the integrated code.
4. Byte-level upload progress on phones; a photo default layout of one ticket each.
5. With AI reading off, read digital PDFs from their text anyway (today the batch stops first).

## Verification (integrated branch, 2026-11-26)

| Check | Result |
|---|---|
| Backend, SQLite | 1,780 passed, 37 skipped, 3 xfailed |
| Backend, PostgreSQL | 1,804 passed, 13 skipped, 3 xfailed |
| Browser (production build) | 362 passed, 1 skipped |
| Real stack (`tests/e2e/run.sh`) | 1 + 7 passed. Two steps updated for deliberate changes. The 2x2 demo kit now chooses the 2x2 layout, because auto is the default. A 99-ton value must be checked against the scan before Accept. |
| Type check | Clean |
| Migrations | 33 to 34 on the data copy and the demo |
| Demo | `verify_demo.py` 34 of 34; runs with `APP_ENVIRONMENT=demo`, AI reading off |
