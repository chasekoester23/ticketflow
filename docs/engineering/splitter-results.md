# Splitter fixes: ink detection (B1) and adaptive 2×2 crops (L1, L2)

> **SYNTHETIC — engineering benchmark, not real-world accuracy.** Every number here comes from fictional tickets drawn by `backend/synthetic/`. They measure TicketFlow's splitter geometry and workflow on those pages. They do not predict results on real scans, and the thresholds below have not been calibrated on a real scanner.

Branch `feature/d3-splitter-fixes`, on top of `feature/d3-integrate-synthetic` (which brings PR #30's generator into main). The findings come from [synthetic-tickets.md](synthetic-tickets.md#findings-in-ticketflow).

## What changed

**B1: slot detection at the crop's own resolution** (`splitter.assess_slot`). The old test shrank every crop to 200×200 px and counted dark pixels. On a 1-up page (1700×2200 px) that is a 9–11× reduction, so pen strokes averaged to light grey and clean tickets read as "faint". Ink is now measured at native resolution, or after a whole-number reduction above 2400 px. It is the share of the slot covered by **connected strokes** (8-connected, with a minimum area) that are clearly darker than the paper next to them. That is a morphological black-hat after a 3×3 median, so speckle, tint and smooth shadows don't count. The result doesn't depend on the crop's size.

- Thresholds are unchanged: content ≥ 0.002, a visible mark ≥ 0.0015.
- The **old thumbnail test is kept as a second opinion that can only raise a slot**. Whatever it called content or faint stays at least that, so no slot reads quieter than before. When a crop has grown, the old test reads the part the fixed cut held, exactly as before.
- "Empty" now needs both measures to agree.

**L1, L2: 2×2 crops grow to the ticket** (`splitter.adapt_grid`, `find_ticket_regions`).

1. The page is averaged to about 50 cells per inch, which smooths scanner noise but keeps each ticket's paper edge, rules and writing.
2. A black-hat at 12 grey levels finds marks. Specks under 10 cells and the outer 1% of the page (scanner edges) are dropped.
3. Pieces of one ticket whose edge broke apart are merged, closest pair first, when they are aligned, within 0.6 in, and the result is still no bigger than one ticket (1.4 quarters):
   - first only within each quarter;
   - then across a quarter line only when neither quarter keeps another ticket-sized piece.
4. A region at least 1 in each way belongs to the quarter holding its centre.
5. Each crop is **the fixed cut joined with its own regions plus 0.08 in**. It only ever grows, so no ticket's coverage can fall.
6. Crop checks: when a crop is uncertain, the slot gets a ticket flag starting `Crop check:`.
   - *Ticket may continue past its crop*: the reach would be more than 0.4 of a quarter past the line, or the region is too big to be one ticket. The crop **falls back to the fixed cut**.
   - *Crop may include part of another ticket*: growing it added more than 2% of the crop's area from another slot's ticket.
   - *Slot may hold more than one ticket*: two separate regions in one slot, or two ticket-sized pieces joined across a quarter line.
7. **No phantom from an overhang.** A slot with no ticket of its own, whose fixed cut shows part of a neighbour's ticket, is at most "faint" (`assess_crop`), so a person decides.

**Stored geometry.** A slot already on record keeps the box it was recorded with when its file is retried (`scan_manifest.recorded_crop`). Its stored crop image, hash and ticket stay consistent across this change. Originals are untouched, and `crop_box` on each slot and ticket still holds the geometry used.

## Contract changes (API and UI)

No schema, endpoint or UI change.

- `crop_box` on a 2×2 slot or ticket can now extend past its quarter and overlap a neighbour's box. It was always a list of four page pixels; the frontend only shows the stored crop image.
- New ticket flag strings start with `Crop check:`. They display with the other warnings in the review form.
  - Like "Tonnage unusually large", they are **warnings, not blockers**. The reviewer approving the ticket is the confirmation, and tonnage still needs their explicit confirmation.
  - Editing a ticket keeps them, because they come from the scan, not the values (`api._refresh_flags`).
- `ScanSlot.ink_fraction` now stores the stroke measure, not the thumbnail measure. It is never money.
- The app now imports `cv2` and `numpy` at runtime. Both were already pinned in `requirements-dev.txt` and `constraints.txt`, which CI and the app install from.

## Before and after

Extractors: `oracle-crop-aware` (labels only for fields fully inside TicketFlow's crop) and `noisy-simulated` (seeded invented error model, simulated reviewer). The crop and detection columns don't depend on the extractor.

Column notes:
- **Read faint** counts real tickets the detector didn't take as a ticket: false negatives that need a person. No ticket was read as *empty* in any run.
- **Needs a look** counts slots that are faint or carry a crop check. A slot can be both.
- **Files failed** counts files the pipeline failed by itself ("No ticket content detected") before anyone touched the scan manifest.

### `benchmark`: 240 tickets, seed 20261109

| Difficulty | Complete crops | Mean coverage | Worst coverage | Core field cut | Auto-detected | Read faint | Needs a look (faint / check) | Files failed | All-six, crop-aware |
|---|---|---|---|---|---|---|---|---|---|
| easy | 67% → **100%** | 99.5 → 100.0% | 97.4 → 100.0% | 0 → 0 | 92 → 100% | 5 → 0 | 5 → 0 | 0 → 0 | 100 → 100% |
| moderate | 38% → **100%** | 98.6 → 100.0% | 94.8 → 100.0% | 1 → 0 | 80 → 100% | 12 → 0 | 17 → 8 (5 / 3) | 0 → 0 | 98.3 → 100% |
| hard | 37% → **100%** | 96.3 → 100.0% | 71.9 → 100.0% | 8 → 0 | 70 → 97% | 18 → 2 | 29 → 19 (13 / 6) | 1 → 0 | 86.7 → 100% |
| extreme | 20% → **82%** | 95.7 → 99.2% | 76.9 → 84.9% | 18 → 1 | 12 → 28% | 53 → 43 | 68 → 62 (58 / 10) | 1 → 0 | 70.0 → 98.3% |
| overall | 40% → **95%** | 97.5 → 99.8% | 71.9 → 84.9% | 27 → 1 | 63 → 81% | 88 → 45 | 119 → 89 (76 / 19) | 2 → 0 | 88.8 → 99.6% |

- Every run, before and after: 0 missed tickets after the manifest, 0 of 43 blank slots got a record, and 0 processing failures (jobs, files or tickets).
- **Oracle:** 202 of 202 approved costs exact, before and after.
- **Crop-aware:** 201 → 202 approved, all exact.
- **Noisy-simulated:** all-six 65.4% before and after; 196 exact and 2 wrong, before and after. The two are planted errors the simulated reviewer missed at a 0.9 catch rate, not a splitter error.
- The one field still cut (extreme) is on a ticket partly off the glass, in a faint slot that a person reviews.

### `benchmark-mini`: 96 tickets, seed 20261108, committed

| Difficulty | Complete crops | Mean coverage | Worst coverage | Core field cut | Auto-detected | Read faint | Needs a look | Files failed | All-six, crop-aware |
|---|---|---|---|---|---|---|---|---|---|
| easy | 54% → **100%** | 99.4 → 100.0% | 98.0 → 100.0% | 0 → 0 | 92 → 100% | 2 → 0 | 2 → 0 | 0 → 0 | 100 → 100% |
| moderate | 42% → **100%** | 98.5 → 100.0% | 93.1 → 100.0% | 2 → 0 | 79 → 100% | 5 → 0 | 8 → 3 | 0 → 0 | 91.7 → 100% |
| hard | 17% → **100%** | 95.0 → 100.0% | 74.8 → 100.0% | 4 → 0 | 54 → 96% | 11 → 1 | 15 → 7 | 1 → 0 | 83.3 → 100% |
| extreme | 21% → **88%** | 95.5 → 99.7% | 75.8 → 96.7% | 6 → 1 | 17 → 46% | 20 → 13 | 25 → 19 | 1 → 1 | 75.0 → 95.8% |
| overall | 33% → **97%** | 97.1 → 99.9% | 74.8 → 96.7% | 12 → 1 | 60 → 85% | 38 → 14 | 50 → 29 | 2 → 1 | 87.5 → 99.0% |

- Phantoms: 0 of 17 blank slots, before and after.
- **Oracle:** 81 of 81 costs exact, before and after.
- **Crop-aware:** 78 → 81 approved, all exact.
- **Noisy-simulated:** 79 exact and 1 wrong, before and after (reviewer miss).

### Held-out check, against overfitting

The thresholds were set while looking at the two sets above. This check uses a seed never looked at during development (7777), different counts, the harsher `stress` preset, and a different reviewer (seed 11, catch rate 0.8):

```
python -m synthetic generate --difficulty <d> --count <n> --seed 7777 --out <dir> --formats pdf
python -m synthetic evaluate <dir> --extractor oracle-crop-aware|noisy-simulated --seed 11 --catch-rate 0.8
```

| Set | Complete crops | Mean coverage | Worst coverage | Core field cut | Auto-detected | Read faint | Phantom (blank slots with a record) | Needs a look | Files failed |
|---|---|---|---|---|---|---|---|---|---|
| easy, 24 | 67% → **100%** | 99.6 → 100.0% | 97.3 → 100.0% | 0 → 0 | 83 → 100% | 4 → 0 | 0/1 → 0/1 | 4 → 0 | 0 → 0 |
| moderate, 36 | 36% → **100%** | 98.6 → 100.0% | 92.3 → 100.0% | 2 → 0 | 58 → 100% | 15 → 0 | 0/3 → 0/3 | 17 → 5 | 1 → 0 |
| hard, 36 | 25% → **100%** | 95.1 → 100.0% | 70.6 → 100.0% | 9 → 0 | 56 → 92% | 16 → 3 | 0/10 → 0/10 | 26 → 14 | 2 → 1 |
| extreme, 36 | 25% → **89%** | 94.8 → 99.8% | 73.7 → 98.2% | 15 → 1 | 19 → 47% | 29 → 19 | **1/10 → 0/10** | 38 → 30 | 1 → 0 |
| stress, 40 | 22% → **65%** | 95.6 → 99.0% | 77.3 → 85.5% | 7 → 0 | 15 → 20% | 34 → 32 | 0/19 → 0/19 | **53 → 56** | 1 → 1 |

- Held-out gives the same picture as the development sets.
- **The old code made one phantom ticket** (extreme): a blank slot got a record. The new code makes none.
- Crop-aware approved costs: all exact in every set, before and after.
- Noisy-simulated: identical before and after in every set, including stress's 2 wrong costs, which are reviewer misses of planted errors.
- **Stress needs a look rose by 3 (53 → 56).** Faint slots fell from 53 to 51, but 9 slots got crop checks. That is the price of flagging instead of guessing on the hardest pages.

### Gates

| Gate | Result |
|---|---|
| Zero new false negatives (a ticket the old code took as content, now faint or empty) | **Pass**: 0 on benchmark, mini and all five held-out sets. No ticket read as empty anywhere. |
| Zero phantom tickets | **Pass**: 0 blank slots with a record and 0 approvals without a ticket on paper, in every run. Held-out extreme: 1 before, 0 after. |
| Zero cost errors | **Pass**: oracle and crop-aware have every approved cost exact. Noisy-simulated's wrong costs are reviewer misses and are identical before and after. |
| No ticket's coverage falls | **Pass**: 0 tickets with lower coverage than before in every set (the crop only grows). |
| Never swallow another ticket silently | **Pass** on the measured sets: every crop whose growth added more than 2% of its area from another ticket carries a check. Unflagged growth is 1.7% at most, the padding. This is asserted over all benchmark-mini pages in `tests/test_splitter_regions.py`. |

Processing time per page rose by about 0.06 s (0.30 → 0.36 s on the benchmark, in concurrent runs).

## Tests

- `tests/test_synthetic_findings.py`: B1, L1 and L2 were strict xfails. They now pass on rendered synthetic pages, and L1/L2 also assert that the fixed cut really did trim them.
- `tests/test_splitter_regions.py`: clean, faint-but-real and blank 1-up pages; no slot quieter than the old test under every scan condition; size-independent ink; the fallback and two-ticket checks; invariants over every committed benchmark-mini 2×2 page (crops only grow, no phantom, no silent swallowing); stored geometry on retry; and a flagged crop that survives edits and doesn't block approval.

## Still open

- **Real scans.** Every threshold here (stroke contrast 60/20, region contrast 12, merge gap 0.6 in, reach 0.4) was set on synthetic pages, and the held-out sets come from the same renderer. Before relying on the rates, calibrate on a few owner-approved real 1-up and 2×2 scans, the way `eval_data/real/` describes.
- **Extreme and stress pages are still mostly "faint"** (28% and 20% auto-detected). Those tickets are written faintly on purpose. They go to a person and are never lost, but each costs a click.
- **A solid dark scanner edge on a blank 1-up page reads as content**, exactly as before. The old thumbnail test called it content, and its vote is kept so nothing reads quieter. On a 2×2 page the outer 1% is ignored for regions, but the old vote still applies to the fixed cut.
- **No recrop tool.** A person can see the original page and the stored `crop_box`, but can't draw a new crop in the UI.
- Page width is assumed to be about 8.5 in when converting inches to cells, which is close enough for letter and A4.
