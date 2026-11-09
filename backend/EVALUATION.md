# Real-ticket evaluation protocol (local only)

TicketFlow's first five development batches were checked using synthetic
printed tickets and a fake extractor. **Those tests do not tell us how often
the real AI reads handwriting correctly.** This workflow measures a real
provider on an independently hand-labeled sample.

## Before handling any actual tickets

- Get permission to use the documents. Remove/redact personal, commercial,
  or otherwise confidential information wherever possible. If a ticket belongs
  to another organization, get authorization before uploading it to any AI API.
- Keep all real PDFs, annotations, and accuracy reports in
  `backend/evaluation_data/`. That directory is Git-ignored. Do **not** commit
  or publish actual tickets or any API keys. Do not use a public deployment:
  The local app has login and company-scoped records, but is **not yet
  production hardened or approved for public deployment**. Keep real
  ticket data off public repositories and demonstrations.
- The selected extraction provider (Gemini by default) receives a cropped image
  of each scanned ticket when you run `run`. Verify the selected account's
  data-handling terms, billing tier, privacy requirements, and customer consent
  before doing that. Free-tier data policies can differ from paid-tier policies.
  `prepare` and `check` make **zero network/model API calls**.
- Use a diverse, deliberately chosen pilot sample: different suppliers,
  handwriting styles, dates, material formats, scan quality, rotations,
  multi-page PDFs, and known difficult tickets. Start with 25–50; expand after
  seeing the error types. The sample is not automatically representative.

## Practice tickets you write yourself (synthetic, no permission needed)

Before real tickets are available, you can measure the pipeline on blank
practice tickets that people fill in by hand. **Results are a best case**:
few writers, clean paper and our own layouts. They are labeled
`synthetic_practice_self_written` in the report and must never be quoted as
accuracy on real supplier tickets.

1. **Print.** From `backend/`:
   ```bash
   python scripts/make_practice_tickets.py --sheets 5
   ```
   This writes `evaluation_data/practice/practice-tickets.pdf` (4 tickets per
   page, three different layouts) and a pre-numbered
   `evaluation_data/practice/answer-key.csv`. Print at **100% scale** (not
   "fit to page").
2. **Fill them in like a real driver would.** Use several different people
   if you can, write quickly, mix pens and pencil, vary formats (`10/3/26`,
   `2026-10-03`; `24.5`, `24.50`), fill the gross/tare pound fields too, and
   include look-alike digits (1/7, 0/6, 5/S) and a crossed-out correction.
   Fold, smudge or photocopy a few. The **net weight is in tons**.
3. **Scan the sheets in order** (sheet 1 first) into one PDF in
   `evaluation_data/practice/`, e.g. `scanned-practice.pdf`.
4. **Fill the answer key** in Excel or Numbers and save as CSV. For each
   ticket, type what is **actually legible** on the paper. Write
   `UNREADABLE` for a field even a person cannot read. Dates may be
   `YYYY-MM-DD` or `M/D/YYYY`; net tons is a plain number (no units). Leave a
   row completely empty for a ticket nobody filled in; it is skipped. Do not
   change the `practice_id`, `sheet` or `slot` columns.
5. **Convert and check** (no API calls):
   ```bash
   python scripts/practice_key_to_labels.py \
     --key evaluation_data/practice/answer-key.csv \
     --scan evaluation_data/practice/scanned-practice.pdf
   python scripts/evaluate_extraction.py check --manifest evaluation_data/practice/labels.json
   ```
   Then confirm with the scan preview (below) that each crop shows the
   practice ID you expect; a sheet scanned out of order would mislabel it.
6. **Run the model** (uses API quota; see Step 3 below):
   ```bash
   python scripts/evaluate_extraction.py run --manifest evaluation_data/practice/labels.json --pause-seconds 6
   ```
   The benchmark uses `GEMINI_MODEL` from `backend/.env` unless `--model` is
   given. On the free tier, `--pause-seconds` keeps per-minute limits from
   being counted as extraction failures, and the free tier's daily limit may
   require splitting a large run across days. Results from a free-tier
   "Flash" model do not predict a paid "Pro" model's accuracy.

If `check_api_key.py` fails, it now prints the provider's reason and a
specific fix (key removed from the output). `python scripts/check_api_key.py
--list-models` shows which models your key can see without using quota.

## No-cost preflight: inspect every physical crop before evaluating handwriting

When you receive authorized paper tickets, first run the local **scan preview**
without configuring a Gemini key:

\`\`\`bash
# From backend/, with scans privately stored in the git-ignored folder:
python scripts/preview_scans.py --scans evaluation_data --layout grid_2x2
\`\`\`

Use \`--layout single\` if each file/page contains only one whole ticket. It
creates a new \`evaluation_data/scan_preview/\` folder containing:
- \`pages/page-*.jpg\`: side-by-side previews labeled by crop position.
- \`crops/crop-*.png\`: full-resolution individual crops for reading faint text.
- \`reconciliation.csv\`: a row for **every potential position**, including
  blank-looking crops, with \`human_ticket_present\` left empty.

**Compare each crop against the original scan.** Mark each row's
\`human_ticket_present\` as \`yes\`, \`no\`, or \`uncertain\`, with notes if
needed. The \`looks_blank\` column is only a heuristic prediction. Even faint
but genuine handwritten tickets can be labeled blank by software. Confirm the
actual sheet count before claiming TicketFlow captured every delivery. Correct
orientation or rescans as needed; do not assume a nonblank crop contains a
ticket or a blank-looking crop does not.

The preview script never calls Gemini, never connects to a server, never
touches the database, and never modifies the original scans. It refuses to
overwrite an existing preview directory. These previews **still contain
possibly sensitive information**: keep them local, consented, and inside the
git-ignored evaluation directory. If the folder already exists and you need
another run, use a NEW direct subfolder name with
\`--output evaluation_data/preview-second-pass\` rather than deleting old work.

Run \`prepare\` only after deciding which crops are real tickets. The generated
labels file still needs separate independent human annotation; it will not
automatically take labels from the reconciliation CSV.

## Step 1: Prepare scans and ground truth

From the `backend/` directory:

```bash
mkdir -p evaluation_data
# Privately copy scans into backend/evaluation_data/
python scripts/evaluate_extraction.py prepare --scans evaluation_data --layout grid_2x2
```

For already cropped individual tickets use `--layout single`.
The command writes `evaluation_data/labels.json`. **It contains blank,
unlabeled fields** (an intentionally incomplete annotation template).

Open each original ticket image, identify the correct page/crop, and fill
**all six** `expected` values manually. Never just copy the AI output.
Example for one entirely fictional delivery:

```json
{
  "version": 1,
  "tickets": [
    {
      "id": "sample-0001",
      "source": "scan.pdf",
      "layout": "grid_2x2",
      "page": 1,
      "position": 0,
      "expected": {
        "ticket_number": "10482",
        "delivery_date": "2026-10-15",
        "material_type": "No. 53 Crushed Stone",
        "tonnage": "24.50",
        "supplier": "Example Aggregates",
        "job_location": "Project A"
      }
    }
  ]
}
```

Positions for `grid_2x2` are `0` top-left, `1` top-right, `2`
bottom-left, and `3` bottom-right. Pages are 1-based. Remove entries
for blank quadrants or non-ticket pages; do not pretend blank regions are
legitimate ticket samples.

- Use `null` only if the field truly isn't present or is unreadable.
- Use `YYYY-MM-DD` for dates and a **string** in tons for tonnage (e.g.
  `"24.50"`; never enter a pound amount as a ton amount).
- Use the actual written source names, material names, and ticket numbers.
  The benchmark matches whitespace/case-insensitive text, normalized dates
  and exact decimal tons. Do not silently normalize distinct supplier names.
- Use nonidentifying sample IDs instead of actual ticket numbers as IDs.
- If you aren't sure what the ticket says, have a second person independently
  resolve the value or label it null. Do not guess.

## Step 2: Check locally without using an API

```bash
python scripts/evaluate_extraction.py check --manifest evaluation_data/labels.json
```

This validates **every label** and renders **every referenced crop**. It
fails early on blank crops, nonexistent files, missing labels, invalid dates,
duplicate positions, or paths escaping the dataset folder. It performs **no
AI inference and does not establish handwriting accuracy**.

## Step 3: Evaluate the real model

Configure `EXTRACTION_PROVIDER=gemini`, `GEMINI_API_KEY` and (optionally)
`GEMINI_MODEL` in `backend/.env`, then verify API access. **Do not share keys
in chat, commit them to Git, or use unauthorized employer documents.**

```bash
python scripts/check_api_key.py
python scripts/evaluate_extraction.py run --provider gemini --manifest evaluation_data/labels.json
```

For a consented model comparison, the optional alternatives use the **same
independently labeled crop manifest**, prompt and image-size cap:

```bash
# Only if you choose to spend additional API calls and have provider keys:
# python -m pip install -r requirements-benchmark.txt
# python scripts/evaluate_extraction.py run --provider anthropic --manifest evaluation_data/labels.json
# python scripts/evaluate_extraction.py run --provider openai --manifest evaluation_data/labels.json
```

Reports default to separate `accuracy-report-gemini.json` and provider-specific
filenames and refuse to overwrite a previous result. Reports include an SHA-256
fingerprint of the annotation file to make incompatible comparisons detectable.
**The fingerprint is not an anonymization guarantee; keep reports private.**

This invokes the live extractor **once per labeled ticket**; the provider
SDK may retry transient failures, and real API charges depend on usage. Start with a
small, consented sample and monitor your API usage/billing separately. By default the result is written to `evaluation_data/accuracy-report-gemini.json`;
the script refuses to overwrite a previous report. Use
`--output evaluation_data/accuracy-report-second-run.json` for a new run.

The report includes:

- Exact per-field correctness, omissions, mismatches, and unexpected values.
- The fraction of tickets where all six fields match ground truth.
- Model extraction failures and model-call timing per ticket.

Accuracy includes correctly predicted empty/null fields; interpret both
per-field and all-six-field metrics. **Timing excludes human review.** The
report does not contain ground-truth values, predicted text, or ticket images;
the private labels file still contains sensitive data. Neither cost-per-call
nor PDF splitting recall is currently measured—do not claim either metric.

## What the business decision will use

A conservative rollout emphasizes **wrong net tonnage**, not simply average
character accuracy. Count unreadable/missing values separately from mismatches
and invented non-null values, and review ticket numbers, date and material.
Compare wall-clock **human review time** and repeated corrections separately
from API latency. Price and tokens per ticket need to be measured against
actual invoices; this CLI does not collect reliable cost accounting.

If comparing providers, use reports from the SAME source and human labels,
and run:

```bash
python scripts/compare_extraction_reports.py \
  evaluation_data/accuracy-report-gemini.json \
  evaluation_data/accuracy-report-anthropic.json
```

Use a distinct held-out set before claiming that prompt or preprocessing
improvements generalize. Never approve any cost or tonnage without a person.

## Step 4: Use the findings

First prioritize **wrong numbers** (especially tons), then missed ticket
numbers and dates, then supplier/material normalization. For each recurring
error type, add more diverse labeled scans and evaluate again. Do not tune
the prompt using the entire benchmark and then present a retest on that
same sample as independent evidence; keep a separate holdout set.

Regardless of measured accuracy, TicketFlow's human **Accept & Next**
approval remains mandatory for every extracted ticket. Never auto-approve
a delivery quantity based on benchmark scores.

## Whole-workflow evaluation (after a pilot batch is finished)

The crop benchmark above measures the model alone. `scripts/evaluate_workflow.py` measures the product: run the real batch through TicketFlow as the contractor would (upload, review, approve, enter the invoice), then score the result against labels a person wrote from the paper. It makes no model call and opens the database read-only. Run it on a copy.

```bash
cp ticketflow.db evaluation_data/pilot-copy.db
python scripts/evaluate_workflow.py --database evaluation_data/pilot-copy.db --organization 1 \
    --batch 12 --truth evaluation_data/truth.json --out evaluation_data/workflow-report.json
```

`truth.json` (git-ignored, like everything in `evaluation_data/`):

```json
{
  "labelled_by": "initials of the person who read the paper",
  "labelled_independently": true,
  "synthetic": false,
  "api_cost_total": "4.12",
  "slots": [
    {"file": "scan-01.pdf", "page": 1, "position": 0, "ticket": true, "crop_complete": true,
     "expected": {"ticket_number": "1001", "delivery_date": "2026-10-06", "material_type": "Sand",
                  "tonnage": "20.000", "supplier": "Ridgeline Aggregates", "job_location": "Main St"}},
    {"file": "scan-01.pdf", "page": 1, "position": 1, "ticket": false}
  ],
  "invoice_checks": [{"invoice_number": "INV-9", "material": "Sand", "person_found_discrepancy": true}]
}
```

- Label every position on every page, blanks included (`"ticket": false`), or missed tickets can't be counted. Positions are 0 to 3, left to right, then top to bottom.
- `expected` is read from the **paper**, never from TicketFlow's screen. Use `null` for a field nobody can read. The file is refused unless it says `labelled_independently: true` and names who labelled it.
- `crop_complete`: whether the crop TicketFlow showed held the whole ticket. Leave it out if you didn't check.
- `api_cost_total`: copied from the provider's bill for this batch. TicketFlow doesn't record model calls or tokens, so cost isn't measured.
- `invoice_checks`: the invoices a person reconciled by hand, and whether they found a problem. The report compares that with TicketFlow's verdict.

The report gives, as counts and rates: tickets detected and missed, complete crops, **extraction accuracy** per field (the model's untouched output), **final accuracy** per field (what people approved), critical errors in ticket number and tonnage (wrong, as opposed to left blank) before and after review, how many approved tickets people corrected and whether each correction fixed or broke the field, review time (opt-in timings), upload-to-last-approval time per batch, processing jobs, retries and failures, cost per approved ticket, and invoice lines found, missed, or undecided by TicketFlow. Its `limitations` list says what the sample can't show. `"synthetic": true` stamps the report as an engineering check, not evidence.

Repeat `--truth` to score several ground-truth files together. Synthetic and real files are never combined: the script refuses, and so does a file whose slots are marked differently from the file. To build and score fully fictional handwritten scans offline, see `docs/engineering/synthetic-tickets.md` (`python -m synthetic`).
