# Proposal: moving from one local machine to a hosted pilot

**Status: proposal for review. No hosting code exists, and nothing here has been provisioned or priced.** Each part needs your approval first, because several involve paid services and sending customer documents off your machine.

**Recommendation: don't start this yet.** Run real, permissioned tickets locally first. If real tickets don't save time after review, hosting is wasted. If a customer needs it, do the parts below in order.

## Order of work

| # | Work | Why this order | Rough size |
|---|---|---|---|
| 1 | PostgreSQL and a fixed migration baseline | Everything else depends on it | 1–2 weeks |
| 2 | Private object storage | Files must leave the app server before a second server exists | 1 week |
| 3 | Worker leasing | Needs PostgreSQL | 1 week |
| 4 | Backups and a restore drill | Must exist before any customer data | 3–5 days |
| 5 | Shared login throttling, HTTPS, production settings | Required once more than one server runs | 3–5 days |
| 6 | Independent security review | Last, on the real setup | External |

Sizes are engineering estimates, not commitments. They assume one person.

## 1. PostgreSQL and deterministic migrations

**Today:** the first Alembic revision and API startup both call `Base.metadata.create_all`, so a new database depends on today's models, not the migration history. Some migrations use SQLite-specific paths. Nothing has run on PostgreSQL. Tests use SQLite only, and concurrency has only been tested there.

**Plan:**
- Freeze an explicit baseline revision, and remove `create_all` from startup. The app then only checks the schema version.
- Run the backend test suite in CI against PostgreSQL. Re-run the ticket link concurrency test there. Partial unique indexes already declare both dialects, but that's untested.
- Add a read-only SQLite-to-PostgreSQL copy tool that compares row counts and money totals, and refuses to report success if they differ.
- Review approval and retry paths for concurrency. SQLite serializes writers, so some races can't show up today. `test_ticket_evidence` covers links only.
- Keep SQLite working for local use.

**Risk:** subtle differences in decimals, datetimes and JSON. Money totals must be compared exactly during the copy.

## 2. Private object storage

**Today:** `storage_path`, `crop_path` and scan slot paths are absolute local paths.

**Plan:**
- Add a small storage interface with `put`, `get` and `exists`. Keep a local driver, and add a private S3-compatible driver.
- Migrate paths to object keys with a company prefix. Keep serving files through the authorized API, so **no public or long-lived URLs**. Tenant checks stay in one place.
- Server-side encryption, no public bucket access, and a retention rule agreed with the customer.

**Decision needed:** which provider. It costs money, and documents would leave your machine.

## 3. Worker leasing and idempotence

**Today:** one worker holds an OS file lock. AI calls are at-least-once, so a crash can repeat a paid call.

**Plan:**
- Claim jobs with `FOR UPDATE SKIP LOCKED`, plus a lease and heartbeat so an expired job can be reclaimed.
- Cap retries and move repeated failures to a visible state, never silently dropping them.
- Key extraction by crop hash and model, so a retried job doesn't pay twice.
- Per-company concurrency and spend limits.

The scan manifest's unique `(file, page, position)` already prevents duplicate slots. `Ticket` has no matching constraint, and should get one here, after checking existing data for duplicates.

## 4. Backups and restore drills

- Encrypted automated backups, with point-in-time recovery if the provider offers it.
- Back up the database and object store together, with the database first, so restored rows never point at missing files.
- **A restore drill before the first customer, then quarterly, timed and written down.** A backup that hasn't been restored doesn't count.

## 5. Sign-in and transport

- Login throttling is per-process memory today. Move it to a shared store, or to PostgreSQL, to avoid a new service.
- HTTPS only, `SESSION_SECURE_COOKIE=true`, HSTS, exact `CORS_ORIGINS` and `TRUSTED_HOSTS`, explicit proxy trust.
- Tighten the frontend content security policy with Next.js nonces.
- Request-size and rate limits at the edge.
- Email invitations and password reset need an email provider. MFA for owners comes after that.

## 6. Incident response

Written before the first customer:

- Who gets alerted, and how.
- How to revoke all sessions, which is a table delete.
- How to find which company's data was affected. Audit tables make this possible for changes, but **reads aren't logged today**, so file-access logging needs adding.
- How and when customers are told, and what the contract requires.
- Rotating API keys and credentials.

Structured logs with request IDs and no document contents are needed for any of this.

## Later: only after pilots ask for them

**QR field uploads.** A signed, expiring link per project, opening a small mobile upload page. Uploads go to a quarantine queue a person accepts, and never directly to records. Needs rate limits, size limits, link revocation and spam protection. **It can't exist until the hosted setup does**, because it means an unauthenticated endpoint on the internet.

**Imports and integrations.** Start with what needs no partner agreement:
1. Excel/CSV export in a customer's own format, mostly done.
2. Read-only CSV import of supplier invoices or ticket logs, going through the same draft-and-confirm flow as PDFs.
3. Procore or HCSS only when a paying customer needs it. They need partner access and terms review. Never write to accounting systems automatically.

## Release checklist for a hosted pilot

- [ ] PostgreSQL CI green, including concurrency tests
- [ ] SQLite-to-PostgreSQL copy verified with exact money totals
- [ ] Restore drill done and timed
- [ ] Object storage private; a test proves another company gets 404
- [ ] Worker crash test on the hosted setup; no double-counted tickets or double-billed AI calls
- [ ] HTTPS, secure cookies, exact hosts and origins
- [ ] Document owner's written permission; AI provider's retention terms reviewed
- [ ] Privacy policy, customer agreement, incident plan
- [ ] Independent security review, findings fixed
- [ ] `SECURITY_PRELAUNCH.md` updated to match reality

## Decisions needed from you

1. Whether a real customer needs hosting yet.
2. Budget and provider for hosting and storage.
3. Who can legally agree to customer terms and handle an incident.
