# Founder launch checklist: the fictional demo, then a pilot

The smallest set of things **you** do to put (a) the fictional demo and (b) one contractor's pilot online. Everything here is written from the code and checked by CI on a local copy of the stack only. **Nothing has been bought, provisioned or deployed.** Railway's and Cloudflare's screens change, so check each step against their current docs. The full guide is [railway.md](railway.md); costs are in [costs.md](costs.md); the open risks are in [audit.md](audit.md) and [readiness.md](readiness.md).

## What is ready, and what needs you

| Ready in the code (no money, no accounts) | Needs your account, card or decision |
|---|---|
| `APP_ENVIRONMENT=demo / pilot / production`, checked at startup by the API **and** the worker (below) | Railway account and two projects (demo, pilot) |
| Docker images, Railway config files, migrations run before each API deploy | Cloudflare account, two R2 buckets, two R2 tokens |
| Hosted demo seeder: `seed_demo.py --hosted`, refuses anything not named "demo" | A domain and two DNS records |
| Encrypted PostgreSQL backups and a restore drill (`backup_pg.py`, `restore_pg_drill.py`) | A third bucket for backups, ideally another account |
| Health endpoints, `check_health.py`, webhook alerts | An uptime monitor and a chat webhook (free tiers exist) |
| Secret scan, dependency audit and Bandit in CI | A Gemini API key with a budget cap, **pilot only** |
| Getting started checklist and pasted lists for a new company | A written data-handling note for the contractor |

Error reporting: there is **no** error-reporting service built in (no Sentry or similar). Errors are one JSON line in the platform log, with a reference id the person sees, and `check_health.py` alerts on failures. That is enough for one pilot. Choose a service later; it would be a new dependency.

## The rule that keeps them apart

| | Demo (`APP_ENVIRONMENT=demo`) | Pilot (`APP_ENVIRONMENT=pilot`) |
|---|---|---|
| Variables file | [demo.env.example](demo.env.example) | [shared.env.example](shared.env.example) |
| AI reading | Must be off (`AI_READING_ENABLED=false`), and **no** AI key may be set | On, only with `MONTHLY_TICKET_READS_PER_ORGANIZATION` above 0 |
| Database name | Must contain `demo` (`quarvera_demo`) | Must **not** contain `demo` |
| Buckets (documents and backups) | Must contain `demo` | Must **not** contain `demo` |
| Always | PostgreSQL, `STORAGE_BACKEND=s3` over https, `SESSION_SECURE_COOKIE=true`, exact origins and hosts, API docs off, no default or example passwords | the same |

If anything is wrong, the API and the worker refuse to start, and the deploy log names the variable. System health (in the app) and `/api/status` show which environment is running. Use separate Railway projects, so one can't reference the other's Postgres.

## (a) The fictional demo

About $15–25 a month at staging size (costs.md). No AI spending at all.

1. **Accounts (you):** Railway, Cloudflare.
2. **Bucket:** in R2 create `quarvera-demo-documents`. Public access off, no CORS. Create an R2 API token with **Object Read & Write** on that bucket only. Keep the access key ID, secret and S3 endpoint in your password manager.
3. **Railway project `quarvera-demo`:** add PostgreSQL 16 (no public TCP proxy), then the `api`, `worker` and `web` services exactly as in railway.md §2.
4. **Database:** the Postgres template's database is called `railway`, which the demo refuses. Open the Postgres service's query tab (or `psql`) and run `CREATE DATABASE quarvera_demo;`. Point `DATABASE_URL` at `.../quarvera_demo`.
5. **Variables:** paste [demo.env.example](demo.env.example) into the project's Shared Variables and fill every `<placeholder>`. Add [api.env.example](api.env.example) to `api` (set `APP_ORIGIN` to the demo's address), [worker.env.example](worker.env.example) to `worker`, [web.env.example](web.env.example) to `web`. Secret names to set: `DATABASE_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`. Set **no** `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.
6. **DNS (you):** in `web` → Networking add `demo.<your-domain>`; at your DNS provider add the CNAME Railway shows (DNS only to start). Put the same address in `CORS_ORIGINS` and `APP_ORIGIN`.
7. **Deploy** `api` (it runs `alembic upgrade head` first), then `worker`, then `web`.
8. **Seed**, from a checkout of this repository on your own computer (the seeder needs the repository's test helpers, which aren't in the image). It needs a way to reach the demo database: temporarily enable Railway's TCP proxy on the demo Postgres, or use `railway connect`, and turn it off again afterwards. In a new terminal:
   ```bash
   cd backend && source .venv/bin/activate
   export APP_ENVIRONMENT=demo STORAGE_BACKEND=s3 S3_BUCKET=quarvera-demo-documents
   export S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com S3_ACCESS_KEY_ID=... S3_SECRET_ACCESS_KEY=...
   python scripts/seed_demo.py --hosted --database-url 'postgresql://<user>:<password>@<proxy-host>:<port>/quarvera_demo'
   ```
   It refuses unless `APP_ENVIRONMENT=demo`, the database name contains `demo`, the bucket name contains `demo`, the demo's startup checks pass, and the database has nobody in it yet. It takes about 10 minutes and prints a **one-time** `demo-owner` password, changed at first sign-in. It never resets a database. To rebuild, drop and recreate `quarvera_demo`, deploy `api` once so the migrations run, and seed again.
9. **Verify:**
   - Open `https://demo.<your-domain>/login` and sign in as `demo-owner`. Choose the new password.
   - System health shows **Environment: Fictional demo**.
   - Uploading with automatic reading is refused ("turned off"). Manual entry works.
   - `scripts/verify_demo.py` checks the local demo only. For the hosted one, compare the Overview totals with `backend/demo_workspace/hosted-demo-expected-results.json`, which the seeder wrote on your computer.

## (b) A pilot for one contractor

About $40–60 a month hosting for a few companies, plus AI reading (costs.md; check current provider prices first).

1. **Accounts (you):** Railway, Cloudflare, a Google Cloud project with a Gemini API key **and a budget alert or cap**. Optionally a second provider or account for backups.
2. **Buckets:** `quarvera-pilot-documents` (documents) and a separate backup bucket, e.g. `quarvera-pilot-backups`, ideally in another account. Neither name may contain `demo`. Tokens:
   - documents: Object Read & Write, that bucket only (api and worker);
   - backups: read and write on the backup bucket only;
   - a **read-only** token on the documents bucket, for the backup job.
3. **Railway project `quarvera-pilot`:** PostgreSQL 16 and the three services, as in railway.md.
4. **Variables:** paste [shared.env.example](shared.env.example) (it sets `APP_ENVIRONMENT=pilot`) and fill every placeholder. Then add the `api`, `worker` and `web` files. Secret names to set:
   - `DATABASE_URL` (`${{Postgres.DATABASE_URL}}`)
   - `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`
   - `GEMINI_API_KEY`
   - `MONTHLY_TICKET_READS_PER_ORGANIZATION`: a number, not a secret
   - optional `STORAGE_ENCRYPTION_KEY`: losing it loses the documents
   - for backups: `BACKUP_ENCRYPTION_KEY`, `BACKUP_ENCRYPTION_KEY_ID`, `BACKUP_S3_ENDPOINT_URL`, `BACKUP_S3_BUCKET`, `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY`, `BACKUP_SOURCE_S3_*`
   - for alerts: `ALERT_WEBHOOK_URL`
5. **DNS (you):** `app.<your-domain>` → CNAME to the `web` service. Wait for the certificate, then check that `http://` redirects to `https://`.
6. **Deploy** `api` (migrations first), then `worker`, then `web`.
7. **The company and its owner**, from inside the API container (`railway ssh --service api`):
   `python scripts/create_company.py --name "<Contractor name>" --username <owner>`. It asks for the password without echoing it.
8. **Backups:** add a Railway cron service from the same image, command `python scripts/backup_pg.py`, schedule `30 2 * * *` ([docs/operations/backups.md](../operations/backups.md)). Run it once by hand. Then run one restore drill into a throwaway database whose name you choose:
   `python scripts/restore_pg_drill.py --latest --target-url postgresql://.../quarvera_drill --cleanup`.
9. **Monitoring (you choose):**
   - A free external uptime monitor on `https://app.<your-domain>/health/ready` every 5 minutes, emailing you.
   - A cron service running `python scripts/check_health.py --url https://app.<your-domain>` every 15 minutes, with `ALERT_WEBHOOK_URL`.
10. **Verify before the first real document:**
    - The deploy log shows the API started, with no refusal.
    - System health shows **Environment: Customer pilot**, the worker running, and a recent backup.
    - Upload one **fictional** scan, check it's read. While signed in as the owner, open `https://app.<your-domain>/api/system/usage`: it should show one read this month. There's no Usage screen yet.
    - Sign in as the owner. The dashboard's Getting started list shows what's left.
    - Give the contractor the written data-handling note: what's stored, where, for how long, and how to get it out.

### The contractor's first day

They don't need rates, suppliers or projects before the first upload:
- Upload with no project, enter or check the tickets, and approve. A ticket approved with no rate shows **rate missing** and is counted as cost incomplete, **never $0**.
- Lists can be pasted in at once from a spreadsheet, a CSV or one name per line. Each is previewed first, then applied:
  - `POST /api/setup/import/suppliers`
  - `POST /api/setup/import/materials`
  - `POST /api/setup/import/haulers`
  - `POST /api/setup/import/projects` (number, name, location)

  These are API only for now; there are no screens for them yet.
- The rate sheet goes in on Pricing when they have it. A rate added later prices tickets **not yet approved**. A ticket already approved without a rate keeps its locked state: it needs the documented rate entered as an override and re-approval. That is the price-lock rule, on purpose.

## Repository security checks (free, no accounts)

| Check | Where | Run it yourself |
|---|---|---|
| Secret scan of every tracked file. It reports file and line, never the value | `.github/workflows/secret-scan.yml`, every push and PR | `python backend/scripts/scan_secrets.py` |
| Python packages (pip-audit) | `dependency-audit.yml` | `pip install pip-audit==2.10.1 && cd backend && pip-audit -r constraints.txt --no-deps --disable-pip` |
| Browser packages (npm audit) | `dependency-audit.yml` | `cd frontend && npm audit --omit=dev` |
| Bandit on our own Python | `dependency-audit.yml` | `pip install bandit==1.9.4 && cd backend && bandit -q -r app scripts` |

pip-audit and npm audit need the network: their advisory data comes from PyPI's and npm's databases. `npm audit --offline` exits 0 with an empty report, which proves nothing; don't read it as a clean result. Exceptions, with reasons and review dates, live in `.github/audit-exceptions.json` ([docs/security/dependency-scanning.md](../security/dependency-scanning.md)).

If the secret scan ever finds a real key, **revoke it at the provider first**. Removing it from the repository afterwards doesn't un-leak it, since it stays in git history.
