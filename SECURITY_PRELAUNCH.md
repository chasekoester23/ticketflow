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
