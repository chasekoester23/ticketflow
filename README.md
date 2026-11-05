# TicketFlow (Phase 1 vertical slice)

**Security status:** Local development/pilot only — not production hardened. See [SECURITY_PRELAUNCH.md](SECURITY_PRELAUNCH.md) for implemented guardrails and launch blockers. Do not host unredacted customer documents publicly yet.

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

Key decisions: separate, local SQLite-backed processing worker (no in-process extraction); SQLite models intended to port to Postgres;
tonnage is always human-confirmed; no confidence scores, only deterministic flags; stored files get random internal names.

## Run it

```bash
# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
test -f .env || cp .env.example .env  # keep any existing local secrets intact
alembic upgrade head          # safe additive schema migration, required before starting
python scripts/create_owner.py --username owner  # ONLY for first-time installation; then add people from the Team page
uvicorn app.main:app --reload --port 8000

# Worker (third terminal; use the same Python environment)
cd backend
python scripts/run_worker.py

# Frontend (second terminal) — use Node.js 22 LTS (the CI version; see frontend/.nvmrc).
# Node 23 fails `next build` with "Unexpected end of JSON input"; npm warns on other versions.
# On this Mac Node 22 is at /usr/local/bin:  export PATH=/usr/local/bin:$PATH
cd frontend
npm install
npm run dev                   # http://localhost:3000
```

Create a project first (Projects page), then try `backend/sample_4_tickets.pdf` (synthetic, printed-style, not handwriting) with layout "Four tickets (2x2)".

Privacy note: with `EXTRACTION_PROVIDER=gemini` and a valid Gemini key, cropped ticket images
are sent to the Google Gemini API for extraction. If you explicitly select Anthropic,
images go to Anthropic instead. **Get document-owner authorization before sending
real contractor scans to ANY external provider.** Paid/free data-handling policies differ.

## Pilot hardening: pricing history, approvers in exports, page headers

- **Price change history.** Adding or removing a supplier or material, and
  setting, changing or removing a unit rate, is recorded with the person and
  time (old and new rate). See **Suppliers & rates → Price change history**.
  Every role can read it; only managers and owners can change prices.
  Changes made before this release were not recorded and are not guessed.
- **Excel exports name the approver.** Two columns are appended at the end
  (existing columns do not move): *Approved By* and *Approved At (UTC)*.
  Tickets approved before individual accounts say *Not recorded*; unapproved
  tickets leave both blank.
- **Frontend security headers** forbid embedding TicketFlow pages in other
  sites (clickjacking) and set conservative browser policies.

**Upgrade an existing local installation** (additive migration `20261103_08`,
which only creates the `pricing_events` table): stop the backend, worker and
frontend; after the PR is merged run `git checkout main && git pull origin main`;
back up with `cp backend/ticketflow.db backend/ticketflow-before-pricing-history.db`;
then `cd backend && source .venv/bin/activate && python -m alembic upgrade head`
and restart all three.

## Team accounts, roles and attributable approvals

Every person now signs in with **their own account**. Approvals, field
corrections, uploads and invoice-line corrections record *who* made them,
so a billing dispute can be traced to a named person. Never share a login.

| Role | Can do |
|---|---|
| **Owner** | Everything, plus add people, change roles, deactivate, reset passwords |
| **Manager** | Approve tickets (locks tons + unit price), skip non-tickets, set rate overrides, manage projects, suppliers/rates and invoice comparisons |
| **Clerk** | Upload scans, enter/correct ticket fields, flag problems. **Cannot** approve quantities, skip tickets or change prices |
| **Viewer** | Read-only records, dashboards and exports |

Roles are enforced by the API (`backend/app/permissions.py`); the browser
only hides buttons a role cannot use. A clerk's edit to an approved ticket
withdraws that approval (it can only *remove* tons from totals) and is audited.

- **Adding people:** an owner opens **Team**, enters a name, username and role.
  TicketFlow shows a one-time temporary password (no email is sent). Hand it
  over in person or by phone. The person must choose their own password at
  first sign-in before anything else works.
- **Leaving or role changes:** deactivating someone, changing their role, or
  resetting their password signs them out everywhere immediately. Owners
  cannot deactivate or demote themselves, and a company always keeps at least
  one active owner.
- **Owner forgot their password:** from a terminal on the server,
  `cd backend && python scripts/reset_password.py --username <name>`. This is
  recorded in the company's account history as a server-terminal action.
- **History before this release** is shown as *"before individual accounts"*.
  TicketFlow deliberately does not guess who approved older tickets.
- Sign-in hardening in this release: lockouts can no longer be flushed by
  spraying usernames, there is a per-account failure limit in addition to the
  per-address one, unknown usernames take the same time as wrong passwords,
  each sign-in issues a new session, and expired sessions are purged.

**Upgrade an existing local installation** (additive migration `20261103_07`):

1. Stop the backend, worker and frontend (**Control+C** in each terminal).
2. After the PR is merged: `git checkout main && git pull origin main`.
3. **Back up the database:** `cp backend/ticketflow.db backend/ticketflow-before-team-accounts.db`
4. `cd backend && source .venv/bin/activate && python -m alembic upgrade head`
5. Restart backend, worker and frontend. Sign in as before.

The migration adds columns and one table; it rebuilds no tables and deletes
no rows. **Every existing account becomes an Owner** (each was created as its
company's owner). Existing audit history, verified tickets, batches and
invoice comparisons keep an empty "who" field. If the backend says *"Team
accounts migration required"*, step 4 has not run against that database yet.

## Scan check: account for every paper ticket

A ticket that never becomes a record is worse than a misread one, because totals look complete without it. After processing, the batch panel on **Ticket intake** shows a **Scan check**: each original page beside all of its slots, with counts of pages, slots, tickets on record, faint, empty and confirmed-blank slots.

- **Nothing is discarded.** Every slot is kept with its image, including blank-looking ones.
- **Faint slots block a clean result** until a person chooses **This is a ticket** or **Confirm blank**. They also keep invoice comparisons for that project at "Incomplete", as do failed files and count mismatches.
- **Enter the paper count** at upload or later. The result reads "Matches your paper count" only when you entered a count and it matches. Without one it says the count isn't confirmed.
- **Marking a slot** creates an unapproved, empty record. In AI batches it makes no AI call: it stays unread until someone uses Retry, or types it in. Neighbouring tickets aren't touched.
- **Rotate 90°** writes a derived image and keeps the original. Approved tickets refuse it until the approval is withdrawn, and the check for identical scans still applies.
- **Who can do what:** clerks can mark tickets, rotate and enter counts. Only managers and owners can confirm blank, since that rules a slot out of the count. Decisions are recorded with the person's name.
- **Older batches** show **Build scan check**, which reads the stored scan and adds slot records without changing any ticket.

The detection thresholds are cautious defaults, not calibrated on real scanner output. Expect to confirm some blank areas by hand.

Upgrading adds migration `20261105_10` (two new tables and one nullable column). The backend refuses to start until it has run: back up `backend/ticketflow.db`, then run `python -m alembic upgrade head`.

## Other spellings of suppliers and materials

Tickets from one quarry can spell its name several ways. Under **Suppliers & rates → Other spellings**, a manager can say that a spelling means a catalog entry. When a ticket uses that spelling exactly (ignoring case and punctuation), the review screen offers **Use your catalog name**.

- **An alias only suggests.** A person applies it and saves, as an ordinary audited edit. It never changes a stored ticket, a rate lookup, or a locked price, and applying it to an approved ticket withdraws the approval as any edit does.
- **Matching is exact, never fuzzy.** One spelling means one thing per company, and aliases never cross companies.
- **What the reader returned stays visible.** The review form shows "Read from the ticket" beside any value that differs.

Upgrading adds migration `20261105_11` (new `name_aliases` table only).

## Invoice PDFs: drafts beside the original

**Invoice comparison → Invoice PDFs** lets a manager upload a supplier invoice and correct draft lines beside the original page. Nothing counts until someone clicks **Confirm line**, which creates a normal comparison through the same code as typed entry, with the same checks and the same math. Typed entry still works as before.

- **What reads the PDF:** the file's own text layer, on this machine. No AI and no network. There is no AI invoice reader yet: writing one needs an API budget and permission to send invoices to a provider. `INVOICE_EXTRACTOR=manual` turns drafting off entirely.
- **Scanned PDFs** have no text layer, so they're flagged and entered by hand beside the original.
- **The reader is deliberately strict.** It only drafts rows with a material, quantity, a recognized unit, rate and amount, plus clearly labelled invoice number, supplier and delivery period. It has been tested on our fictional layout only: expect real invoices to need hand entry or layout-specific work.
- **Nothing is folded in silently.** Freight, tax, totals and other money rows are listed as "not used", and never join a material subtotal.
- **Units are never converted.** A line billed in cubic yards can't be confirmed, because comparison works in tons only.
- **Credits are blocked.** Credit memos and negative amounts are flagged, and nothing is drafted.
- **Duplicates:** the same file can't be uploaded twice, and an invoice number with the same supplier and material can't be confirmed twice. The overlapping-date rule still applies.
- **Wrong file?** Void it. The file and its history are kept, but voiding is refused once a line has created a comparison.
- **History:** every edit, confirmation and rejection is recorded with the person's name. A comparison made this way links back to its original page.

Upgrading adds migration `20261105_12` (three new tables only).

## Invoice comparison (Batch #16)

The **Invoice comparison** page lets a project engineer manually enter one
supplier **material** invoice line at a time: invoice number, project, supplier,
material, inclusive delivery dates, billed tons, billed unit price and material
subtotal (exclude tax, freight and unrelated charges). It then compares those
amounts against the **current verified tickets** in the same company/project,
supplier/material and date range.

- Comparison is **not** invoice approval or authorization to pay.
- The output shows billed-minus-verified tons, billed-minus-verified material
  cost, arithmetic errors, unit-rate mismatches, unverified tickets, and
  unpriced approved tickets. Unpriced, pending, or possibly relevant
  unclassified tickets cannot produce a
  "figures match" result. A match is an arithmetic result, not proof of every
  delivery, invoice authenticity, or authorization to pay.
- A matching ticket keeps its locked historical unit price, not the current
  rate. Removing verification updates comparisons immediately; invoice
  quantities do not enter delivery actuals or dashboards.
- Comparisons are saved in a company-scoped database table and corrections
  are audited. Different material lines from the same invoice can be added,
  but **overlapping date windows for the same project/supplier/material are
  rejected** because allocation across partial or overlapping invoices is
  not implemented. Check billing periods before entry.
- First version is **manual invoice entry only**. It does not extract invoice
  PDFs, allocate partial loads, reconcile tax/freight,
  post entries to accounting, or use Gemini. No external cost or credentials.
- From an invoice comparison's details, **Download CSV report** exports a
  point-in-time summary of amounts, open review warnings, and the supporting
  ticket register. Each ticket is marked **exact** or **unclassified** and carries
  its current review state; a pending or possibly relevant ticket is never
  presented as verified. Reports are company-scoped, disable caching, and
  neutralize spreadsheet-formula characters from invoice/ticket text.
  **CSV exports are investigation aids, not approvals for payment**; always
  re-open a fresh report after changing ticket approvals.

### Checking individual tickets on an invoice

Equal totals do not prove the right tickets were billed. When the invoice
lists its delivery tickets, paste them into **Ticket numbers listed on this
invoice**: copy two columns from Excel, ticket number then billed tons (tons
optional). A header row and blank rows are fine. An unreadable row rejects
the whole list with its line number, so nothing is dropped silently.

Each invoice ticket is checked against the company's delivery records:

| Result | Meaning |
|---|---|
| Matched | Exactly one verified, complete delivery with this number, in this project, supplier, material and date range; billed tons equal verified tons (if entered) |
| Tons differ | Matched, but billed tons are not the verified tons |
| Not in TicketFlow | No delivery has this number. A number differing only in leading zeros is shown as a hint, never counted |
| Outside this invoice line | The delivery exists but is recorded for another project, supplier, material or date |
| Several records — not matched | More than one delivery could have this number; resolve the duplicate first |
| Not verified or incomplete | The delivery still needs review, or lacks a supplier, material, date or tons |
| Delivered, not on invoice | A delivery in this line's scope that the invoice does not list |

Rows are also flagged when a number is **repeated on this invoice** or is
**also on another saved invoice** from the same supplier (possible double
billing). If every ticket has billed tons, their sum is checked against the
invoice line's billed tons.

Matching rules are deliberately conservative: numbers compare exactly,
ignoring only letter case and surrounding spaces; **leading zeros count**.
Nothing is matched by guesswork, and no ticket value is changed. Every
matched or missing delivery links to its scan and to its review history.

Lines without a ticket list keep the original totals comparison and are
labelled **"Totals-only comparison — individual tickets not checked."**
Their best possible result reads "Totals match", never "Tickets match".
Changing a line's ticket list is recorded in its correction history with the
full previous list. The CSV report states the comparison type and, for
itemized lines, includes the ticket-by-ticket check.

Upgrading adds migration `20261104_09` (new `invoice_ticket_refs` table only).
Existing invoice comparisons become totals-only comparisons, with unchanged figures.

**For an existing local Mac installation:** after the PR is reviewed and
merged, stop the backend/worker, pull \`main\` in the **inner** TicketFlow clone,
back up \`backend/ticketflow.db\` before migrating, and run
\`cd backend && source .venv/bin/activate && python -m alembic upgrade head\`.
This applies additive revision \`20261029_06\`. No files, user accounts, or
existing ticket/price rows are reset. Restart backend and frontend to load the
new navigation item. Keep the original \`.env\`.

## Gemini primary handwriting reader (Batch #14 candidate)

**Gemini 3.1 Pro Preview is the selected primary reader.** We chose the
vendor, **not a verified handwriting accuracy number**; our synthetic
automated tests cannot predict real performance with sloppy delivery notes.
This Gemini model is in preview and availability/pricing can change.

For an existing installation, do not copy .env.example over your .env.
Keep your existing database, projects and user account. Update the existing
\`backend/.env\` locally (never commit keys):

\`\`\`ini
EXTRACTION_PROVIDER=gemini
GEMINI_MODEL=gemini-3.1-pro-preview
GEMINI_API_KEY=YOUR_PRIVATE_KEY_HERE
\`\`\`

From \`backend/\` in your existing virtual environment:

\`\`\`bash
python -m pip install -r requirements-dev.txt
python scripts/check_api_key.py   # ONE small live call; requires billing/model access
\`\`\`

Restart the backend and the **separate worker** to pick up environment changes.
On the Upload page choose **AI extraction** to invoke Gemini on the individual
stored ticket crops, then verify every field in the original-image review UI.
**Manual entry** remains zero-AI and works without any key. No database schema
changes are needed.

Without a Gemini key, an AI batch fails visibly; it NEVER fabricates OCR.
An old \`ANTHROPIC_API_KEY\` does not enable the Gemini reader. Anthropic is an
explicit opt-in alternative (\`EXTRACTION_PROVIDER=anthropic\` and
\`EXTRACTION_MODEL=...\`). This is not an automatic provider fallback.

Private, permissioned benchmark support is in \`backend/EVALUATION.md\`.
The model's actual recognition accuracy, cost per ticket and value to
contractors remain unmeasured.

## Batches #12–13: efficient review + manual intake

This release adds a **manual entry mode that never calls an AI model**,
so ticket processing can be demonstrated and used locally without an
Anthropic key. Every uploaded PDF/photo is still rendered and split by
the separate processing worker. In manual mode, each actual nonblank crop
becomes a **blank, unapproved** ticket record. A person fills the fields
from the image and explicitly presses **Accept & next**. Required fields
must pass the same server validation; manual mode does not mark anything
verified or include any unverified tonnage in cost reporting.

Also included: a ticket-number navigation strip beside the review workflow,
a larger original-scan viewer, Alt+left/right navigation (when not editing
a field), and Cmd/Ctrl+Enter to explicitly invoke the existing Accept &
next button. Unsaved edits are still guarded and the server never accepts
an invalid quantity.

**Safely upgrade an existing local install:** Merge the batch PR, stop
the running backend/worker, run `git pull origin main`, **back up**
`backend/ticketflow.db`, then from `backend/` with the local virtual
environment active run `python -m alembic upgrade head`. The additive
`20261029_05` migration introduces `batches.intake_mode`, with old
batches kept in extraction mode. Do not recreate users or delete data.

To rehearse without any AI model:

```bash
cd backend
source .venv/bin/activate
python scripts/make_demo_tickets.py
```

Open TicketFlow, create/select a project clearly labeled as a demo, then
choose **Manual entry** and **Four tickets per page** before uploading
`backend/demo_output/synthetic-4up.pdf`. Run the normal API, frontend,
and **processing worker** (three local terminals). The worker is needed
to rasterize/split scans, but it will not call AI for manual batches.
Check the four blank tickets, fill fields, verify, and inspect Excel export.

See `backend/PILOT_CHECKLIST.md` for the exact next-morning scan protocol,
privacy and consent requirements, and first real-ticket test. See
`backend/EVALUATION.md` before any paid real-model benchmark. Synthetic
data is **not** evidence of handwriting accuracy.

## Durable processing worker (Batch #10)

TicketFlow now saves a processing job **in the same database transaction**
as an accepted file upload. FastAPI no longer performs AI extraction inside
its own request worker. A separate worker must be running to process queued
uploads and retries. Uploaded documents are not lost when the website restarts.

**Upgrade your existing Mac installation safely:**

1. Stop the frontend and backend with **Control+C** in their Terminal windows.
2. After merging PR #10, run `git pull origin main`.
3. From the main TicketFlow directory, back up your database before upgrading:
   `cp backend/ticketflow.db backend/ticketflow-before-worker.db`.
   Do not delete or reset the SQLite database.
4. From `backend/` with the Python virtual environment activated, run
   `python -m alembic upgrade head`. The migration adds only a
   `processing_jobs` table and queues any previously interrupted
   `uploaded` or `processing` batches without changing confirmed tickets.
5. Run the backend as before (port 8000). In a **third Terminal**, with
   the same virtual environment activated, run:
   `cd backend && python scripts/run_worker.py`.
   In another Terminal run the frontend (port 3000).
6. If a processing worker is killed, restart **the worker**. It acquires
   an exclusive local OS lock, recovers unfinished jobs, and resumes only
   unfinished files/crops. Existing edits, approved tons, audit logs, and
   locked historical prices remain preserved. Use `--once` to drain at
   most one job for a local test.

**When no worker is running**, accepted uploads remain visibly queued; they
do not process automatically. **When ANTHROPIC_API_KEY is missing**, the
worker marks an extraction attempt as failed rather than fabricating results.
You can leave the API key unconfigured while developing other features.

This is a **single-machine Mac/Linux worker**, not a distributed production
queue. Do not run the worker on multiple hosts sharing the same database or
file storage. Processing is *at least once*: a crash between a paid AI call
and a database commit can trigger another model call after restart. The
local lock prevents two workers on the same machine from processing
simultaneously; it is not a distributed coordination mechanism. Production
requires a managed queue, durable worker execution, idempotency safeguards,
observability, backups, and platform-specific locking.

## Single-owner authentication (Batch #8)

**Authentication is now required.** This protects the existing local
single-workspace prototype. **It is NOT multi-company data isolation**, and
the application is NOT yet safe for public deployment to unrelated contractors.

1. Back up your SQLite database before upgrading. From `backend/`, run
   `alembic upgrade head`. The new migration adds `users` and
   `login_sessions` without deleting existing ticket data.
2. Run `python scripts/create_owner.py --username owner` once and enter
   a unique 12+ character password when prompted. The script only creates
   the very first account; add employees from the **Team** page instead
   (see "Team accounts, roles and attributable approvals" above).
3. Start FastAPI and Next.js, visit `http://localhost:3000/login`, and
   sign in. Existing scans, dashboards, pricing, exports, and image endpoints
   require authentication; a signed-out user receives HTTP 401.
4. Sessions use HTTP-only cookies (not localStorage), hashed opaque tokens
   in the database, a 12-hour expiry, and server-side logout invalidation.
   Mutating API requests require `X-TicketFlow-Request: 1`. Browser requests
   from origins not in `CORS_ORIGINS` are rejected.
5. For localhost development `SESSION_SECURE_COOKIE=false` works with
   `http://localhost`. For a future HTTPS deployment, set
   `SESSION_SECURE_COOKIE=true`, set explicit HTTPS `CORS_ORIGINS`, and
   run behind TLS. The backend refuses non-localhost frontend origins
   with insecure cookies. The UI/API must also be configured as a
   same-site pair to use SameSite=Lax session cookies.

**Security limitations:** Owner login and basic organization-scoped access
controls are present, but they have not received an independent security review.
Production-grade distributed login rate limits, account recovery, HTTPS hosting,
durable job workers, private object storage, audit identity and backups are
not yet implemented. Do not mistake passing security regression tests for
a completed customer-ready SaaS platform.

## Company data isolation (Batch #9)

Each account now belongs to exactly one organization. Every operational API
scopes access by **the organization stored on the signed-in user**, not by any
client-supplied company ID. This includes project creation, uploads, ticket
images, edits, audit history, retry, dashboards, records, Excel export,
supplier/material catalogs and rates. Duplicate warnings are scoped to
one company; company A cannot see company B's documents, prices or counts.

**Existing local installation: do not delete or reset the database.**
First stop both dev servers, then from your TicketFlow checkout:

```bash
git pull origin main
cp backend/ticketflow.db backend/ticketflow-before-orgs.db
cd backend
python -m alembic upgrade head
```

The migration adds an `Original workspace` organization and **assigns
all existing accounts, projects, uploads and supplier rates to it**. Your
existing `owner` password and TEST-001 project remain attached to this
workspace. Migration #9 rebuilds several SQLite tables to replace globally
unique supplier names, material names and project codes with per-company
uniqueness. **Make the backup before running it.** Existing tickets, cropped
images, financial price snapshots and audit events are left in place.

For local *development testing only*, a trusted machine administrator can
provision another company plus its owner:

```bash
python scripts/create_company.py --name "Demo Company B" --username companybowner
```

Enter a new password at the terminal. The script refuses duplicate usernames.
There is still **no public signup and no browser-based tenant switching**.

**Security boundary / deployment caution:** This code adds backend company
filters and regression tests using two authenticated users. It is not a
completed SaaS security program. A production launch still needs a proper
identity and permission model for multiple staff within one company, external
security review, private cloud document storage, HTTPS, backups, a durable job
worker, distributed login rate limiting, server-side request body limits,
monitoring and a hardened database/deployment setup. SQLite remains a local
development choice. Do not invite real contractors or put confidential data
on a public server until those controls are complete.

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
6. When the pending queue is exhausted, the reviewer is taken to the flagged
   queue automatically. Nothing is accepted automatically; each flagged ticket
   still requires a deliberate **Accept & next** action.
7. The queue can be searched by ticket number, supplier, material or job.
   Searching only changes what is visible and never changes stored data.

For larger batches, duplicate candidate detection and missing-rate catalog
lookups are indexed/cached once per ticket list rather than re-querying the
entire database for each ticket. Duplicate warnings remain advisory and are
never used to automatically delete or exclude a delivery record.

Use `/upload?batch=<batch_id>&ticket=<ticket_id>` to open a specific record.
Automatic extraction and human acceptance remain separate by design.

**Manual browser QA before a customer pilot:** open the sample 2×2 PDF, verify
image/text alignment and navigation, deliberately edit a field and try to leave
without saving, flag a ticket, revisit it from Records, correct and accept it,
confirm the progress counters, export the workbook, and repeat at mobile/tablet
widths. Automated backend and build checks do not replace these user tests.

## Measure real handwriting accuracy (separate from synthetic tests)

Automated tests use known printed-style fixture values, **not live handwriting
recognition**. A real pilot needs independently labeled scans and measured
six-field extraction accuracy before any performance or time-saving claims.

See [backend/EVALUATION.md](backend/EVALUATION.md) for the local-only protocol:
prepare private label templates, check every original PDF crop without an API
call, then run a consented sample through the configured provider and generate
a field-by-field report. Real documents and labels belong under
`backend/evaluation_data/` (Git-ignored), never in this repository.

**No real tickets yet?** `python scripts/make_practice_tickets.py` prints blank,
clearly synthetic practice tickets (4 per page) and an answer key so people
can hand-write a practice set. See "Practice tickets you write yourself" in
EVALUATION.md. Results are a best case and are labeled as synthetic in the
report; they are not evidence of accuracy on real supplier tickets.

**Important:** this tool is a benchmark, not an alternate upload process.
The contractor must still click **Accept & next** on every ticket in TicketFlow.
Supplier invoices and financial decisions must rely on human-verified records.

## Check your API key

```bash
cd backend && python scripts/check_api_key.py
```
Prints OK, or the provider's reason with a specific fix: rejected key, model
not on your plan (for example a paid-only model on the free tier), quota or
rate limit, unknown model name, or a temporary provider outage. A malformed
`GEMINI_MODEL` line is caught before any request. The key is never printed.
`python scripts/check_api_key.py --list-models` shows the models your key can
see without using quota. The UI also shows a banner if no key is set.

## Test

```bash
cd backend && source .venv/bin/activate && pytest -q
```

## Not built yet

ZIP upload, manual crop boxes, invoice PDF extraction, email-based password
reset/invitations, SSO/MFA, PostgreSQL, private cloud document storage, a
distributed job queue, monitoring and backups, and hosted deployment. See
[SECURITY_PRELAUNCH.md](SECURITY_PRELAUNCH.md) for the security status and
the controls still required before a hosted customer pilot.

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

**Security:** authentication, per-company isolation and role-based access
exist, but TicketFlow has not had an independent security review and is not
hosted. Do not expose it publicly or upload third-party confidential documents
to a shared server until the items in [SECURITY_PRELAUNCH.md](SECURITY_PRELAUNCH.md)
are complete.
