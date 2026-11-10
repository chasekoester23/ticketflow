# Reading tickets: speed and reliability

## What the worker does now

- **Several tickets at once.** `EXTRACTION_CONCURRENCY` tickets are read in parallel (default 4). Only the Gemini call runs on other threads. Every database write stays on the worker's own thread, so SQLite still sees a single writer.
- **Saved as soon as each is read, in scan order.** A person can review the first tickets while the rest are read, and tickets only ever appear at the end of the list. The upload page shows "Reading tickets: 7 of 20 ready".
- **One pool per batch,** so twenty single-ticket photos are read as fast as five four-up pages.
- **Retries for temporary failures only:** 429, 500, 502, 503, 504, timeouts and dropped connections. `EXTRACTION_MAX_ATTEMPTS` tries (default 3), with exponential backoff and jitter (1 s, then 2 s, capped by `EXTRACTION_RETRY_MAX_SECONDS`). A 429 honours Gemini's requested wait, and every thread slows down together.
- **Never retried:** a refused key (401, 403), an unknown model (404) or a rejected request (400). A refused key stops the batch: no further calls are made, and every ticket says what to fix.
- **One failure doesn't stop the rest.** A failed ticket says what kind of failure it was. "Retry failed files" reads only the failed tickets again. If someone types a failed ticket in by hand while it is being read again, their values win.
- **Cost tracking.** Each ticket's reading records the model, attempts, seconds, image size and Gemini's token counts in `raw_extraction["_ticketflow"]`.

## The image Gemini receives

The default is `GEMINI_IMAGE_FORMAT=webp_lossless`: exactly the same pixels as PNG, so it can't change what's read. These figures come from `python scripts/benchmark_ocr.py encode --scan-like`, on fictional tickets with scanner noise added:

| Format | Median per ticket | Full page | Pixels |
|---|---:|---:|---|
| png (before) | 1,080 KB | 3,093 KB | identical |
| webp_lossless (default) | 637 KB | 1,908 KB | identical |
| jpeg, quality 92 | 196 KB | 515 KB | lossy |

With tickets read in parallel, upload speed matters. Twenty tickets as PNG is about 22 MB, which takes roughly 17 s on a 10 Mbps upload. JPEG would cut that by another two-thirds, but it's lossy, so only switch after `live` shows it reads your handwriting as well.

## Measured: TicketFlow's own pipeline

`python scripts/benchmark_ocr.py simulate`, on the previous code (`ded8d75`) and on this branch, on the same Mac. The worker pipeline and the retry code are real; Gemini is a stand-in that takes about 4 s per ticket. **These are not Gemini's real speeds.**

| Tickets | Before | After, 4 at a time |
|---:|---:|---:|
| 1 | 3.8 s | 2.8 to 3.8 s |
| 4 (one four-up page) | 12.8 s | 5.1 s |
| 20 (five pages) | 76.7 s | 24.6 s |
| 20, with 25% of calls failing with a temporary 503 | 71.4 s, **7 tickets failed** | 30.4 s, **none failed** (27 calls) |

TicketFlow's own overhead, with a near-instant reader, is 0.8 s for 20 tickets. Total time is therefore about *(tickets ÷ concurrency, rounded up) × Gemini's time per ticket*. The single-ticket goal of 2 to 5 s depends entirely on how fast the model answers one ticket.

## Choosing a faster model, with evidence

`gemini-3.1-pro-preview` thinks before it answers, and that's most of its time. A Flash model, or `GEMINI_THINKING_LEVEL=low`, is likely much faster, but it may misread handwriting. The default stays as it is until a live comparison on your own labelled tickets shows equal field accuracy and equal exact tonnage:

```bash
python scripts/evaluate_extraction.py prepare --scans evaluation_data --layout grid_2x2   # then label from the paper
python scripts/benchmark_ocr.py live --manifest evaluation_data/labels.json \
  --models gemini-3.1-pro-preview,gemini-3-flash-preview --concurrency 4 \
  --price gemini-3.1-pro-preview=IN,OUT --price gemini-3-flash-preview=IN,OUT \
  --i-authorize-paid-calls
```

Prices come from Google's price page, in US dollars per million input and output tokens. The report gives wall time, median and 95th-percentile time per ticket, field accuracy, exact tonnage, failures, tokens and estimated cost per model. It sends your images to Google and costs money, so it refuses to run without `--i-authorize-paid-calls`.

## Settings

| Setting | Default | Notes |
|---|---|---|
| `EXTRACTION_CONCURRENCY` | 4 | Lower it if Gemini returns 429s; free-tier keys may need 1 or 2 |
| `EXTRACTION_MAX_ATTEMPTS` | 3 | 1 turns retries off |
| `EXTRACTION_RETRY_BASE_SECONDS` | 1.0 | First wait; doubles each try |
| `EXTRACTION_RETRY_MAX_SECONDS` | 20 | Longest single wait, including a 429's request |
| `EXTRACTION_TIMEOUT_SECONDS` | 60 | Per request |
| `GEMINI_IMAGE_FORMAT` | webp_lossless | png, webp_lossless or jpeg |
| `GEMINI_MAX_IMAGE_SIDE` | 1800 | Pixels on the longest side |
| `GEMINI_THINKING_LEVEL` | unset | minimal, low, medium or high; unset uses the model's own default |

## Limits

- No real Gemini call was made to produce these numbers. Real speed, the real rate of 503s and the effect of model, image format or thinking level on handwriting can only come from `live`.
- A worker killed mid-batch keeps every ticket it saved and reads those again never. Tickets that were being read at that moment are read again, up to four times the concurrency. That's what the parallel reading costs.
- Rate limits depend on your Google account's tier, so concurrency above what the tier allows only produces 429s, which are retried more slowly.
