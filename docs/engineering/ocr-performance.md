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

## Digital PDF tickets: read from their own text, no reader call

A ticket a supplier's system printed straight to PDF carries its words as text. Before a crop is sent to the reader, `services/ticket_text.py` looks at the PDF's text layer inside that crop (PyMuPDF word positions, mapped from the rendered pixels with the same zoom the render used). If labelled fields ("Ticket No", "Date", "Supplier", "Material", "Net Tons" / "Quantity 9.5 CY" / "Net 49,000 lb", "Gross", "Tare", "Truck", "Job", "Job No") give ticket number, date, supplier, material, quantity with its unit and job location, each once and without conflict, the ticket is saved from that text. No reader call is made, no `extraction_usage` row is written, and it doesn't count against `MONTHLY_TICKET_READS_PER_ORGANIZATION` (a company over its limit still gets digital tickets read). `raw_extraction["source"]` is `"pdf_text"`, or `"reader"` for a reader call. The ticket is still unapproved and its quantity unconfirmed: a person reviews it exactly as before.

Anything in doubt goes to the configured reader, exactly as before: a missing field, a field printed twice with different values, a quantity with no unit or an unknown one, a net weight that disagrees with the tons, an unparseable number, a rotated page. It never applies to scans: a scanned page has no text layer; a scan carrying a scanner's OCR text (an image under the crop) and hidden text (invisible, transparent or white) are refused too, so text a person can't see never becomes the record. Pounds stay pounds in what was read and are saved as tons by the same rule as a person's entry (`units.pounds_to_tons`, keeping `source_quantity`/`source_unit`). A supplier with no "Supplier:" label is named only when exactly one of the company's own catalog suppliers is a whole line of the ticket.

Measured, synthetic (`reportlab` tickets drawn for the tests, not real supplier PDFs): 20 letter pages, 80 tickets in four layouts (tons, pounds with gross/tare, cubic yards, labels and values in separate columns), with one ticket in five pages missing its job: **76 read from text, 4 reader calls** instead of 80. The text layer costs about 3 ms per page on the development Mac. Scanned fixtures (`make_scan_pdf`) have no text layer and are all sent to the reader. How many real supplier tickets are digital, and labelled this way, is unknown until measured on real files.

The PDF is read from storage a second time for its text (once per file). If AI reading is turned off, the batch still stops before reading, so a digital PDF isn't read from text either; that is unchanged.

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
