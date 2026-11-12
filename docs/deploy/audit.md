# Cloud deploy audit

What stands between this code and a safe hosted deployment on Railway with Cloudflare R2, as of branch `feature/cloud-deploy-storage` (2026-11). Read with [the hosted architecture proposal](../engineering/hosted-architecture-proposal.md) and [PostgreSQL](../engineering/postgres.md).

**BLOCKER** means don't put a customer's real documents on the hosted stack until it's fixed. Workstream owners are in brackets: A is deploy and storage, B is worker and database, C is security, backups and monitoring.

## Ranked

| # | Severity | Area | Finding | State |
|---|---|---|---|---|
| 1 | **BLOCKER** | Worker locks [B] | `run_worker.py` holds `flock` on `STORAGE_DIR/worker.lock`, on its own container's disk. Two replicas, or an old and a new container overlapping during a deploy, each get their own lock, and `recover_interrupted` assumes it's the only worker. | Open. Until leases land, run **exactly one** worker replica, and the worker config sets no overlap (`railway.worker.json`). |
| 2 | **BLOCKER** | Worker health [B/C] | The worker's heartbeat is a file in its own `STORAGE_DIR`. The API runs in another container, so System health and `/api/status` always report the worker as stopped. | Open. Needs a heartbeat in the database. |
| 3 | **BLOCKER** | Backups [C] | `backup.py`, `verify_backup.py` and `restore_drill.py` copy SQLite and read `STORAGE_DIR` directly. None of them handles PostgreSQL or the bucket. R2 has no object versioning to fall back on (confirm on Cloudflare's docs). | Open. These scripts were deliberately left to C. Railway's Postgres backups depend on the plan. |
| 4 | **BLOCKER** | Documents | Containers on Railway have ephemeral disks, so documents on local disk would vanish at the next deploy. | **Fixed here.** `STORAGE_BACKEND=s3` stores them in a private R2 bucket, and production refuses to start with local storage. |
| 5 | High | Database | Production already refuses SQLite, and the full suite passes on PostgreSQL 16 in CI. Still missing: separate migration and application roles, row-level security, and point-in-time recovery or a standby. A single Railway Postgres service has none of these. | Partly done [B]. |
| 6 | High | Per-process limits | Login throttling and the per-person rate limits live in each process's memory, so they're only right with one API replica and `WEB_CONCURRENCY=1`. | Open [B/C]. The config pins one replica and one process. |
| 7 | High | Proxy trust | Behind a proxy every client looks like the proxy, which breaks login throttling and the local-only first-run setup. | **Fixed here.** The API has no public domain. The frontend's proxy forwards only the address Railway's edge recorded (`TRUSTED_PROXY_HOPS`), and uvicorn runs with `--proxy-headers`. First-run setup is refused through the proxy (the smoke test checks this). |
| 8 | High | CORS and same-origin | The browser called the API on a second origin, which needed CORS, credentialed cross-origin requests and a cross-site cookie. | **Fixed here.** See [same-origin](#why-same-origin-removes-cors). |
| 9 | High | HTTPS | TLS ends at Railway's edge. `SESSION_SECURE_COOKIE=true` marks the cookie Secure and turns on HSTS, and production refuses `http://` origins and storage endpoints. | Done, given a custom domain (railway.md). |
| 10 | Medium | Same-origin documents | Original PDFs now open on the app's own origin. Browser PDF viewers don't give a PDF the page's script access, but the proposal's advice stands: test `Content-Security-Policy: sandbox` or `Content-Disposition: attachment` on `/original` in each browser. | Open [C]. |
| 11 | Medium | System health [C] | With object storage, the "Disk space" check describes the container's disk, which means nothing. "Stored documents" works through the store, at one HEAD request per sampled document (up to 200 per visit). | Open. |
| 12 | Medium | Logging and monitoring [C] | JSON logs go to stdout, so Railway collects them, with retention that depends on the plan. There are no alerts on 5xx responses, queue age, failed jobs or backup age. | Open. |
| 13 | Medium | Retention | Deletes happen only for a rejected upload or an invoice whose save failed, and are limited to one folder. Nothing removes a departed company's documents, and the bucket has no lifecycle rules. | Open (proposal §9). |
| 14 | Low | Sessions | Sessions are opaque, server-side, revocable rows in the database, so they work across replicas. The cookie is host-only on the frontend's domain, `HttpOnly` and `SameSite=Lax`. | OK. |
| 15 | Low | CI | Nothing built images or ran the stack before. | **Fixed here.** `deploy-validation.yml` does both. |
| 16 | Low | Existing docs | `operations.md` and the backup docs describe one local machine. The proposal's costs assumed a large cloud provider, and [costs.md](costs.md) has Railway's. | Update after C's work. |

## Why same-origin removes CORS

- The browser loads the app from `https://app.example.com`. It calls `https://app.example.com/api/...` on the same origin.
- The Next server (`frontend/app/api/[...path]/route.ts`) forwards each request over Railway's private network to `API_INTERNAL_URL`. It streams bodies both ways and passes every header through: cookies and each `Set-Cookie` separately, `X-TicketFlow-Request`, `Retry-After`, `X-Request-ID`, `Content-Disposition` and content types. It drops only hop-by-hop headers and any forwarding headers the browser forged.
- No request crosses origins, so the browser never sends a preflight or asks for `Access-Control-*`. `CORSMiddleware` stays, and simply never matters.
- `CORS_ORIGINS` still lists the site's own `https://` address, because the CSRF gate checks `Origin` against it on every state change.
- The session cookie is first-party on the app's own domain, so third-party-cookie blocking in browsers can't break sign-in.
- The API has no public URL at all. That's a smaller attack surface, and it's why trusting forwarded headers from the private network's range (`TRUSTED_PROXY_IPS`) is safe there.

Local development is unchanged: unset `NEXT_PUBLIC_API_URL` still means `http://localhost:8000`, with CORS.

## Every place documents touch storage

Found by grepping `storage.`, `storage_dir`, `STORAGE_DIR`, `FileResponse`, `read_bytes` and `os.open`:

| Where | How it reaches documents |
|---|---|
| Uploads, crops, rotation and page images (`routers/api.py`, `routers/scans.py`, `services/pipeline.py`, `services/scan_manifest.py`) | `storage.current()`, so the bucket when `STORAGE_BACKEND=s3` |
| Invoice documents (`routers/invoice_documents.py`) | `storage.current()` |
| The worker (`scripts/run_worker.py` → `pipeline`) | `storage.current()`. Its lock and heartbeat stay on local disk (#1, #2) |
| `check_documents.py`, `services/document_audit.py`, the "Stored documents" health check | Through the store. With no `--storage`, the check uses the bucket |
| `make_synthetic_dataset.py`, `benchmark_ocr.py` | `storage.current()` (test and demo tools) |
| `backup.py`, `verify_backup.py`, `restore_drill.py`, `doctor.py`, the disk-space check | **Read `STORAGE_DIR` directly. Left for C** (#3, #11) |

`tests/test_b_storage.py::test_everything_goes_through_the_storage_service` keeps it that way inside `app/`.
