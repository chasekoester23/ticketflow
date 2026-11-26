# Hosted deployment readiness (reviewed 2026-11-26 against the code)

Quarvera runs as three processes from two images:
- `web`: Next.js, the only public service.
- `api`: FastAPI, private.
- `worker`: the same image as `api`, private.

Behind them sit PostgreSQL and S3-compatible object storage (Cloudflare R2 in the guide). The step-by-step guide is `docs/deploy/railway.md`, and costs are in `docs/deploy/costs.md`.

Nothing has been purchased, provisioned or deployed. Every step marked **(you)** needs your account, card or approval.

## What is ready in the code

| Need | State | Where |
|---|---|---|
| Production refusal checks | The API won't start in production without PostgreSQL, S3 storage, secure cookies, exact origins and hosts, and API docs off | `backend/app/config.py` |
| PostgreSQL | Migrations build the schema. Row-level security per company. The full suite passes on PostgreSQL 16 (1,506 tests) | `migrations/`, `services/row_security.py` |
| Private documents | S3/R2 driver with optional encryption at rest. Documents are only ever served through the API, after a company check | `services/object_store.py` |
| Background processing | Durable job queue with leases. Several workers can run safely. Crash recovery is tested | `services/jobqueue.py`, `scripts/run_worker.py` |
| Images and configs | Dockerfiles for both images. Railway configs for `web`, `api` and `worker`. `api` runs `alembic upgrade head` before each deploy | `*/Dockerfile`, `*/railway*.json` |
| Backups | `pg_dump` backups with verification, and a restore drill script. Both run in CI | `scripts/backup_pg.py`, `scripts/restore_pg_drill.py` |
| Health | `/health` (liveness), `/health/ready` (readiness), System health page | `routers/system.py` |
| Tenant isolation tests | Every route is swept for cross-company access, including the new directory, Review Queue, invoice register and cost reports | `tests/test_t_tenant_sweep.py`, `tests/test_c_cross_company.py` |
| Smoke test | Starts the real stack and checks sign-in, upload and the worker | `deploy/smoke/` |

## The four targets

### 1. Public marketing website

- **Ready:** `/welcome`, `/login` and `/signup` (request access) are in the web app. They carry the official logo and load nothing from other companies.
- **Needed:**
  - **(you)** a domain.
  - The `web` service deployed, alone or with the stack.
  - Replace the welcome page's illustrative copy with the final positioning text you approve.
- **Not needed:** a separate site. It can split out later if marketing outgrows the app.

### 2. Hosted fictional demonstration

- **Ready:**
  - The seeder builds Ridgeline Civil Constructors through the real API. It refuses to touch any database but its own.
  - The full stack runs on Railway per the guide.
  - `scripts/seed_demo.py --hosted --database-url ...` seeds a PostgreSQL database. It refuses unless `APP_ENVIRONMENT=demo`, the database and bucket names contain "demo", and the database is empty. Its refusals are tested; the full run on PostgreSQL hasn't been run yet.
  - `APP_ENVIRONMENT=demo` ([demo.env.example](demo.env.example)) refuses to start with AI reading on, an AI key present, or a database or bucket not named "demo". A pilot refuses anything named "demo".
- **Needed:**
  - A separate Railway project and bucket for the demo. Steps: [founder-launch-checklist.md](founder-launch-checklist.md).
  - **(you)** the Railway and R2 accounts.
- **Cost:** about $15–25/month (staging-size services). No AI calls: demo tickets are entered by hand.

### 3. Secure real-contractor pilot

- **Ready:** everything in the table above.
- **Needed before the first real document:**
  1. **(you)** Railway, R2 and a Gemini API key with a spending cap. Set the variables per `docs/deploy/*.env.example`.
  2. Pilot environment: `APP_ENVIRONMENT=pilot` (which implies `TICKETFLOW_ENV=production`), `STORAGE_BACKEND=s3`, `MONTHLY_TICKET_READS_PER_ORGANIZATION` above 0, and optionally a `STORAGE_ENCRYPTION_KEY`. Startup refuses default or example passwords and anything named "demo".
  3. Run one backup and one restore drill on the hosted database. Schedule the backup.
  4. Uptime check on `/health/ready`, and an error alert (the monitoring notes are in `docs/operations/monitoring.md`).
  5. Create the company and its owner with `scripts/create_company.py`. After that, the contractor can upload before entering any rates. Suppliers, materials, haulers and projects can be pasted in at once (`/api/setup/import/*`, API only for now).
  6. A written data-handling note for the contractor: what's stored, where, for how long, and how they get it out (Excel exports exist).
- **Cost:** about $40–60/month hosting for a few companies, plus AI reading. `costs.md` estimates about $125/month across 5 companies; verify current provider prices first.

### 4. Production commercial SaaS

Needs the pilot list, plus:
- An independent security review (external, $5k–25k).
- Status page and support process.
- Self-serve sign-up and billing, only once pricing is decided.
- Data retention and deletion on request.
- Off-site backup copies in a second account.

None of these block a pilot.

## Security review of this round's changes

| Area | Finding |
|---|---|
| Company isolation | Every new read takes the company from the session and refuses another company's project id with 404. These are the directory, Review Queue, invoice register, cost report and its Excel. All are in both cross-company test suites |
| Authorization | Reads are open to any role in the company. Writes keep their permissions: hauling rates need `pricing.manage`, a ticket's hauler `tickets.edit`, and haulers and trucks `trucking.edit` |
| Hauler on a ticket | Must belong to the signed-in company (test) |
| Historical integrity | The hauling charge is locked at approval by a database trigger, like the material price (test) |
| Exports | Every new spreadsheet cell from user text goes through the formula-injection guard |
| Rate limits | Report and export endpoints are limited per person: reports 120 a minute, exports 30 |
| Open | Sessions and rate limits are per API process (fine for one `api` instance, the pilot setup). Shared limits are needed before running several API instances |
