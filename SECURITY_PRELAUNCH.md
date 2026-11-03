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
