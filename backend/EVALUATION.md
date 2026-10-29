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
