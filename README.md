# TicketFlow (Phase 1 vertical slice)

Upload scanned delivery tickets to a project -> split into individual tickets -> extract six fields with a vision model -> review/correct/verify -> price -> dashboard, filterable records, Excel export.

Pages: Dashboard, Projects, Upload & Review, Records (filters + filtered export), Suppliers & Pricing.

## Architecture (deliberately small)

```
backend/   FastAPI + SQLAlchemy (SQLite) + PyMuPDF/Pillow + openpyxl
  app/services/splitter.py     render PDF pages, split single / 2x2, blank-quadrant detection (pure functions)
  app/services/extraction.py   vision extractor behind a small interface; strict JSON; nulls instead of guesses
  app/services/validation.py   date/tonnage normalization + deterministic review flags
  app/services/pipeline.py     file -> pages -> crops -> Ticket rows; per-file failure isolation; retry
  app/services/export.py       openpyxl workbook (Delivery Records + Summary), Decimal money math
  app/routers/api.py           upload (type + magic-byte + size checks), review, verify, skip, audit, export
  tests/                       unit + end-to-end tests, synthetic ticket fixtures
frontend/  Next.js 15 + Tailwind: upload, status polling, side-by-side review, export
```

Key decisions: one FastAPI process (background tasks, no queue yet); SQLite with models that port to Postgres;
tonnage is always human-confirmed; no confidence scores, only deterministic flags; stored files get random internal names.

## Run it

```bash
# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then put your key in ANTHROPIC_API_KEY=...
alembic upgrade head          # safe additive schema migration, required before starting
uvicorn app.main:app --reload --port 8000

# Frontend (second terminal)
cd frontend
npm install
npm run dev                   # http://localhost:3000
```

Create a project first (Projects page), then try `backend/sample_4_tickets.pdf` (synthetic, printed-style, not handwriting) with layout "Four tickets (2x2)".

Privacy note: with a key set, cropped ticket images are sent to the Anthropic API for extraction. Disclose this to
any customer before using their documents.

## Ticket review workflow

Every uploaded ticket begins in **Needs review**. The AI never automatically
accepts quantities or costs. After processing a batch, open Upload & Review:

1. Compare the **original cropped scan** (left) to editable extracted values
   (right). Use image zoom, rotation or Open full image to inspect handwriting.
2. Correct any fields and click **Accept & next** to save, validate, verify and
   advance to the next unreviewed ticket. The backend rejects incomplete or
   invalid tickets; verified quantities alone count in project financial totals.
3. Click **Flag for later** for ambiguous numbers or an unreadable image. This
   saves edits, defers approval, and keeps the ticket visible in the separate
   Flagged queue as well as Records and Excel. Flagged values **do not** count
   as verified tonnage/cost.
4. Switching tickets with unsaved edits prompts before discarding changes.
   Browser navigation/closing warns about unsaved data. Use Save changes to
   store a correction without approving it.
5. **Skip non-ticket** excludes a blank or irrelevant scan from financial totals
   and Excel; this action requires confirmation.

Use `/upload?batch=<batch_id>&ticket=<ticket_id>` to open a specific record.
Automatic extraction and human acceptance remain separate by design.

**Manual browser QA before a customer pilot:** open the sample 2×2 PDF, verify
image/text alignment and navigation, deliberately edit a field and try to leave
without saving, flag a ticket, revisit it from Records, correct and accept it,
confirm the progress counters, export the workbook, and repeat at mobile/tablet
widths. Automated backend and build checks do not replace these user tests.

## Check your API key

```bash
cd backend && python scripts/check_api_key.py
```
Prints OK or a specific failure (missing key, rejected key, unknown model). The UI also shows a banner if no key is set.

## Test

```bash
cd backend && source .venv/bin/activate && pytest -q
```

## Not built yet (by design)

ZIP upload, manual crop boxes, authentication/multi-company, deployment, invoice matching.

## Price integrity and existing databases

**Do not delete your database to update the app.** Before migrating an existing
backend/ticketflow.db, stop the backend and make a backup. Then run
`cd backend && alembic upgrade head` from your activated Python environment.
You can also use DATABASE_URL to migrate an alternate database.

- When a ticket is verified, TicketFlow snapshots the supplier/material rate
  (or the manually entered override). Future changes to the supplier rate table
  **do not** reprice this verified ticket.
- If a rate is missing at verification, the ticket is explicitly marked unpriced.
  Adding a rate later does not silently assign a cost. Enter the documented
  rate as an override and re-verify to lock the price.
- Existing tickets verified before this migration are marked **legacy**:
  their original verification-time price was never recorded. TicketFlow
  deliberately does not substitute today's price as historical fact. Enter the
  documented historical price as an override, save, then re-verify.
- Changing any field on an already verified ticket requires re-verification
  and a new price snapshot. This is an intentional correction, not retroactive
  mutation due to changes in the global rate table.
- The initial Alembic revision preserves existing tables and records and adds
  three nullable price-snapshot columns. The production migration system is now
  in `backend/migrations/`. Migrations must be applied before deploying code
  that requires the new columns.

**Scope:** pricing currently covers material-only cost; it excludes freight,
taxes, and project/date-specific contractual rate history. Historical source
prices that were never stored cannot be recovered automatically.

**Security:** TicketFlow still has no authentication or company data isolation.
Do not expose it publicly or upload third-party confidential documents until
those controls have been implemented and reviewed.
