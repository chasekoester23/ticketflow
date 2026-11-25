# Quarvera commercial transformation: living implementation document

Branch: `feature/commercial-foundation`. It is stacked on `feature/ux-simplification`, which sits on
`feature/trucking-reconciliation` (PR #38), which sits on `feature/hourly-trucking` (PR #37).

The next session starts from the **Continuation point** section at the bottom.

Positioning: construction material and trucking cost management, from the field to the invoice.
Tagline: "Every delivery. Every truck. Every dollar."

## Phase 1: audit (2026-11-24)

### What is genuinely implemented

| Area | State | Evidence |
|---|---|---|
| Tenancy | Every business table has `organization_id`; all queries filter by the signed-in user's company; PostgreSQL row-level security policies; tenant-sweep and cross-company guessing tests | `tests/test_t_tenant_sweep.py`, `tests/test_c_cross_company.py`, `services/row_security.py` |
| Roles | Owner, Manager, Clerk, Viewer; routes depend on named permissions, never on role names | `app/permissions.py` |
| Material tickets | Upload (PDF/JPG/PNG, magic-byte check, 25 MB limit), single or 2×2 layouts, Gemini extraction with retries and usage metering, side-by-side review with keyboard flow, approval that locks tons and price (DB trigger), duplicate detection (number + supplier keys), flags, audit events | `routers/api.py`, `services/pipeline.py`, `app/upload/` |
| Pricing | Company or project supplier×material rates with effective dates, a timeline (gaps/overlaps/expiry), rate-change preview, price snapshot at approval | `services/pricing.py` |
| Names | Supplier and material aliases (reversible, audited) | `NameAlias`, `app/records/names` |
| Hourly trucking | Haulers, trucks, hourly agreements (minimum, standby, increments), shifts, approval lock, allocation to the cent, disputes, original time sheets | `services/trucking.py` |
| Invoices | Supplier invoice lines compared per ticket; PDF text-layer line drafting (no AI, no scans); hauling invoices checked against shifts; exceptions center with owner, status, history | `routers/invoices.py`, `services/hauling_invoices.py`, `services/exceptions.py` |
| Reports | Project report (xlsx), unbilled tickets (csv), pilot measurement | `routers/reports.py` |
| Usage | `extraction_usage` row per extraction (tokens, attempts, seconds, outcome) | `services/usage.py` |
| Operations | Durable job queue with leases, heartbeats, backup and restore drills, deploy validation | `services/jobqueue.py`, `scripts/` |

### Partial, missing or duplicated

- **Units**: tickets are tons only (`tonnage`). There is no unit of measure, gross/tare, truck number, hauler or cost code on a ticket. The invoice text reader recognises other units but refuses to compare them.
- **Trucking billing methods**: hourly only. Per-load, per-ton and freight-included arrangements don't exist. Tickets carry no hauler or truck, so a per-ton hauling cost can't be derived yet.
- **Freight included**: there is no flag on supplier rates. Today nothing double counts, because no per-ton hauling exists. The flag becomes necessary when per-ton hauling is added.
- **Company identity**: `suppliers` and `haulers` are separate tables with no link, so a company that is both is entered twice.
- **Time-sheet and hauling-invoice AI extraction**: not implemented (manual entry plus an attached original).
- **Scanned supplier invoices**: no AI; text-layer PDFs only.
- **Review**: the ticket review is strong, but there is no single queue across tickets, truck shifts, invoice drafts and failed documents.
- **Navigation**: pages hidden in tabs (Delivery records, Reports). The project filter is repeated per page, with no global project context.
- **Brand**: a generic stacked-layers icon and text wordmark. "TicketFlow" still appears in metadata, the welcome page, invoice labels, API error messages and the backend.
- **Roles**: four roles. The requested six personas are mapped in Phase 6, not added blindly.
- **Subscription/lifecycle**: no plan, trial or offboarding state. Usage is measured, but not reported per period.
- **Demo**: `create_demo_workspace.py` makes an empty owner only. A rich synthetic demo lived on the unmerged `demo/ibm-interview` branch.

### Lifecycles traced

- **Delivery ticket**:
  1. Upload creates a batch and its uploaded files (original stored), then enqueues a job.
  2. The worker splits pages (single or grid), crops, extracts, validates and flags, creating tickets in `needs_review` (or `failed`, with an error, keeping the crop).
  3. In review, a person corrects fields. `verify` snapshots the applicable rate (or marks the price missing) and the trigger locks tons and price.
  4. Records and the dashboard count verified tickets only. Invoice lines match by ticket number and supplier.
- **Truck shift**:
  1. Entered by hand (the original sheet is attachable) as `needs_review`.
  2. Approval picks the most specific applicable agreement and computes billable hours (increments, minimum, standby), cost and allocations, then the trigger locks them.
  3. A hauling invoice line matches shifts by truck and date, and the differences feed the exceptions center.

## Architectural direction

- Keep the monolith: FastAPI plus SQLAlchemy plus a worker process, PostgreSQL in production, Next.js frontend. No new services.
- Additive migrations only. Approved financial rows stay trigger-locked, and corrections go through audited reopen flows.
- Project context is a client-side preference (URL first, then stored choice). The server still authorizes every project id it receives, and tenant tests cover that.
- New billing methods (per-load, per-ton, freight included) need explicit models, migrations and tests. They are not simulated in the UI.

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Audit, plan, this document | Done |
| 2 | Official logo and favicons; light shell with grouped sidebar (Workspace / Operations / Financials / Management / Settings); Upload documents menu; global project selector (search, recent, archived, New project) shared by every page; project directory with open work, verified tons, material, trucking and verified cost; TicketFlow removed from customer-facing text | Done |
| 3 | Review Queue (`/review`): tickets by batch, failed files, truck shifts, drafted invoice lines, oldest first, filtered by project and kind. Automatic reading is the default when it is set up, in plain language | Done |
| 4 | Suppliers & Haulers (`/suppliers`): suppliers with delivered tons and costs and rates on file, ticket names not yet set up, haulers with trucks and rates, hauler and truck setup. Rates & Pricing: Material pricing and Trucking rates tabs. Trucking page is the operational register (Shifts, Hauling invoices); `?tab=setup` redirects | Done |
| 5 | Invoice register (`/invoices`): supplier and trucking invoices in one list with exact server-side totals; line comparison moved to `/invoices/lines` (old `?line=` links redirect). Reports hub | Register done; reports hub next |
| 6 | Demo workspace seeder (`scripts/seed_demo.py`); roles mapping; usage reporting; security review | Seeder done |
| 7 | Full verification and final report | Planned |

## New endpoints (all company-scoped, in the tenant sweep and cross-company tests)

- `GET /api/projects/directory`: every project with counts and approved costs; `unpriced_tickets` keeps a missing rate from reading as $0.
- `GET /api/review-queue?project_id=&kind=`: what is waiting for a person; links to where the work is done.
- `GET /api/invoice-comparisons/register?project_id=`: one row per supplier or trucking invoice; `compared` is null while any part can't be priced; `ticket_level` is false for totals-only lines.

## Measured performance (2026-11-25)

Measured with `scripts/measure_performance.py --tickets 5000 50000` on SQLite on the development Mac. These are fictional data, with each request in its own process. Use the figures to compare runs, not as a promise.

| Request at 50,000 tickets | Seconds | SQL statements |
|---|---:|---:|
| Project directory | 0.13 | 5 |
| Review queue | 0.13 | 5 |
| Invoice register | 0.18 | 23 |
| Cost report, by week | 0.35 | 30 |
| Cost report, one project, by day | 0.22 | 31 |
| Cost report Excel | 0.42 | 30 |
| Dashboard (existing) | 0.32 | 14 |
| Records list, first page (existing) | 0.48 | 161 |
| Excel export, everything (existing) | 4.68 | 4 |

Two findings to follow up:

- The records list runs 161 SQL statements for a 50-row page, which looks like a per-row lookup.
- The full Excel export takes 4.7 s at 50k tickets.

## Demo workspace

`python scripts/seed_demo.py --reset` (from `backend/`) builds "Ridgeline Civil Constructors (DEMO)" in `backend/demo_workspace/quarvera-demo/` through the real API, with manual intake and no AI calls. It refuses to run anywhere else. It contains:

- 4 projects (1 archived), 3 suppliers, 4 materials and 5 rates. One of the rates is a project agreement and one is a later increase.
- 24 tickets: 19 approved, 2 of them approved without a rate, 1 flagged and 4 waiting.
- 2 haulers with 5 trucks and 2 hourly agreements (minimum, standby, quarter hours).
- 7 shifts: 5 approved, 1 waiting and 1 disputed.
- 2 supplier invoices: one matches, and one bills a ticket that was never delivered.
- 2 trucking invoices: one bills 10 hours for a 9-hour day, and one matches.

The script prints a one-time password for `demo-owner`.

## Decisions log

- **2026-11-24: light sidebar.** The approved logo is navy on transparent and is not to be recoloured, so it can't sit on a navy sidebar. The shell uses a white sidebar with navy text and an orange active marker.
- **2026-11-24: brand tokens sampled from the artwork.**
  - Navy `#041d2f`, orange `#eb781d`, slate `#64737f`.
  - White on `#eb781d` is about 2.9:1 contrast, below the AA minimum, so buttons with white text use the darker `#b9560c`. Text links use `#a24b0a`.
  - The brand orange itself is kept for markers, borders and focus.
- **2026-11-24: logo files.** Logo files are copied from `~/Downloads/quarvera-brand-pack` unmodified, apart from a size-reduced copy of the horizontal logo for the sidebar (resampled only). The pack's README notes they are raster and that a designer should vectorize them; that is still outstanding.

- **2026-11-24: project context is a preference.**
  - The address wins, then the person's last choice (localStorage, per user).
  - Pages bind their project filter to it instead of keeping their own dropdowns.
  - The server authorizes every id it receives.
- **2026-11-24: trucking setup split.**
  - Haulers and trucks are managed in Suppliers & Haulers, and hourly rates in Rates & Pricing.
  - Trucking keeps the daily work.
- **2026-11-24: billing methods.**
  - Per-load, per-ton and freight-included trucking are not modelled. Tickets carry no hauler or truck, so a per-ton hauling cost can't be derived honestly yet.
  - Rates & Pricing says so instead of simulating it.
  - Next step: an optional hauler and truck on a ticket, a `billing_method` on hauling rates (`hourly | per_ton | per_load`) with its own calculation and tests, and `freight_included` on supplier rates. Hauling cost would be derived only when freight isn't included.
- **2026-11-24: roles.** The existing four roles map onto the six requested personas, and no new roles were added:
  - Owner: organization owner.
  - Manager: administrator and project manager.
  - Clerk: project engineer and field uploader.
  - Viewer: accounting and read-only.
  - Split a role only when a customer needs it, for example a field uploader who may not edit, or accounting that may manage invoices but not approve tickets.

## Continuation point

Phases 2 to 4 are done, and Phase 5's register and Phase 6's seeder are done. Next:

1. Reports hub at `/reports`: material, trucking, project cost and invoice reports with date filters and Excel export. Pilot measurement moves under the hub.
2. Delivery Records: units of measure, truck and hauler columns (needs a migration).
3. Non-hourly trucking (see decisions).
4. Usage report per workspace from `extraction_usage`.
5. Final verification.
