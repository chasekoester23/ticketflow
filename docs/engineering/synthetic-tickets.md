# Synthetic handwritten delivery tickets

> **SYNTHETIC — engineering benchmark, not real-world accuracy.**
> Every report, label file and contact sheet this tool writes carries that stamp, and names the extractor used.

`backend/synthetic/` generates fictional, handwritten-looking delivery tickets, lays them on scanner pages, and runs them through TicketFlow's real intake, splitter, pipeline, review and approval code on a throwaway database. It is offline: no downloads, no fonts fetched, no model calls.

## What synthetic results prove, and what they don't

**They can't stand in for real handwriting or real OCR accuracy.** The handwriting comes from about 80 hand-made stroke glyphs, bent by five styles. Real writers, pens, carbon paper, stamps, coffee rings and scanners vary in ways this generator doesn't model. The `oracle` and `noisy-simulated` extractors don't read the image at all: they answer from the labels. Their field accuracy shows that the pipeline and the arithmetic work, not that anything can read a ticket. Never quote a synthetic number as accuracy, and never put one beside a real one. The evaluator refuses to mix synthetic and real labels in one report.

**They are useful for:**

- **Splitter and intake geometry.** The tickets' true corners on the page are exact, so the coverage of each ticket by the crop TicketFlow actually made is a real measurement of the fixed 2×2 and single splitter under skew, rotation and off-centre placement, on fictional layouts.
- **Slot detection.** Whether TicketFlow's own detector treats each slot as content, faint or empty, on pages with known contents.
- **Pipeline and workflow regression.** Upload, the job queue, rendering, records, flags, corrections, approval, duplicate and price-snapshot rules, cost arithmetic and scoring all run for real, with answers known in advance.
- **Evaluation tooling.** The ground truth uses the `scripts/evaluate_workflow.py` format, so that scorer is exercised end to end.

The page geometry (where the tickets lie) is synthetic, so crop rates depend on the presets. Read them as "this is what the splitter does to tickets placed like this", not as how often real scans will be cut.

## How it works

```
records (seeded)  ->  plan per ticket  ->  drawn ticket + render log + labels
      |                                              |
      |                     pages: place, skew, clip, degrade  ->  PNG pages, multi-page PDFs, pages.json
      v                                              v
records.json/csv                       truth.json (evaluate_workflow format)  ->  validate  ->  evaluate
```

1. **Records** (`records.py`) are the source of truth: ticket number (unique per supplier), date (always in the past), material (aggregates, sand, asphalt, fill, rip rap, recycled), fictional supplier and town (phones in the reserved 555-01xx range), project, job location, net tons, and for scale tickets gross, tare and net, in lb (net tons = net lb / 2000, exact) or tons (gross − tare = net). Truck, driver, hauler, PO and load number, and a fictional rate sheet. Same seed, same bytes.
2. **Templates** (`templates.py`): ten fictional supplier forms, as data: a certified scale ticket, a haul ticket, a delivery slip, a carbon-copy weight ticket (pink paper, purple carbon ink, soft), a landscape asphalt ticket, a ticket with a perforated tear-off stub that repeats the number and tons, a plant-printed hot-mix ticket (mostly printed, job written by hand), a fill tally with a material check-box list, a boxed grid form, and a long narrow ticket (6.4 in, taller than a 2×2 quadrant). Each field has a box, a label and its wording ("Net Tons", "Qty (Tons)", "Est. Tons", "TONS"...), how it is filled in (hand, print, red stamp, check mark), whether it's optional, and its decoration (underline or cell).
3. **Handwriting** (`glyphs.py`, `handwriting.py`): single-stroke vector glyphs for digits, upper and lower case and punctuation, smoothed with Catmull-Rom curves and drawn at 2× with Pillow. Styles (`neat`, `rushed`, `messy`, `faint`, `difficult`) set point jitter, slant, baseline drift, size wobble, stroke width (0.5 mm pen at 150 dpi for `neat`), pressure (ink darkness), letter spacing, line angle and stroke overshoot. Effects:
   - **crossed out**: a wrong value (a slipped digit, or a different job or product), struck through with one line, two lines or a scribble, and the correction written beside it (or above it in a tall cell);
   - **ambiguous numerals**: variant glyphs drawn to look like their pair: 1/7, 5/S, 0/6, 8/3;
   - **smudged**: a value scribbled over until it can't be read;
   - **missing**: a field that matters left blank; optional fields left unused.
   Printed form text uses Pillow's bundled default font, Aileron Regular (released CC0 by dotcolon, embedded in Pillow); nothing else is used or downloaded.
4. **Truth integrity.** The canvas logs every glyph as it draws it, per field and role (value, correction, struck, strike, smudge, check, printed), with its ink box. Labels separate `true_value` (what the ticket means), `written_value` (what the form shows before effects), `visible_value` (what can still be read: the correction if crossed out, nothing if smudged or missing), `struck_value`, `ambiguous_chars`, `legibility`, `intentionally_missing` and `smudged`.
5. **Pages** (`scan.py`): 1-up and 2×2 4-up letter pages at 150 dpi. Tickets get rotation, off-centre placement, upside-down placement and, for landscape tickets in a 2×2 grid, a quarter turn. Some are deliberately pushed partly off the page; some slots are left blank. Long tickets are laid against the glass edge with the slot beside them left free, as a person would. The whole sheet is then turned, sheared and shifted. Tickets never overlap and never leave the page unless the plan says so; a page that breaks either rule is laid out again more calmly. Then blur, contrast loss and faint scans, an uneven lighting gradient, noise, greyscale or colour, and JPEG compression. Output: PNG pages and multi-page PDFs (JPEG pages, as scanners write them).
6. **Geometry bookkeeping.** Every step is an explicit affine matrix applied to pixels and to the bookkeeping alike. Each slot records its ticket, corners (`quad`), `bbox`, `rotation_deg`, `visible_fraction`, `fully_visible`, `intentionally_clipped`, the matrices, and each field's corners and visibility. Visibility is cut twice: by the glass, then by the scan window after the sheet moves, because a corner pushed off the glass doesn't come back when the sheet is turned.

## Difficulty presets and named sets

| Preset | Handwriting | Effects (per ticket) | Placement | Scan |
|---|---|---|---|---|
| easy | neat 70%, rushed 30% | ambiguous 4% | ±0.6° turn, 0.04 in jitter | light blur, little noise, JPEG 80–90 |
| moderate | + messy, faint | missing 4%, crossed out 8%, ambiguous 12% | ±2°, 0.16 in, 2% upside down | some faint scans, gradient, JPEG 70–88 |
| hard | rushed, messy, faint, difficult | missing 10%, crossed 18%, ambiguous 25%, smudged 4% | ±4°, 0.3 in, 6% clipped, long tickets in 2×2 | blur ≤1.5 px, faint 15%, JPEG 50–72 |
| extreme | messy, faint, difficult | missing 18%, crossed 30%, ambiguous 45%, smudged 8% | ±7°, 0.45 in, 14% clipped | blur ≤2.2 px, faint 30%, JPEG 30–55 |
| stress | mostly difficult | everything more often | ±11°, 0.65 in, 25% clipped | worst |

Named sets fix a seed and stratify layouts across difficulties (each template equally often per difficulty):

| Set | Seed | Tickets | Where |
|---|---|---|---|
| `benchmark-mini` | 20261108 | 24 per difficulty (96) on 38 pages | committed: `backend/eval_data/synthetic/benchmark-mini/` (PDFs, ~8 MB) |
| `benchmark` | 20261109 | 60 per difficulty (240) on 97 pages | generated: `eval_data/synthetic/generated/benchmark/` (gitignored) |
| `stress` | 20261110 | 200 | generated (gitignored) |

`backend/eval_data/real/` is gitignored and reserved for real scans and labels; the synthetic evaluator refuses any dataset under a folder named `real`.

## Files and label schema

A dataset folder:

| File | Contents |
|---|---|
| `dataset.json` | schema `ticketflow.synthetic.dataset/1`, stamp, generator version, seed, set, counts, sha256 of every file |
| `records.json`, `records.csv` | schema `ticketflow.synthetic.records/1`: the records and the fictional rate sheet |
| `labels.json` | schema `ticketflow.synthetic.labels/1`: per ticket, its difficulty, template, style, the full plan, its placement, and per field the label below |
| `render_log.json` | per ticket and field, the segments drawn: role, glyph keys (`"1~7"` is a 1 drawn like a 7), text, ink box |
| `pages.json` | schema `ticketflow.synthetic.pages/1`: per page its file, layout, scan settings and page matrix; per slot the ticket and geometry above |
| `truth.json` | ground truth for `scripts/evaluate_workflow.py` (below) |
| `scans/*.pdf` | one PDF per difficulty and layout (≤10 pages each, under TicketFlow's limits) |
| `pages/*.png` | the same pages as PNG (optional; not committed) |

Per field in `labels.json`:

```json
{"field": "net_tons", "ink": "hand", "label_text": "Net Tons", "box": [24, 382, 324, 462],
 "true_value": "18.40", "written_value": "18.4", "visible_value": "18.4",
 "struck_value": "19.4", "crossed_out": true, "smudged": false,
 "intentionally_missing": false, "omitted_optional": false,
 "ambiguous_chars": [{"index": 0, "char": "1", "confusable_with": "7"}],
 "legibility": "ambiguous", "ink_bbox": [160.2, 401.0, 230.9, 452.3]}
```

`legibility` is `printed`, `clear`, `degraded`, `poor`, `ambiguous`, `illegible` (smudged), `missing` or `not_applicable`.

`truth.json` is the `evaluate_workflow` format, extended compatibly (the scorer ignores the extra keys): `labelled_by` names the generator, `labelled_independently` is true (labels come from the generator, never from TicketFlow's output), `synthetic: true`, `label_source: "synthetic"`, the stamp, and per slot `file`, `page`, `position` (0–3 left to right, top to bottom, as the splitter numbers them), `ticket`, and the six `expected` fields. Each slot also carries `synthetic: true`, `ticket_id`, `difficulty`, `layout`, `template`, `style`, `visible_fraction` and `fully_visible`. `expected` is what a careful person reading the paper would write: the true value, or null when nothing legible is there (missing, smudged) or the field is cut off the scan (less than 95% of its box on the page). `crop_complete` is left out: the evaluator measures it geometrically.

## Commands

From `backend/`:

```sh
python -m synthetic generate --set benchmark-mini --out eval_data/synthetic/benchmark-mini --formats pdf
python -m synthetic generate --set benchmark            # into eval_data/synthetic/generated/benchmark
python -m synthetic generate --difficulty hard --count 40 --seed 7 --out /tmp/hard40
python -m synthetic assemble DATASET --formats pdf,png  # remake pages (adds PNGs to the committed set)
python -m synthetic export-labels DATASET --out truth.json
python -m synthetic preview DATASET --out sheet.jpg --per-difficulty 2 --tickets tickets.jpg
python -m synthetic validate DATASET [--quick] [--determinism]
python -m synthetic evaluate DATASET --extractor oracle --out report.json --markdown report.md
```

`generate` refuses a non-empty folder unless `--overwrite`, and never deletes a folder that isn't a synthetic dataset. It builds into `<out>.partial` and moves it into place at the end, so a failed build never looks complete.

`validate` exits non-zero on any problem. It checks schemas and stamps; every file's hash; the records (gross − tare = net in the right unit, unique numbers, dates, rates); **truth integrity** (each field's glyphs re-derived from the render log equal the label's written, visible and struck values and ambiguous characters; the written value means the true value; ink stays inside its box and the ticket); a fresh re-render gives the same log and labels; pages (corners re-derived from the matrices, bounding boxes, visible fractions, no ticket covering another, nothing that matters cut off unless the clip was intended); and that `truth.json` equals a fresh export. `--determinism` rebuilds from the seed and compares every file byte for byte.

## Preview

`preview` writes a contact sheet: each page with every ticket's true outline (green on the page, orange deliberately clipped, with its visible percentage and rotation), blank slots crossed out, and TicketFlow's fixed 2×2 cut lines dashed in red. `--tickets` adds a sheet of drawn tickets with each field box and its labelled visible value, crossed-out value and ambiguous characters. The committed sheets are `backend/eval_data/synthetic/preview/benchmark-mini-pages.jpg` and `-tickets.jpg`.

## Evaluate

`evaluate` builds a sandbox: the FastAPI app on a brand-new SQLite file and storage folder in a temporary directory (`--workdir` keeps it; it must be empty). Every global the app uses for its database, storage, worker hook and upload rate limit is pointed there and restored afterwards. The CLI sets `DATABASE_URL` and `STORAGE_DIR` to the sandbox before the app is imported, so nothing can reach a real database. Then:

1. It seeds a company, an owner account, a project, and the fictional suppliers, materials and rates.
2. **Real intake.** One batch per difficulty and layout goes through `POST /api/batches`; the real job queue, PDF renderer, 2×2 or single splitter, slot detection, record creation and flags run.
3. **Crop completeness.** For each true ticket: `coverage` is the share of its visible area inside the crop TicketFlow made (from `ScanSlot.crop_box`, scaled from the 200 dpi render), `complete` means coverage ≥ 99.5%, plus IoU, core fields cut by the crop (less than 98% of the field box inside), and crops showing more than 1% of another ticket.
4. **Detection.** Which slots got a record automatically, and how each ticket slot was classified (content, faint, empty).
5. **Scan manifest.** A simulated person does what the manifest asks: a slot with a ticket on paper but no record is marked as a ticket (`POST /api/scan-slots/{id}/mark-ticket`) and the batch retried; a faint slot that's really blank is confirmed blank.
6. **Review.** A simulated reviewer compares each record with the paper (the label), fixes each wrong field with probability `--catch-rate` (default 0.9, seeded) through `PATCH /api/tickets/{id}`, approves through `POST /api/tickets/{id}/verify`, flags what it can't approve, and skips records for slots with no ticket on paper.
7. **Scoring** by `evaluate_workflow` (overall and per difficulty): extraction accuracy per field, critical errors in ticket number and tons, correction rate, approved accuracy, processing jobs, retries and failures. Plus a **financial check** of each approved cost against true tons × the fictional rate, and end-to-end timing.

Extractors plug into TicketFlow's `Extractor` interface. They recognise each crop by a hash of its pixels, from the same renderer and splitter run beforehand:

| `--extractor` | Answers | What it tests |
|---|---|---|
| `oracle` | the label for the slot | the pipeline and the money math; must score 100% |
| `oracle-crop-aware` | the label, but only for fields fully inside TicketFlow's crop | what the splitter's crops cost a perfect reader |
| `noisy-simulated` | the oracle plus a seeded error model: ambiguous characters misread as their pair, digit slips that grow with poorer legibility, dropped fields, guesses at unreadable ones (`--error-model drop=0.05,...`) | review, correction, approval and failure metrics. Every planted error is listed in the report. Its rates are invented |
| `gemini` | the real paid provider | refuses to run without `--i-authorize-paid-calls`; never used in tests and not run for this report |

No free local OCR is importable in the pinned environment (`pytesseract` and `easyocr` aren't installed), so there is no OCR option.

## First measurement (November 2026)

The `benchmark` set (240 tickets, 60 per difficulty, ten layouts equally) through the code on `feature/d2-synthetic-tickets`. The crop and detection columns don't depend on the extractor.

| Difficulty | Crops complete | Mean coverage | Worst coverage | Tickets with a core field cut | Detected automatically | After the manifest |
|---|---|---|---|---|---|---|
| easy | 40/60 (67%) | 99.5% | 97.4% | 0 | 92% | 100% |
| moderate | 23/60 (38%) | 98.6% | 94.8% | 1 | 80% | 100% |
| hard | 22/60 (37%) | 96.3% | 71.9% | 8 | 70% | 100% |
| extreme | 12/60 (20%) | 95.7% | 76.9% | 18 | 12% | 100% |
| overall | 97/240 (40%) | 97.5% | 71.9% | 27 | 63% | 100% |

`benchmark-mini` (96 tickets, committed) agrees: complete 54% / 42% / 17% / 21% from easy to extreme, mean coverage 99.4% / 98.5% / 95.0% / 95.5%, a core field cut on 0 / 2 / 4 / 6 tickets, detected automatically 92% / 79% / 54% / 17%.

Read it this way:

- **Easy pages still lose a third of crops by a sliver.** The 2×2 cut trims 1% of the page off every quadrant edge, leaving 4.08 in of width. The wider templates (4.1–4.2 in) can't fit even when laid squarely; what's lost is border, not fields (no core field cut on easy pages).
- **The fields cut on hard and extreme pages** are mostly Net Tons on long tickets (the cut runs through them) and fields near edges of tickets laid 0.3–0.5 in off centre or turned 4–7°.
- **No phantom tickets.** No blank slot got a record (0 of 43), even with long tickets and offsets reaching into them.
- **With the `oracle`:** extraction and approved accuracy 100% on every field, every approved cost exactly right (202 approved; the rest were flagged because a field is missing, smudged or cut off on paper). **With `oracle-crop-aware`:** all-six extraction falls from 100% to 98% (moderate), 87% (hard) and 70% (extreme): the fields a perfect reader couldn't see in TicketFlow's crop. **With `noisy-simulated`** (default error model, catch rate 0.9): corrections and missed corrections flow through to approved figures and costs as they should; these rates are invented and only show the workflow metrics work.

### Findings in TicketFlow

Each has a strict-xfail test in `backend/tests/test_synthetic_findings.py`, which turns into a failure (XPASS) once behaviour changes.

- **B1, bug: clean 1-up tickets read as "faint", and a good file fails as "No ticket content detected".** `splitter.assess_slot` shrinks the whole crop to 200×200 px before counting dark pixels. On a 1-up page (1700×2200 px at 200 dpi) that is a 9–11× reduction, and a 0.5 mm pen stroke averages to light grey. 7 of the 10 layouts, neatly written on a clean scan, measure 0.0006–0.0019 on the thumbnail (threshold 0.002) against 0.007–0.012 at full resolution. Detection depends on the crop's size, not its content: 30 of 35 single-page tickets in the benchmark set (5 of 8 easy ones) and 10 of 13 in the mini set needed a person. When every slot in a file is faint, `process_file` fails the file with "No ticket content detected in this file... upload a corrected document": wrong advice for a good scan. Nothing is lost (the manifest lets a person mark the slot, and the batch retries), but every such ticket costs a click and a retry. Not fixed here: the thresholds guard against losing tickets, and changing them needs calibration on real scans. A likely fix is to count ink at a resolution tied to the page's dpi rather than a fixed thumbnail.
- **Faint detection on degraded 2×2 scans.** Only 12% of extreme and 70% of hard tickets were detected automatically; the rest were "faint" and needed a person. That is the designed behaviour ("a false alarm costs one click; a lost ticket costs far more"), and nothing was lost, but it is a lot of clicks on bad scans. In hard and extreme pages, blank slots also read as faint because of noise and lighting, so people confirm them blank.
- **L1, known limitation F6, now measured: the 2×2 margin trims centred quarter-page tickets.** A 4.13 in ticket laid squarely in its quadrant has 98.7% inside the crop.
- **L2, known limitation F6, now measured: long tickets in a 2×2 batch lose their bottom fields.** A 6.4 in ticket against the top of the glass has half its Net Tons box below the cut.

No crashes, failed jobs or wrong costs came from TicketFlow in any run.

### Bugs the validator caught in the generator

Worth recording, because they are exactly what makes synthetic numbers wrong without anyone noticing: tickets placed over each other (hiding fields the labels called readable); tickets accidentally pushed off the page; visibility computed after the sheet moved, which counted corners already lost off the glass as visible; and a crossed-out value that came out identical to its correction. Each now has a check in `validate` and was fixed before any number above was taken.

## Limitations

- One renderer, ten layouts, one glyph set: models trained or tuned on these would overfit them. The point is the pipeline, not the pictures.
- Pages are letter size at 150 dpi; scanner behaviour (auto-crop, deskew, colour dropout) isn't modelled. Tickets never overlap.
- The reviewer is a probability, not a person: no review time, no fatigue, no second opinions.
- The oracle identifies crops by pixel hash, so it only works on crops made by the same renderer and splitter (a mismatch shows as an extraction failure, never a silent pass).
- Rendered PDFs and PNGs are byte-identical across runs on one machine; another platform with a different JPEG or zlib build may differ in bytes, though records, labels and geometry are pure Python and don't.

## Next steps

1. Fix or recalibrate B1 against a few real 1-up scans (count ink at the page's dpi), then flip its xfails.
2. Run the same evaluation on real pilot scans with person-written labels in `eval_data/real/`, and compare crop completeness with these synthetic rates before deciding on crop adjustment (F6).
3. If crop adjustment is built, this benchmark is its regression test: crop completeness and core fields cut by difficulty should rise without phantom tickets.
4. Add more layouts and a second glyph set before using any of this to compare OCR providers, and then only with `--i-authorize-paid-calls` and a budget.
