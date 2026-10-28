# TicketFlow (Phase 1 vertical slice)

Upload a scanned PDF of delivery tickets -> split into individual tickets -> extract six fields with a vision model -> review/correct/verify -> download Excel.

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
pip install fastapi "uvicorn[standard]" python-multipart sqlalchemy alembic pydantic pydantic-settings \
            pymupdf pillow opencv-python-headless openpyxl anthropic pytest httpx reportlab
cp .env.example .env          # then put your key in ANTHROPIC_API_KEY=...
uvicorn app.main:app --reload --port 8000

# Frontend (second terminal)
cd frontend
npm install
npm run dev                   # http://localhost:3000
```

Try `backend/sample_4_tickets.pdf` (synthetic, printed-style, not handwriting) with layout "Four tickets (2x2)".

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

ZIP upload, manual crop boxes, projects, dashboard, filtering, auth, Alembic migrations.

Note: the SQLite file is created with create_all, which does not alter existing tables. If you ran an earlier version, delete backend/ticketflow.db once.
