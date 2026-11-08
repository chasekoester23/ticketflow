# TicketFlow pre-launch security status

Last updated: 2026-11-07 (production settings, rate limits, idle sessions, router audit, migration `20261106_20`).

TicketFlow handles confidential construction documents and money figures.
This file records **what is actually implemented and tested** and **what is
still required** before any hosted customer pilot. Passing automated tests is
not a penetration test, a security review, or a compliance certification. No
independent review has been done. **Nothing here replaces independent security
testing of the real deployment by someone who didn't build it.**

**Status: development / local pilot only.** Safe use today is a single
trusted machine on a private network, with documents whose owners have given
permission. Do **not** expose TicketFlow publicly or store live customer
documents on shared hosting yet.

## Implemented controls (verify in every release)

| Area | Control | Where |
|---|---|---|
| Authentication | Salted scrypt password hashes; 12+ character passwords; very common passwords refused (a short list of what guessing tries first, plus repeats and keyboard or number runs) | `backend/app/auth.py` |
| Sessions | Random opaque tokens stored only as SHA-256 digests; HTTP-only, SameSite=Lax cookie; 12 h expiry **and** an idle timeout (`SESSION_IDLE_MINUTES`, default 120), after which the session is deleted, not just refused; activity recorded at most once a minute; server-side logout; new token on every sign-in; expired sessions purged at sign-in | `auth.py`, migration `20261106_20` |
| Production settings | `TICKETFLOW_ENV=production` makes the API and the worker refuse to start unless the database is PostgreSQL, cookies are secure, `CORS_ORIGINS` lists exact `https://` origins and `TRUSTED_HOSTS` real hostnames (no wildcards or local addresses), and API docs are off. An unrecognised value such as `prod` also refuses | `app/config.py` (`configuration_problems`), `main.py`, `scripts/run_worker.py` |
| API documentation | `/docs`, `/redoc` and `/openapi.json` are off unless `ENABLE_API_DOCS=true`. The route-inventory tests still read the schema in-process | `main.py` |
| HSTS | `Strict-Transport-Security: max-age=31536000; includeSubDomains` whenever `SESSION_SECURE_COOKIE=true` | `main.py` |
| Expensive requests | Per person, per minute: uploads (30), exports (30), reports (120), answered with 429 and `Retry-After`. **Per process only**: each API process counts separately and a restart forgets. Enough for one machine; a hosted deployment needs limits at its edge too. `RATE_LIMIT_*_PER_MINUTE=0` turns one off, for load tests | `app/rate_limit.py`; a test fails if a listed endpoint loses its limit |
| Error responses | 422s list which fields and why, and never repeat the submitted values (FastAPI's default does). 500s give a reference only. Database errors never show the database's text, and engines use `hide_parameters` | `main.py`, `db.py` |
| Logs | One JSON line per request with route template, status, timing and **user id**, never the query string or document values. An unexpected error logs its type and code location, never its message, since an error's text can quote a record. A caller's `X-Request-ID` is kept only if it's plain. The worker logs JSON with job, batch and file ids on the same rules | `main.py`, `services/job_log.py` |
| Session revocation | Deactivation, role change and password reset end the person's sessions immediately; changing your own password ends your other sessions | `services/accounts.py` |
| Brute force | Per (address, username) lockout (8 failures / 15 min) plus per-account lockout (20 / 15 min); bounded memory without flushing every lockout; unknown usernames take the same time as wrong passwords | `auth.py` |
| CSRF | Custom `X-TicketFlow-Request` header required on mutating `/api` requests; `Origin` checked against `CORS_ORIGINS` | `main.py` |
| Host header | Requests with an unexpected `Host` are rejected. Local defaults: `localhost`, `127.0.0.1`, `testserver`. Deployments must list exact API hostnames in JSON `TRUSTED_HOSTS`; wildcards are refused at startup | `config.py`, `main.py` |
| Company isolation | Company ID always from the signed-in user, never from the request; cross-company IDs return 404; two-company regression tests | all routers; `tests/test_organization_isolation.py` |
| Authorization | Named permissions per role (owner / manager / clerk / viewer), enforced server-side; tests fail if any mutating API route lacks a permission or any API route answers an anonymous request. The route list is built from our own routers and checked against the OpenAPI schema | `app/permissions.py`; `tests/test_team_accounts.py` |
| Accountability | Ticket edits and approvals, uploads, invoice-line changes, team changes **and supplier/material/rate changes** record the acting person; older history is explicitly unattributed, never guessed. Excel exports include who approved each ticket and when | `models.py`, `routers/*.py`, `services/export.py` |
| Onboarding | Owner-issued temporary passwords (about 120 bits, shown once); the API is blocked until the person sets their own password | `routers/team.py` |
| Lockout safety | A company always keeps one active owner; owners cannot deactivate or demote themselves; terminal-only recovery script, audited | `services/accounts.py`, `scripts/reset_password.py` |
| Uploads | Extension allow-list, magic-byte check; limits on file count, per-file bytes, total batch bytes and file-name length; whole selection rejected if any file is invalid; random internal file names | `routers/api.py`, `config.py` |
| Scan images | Slot and page images require sign-in and are company-scoped; cross-company IDs return 404; internal paths are never returned | `routers/scans.py` |
| Invoice PDFs | PDF only, magic-byte check, size and page limits, encrypted files refused; stored 0600 with random names; originals served only through company-scoped routes. Reading stays on this machine | `routers/invoice_documents.py` |
| Scan processing | PDF page count, per-page rendered pixels, total rendered pixels and image dimensions are bounded **before** heavy decoding; exceeding a limit is a visible failure, never an empty "success" | `services/splitter.py`, `services/pipeline.py` |
| File privacy | Every stored document is written 0600, all at once (a crash leaves nothing half-written), in folders created 0700, through one service that can't be led outside `STORAGE_DIR` by `..`, an absolute path or a symlink. Not retroactive for older files | `services/storage.py` |
| Backups | Folders 0700, files 0600, whatever the umask; restored copies are made private too. `doctor.py` reports a storage or backup folder others can read | `scripts/backup.py`, `verify_backup.py`, `doctor.py` |
| API headers | `Cache-Control: private, no-store` on `/api`; `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` | `main.py` |
| Page headers | Every frontend page sends `Content-Security-Policy: frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, and no `X-Powered-By` | `frontend/next.config.mjs` |
| Exports | Spreadsheet-formula neutralization for user-controlled text in Excel (including approver names) and invoice CSV reports; numbers stay numeric | `services/export.py`, `routers/invoices.py` |
| Pilot measurement | Review timing is off by default and stores no person or ticket contents | `routers/reports.py` |
| Secrets | Keys only from environment / `.env` (git-ignored); never returned by the API | `config.py` |

## Router audit, 2026-11-07

Every router was read for company scoping and authorization: `api`, `auth`, `invoice_documents`, `invoices`, `pricing`, `projects`, `records`, `reports`, `scans`, `system`, `team`. A test now proves every route learns who is asking, unless it's one of the four deliberately public ones (sign-in, sign-out, and the first-run setup pair). The existing tests prove every mutating route declares a permission and every route refuses an anonymous request.

| Finding | Severity | State |
|---|---|---|
| Every lookup by id is scoped to the caller's company, directly or through a parent row already checked to be theirs. None was found unscoped | — | Verified by reading; two-company tests in `test_organization_isolation.py` |
| A rejected request's 422 repeated the submitted values, which here are a ticket's | Low | **Fixed**, tested |
| An unexpected error logged its message, which can quote a record | Medium | **Fixed**, tested |
| The AI reader's validation error, which quotes what it returned, was saved on the ticket and returned by the API | Low | **Fixed**, tested |
| A missing or unreadable upload recorded the operating system's error, including the absolute path, and the API returned it | Low | **Fixed** with the storage service, tested |
| A caller-supplied `X-Request-ID` went into logs and headers unchecked | Low | **Fixed**, tested |
| Viewers can create saved views, which every colleague in the company sees. They hold only filters and column choices | Low | Accepted |
| A `project_id` filter naming another company's project returns empty results, not 404. Nothing about that project is revealed | — | Accepted |
| A duplicate-approval 409 names the other ticket's supplier and number, to someone in the same company | — | By design |
| Stored PDFs and images are served inline from the API's own origin, with `nosniff` and the type they were checked as. A hosted deployment should serve them from a separate origin or with a sandboxing policy, tested against the browsers' PDF viewers | Medium for hosting | Open: checklist item 3 |
| `/api/status` tells any signed-in role which AI model is configured | Low | Accepted |

## Required before a hosted customer pilot (priority order)

What this list was before this round is kept below it. In short, for a secure hosted deployment, in order:

1. **Independent security review and penetration test** of the real deployment, before any real customer document is uploaded, and findings fixed. Everything else on this list is what that review should find already done.
2. **PostgreSQL, run as `TICKETFLOW_ENV=production`.** The copy tool (`scripts/migrate_sqlite_to_postgres.py`) has only been run on fictional data. Least-privilege database credentials for the app, separate from the migration role. Row-level security as defense in depth (see the hosted proposal).
3. **Private document storage** outside the app server, encrypted, never public, served only through the authorized API and from a separate origin or sandboxed. An object-storage driver beside `services/storage.py`.
4. **HTTPS end to end**, secure cookies, HSTS (sent automatically once cookies are secure), exact origins and hosts, explicit proxy trust. Tighten the frontend's content security policy with nonces.
5. **Limits at the edge**: request size and rate, in the reverse proxy or load balancer. The application's own limits are per process.
6. **Shared login throttling and rate limits** across processes (PostgreSQL or a small shared store).
7. **Backups**: encrypted, automated, off the server, with the restore drill run before the first customer and then on a schedule. `backup.py` doesn't encrypt and doesn't handle PostgreSQL.
8. **Monitoring and incident response**: alerting on errors, `/health/ready`, failed jobs and stale backups; a written incident plan; read access to documents logged.
9. **Secrets** in a secret manager, rotated; dependency and vulnerability scanning in CI.
10. **Accounts**: email invitations and reset, MFA for owners, SSO for larger customers.
11. **Vendors and privacy**: document owners' written permission, AI provider retention terms, privacy policy, customer agreement, retention and deletion practice.

### The earlier list

1. **PostgreSQL and a deterministic migration baseline.** The first Alembic
   revision builds tables from the *current* models
   (`Base.metadata.create_all`), and API startup also calls `create_all`.
   Fresh databases therefore depend on today's code, not the migration
   history. Freeze an explicit baseline, remove startup `create_all`, run CI
   against PostgreSQL, add tenant-isolation tests on PostgreSQL, and build a
   read-only SQLite→PostgreSQL copy tool. Rehearse backup and restore.
2. **Private document storage.** Files are stored by absolute local path
   (`storage_path`, `crop_path`). Introduce a storage interface with object
   keys, a local driver, and a private S3-compatible driver with encryption,
   no public object URLs, and lifecycle retention. Serve images only through
   the authorized API.
3. **Worker and queue.** Replace the single-machine file-locked worker with
   database-level claiming (`FOR UPDATE SKIP LOCKED`), job leases/heartbeats,
   a retry cap, idempotency around paid AI calls, concurrency and cost limits,
   and monitoring.
4. **Transport and production configuration.** HTTPS only;
   `SESSION_SECURE_COOKIE=true`; exact JSON `CORS_ORIGINS` and `TRUSTED_HOSTS`
   (never wildcards); HSTS; explicit proxy trust. Tighten the frontend CSP to
   restrict script and connect sources using Next.js nonces.
5. **Request limits at the edge.** Body size and request-rate limits in the
   reverse proxy, in addition to the application's own checks.
6. **Distributed rate limiting.** Login throttling is per-process memory;
   multiple API instances need a shared store.
7. **Operations.** Structured logs with request IDs (no document contents or
   personal data), error alerting, a health check that verifies the database,
   automated encrypted backups with restore drills, dependency/vulnerability
   scanning, secret management with least-privilege credentials.
8. **Account recovery and stronger sign-in.** Email-based invitations and
   password reset (needs an email provider), optional MFA for owners, SSO for
   larger customers.
9. **Document privacy and vendors.** Written document-owner permission before
   collecting scans or sending them to Gemini or any AI vendor; review the
   provider's retention and data-use terms; publish a privacy policy,
   customer agreement, retention/deletion practice and incident response plan.
10. **Independent review.** External security review / penetration test of the
    hosted deployment before real customer documents are uploaded.

## Accounting-output safety (permanent rules)

- Quantities count only after explicit human approval; nothing is approved
  automatically.
- Every scanned slot is kept on record. Unclassified faint slots, failed files
  and paper-count mismatches keep a project's invoice comparisons incomplete.
- Unresolved quantities, missing rates and unclassified tickets are shown,
  never hidden or replaced with zero.
- Invoice ticket numbers are matched only exactly and only to one verified,
  complete delivery in the invoice line's scope; ambiguous numbers are never
  matched. A totals-only comparison is always labelled as not checking
  individual tickets.
- A drafted invoice line counts for nothing until a manager confirms it. Units
  are never converted, and credits are blocked rather than entered.
- A delivery can be linked to at most one invoice line, enforced by a database
  index. Partial allocation is not supported.
- Arithmetic agreement between an invoice and tickets is not proof of invoice
  authenticity and is never an approval to pay.

## Known, accepted limitations of the current release

- Usernames are unique across all companies. Choosing a taken name returns
  "not available" without saying which company holds it.
- The per-account lockout lets someone who knows a username delay that
  person's sign-in for up to 15 minutes. That's an accepted trade-off against
  distributed password guessing.
- Role checks in the browser are only for usability; the API is the
  authority. Every new mutating endpoint must declare a permission (enforced
  by `test_every_mutating_api_route_declares_a_permission`).
- Rate changes made before migration `20261103_08` have no recorded history.
