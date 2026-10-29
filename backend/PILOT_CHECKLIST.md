# TicketFlow first real-ticket test — operator checklist

This is a local evaluation, **not a claim that real handwriting works**.
Real-model performance has not yet been measured. AI inference is OFF for
manual-entry batches, and no ticket is approved until a person accepts it.

## Before you get the scans

- Obtain permission from the ticket owner/project to use the documents. If
  another employer or contractor owns the tickets, ask first. Redact personal
  identifiers and sensitive project/rate information if appropriate.
- Never send confidential scans to an AI provider without authorization to
  transmit them. TicketFlow's local manual-entry mode sends **no model
  requests**; the API/worker and browser still run on your machine.
- Make a private, Git-ignored folder: `backend/evaluation_data/`. **Never**
  commit real scans, annotation JSON, client names, or API keys to GitHub.
- Do not share real ticket images in a public issue, PR, or screenshot.
- Save a backup copy of the original, unmodified scans so crop mistakes can
  be diagnosed without destroying source documents.

## Which tickets to collect

1. Start with 5–10 permissioned tickets to inspect layout and cropping.
2. Later build a diverse evaluation set of at least 30–50 independently
   hand-labeled tickets before estimating AI extraction performance.
3. Include multiple suppliers, handwriting styles, printed fields, faded
   and rotated scans, odd dates and tonnage formats, and known difficult
   examples. Avoid measuring only easy, clean documents.
4. Capture the expected source values for six fields: number, date, material,
   net tons, supplier, job. Record ambiguous/unreadable fields as such;
   never guess from the model's output.

## Local manual-entry rehearsal (no AI key)

From the root TicketFlow project folder after Batch #12/#13 is merged:

```bash
# Back up existing database FIRST, then migrate (see README).
# Terminal 1:
cd backend
source .venv/bin/activate
python -m uvicorn app.main:app --reload --port 8000
```

```bash
# Terminal 2:
cd frontend
npm run dev
```

```bash
# Terminal 3 — REQUIRED for manual scan crop preparation:
cd backend
source .venv/bin/activate
python scripts/run_worker.py
```

Practice with a synthetic scan before touching real documents:

```bash
# In backend/ — creates a clearly labeled FICTIONAL PDF:
python scripts/make_demo_tickets.py
```

1. Open `http://localhost:3000`, sign in, and choose a **test project** (or
   create one with a clear demo code such as `DEMO-001`).
2. Upload `backend/demo_output/synthetic-4up.pdf`, choose **Four tickets
   per page** and **Manual entry**, then queue.
3. Confirm the worker produces four scan crops with **blank fields**, not
   fake extracted values. If some crops are missing, inspect the source and
   splitting/blank detection before proceeding.
4. Select ticket 1 and compare every field to the fictional original. Fill
   all six. Save changes, then use **Accept & next**. Approval must fail if
   required fields remain missing.
5. Verify the review queue, duplicate warnings, date and tonnage validation,
   and confirmed-only dashboard totals. Test a flag and a correction.
6. Export Excel and check ticket count, material, tons, and costs against the
   numbers you actually entered. Money should not appear for unpriced
   tickets and should remain locked for approved, priced ones.
7. Practice keyboard navigation: **Alt + Left/Right** outside text fields,
   **Cmd + Enter (Mac) / Ctrl + Enter (Windows/Linux)** for explicit
   Accept & next. A shortcut must never bypass server verification.

Do not import fictional tickets into a real customer project.

## First real scans — local manual review

- Use a **separate, permitted project** for real scans.
- For multiple tickets on one scanner page, choose **Four tickets per page**;
  for a single photo/ticket per page, choose **One ticket per page**.
- Check source orientation, clipping, and that the number of produced crops
  matches the true number of tickets. Cropping is currently fixed 2×2 or
  single-page — not automatic document detection.
- Record any incorrect splitting or blank quadrant detection. Keep manual
  values faithfully transcribed; no invented model values.
- Compare exports against physical originals before relying on costs.
- Track approximate minutes per ticket, missing fields, recurring scan
  problems, and how many times the user had to correct information.

## When you are ready to measure actual AI extraction

Follow `backend/EVALUATION.md` for independently labeled ground truth,
local `prepare`/`check` commands, model configuration, and the separate
paid `run` phase. **Do not compute an accuracy percentage from printed
synthetic fixtures.** Do not promise customer-facing accuracy or savings
before the real-ticket results justify them.

## Known limitations

Local SQLite and a single-machine worker are not production hosting.
Manual mode still requires the separate worker process for crop creation.
Scanned PDFs are rasterized; very large multi-page PDFs may be memory
intensive. The app has no automatic photo perspective correction, no
source-independent duplicate certification, and no measured handwriting
recognition accuracy yet. Verify money and quantity records against
authorized originals before using them commercially.
