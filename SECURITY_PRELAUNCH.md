# TicketFlow pre-launch security status

Last updated: 2026-11-03 (team accounts release, migration `20261103_07`).

TicketFlow handles confidential construction documents and money figures.
This file records **what is actually implemented and tested**, and **what is
still required** before any hosted customer pilot. Passing tests are not a
security review. No independent review or penetration test has been done.

**Current safe use:** a single trusted machine on a private network, with
permissioned documents. **Not yet safe:** a public or shared cloud server.

## Implemented controls

| Area | Control | Where |
|---|---|---|
| Authentication | Salted scrypt password hashes; 12+ character passwords | `backend/app/auth.py` |
| Sessions | Random opaque tokens stored only as SHA-256 digests; HTTP-only, SameSite=Lax cookie; 12 h expiry; server-side logout; new token on every sign-in; expired sessions purged at sign-in | `auth.py` |
| Session revocation | Deactivation, role change and password reset end the person's sessions immediately; changing your own password ends your other sessions | `services/accounts.py` |
| Brute force | Per (address, username) lockout (8 failures / 15 min) plus per-account lockout (20 / 15 min); bounded memory without flushing every lockout; unknown usernames take the same time as wrong passwords | `auth.py` |
| CSRF | Custom `X-TicketFlow-Request` header required on mutating `/api` requests; `Origin` checked against `CORS_ORIGINS` | `main.py` |
| Company isolation | Company ID always from the signed-in user, never from the request; cross-company IDs return 404; two-company regression tests | all routers; `tests/test_organization_isolation.py` |
| Authorization | Named permissions per role (owner / manager / clerk / viewer), enforced server-side; a test fails if any mutating API route lacks a permission | `app/permissions.py`; `tests/test_team_accounts.py` |
| Accountability | Ticket edits and approvals, uploads, invoice-line changes and team changes record the acting person; older history is explicitly unattributed, never guessed | `models.py`, `routers/api.py`, `routers/invoices.py` |
| Onboarding | Owner-issued temporary passwords (about 120 bits, shown once); the API is blocked until the person sets their own password | `routers/team.py` |
| Lockout safety | A company always keeps one active owner; owners cannot deactivate or demote themselves; terminal-only recovery script, audited | `services/accounts.py`, `scripts/reset_password.py` |
| Uploads | Extension allow-list, magic-byte check, 25 MB per-file limit, whole selection rejected if any file is invalid, random internal file names | `routers/api.py` |
| Caching | `Cache-Control: private, no-store` and `nosniff` on all API responses | `main.py` |
| Exports | CSV formula-injection neutralization for invoice reports | `routers/invoices.py` |
| Secrets | Keys only from environment / `.env` (git-ignored); never returned by the API | `config.py` |

## Required before a hosted customer pilot (priority order)

1. **PostgreSQL and a deterministic migration baseline.** The first Alembic
   revision currently builds tables from the *current* models
   (`Base.metadata.create_all`), and API startup also calls `create_all`.
   Fresh databases therefore depend on today's code, not the migration
   history. Freeze an explicit baseline, remove startup `create_all`, run CI
   against PostgreSQL, and build a read-only SQLite→PostgreSQL copy tool.
2. **Private document storage.** Files are stored by absolute local path
   (`storage_path`, `crop_path`). Introduce a storage interface with
   object keys, a local driver and a private S3-compatible driver with
   server-side encryption; serve images through the authorized API only.
3. **Worker hardening.** Limit PDF page count and rendered pixel size before
   rasterizing (a small PDF with thousands of pages can exhaust worker memory
   today), add job leases/heartbeats and a retry cap, and replace the local
   file lock with database-level claiming (`FOR UPDATE SKIP LOCKED`).
4. **Transport and browser headers.** HTTPS only, `SESSION_SECURE_COOKIE=true`,
   HSTS, `frame-ancestors 'none'` / `X-Frame-Options: DENY` and a Content
   Security Policy on the frontend (currently none), `Referrer-Policy`.
5. **Request limits at the edge.** Total request body size and file-count
   limits in the reverse proxy, not just per-file checks in the app.
6. **Distributed rate limiting.** Login throttling is per-process memory;
   multiple API instances need a shared store. Configure trusted proxy
   headers before relying on client addresses.
7. **Monitoring and backups.** Structured logs with request IDs (without
   document contents), error alerting, a health check that verifies the
   database, automated encrypted backups with a tested restore procedure.
8. **Account recovery and stronger sign-in.** Email-based invitations and
   password reset (needs an email provider), optional MFA for owners, SSO
   for larger customers.
9. **Data handling agreements.** Written customer authorization before any
   scan is sent to an AI provider; document retention and deletion policy;
   confirm the AI provider's data-use terms for the paid tier in use.
10. **Independent review.** External security review / penetration test of
    the hosted deployment before real customer documents are uploaded.

## Known, accepted limitations of the current release

- Usernames are unique across all companies. Choosing a taken name returns
  "not available" without saying which company holds it.
- The per-account lockout lets someone who knows a username delay that
  person's sign-in for up to 15 minutes. That's an accepted trade-off against
  distributed password guessing.
- Role checks in the browser are only for usability; the API is the
  authority. All new mutating endpoints must declare a permission (enforced by
  `test_every_mutating_api_route_declares_a_permission`).
# Ticketflow security and privacy prelaunch checklist

**Status: Development / local pilot only. NOT ready to expose publicly or store live customer documents on shared hosting.**
This checklist distinguishes safeguards implemented today from infrastructure and operational work still required. Passing automated tests is **not** a penetration test or a compliance certification.

## Existing protections (verify in every release)
- Password-hashed owner login with HTTP-only sessions; logout revokes a session.
- Private API routes require an authenticated user; company-scoped DB queries prevent normal cross-company access.
- Cross-site state-changing requests require the application header and allowed origin.
- No-store API caching, explicit verification of source ticket quantities, and separate audit events.
- No AI extraction without a configured provider key; do not silently invent missing fields.

## First prelaunch hardening (this change)
- Reject API requests with unexpected Host headers. Local defaults: `localhost`, `127.0.0.1`, `testserver`. Configure exact API hostnames using JSON `TRUSTED_HOSTS` for any deployment; NEVER use `*`.
- Browser response headers include `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and `X-Content-Type-Options: nosniff`.
- Uploads are limited by file count, per-file bytes, and total batch bytes. PDF page counts, per-page raster dimensions, total rendered pixels, and image dimensions are bounded before heavy decoding. Exceeding scan limits is a visible failure, never an apparently successful empty batch.
- Newly uploaded original files and generated ticket crops are written in private per-batch directories with restrictive Unix permissions. This does not retroactively change existing files.
- Excel export protects customer-controlled ticket numbers, supplier/material/job labels, file references, and group names against formula interpretation while preserving numeric quantity/cost cells.
- Tests verify limits, allowed/unallowed hosts, file permissions, and spreadsheet safety.

## Required before any real customer-facing beta (not implemented)
1. **Cloud database & migrations:** migrate SQLite to managed PostgreSQL; run Alembic migrations using backups and restore drills; implement retention and tenant-isolation integration tests against PostgreSQL.
2. **Private object storage:** replace the current local files/worker shared directory with private S3/R2 buckets, scoped object access, encrypted transport/storage, lifecycle retention, upload scan pipeline, and recovery practices. Avoid public object URLs. Current local storage is **not** horizontally scalable.
3. **Worker and queue:** replace the single-machine file-locked worker/SQLite job queue with a production-safe worker/queue and explicit retries, idempotency, concurrency limits, cost controls, and monitoring.
4. **Authentication & authorization:** support individual company users and least-privilege roles (admin/project member/read-only); robust invitation and account recovery; external auth/security review. Current in-memory sign-in throttling is not distributed across instances.
5. **Production configuration:** deploy only over HTTPS, set `SESSION_SECURE_COOKIE=true`, `CORS_ORIGINS=["https://app.your-domain.example"]` (valid JSON), and `TRUSTED_HOSTS=["api.your-domain.example"]` (valid JSON). Do not use wildcard origins/hosts. Configure proxy trust explicitly, and consider platform TLS, HSTS and strict network isolation when ready.
6. **Document privacy & vendors:** obtain document-owner permission before collecting or transmitting scans to Gemini or any AI vendor; review the provider's retention and data-use terms; publish a privacy policy, customer agreement, deletion and retention practices, and an incident response plan. Do not use unredacted employer records without authorization.
7. **Operational security:** secret management (never commit API keys or `.env`), least-privilege service credentials, encrypted backups, restore drills, access/audit logs without document contents, error tracking without PII, vulnerability scanning, dependency lockfile reviews, staged rollouts and manual security review.
8. **Safety of accounting outputs:** maintain explicit human ticket verification, show unresolved quantities/rates and unknown tickets, and never equate arithmetic agreement to invoice authenticity or payment approval. Test invoice scenarios with authorized documents.

## Safe local upgrade

Review and merge the PR **only after GitHub checks pass**. In the correct *inner* repository
(`~/Desktop/ticket/ticketflow/ticketflow`), preserve any local edits and backups before pulling.
For this security change there is **no new database migration**. Run existing backend tests and an
end-to-end synthetic scan before using it on authorized paper tickets.
