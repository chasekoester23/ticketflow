# Deploying TicketFlow on Railway with Cloudflare R2

**Nothing here has been provisioned.** These steps are written from the code in this repository and checked by CI (`deploy-validation.yml`) on a local copy of the stack only. Railway's and Cloudflare's dashboards change, so check each step against their current docs as you go. Before any real customer document is uploaded, clear the launch blockers in [audit.md](audit.md).

## The shape

```
browser ──HTTPS──> Railway edge ──> web (Next.js, public domain)
                                      │  /api/... proxied over the private network
                                      ▼
                                    api (FastAPI, no public domain) ──> Postgres (Railway, private)
                                    worker (same image, no domain)  ──> R2 bucket (S3 API, HTTPS)
```

| Service | Image | Start command | Config file | Public? |
|---|---|---|---|---|
| `web` | `frontend/Dockerfile` | `node server.js` | `/frontend/railway.json` | Yes, the custom domain |
| `api` | `backend/Dockerfile` | `sh docker/start-api.sh` | `/backend/railway.api.json` | **No** |
| `worker` | `backend/Dockerfile` | `python scripts/run_worker.py` | `/backend/railway.worker.json` | **No** |
| `Postgres` | Railway's PostgreSQL template, version 16 | - | - | **No** (private URL only) |

Only `api` runs migrations, as its pre-deploy command (`alembic upgrade head`).

## 0. Environments: staging and production

Use **two separate Railway projects**, `ticketflow-staging` and `ticketflow-production`. Railway environments inside one project are also possible, but separate projects make it harder to point production at staging's database by mistake, and let you give fewer people access to production. Each project gets its own:

- Postgres service, and so its own database.
- R2 bucket: `ticketflow-staging-documents` and `ticketflow-production-documents`.
- R2 API token, limited to that one bucket.
- Gemini API key, ideally in separate Google Cloud projects with separate budgets.
- Domain: `staging.app.example.com` and `app.example.com`.

Never copy production data into staging. Staging gets fictional data only.

## 1. Cloudflare R2: bucket and least-privilege token

Do this once per environment.

1. In the Cloudflare dashboard, go to **R2 Object Storage** and choose **Create bucket**.
   - Name: `ticketflow-production-documents`.
   - Location: automatic, or a jurisdiction hint near your customers. Use a jurisdiction (such as EU) if a contract requires one. It can't be changed later.
   - Default storage class: Standard.
2. In the bucket's **Settings**:
   - **Public access: leave it off.** No `r2.dev` subdomain and no custom domain. TicketFlow never issues public or pre-signed URLs. Every document goes through the API's authorised endpoints.
   - CORS policy: none. The browser never talks to the bucket.
   - Encryption at rest: R2 encrypts every object at rest (AES-256) automatically, with nothing to configure.
3. Go to **R2 → Manage R2 API Tokens → Create API token**.
   - Permissions: **Object Read & Write**, not Admin.
   - Specify bucket: **only** `ticketflow-production-documents`.
   - TTL: none, or a date you'll rotate before. Write the rotation into your calendar.
   - Client IP filtering: leave it off. Railway's outbound addresses aren't fixed, unless you pay for static egress.
4. Copy the **Access Key ID**, the **Secret Access Key** and the **S3 endpoint** (`https://<account-id>.r2.cloudflarestorage.com`) into your password manager. The secret is shown once.

Optional app-level encryption on top of R2's: generate a key with `python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"` and keep it in the password manager. If you lose it, every document written with it is lost. If someone steals it along with the R2 token, encryption doesn't help. Turning it on later is safe: objects written earlier stay readable. Turning it off isn't, because encrypted objects then can't be read. Key rotation isn't implemented.

## 2. The Railway project

1. Create the project `ticketflow-production`, and connect the GitHub repository.
2. **Add PostgreSQL** from Railway's database templates. Check that it's version 16. Don't add a public TCP proxy.
3. Add three services from the same repository, and set each one's settings:

| Setting | `web` | `api` | `worker` |
|---|---|---|---|
| Root directory | `/frontend` | `/backend` | `/backend` |
| Config-as-code file | `/frontend/railway.json` | `/backend/railway.api.json` | `/backend/railway.worker.json` |
| Networking | Generate a domain, then add the custom domain | **No public domain.** Private networking only | No domain |
| Replicas | 1 | **1** (audit #6) | **1** (audit #1) |

   The config files set the builder (Dockerfile), the start command, the restart policy (`ON_FAILURE`), the health check (`/health/ready` for the API, `/login` for the web) and the API's pre-deploy migration. Railway shows them as "set by config file". The `watchPatterns` mean a change to the frontend doesn't redeploy the backend, and the other way round.
4. Name the API service exactly `api`, so its private address is `api.railway.internal`. If you name it something else, change `API_INTERNAL_URL` and `TRUSTED_HOSTS` to match.

## 3. Variables

The full lists, with placeholders only, are in this folder: [`shared.env.example`](shared.env.example), [`api.env.example`](api.env.example), [`worker.env.example`](worker.env.example) and [`web.env.example`](web.env.example). CI starts the backend image in production mode with exactly these files, so they're known to pass the startup checks.

1. Put everything in `shared.env.example` into the project's **Shared Variables**, with the real values:
   - `DATABASE_URL`: `${{Postgres.DATABASE_URL}}`, Railway's reference to the private URL.
   - `S3_*`: from step 1.
   - `CORS_ORIGINS`: `["https://app.example.com"]`, the exact custom domain.
   - `TRUSTED_HOSTS`: `["api.railway.internal","healthcheck.railway.app"]`. The second is the Host Railway's health check sends.
   - `GEMINI_API_KEY`: this environment's key.
2. In `api` and `worker`, reference every shared variable (Railway can do this for all of them at once), then add the service-specific ones from `api.env.example` and `worker.env.example`.
3. In `web`, set `API_INTERNAL_URL=http://api.railway.internal:8080` and `TRUSTED_PROXY_HOPS=1`. Don't set `NEXT_PUBLIC_API_URL`. The image is built with it empty, which makes the browser call its own origin.
4. Mark secrets as sealed where Railway offers it: `S3_SECRET_ACCESS_KEY`, `GEMINI_API_KEY`, `STORAGE_ENCRYPTION_KEY` and the database password.

If anything is missing or unsafe, the API and the worker refuse to start, and the deploy log names the variable. This covers SQLite, local storage, an `http://` storage endpoint, insecure cookies, a non-https or wildcard origin, local hosts and API docs turned on.

## 4. Custom domain and HTTPS

1. In `web` → **Settings → Networking**, add `app.example.com`. Railway shows a CNAME target.
2. At your DNS provider, add that CNAME. If the zone is on Cloudflare, set it to **DNS only** (grey cloud) to start with. Proxying through Cloudflare as well works, but needs SSL mode "Full (strict)", and adds a second hop to X-Forwarded-For: set `TRUSTED_PROXY_HOPS` to match.
3. Wait for Railway to issue the certificate (Let's Encrypt). Then check that `https://app.example.com/login` loads, and that `http://` redirects to it.
4. `SESSION_SECURE_COOKIE=true` makes the session cookie Secure, and sends `Strict-Transport-Security` on API responses.

## 5. First deploy and the first owner

1. Deploy `api` first. Its pre-deploy step runs `alembic upgrade head` against the empty database. Then it starts and passes `/health/ready`. Then deploy `worker`, then `web`.
2. Create the first owner **from inside the API's container**, because first-run setup refuses any request that comes through the proxy. With the Railway CLI, use `railway ssh --service api` (or the dashboard's shell), then run `python scripts/create_owner.py --username <owner>`. It asks for the password without echoing it. Keep `ALLOW_REMOTE_SETUP` unset.
3. Sign in at `https://app.example.com/login`. Upload one fictional scan and check that it's read. Then check System health. The worker currently shows as stopped there, a known gap (audit #2).

## 6. Each later deploy: migrations

- Railway builds the new image, runs `alembic upgrade head` once (the API's pre-deploy command), and only then starts the new API. If the migration fails, the deploy stops and the old version keeps serving.
- Migrations in this repository are additive and forward-only. A new column or table doesn't break the version still running. So the old API keeps working against the new schema while the new one starts, and nothing is down.
- The API refuses to start against a database at the wrong migration. That catches a deploy where the migration didn't run.
- **Before a migration that rewrites or locks a large table**, take a database backup by hand, and deploy outside working hours.
- The worker has no pre-deploy step. It runs the same image, so deploy it after the API.

## 7. Rollback

**Roll back the code, never the schema.**

1. In the service's **Deployments** list, choose **Redeploy** on the last good deployment. Do `api` and `worker` together, because they share an image.
2. The older code runs against the newer schema, which works because migrations only add things. Don't run `alembic downgrade` in production.
3. If a migration itself was wrong, fix it forward with a new migration.
4. If data was damaged, restore from a backup into a **new** database, check it, then switch `DATABASE_URL`. That procedure belongs to the backup work (audit #3).

## 8. Zero-downtime notes

- **API:** Railway starts the new deployment, waits for `/health/ready`, switches traffic, then drains the old one (`overlapSeconds`, `drainingSeconds` in the config). Uvicorn finishes in-flight requests on SIGTERM, for up to 25 seconds.
- **Web:** It's stateless, so the same overlap applies.
- **Worker:** `overlapSeconds: 0`, so two workers never run together (audit #1). A job cut off by a deploy is picked up by the next worker when it starts. Extraction is at-least-once, so a ticket can occasionally be read twice.
- **One replica each, for now.** Login throttling and rate limits are per process (audit #6).

## 9. Retention and deleting documents

- TicketFlow deletes a document only in two cases: an upload it rejected in the same request, or an invoice document whose save failed. Approved tickets and their scans are financial evidence and are kept.
- A delete is limited to one folder (`uploads/<batch>`) or one key, and is validated. It can't empty a section or reach outside the prefix.
- R2 has no object versioning, so a deleted object is gone. Backups of the bucket (audit #3) are what protect against mistakes. Don't add lifecycle rules that expire objects in `uploads/`, `crops/` or `invoices/`.
- When a customer leaves, export their data first. Then delete their objects under an agreed, audited procedure (proposal §9). This isn't built yet.

## 10. Running the same stack locally

`docker compose up --build` in the repository root starts PostgreSQL 16, MinIO, migrations, the API, the worker and the frontend at `http://127.0.0.1:3301`, all with placeholder credentials. CI runs it with `deploy/smoke/docker-compose.smoke.yml`, which swaps in a stand-in reader, and `deploy/smoke/smoke.py`.
