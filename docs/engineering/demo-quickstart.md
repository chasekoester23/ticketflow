# Fictional demo: 4-up tickets and an invoice

Everything here is made up. It uses no AI and no network, and doesn't need an API key. Use a separate database so your real data isn't touched.

## Run it automatically

This starts a real backend, worker and frontend on a temporary database, runs the whole workflow in a browser, and removes everything afterward. It needs Node 22, `npm ci` in `frontend/`, and Playwright:

```bash
cd frontend
npm install --no-save --package-lock=false @playwright/test@1.61.0
npx playwright install chromium
tests/e2e/run.sh
```

It never reads `backend/ticketflow.db`, and it blanks API keys in its own environment.

## See it with thousands of records

To look around a busy company, with misspellings, duplicates, blank tickets and 12 planted invoice mistakes:

```bash
python scripts/make_synthetic_dataset.py --tickets 5000
```

Run it after `create_demo_workspace.py` in the steps below. It refuses any database without "demo" in its file name. Open **Invoice comparison** to see each planted mistake explained, and **Name cleanup** for the misspellings.

## Run it by hand

Use three terminals, all with the same demo settings:

```bash
cd backend && source .venv/bin/activate
export DATABASE_URL=sqlite:////tmp/ticketflow-demo.db STORAGE_DIR=/tmp/ticketflow-demo-files
python -m alembic upgrade head
python scripts/create_demo_workspace.py          # demo-owner / demo-password-not-real
python scripts/make_demo_kit.py --output-dir /tmp/ticketflow-demo-kit
python -m uvicorn app.main:app --port 8000
```

```bash
cd backend && source .venv/bin/activate
export DATABASE_URL=sqlite:////tmp/ticketflow-demo.db STORAGE_DIR=/tmp/ticketflow-demo-files
python scripts/run_worker.py
```

```bash
cd frontend && npm run dev
```

Your real database isn't used as long as `DATABASE_URL` is set in the backend terminals. `create_demo_workspace.py` refuses any database without "demo" in its file name.

Before starting, add project `24-115`, supplier `Example Aggregates`, material `No. 53 Crushed Stone`, and a $30 rate.

## Script (about 5 minutes)

**The problem.** "Offices get paper tickets, often four to a scanner page. A faint one can vanish, and a total can look right while being wrong. Invoices can have the right total and the wrong tickets."

1. **Ticket intake.** Upload `demo-tickets.pdf`, choose the project, leave Manual entry selected, and enter **3** paper tickets.
   "We counted three pieces of paper."
2. **Scan check.** It says *You expected 3, but 2 are on record.* Slot 2 is highlighted as faint.
   "A blank-page test would have dropped this. Nothing is discarded, and it blocks a clean result until a person decides." Click **This is a ticket**. It now reads *Matches your paper count*.
3. **Review.** Open each ticket and enter what the scan says: `10482` 24.50 t, `55120` 18.40 t, `10483` 22.10 t, all dated 2026-10-15, No. 53 Crushed Stone, Example Aggregates, North Lot. Click **Accept & next**.
   "Nothing counts until a named person accepts it, and the price locks then."
4. **Export.** **Delivery records → Export**. Point out the approver columns.
5. **Invoice PDFs.** Upload `demo-invoice.pdf`.
   "The original is beside an editable draft. Freight and the invoice total aren't folded into the material line. Nothing counts until confirmed." Click **Confirm line**.
6. **Invoice comparison → Details.** The totals agree exactly, and the result is still **Difference found**: `55999` is **Not in TicketFlow**, and `55120` is **Delivered, not on invoice**.
   "A totals check would have passed this."
7. **Confirm 2 clean matches.** "A person confirms what's clean, and a delivery can only be linked to one invoice line. The wrong one can't be linked." Open a **Scan** link to show traceability.
8. **Pilot report.** It shows what's on record and what still needs a person. "There's no percent-complete number on purpose."

**What to say about accuracy:** the demo uses manual entry and a fictional invoice layout. Reading accuracy on real tickets and real supplier invoices hasn't been measured.
