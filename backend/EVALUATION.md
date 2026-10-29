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
  TicketFlow has **no user authentication or tenant isolation yet**.
- The extraction model receives a cropped image of each scanned ticket when
  you run `run`. Review your AI provider's data-handling terms and the
  customer's requirements before doing that. `prepare` and `check` make
  **zero network/model API calls**.
- Use a diverse, deliberately chosen pilot sample: different suppliers,
  handwriting styles, dates, material formats, scan quality, rotations,
  multi-page PDFs, and known difficult tickets. Start with 25–50; expand after
  seeing the error types. The sample is not automatically representative.

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

Configure `ANTHROPIC_API_KEY` in `backend/.env` and verify model access:

```bash
python scripts/check_api_key.py
python scripts/evaluate_extraction.py run --manifest evaluation_data/labels.json
```

This makes **one paid extraction call per labeled ticket**. Start with a
small, consented sample and monitor your API usage/billing separately. By
default the result is written to `evaluation_data/accuracy-report.json`;
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

## Step 4: Use the findings

First prioritize **wrong numbers** (especially tons), then missed ticket
numbers and dates, then supplier/material normalization. For each recurring
error type, add more diverse labeled scans and evaluate again. Do not tune
the prompt using the entire benchmark and then present a retest on that
same sample as independent evidence; keep a separate holdout set.

Regardless of measured accuracy, TicketFlow's human **Accept & Next**
approval remains mandatory for every extracted ticket. Never auto-approve
a delivery quantity based on benchmark scores.
