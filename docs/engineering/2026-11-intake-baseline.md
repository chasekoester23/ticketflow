# Document intake and review baseline, November 2026

**Synthetic documents, a stand-in reader and a scripted reviewer, on a shared laptop.
These figures measure the software around the reader and the reviewer at commit `4eefd72`,
so later changes can be compared with them. They say nothing about how fast or accurate a
real AI reader is, or how fast a person reviews.** No paid or network call was made.

## How to re-run

From `backend/` (same machine, similar load, for a fair comparison):

```bash
python scripts/benchmark_intake.py --json intake.json                 # 10, 50, 100, 300 tickets: about 3 minutes
python scripts/benchmark_intake.py --tickets 72 76 --skip raised split memory duplicates failures   # the page limit
python scripts/benchmark_intake.py --tickets 50 --reader-ms 500 --skip memory duplicates failures  # a slower stand-in
```

From the repository root, for review (needs Node 22, the frontend's packages and Playwright):

```bash
BENCH_API_PORT=8221 BENCH_WEB_PORT=3221 frontend/tests/e2e/review-bench.sh 50 review-50.json
BENCH_API_PORT=8221 BENCH_WEB_PORT=3221 frontend/tests/e2e/review-bench.sh 300 review-300.json
BENCH_THINK_MS=1500 BENCH_API_PORT=8221 BENCH_WEB_PORT=3221 frontend/tests/e2e/review-bench.sh 50 review-50-paced.json
```

The review bench runs `next build` in `frontend/`, which overwrites `.next`: stop a
running `next dev` first and restart it afterwards.

## Machine

Apple M2 (`hw.model` Mac14,15), `hw.ncpu` 8, 8 GB memory, macOS 15.2, Python 3.13.3,
SQLite. Other agents were working on the machine throughout. The load average was 7 to 19
(1-minute) during the intake runs (25 November 2026, 23:26 to 23:45 EST) and about 8.5 for
the limit probe and the 500 ms run (26 November, 11:41). So single figures vary by up to a
factor of about 1.5 between runs (compare runs 2 and 3 below).

## What the intake benchmark does

`backend/scripts/benchmark_intake.py` (new). Each scenario runs in its own process, on a new
SQLite database and document folder in a temporary directory. `DATABASE_URL` and
`STORAGE_DIR` are set before the app is imported, and the script checks the database is
inside that directory. It uses:

- **The real API, in-process**: FastAPI's `TestClient`, as `scripts/seed_demo.py` does, with
  a signed-in owner of one fictional company. `POST /api/batches` with `layout=grid_2x2`,
  `intake_mode=extract`.
- **The real worker**: `scripts/run_worker.py`'s `run(once=True)` in a thread, so the job
  queue, lease, PDF rendering (PyMuPDF at 200 dpi), 2x2 splitting, scan manifest,
  validation, usage rows and batch and file statuses are all the production code.
- **A stand-in reader** in place of the AI reader. Before the upload it is told what each
  crop says, keyed by the crop's exact pixels (the PDF is rendered and split by the same
  functions as the worker, in a separate process), so it reads every ticket perfectly. It
  waits `--reader-ms` per call (default 0), counts its calls, and can be made to fail
  chosen tickets. It reports no token counts.
- **Fictional tickets** drawn by `tests/sample_data.make_page`, four to a letter page, one
  PDF. No two tickets share a number or a load (date, material, tons) unless they are
  deliberately the same ticket, so every duplicate warning counted is a real one.

What each figure means:

| Figure | How it is measured |
|---|---|
| Upload s | Wall time of the `POST /api/batches` request, in-process. No network transfer. |
| 1st read s | From the worker starting to the stand-in's first call. |
| 1st reviewable s | From the worker starting to the first ticket in the batch being saved as `needs_review` (the benchmark looks at the database every 20 ms). |
| Complete s | From the worker starting to the batch leaving `processing`. |
| Reads per ticket, per page | Stand-in calls divided by tickets uploaded and by pages. |
| Peak RSS | `resource.getrusage` high-water mark. On macOS this undercounts, because it leaves out memory the system has compressed, which on this 8 GB machine was a lot. |
| Footprint | macOS physical footprint from `vmmap --summary` on the process itself: "now" just before the upload, "peak" at the end. vmmap prints values over 1 GB with one decimal in GB, so 2560 MB means "2.5 GB", within 50 MB. The documents are drawn in another process so drawing them doesn't count. |

In production the worker looks for new jobs every 1.5 s (`run_worker.py --poll-seconds`
default) and the upload page refreshes every 1.5 s while a batch is read
(`frontend/app/upload/page.tsx`). The benchmark starts the worker straight after the upload,
so a person could wait up to about 3 s more than "1st reviewable" shows, plus the upload's
network transfer.

## Intake, stand-in reader at 0 ms

Runs 2 and 3, one after the other, same code. "Default limits" are the settings at this
commit (`MAX_PDF_PAGES=20`, `MAX_TOTAL_PDF_PIXELS=70,000,000`). "Limits raised" sets both
just high enough for the file; **it is not a configuration anyone runs**, it shows what the
pipeline would do. "Split" is what a person has to do at the defaults: the same tickets as
PDFs of at most 18 pages in one upload.

| Configuration | Tickets | Pages | Files | MB | Upload s | 1st read s | 1st reviewable s | Complete s | Reads/ticket | Reads/page | Result | Footprint MB, before → peak |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| One PDF, default | 10 | 3 | 1 | 0.5 | 0.14 / 0.07 | 0.58 / 0.34 | 0.64 / 0.37 | 0.72 / 0.39 | 1.0 | 3.33 | 10 to review | 117 → 251 / 245 |
| One PDF, default | 50 | 13 | 1 | 2.3 | 0.19 / 0.12 | 1.46 / 1.23 | 1.48 / 1.29 | 1.70 / 1.44 | 1.0 | 3.85 | 50 to review | 120 → 650 / 654 |
| One PDF, default | 72 | 18 | 1 | 3.2 | 0.12 | 2.69 | 2.75 | 3.20 | 1.0 | 4.0 | 72 to review | 121 → 862 |
| One PDF, default | 76 | 19 | 1 | 3.4 | 0.10 | - | - | 1.13 | 0 | 0 | **file failed**: "PDF is too large to process safely" | 122 → 583 |
| One PDF, default | 100 | 25 | 1 | 4.5 | 0.13 / 0.11 | - | - | 0.05 / 0.06 | 0 | 0 | **file failed**: "PDF contains 25 pages; the per-file limit is 20" | 122 → 138 |
| One PDF, limits raised | 100 | 25 | 1 | 4.5 | 0.05 / 0.09 | 2.48 / 2.64 | 2.56 / 2.68 | 3.54 / 3.19 | 1.0 | 4.0 | 100 to review | 122 → 1,126 ("1.1 GB") |
| Split, default | 100 | 25 | 2 | 4.5 | 0.04 / 0.04 | 0.97 / 0.94 | 0.99 / 0.99 | 2.22 / 2.09 | 1.0 | 4.0 | 100 to review | 119 → 732 / 650 |
| One PDF, default | 300 | 75 | 1 | 13.5 | 0.09 / 0.12 | - | - | 0.06 / 0.06 | 0 | 0 | **file failed**: "PDF contains 75 pages; the per-file limit is 20" | 130 → 172 |
| One PDF, limits raised | 300 | 75 | 1 | 13.5 | 0.08 / 0.14 | 6.73 / 9.07 | 6.75 / 9.10 | 8.18 / 10.86 | 1.0 | 4.0 | 300 to review | 131 → 2,560 ("2.5 GB") |
| Split, default | 300 | 75 | 5 | 13.5 | 0.13 / 0.07 | 1.35 / 1.20 | 1.38 / 1.24 | 6.24 / 6.73 | 1.0 | 4.0 | 300 to review | 130 → 757 / 842 |

The 72- and 76-ticket rows come from one separate run (26 November). Listing a finished
batch's tickets through the API (`GET /api/batches/{id}/tickets`, which also works out
duplicates and prices) took 0.02 to 0.14 s at every size.

What this shows about the current code:

1. **One PDF holds at most 72 tickets (18 letter pages) at the defaults, not 80.** The
   page limit says 20, but 20 letter pages at 200 dpi are 74.8 million pixels, over the
   70 million total, so a 19- or 20-page scan fails with "too large" after rendering 18 pages
   (1.1 s here). 100 and 300 tickets in one PDF are refused. **The upload is accepted
   (200) and the file fails only in the worker**, so the person finds out after uploading.
   The 25-page error then says the limit is 20, which a 20-page letter scan would also fail.
2. **The first ticket can't be reviewed until the whole file is rendered and split.** The
   worker records every slot of a file before it reads any of them
   (`pipeline.prepare_file`). With an instant reader the first ticket appears after 1.3 to
   1.5 s for 50 tickets, 2.7 s for 72, and 6.7 to 9.1 s for 300 in one PDF, against 1.2 to
   1.4 s when the 300 are split into five files (only the first file has to be rendered).
   With a real reader, add one read to these.
3. **Memory grows with the file, not the batch.** Every rendered page and every crop of a
   file is held until the file is read: about 2.5 GB at peak for 75 pages in one PDF (raised
   limits), 0.86 GB for 18 pages, and 0.65 to 0.84 GB for 300 tickets split into five files.
   The 50 MB `tracemalloc` peak for the 300-ticket file shows almost none of this is Python
   objects: it is page and crop pixels (Pillow and PyMuPDF memory, which `tracemalloc`
   doesn't see). The worker's process was 0.4 GB after the batch finished.
4. **Exactly one reader call per ticket.** No extra reads, and blank slots on a part-filled
   last page are not read (10 tickets on 3 pages: 3.33 calls per page). One usage row is
   written per call.
5. **Upload itself is fast** (0.04 to 0.19 s in-process for up to 13.5 MB). On a real
   connection the transfer dominates: 13.5 MB at 10 Mbit/s is about 11 s (arithmetic, not
   measured).

### With a slower stand-in (500 ms per read)

50 tickets, one PDF, 4 reads at once (`EXTRACTION_CONCURRENCY` default): first read 1.58 s,
first reviewable 2.14 s, complete 8.45 s, 50 reads. That is close to rendering (about 1.5 s)
plus 13 rounds of 4 reads × 0.5 s (6.5 s), as `ocr-performance.md` describes. **This is a
stand-in waiting 500 ms, not a real reader**: real latency varies per ticket, and real
failures and retries add to it.

## Duplicates

8-ticket PDFs (2 pages). Each case starts from its own original upload, whose first ticket
is approved first. Same result in runs 1, 2 and 3.

| Case | Upload | Tickets created | Reader calls | Tickets warned in the review list | Approve the copy of the approved ticket | Approve a copy of an unapproved ticket, then its original |
|---|---|---:|---:|---:|---|---|
| The identical file uploaded again, as a new batch | **accepted** (200) | 8 | 8 | 8 of 8 | refused, 409 | copy 200, then original 409 |
| Page 1 of the original as page 2 of a different PDF, new batch | accepted (200) | 8 | 8 | 4 of 8 (the repeated page only) | refused, 409 | copy 200, then original 409 |
| The same file twice in one upload | accepted (200) | 16 | 16 | 16 of 16 | - | - |

- **Nothing is refused or flagged at upload.** The upload response has no duplicate field,
  the whole file is read again (paid reads in production) and a full set of tickets is made.
- **The review list warns on each copy**, for example "Identical scanned image to an
  existing ticket (batch 1, page 1, ticket 1)". The warning is in the ticket's `duplicates`
  list; the ticket's `flags` don't mention it, and its status is a normal `needs_review`.
- **Approval stops double counting of an identical scan.** Whichever copy is approved first
  wins; approving any other copy is refused with "Identical scanned ticket already verified
  in this company", which a person can't override. The reviewer still has to open and skip
  or flag each copy.
- These checks rely on **identical pixels**. Re-uploading the same file (or a page copied out
  of it) gives identical crops. A second scan of the same paper would not; then only the
  supplier-and-ticket-number check (409 that a person may override) and the same-load
  warning apply. That case needs real re-scans and wasn't measured.

## Failure recovery

40 tickets (10 pages), the stand-in failing every 5th ticket's read (8 tickets). Same result
in runs 1, 2 and 3.

| | Temporary failure (succeeds when read again) | Failure that reading again doesn't fix |
|---|---|---|
| After processing | batch `failed`, file `failed` ("Some tickets could not be extracted"), 32 tickets `needs_review`, 8 `failed` with the flag "Extraction failed - enter manually or retry" | the same |
| Reviewable meanwhile | the 32 | the 32 |
| Recovery | `POST /api/batches/{id}/retry` once: 8 reads (only the failed tickets), batch and file `ready_for_review`, 40 to review | retry: 8 more reads, still 8 `failed`; then one `PATCH /api/tickets/{id}` per ticket with the 6 values from the paper: batch and file `ready_for_review` |
| API calls to recover | **1** | **9** (1 retry + 8 edits) |
| Reader calls in all | 48 | 48 |

- A batch with one failed read shows as **failed** although most tickets are ready, which a
  person could read as "nothing worked".
- Failed reads are counted as usage (48 usage rows for 40 tickets), as they would be billed.
- The worker's own retries of temporary provider errors (`EXTRACTION_MAX_ATTEMPTS`) live in
  the Gemini adapter, which the stand-in replaces, so they aren't exercised here; see
  `ocr-performance.md`.

## Review time (scripted reviewer)

`frontend/tests/e2e/review-bench.sh` worked on this code as it is; **no bench file was
changed.** It seeds one batch with `scripts/make_review_benchmark.py` (not through intake:
one image per ticket, layout `single`, tickets already marked as read, 10% planted misreads
and 5% blanks), starts the real API and the production frontend build, and a Playwright
script works through the batch.

**The reviewer is a script, not a person.** It signs in, opens the batch, and for each
ticket: if the answer file says a field is wrong, it focuses that field (counted as one key,
standing in for a Tab or a click), presses Ctrl/Cmd+A, types the right value, then presses
Ctrl+Enter to approve, and waits until the next ticket's scan and form are on screen. It
never reads the scan or decides anything. It uses no mouse clicks, so clicks per ticket are 0.

| Run | Tickets | Corrected | Total s | s per ticket | Wait after approve, median / p90 / max ms | Keys per ticket | Keys per clean ticket | API requests per ticket |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| No pause | 50 | 7 | 2.26 | 0.045 | 33 / 46 / 59 | 2.06 | 1 | 2.56 |
| No pause | 300 | 47 | 17.5 | 0.058 | 37 / 81 / 292 | 2.31 | 1 | 2.24 |
| 1.5 s pause per ticket (`BENCH_THINK_MS`) | 50 | 7 | 94.4 (19.4 without the pauses) | 0.39 without the pauses | 187 / 716 / 962 | 2.06 | 1 | 2.56 |

The first ticket was on screen 0.33 to 0.36 s after opening the batch. Per ticket, the page
loads the scan once and approves once; corrections add one save each. A correct read costs
one keystroke; a corrected field costs 3 keys plus the characters typed.

The paced run's waits after approving were longer (median 187 ms against 33 to 37 ms). It
ran at a different time, on the same busy machine, and the cause wasn't investigated; re-run
both together before reading anything into it.

**These are the software's share of review time only.** A person's time is reading the scan,
comparing it with the form and deciding, which here is zero (or a fixed 1.5 s pause).

## What only real documents and people can measure

- **Real reader latency, failure rate and cost per ticket**, and how they vary with the
  model and settings (`scripts/benchmark_ocr.py live`, paid, needs authorisation).
- **Real reading accuracy**: how many fields a person must correct. The stand-in reads
  perfectly and the review bench plants a fixed 15%; real misreads depend on handwriting,
  carbon copies, stamps, folds and scan quality.
- **Real contractor scans**: page sizes other than letter, photos instead of PDFs, crooked
  or overlapping tickets on a page, files larger than 18 pages, and what the 2x2 split does
  with them. Memory and rendering time depend on page size and dpi.
- **Re-scans of the same paper** (different pixels), and how often contractors upload the
  same file twice.
- **Human review time per ticket** and how it changes with batch size, fatigue and the share
  of wrong reads. This needs people, timed with their consent (the opt-in
  `REVIEW_TIMING_ENABLED` setting records it in a pilot).
- **Network**: upload transfer time on site connections, and the API over the internet.

## Comparing with typing tickets into a spreadsheet: an estimate method only

**No spreadsheet entry was timed and no timing is claimed here.** A fair comparison needs
the same people entering the same real tickets both ways. What can be counted now:

- Typing a ticket into a spreadsheet means typing all 6 fields Quarvera reads (ticket number,
  date, material, tons, supplier, job). For the benchmark's fictional values that is about
  71 characters (for example `700123`, `2026-10-14`, `No. 57 Stone`, `21.33`,
  `Example Aggregates`, `Fictional Bench Site`) plus 6 Tab or Enter presses: **about 77
  keystrokes per ticket**, before any autocomplete.
- In Quarvera's review, a correctly read ticket takes **1 keystroke** (Ctrl+Enter), and each
  wrong field 3 keys plus its characters. The scripted reviewer averaged 2.06 to 2.31 keys
  per ticket at a planted 15% correction rate.

Keystroke counts are not time: a person still reads every ticket either way, and checking a
pre-filled form is a different task from copying. To turn this into a time comparison:
choose 50 real tickets, have each participant enter half into their usual spreadsheet and
review the other half in Quarvera (swapping halves between participants), time both from
first ticket to last, and count the errors left in each against the paper.
