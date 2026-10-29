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
