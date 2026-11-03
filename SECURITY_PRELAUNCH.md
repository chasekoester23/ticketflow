# TicketFlow pre-launch security status

Last updated: 2026-11-03 (pilot hardening release, migration `20261103_08`).

TicketFlow handles confidential construction documents and money figures.
This file records **what is actually implemented and tested** and **what is
still required** before any hosted customer pilot. Passing automated tests is
not a penetration test, a security review, or a compliance certification. No
independent review has been done.

**Status: development / local pilot only.** Safe use today is a single
trusted machine on a private network, with documents whose owners have given
permission. Do **not** expose TicketFlow publicly or store live customer
documents on shared hosting yet.

## Implemented controls (verify in every release)

| Area | Control | Where |
|---|---|---|
| Authentication | Salted scrypt password hashes; 12+ character passwords | `backend/app/auth.py` |
| Sessions | Random opaque tokens stored only as SHA-256 digests; HTTP-only, SameSite=Lax cookie; 12 h expiry; server-side logout; new token on every sign-in; expired sessions purged at sign-in | `auth.py` |
| Session revocation | Deactivation, role change and password reset end the person's sessions immediately; changing your own password ends your other sessions | `services/accounts.py` |
| Brute force | Per (address, username) lockout (8 failures / 15 min) plus per-account lockout (20 / 15 min); bounded memory without flushing every lockout; unknown usernames take the same time as wrong passwords | `auth.py` |
| CSRF | Custom `X-TicketFlow-Request` header required on mutating `/api` requests; `Origin` checked against `CORS_ORIGINS` | `main.py` |
| Host header | Requests with an unexpected `Host` are rejected. Local defaults: `localhost`, `127.0.0.1`, `testserver`. Deployments must list exact API hostnames in JSON `TRUSTED_HOSTS`; wildcards are refused at startup | `config.py`, `main.py` |
| Company isolation | Company ID always from the signed-in user, never from the request; cross-company IDs return 404; two-company regression tests | all routers; `tests/test_organization_isolation.py` |
| Authorization | Named permissions per role (owner / manager / clerk / viewer), enforced server-side; a test fails if any mutating API route lacks a permission | `app/permissions.py`; `tests/test_team_accounts.py` |
| Accountability | Ticket edits and approvals, uploads, invoice-line changes, team changes **and supplier/material/rate changes** record the acting person; older history is explicitly unattributed, never guessed. Excel exports include who approved each ticket and when | `models.py`, `routers/*.py`, `services/export.py` |
| Onboarding | Owner-issued temporary passwords (about 120 bits, shown once); the API is blocked until the person sets their own password | `routers/team.py` |
| Lockout safety | A company always keeps one active owner; owners cannot deactivate or demote themselves; terminal-only recovery script, audited | `services/accounts.py`, `scripts/reset_password.py` |
| Uploads | Extension allow-list, magic-byte check; limits on file count, per-file bytes, total batch bytes and file-name length; whole selection rejected if any file is invalid; random internal file names | `routers/api.py`, `config.py` |
| Scan processing | PDF page count, per-page rendered pixels, total rendered pixels and image dimensions are bounded **before** heavy decoding; exceeding a limit is a visible failure, never an empty "success" | `services/splitter.py`, `services/pipeline.py` |
| File privacy | New uploads and ticket crops are written with mode 0600 in 0700 per-batch directories (not retroactive for older files) | `routers/api.py`, `services/pipeline.py` |
| API headers | `Cache-Control: private, no-store` on `/api`; `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` | `main.py` |
| Page headers | Every frontend page sends `Content-Security-Policy: frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, and no `X-Powered-By` | `frontend/next.config.mjs` |
| Exports | Spreadsheet-formula neutralization for user-controlled text in Excel (including approver names) and invoice CSV reports; numbers stay numeric | `services/export.py`, `routers/invoices.py` |
| Secrets | Keys only from environment / `.env` (git-ignored); never returned by the API | `config.py` |

## Required before a hosted customer pilot (priority order)

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
- Unresolved quantities, missing rates and unclassified tickets are shown,
  never hidden or replaced with zero.
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
