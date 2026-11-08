# Proposal: from one local machine to a hosted service

**Status: plan for review. Nothing here has been provisioned, priced with a provider, or signed up for.** Several steps cost money or send customer documents off your machine, so each needs your approval first. Every cost below is a rough engineering estimate, not a quote.

**Recommendation, unchanged: don't start hosting until real, permissioned tickets have saved a pilot customer time locally.** If they don't, hosting is wasted. When a customer needs it, do the stages below in order.

## Where things stand

Done since the first version of this proposal, and tested on fictional data only:

| Prerequisite | State |
|---|---|
| PostgreSQL | The whole backend suite runs on PostgreSQL 16 in CI, schema built by the migrations. The API refuses to start on a database at the wrong migration |
| Moving existing data | `scripts/migrate_sqlite_to_postgres.py`: preflight, one-transaction copy, full verification, rollback on any difference. **Never run on a real database** |
| Documents | One storage service (`services/storage.py`). Rows hold paths relative to `STORAGE_DIR`, so a restored copy finds its documents. The interface is small enough for an object-storage driver; none exists |
| Worker | Jobs claimed with `FOR UPDATE SKIP LOCKED` on PostgreSQL, a retry cap, JSON logs with ids only. Still one worker per machine, behind a file lock |
| Backups | Retention, verified off-device copies, completeness checks and a restore drill, for SQLite only |
| Production settings | `TICKETFLOW_ENV=production` refuses to start without PostgreSQL, secure cookies, exact origins and hosts, and API docs off |
| Health | `/health` for liveness, `/health/ready` for readiness, and System health for people |

Not done: everything below.

## Stages

| # | Stage | Why in this order | Rough size, one engineer |
|---|---|---|---|
| 1 | PostgreSQL service, least-privilege roles, row-level security | Everything else depends on it | 1–2 weeks |
| 2 | Private, encrypted object storage | Documents must leave the app server before a second server exists | 1 week |
| 3 | Workers that can run on more than one machine | Needs 1 and 2 | 1 week |
| 4 | HTTPS, and sessions and limits shared across servers | Needs 1 | 3–5 days |
| 5 | Backups, disaster recovery, monitoring, incident plan | Must exist before any customer data | 1 week |
| 6 | Customer accounts, onboarding, billing | Needed to take a second customer | 1–2 weeks |
| 7 | Independent security review, then findings fixed | Last, on the real setup | External |

Sizes are estimates, not commitments.

## 1. PostgreSQL

- **Managed PostgreSQL 16** in one region, with automated backups, point-in-time recovery and a standby replica. Encrypted at rest. Reachable only from the private network, never from the internet.
- **Roles.** A migration role that owns the schema, used only by `alembic upgrade`. An application role that can read and write rows but not change the schema, drop tables or disable triggers, so the append-only history triggers can't be bypassed with the app's own credentials. A read-only role for support queries.
- **Connections.** Each process pools 10 connections plus 10 overflow (`DATABASE_POOL_SIZE`, `DATABASE_POOL_OVERFLOW`). Beyond a few processes, a connection pooler is likely needed. PgBouncer in transaction mode is **untested** with this app: the advisory locks in `take_turn` are transaction-scoped, which should be compatible, but that needs proving in CI first.
- Statement and lock timeouts are already set per connection, and every session runs in UTC.
- **Moving a local customer:** the copy tool, rehearsed first on a copy of their real data, with its report kept.

## 2. Tenant isolation

Today every query is scoped in application code by the signed-in person's company, and two-company tests cover each router. That is the primary control. For hosting, add a second, independent one.

**Row-level security, as defense in depth.** On every company-owned table, a policy like:

```sql
ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_rows ON tickets USING (organization_id = current_setting('ticketflow.organization_id')::int);
```

- The app sets `ticketflow.organization_id` with `SET LOCAL` at the start of each request's transaction, from the signed-in user, never from the request.
- Child tables without their own `organization_id` (uploaded files, scan slots, audit events, invoice ticket references and links, draft lines) get a policy through their parent, or gain the column. Adding the column is simpler, and faster to check.
- The application role isn't the tables' owner and doesn't have `BYPASSRLS`, so the policies really apply to it.
- The worker and the terminal scripts act for one company at a time, and set it the same way.
- **What it buys:** a forgotten `WHERE organization_id = ...` returns nothing instead of another company's records. **What it costs:** every query path has to set the variable, background ones included, and a connection pooler mustn't carry it between requests. `SET LOCAL` doesn't.
- To test it: run the isolation tests with the application-level filters removed, against a database with the policies, and expect no cross-company rows.

## 3. Private, encrypted document storage

- **An S3-compatible object store:** one bucket per environment, public access blocked at the account level, server-side encryption (provider-managed keys to start, customer-managed later if a contract asks for it), versioning, and object lock on backups.
- **A second driver beside `LocalStorage`**, with the same eight methods. Keys stay as they are (`uploads/12/x.pdf`), prefixed with the company id. `serve()` streams through the API, so every document request is still authorized and company-scoped. **No public or long-lived URLs.** If streaming ever becomes a bottleneck, use pre-signed URLs valid for under a minute, issued only after the same authorization.
- **Serve documents from a separate origin, or with a sandboxing policy**, so a hostile PDF can't act as a page of the API's own origin. Test against the browsers' PDF viewers first: a sandbox policy can stop them rendering.
- Moving local documents: copy the storage folder with checksums, then run `check_documents.py` against the new store before switching over.

## 4. Reliable workers

- More than one worker process, on more than one machine. Claiming already uses `FOR UPDATE SKIP LOCKED`.
- **Replace the file lock with a lease:** a `leased_until` column the worker extends as its heartbeat. Recovery reclaims only jobs whose lease has expired, which is what makes recovery safe with several workers. Today's `recover_interrupted` assumes it's the only worker, so it mustn't run on several machines until this lands.
- The retry cap (`MAX_JOB_ATTEMPTS`) and its visible failed state carry over.
- **Paying for AI twice:** extraction is at-least-once. Store each result keyed by crop hash and model, and reuse it on a retry.
- **Per-company limits** on concurrent jobs and monthly AI spend, enforced before calling the provider, with a clear message when one is reached.
- Workers get only what they need: the database, the object store and the AI provider. No inbound network access.

## 5. HTTPS and sessions

- TLS at the load balancer, with HTTP redirected to HTTPS. `SESSION_SECURE_COOKIE=true`, which also turns on HSTS.
- `TICKETFLOW_ENV=production`, with exact `CORS_ORIGINS` and `TRUSTED_HOSTS`. Proxy trust stated explicitly: only the load balancer's forwarded address is believed, so login throttling and first-run setup see the real client.
- **Shared limits.** Login throttling and the per-person rate limits are per process today. Move both to PostgreSQL, in a small table with expiry, to avoid adding a service. Add request-size and rate limits at the edge as well.
- Sessions stay opaque, server-side and revocable, with the 12-hour limit and the idle timeout. Add "sign out everywhere" for owners.
- Tighten the frontend's content security policy with Next.js nonces.
- First-run setup must never be reachable from the internet. Hosted companies are created by the signup flow in stage 6, and `ALLOW_REMOTE_SETUP` stays off.

## 6. Customer accounts, onboarding and billing

- **Accounts.** Email invitations and password reset (needs an email provider), replacing owner-issued temporary passwords. MFA for owners, then for anyone who approves tickets. SSO for larger customers, later.
- **Onboarding.** A signup that creates a company in a trial state, then the existing first-run checklist: project, rate sheet, team, first upload, first approval, first invoice. A sample fictional project they can delete.
- **Billing.** A payment processor holds card details, and TicketFlow never sees or stores them. TicketFlow stores only the processor's customer and subscription ids, and a plan. Usage that pricing depends on (approved tickets, pages processed, AI calls) is counted from existing tables, not tracked separately. Webhooks from the processor are verified by signature, processed idempotently, and change only plan state. A lapsed subscription makes a company read-only, and never deletes anything. **Nothing in billing touches tickets, invoices or approvals.**

## 7. Backup and disaster recovery

- **Database:** the provider's automated backups with point-in-time recovery, kept 35 days, plus a nightly `pg_dump` to a separate account, encrypted, kept 90 days.
- **Documents:** object versioning, and replication to a second region or account with object lock, so deleting a bucket can't delete its copies.
- **Order:** database first, then documents, so a restored row never points at a document that didn't exist yet.
- **Targets, to agree with customers:** under 15 minutes of data loss (recovery point), service back within 4 hours (recovery time).
- **A restore drill before the first customer, then quarterly, timed and written down.** Extend `restore_drill.py` to PostgreSQL and object storage: restore into a fresh environment, start the app, compare counts and exact totals, open a sample document of each kind.
- **Losing a region:** documented steps to stand up in a second region from the replicated backups. Not automated at first.

## 8. Monitoring and incident response

- **Health:** the load balancer uses `/health/ready`. Alert on it, on the 5xx rate, on queue age and failed jobs, on backup age, and on disk and database storage.
- **Logs:** the JSON request and worker logs already carry request, user, job and batch ids, and no document contents. Ship them to a log service with 30 to 90 days' retention, readable only by the people on call.
- **Reads:** log document access (who opened which document, and when). Today only changes are recorded.
- **An incident plan, written before the first customer:** who's paged, and how; how to revoke every session (a table delete) and rotate credentials and keys; how to tell from the audit tables which companies were affected; when and how customers are told, and what contracts and the law require; a blameless write-up afterwards.

## 9. Data retention and deletion

- **While a customer is active:** everything is kept, because approved tickets and their history are financial evidence.
- **When a customer leaves:** a full export first (records CSV, project reports and the original documents). Then their company's rows and documents are deleted after an agreed period, such as 30 days, and leave the backups as those age out. Deleting a company has to remove its history rows too, so that's an explicit, audited, owner-confirmed procedure that lifts the append-only rule for that company only.
- **AI provider:** documents go for extraction only under terms that forbid training on them and limit retention. Confirm those terms in writing.
- **Logs:** hold no document contents, so they can be kept for less time than the data.

## 10. Cost management

**Rough estimates for a small pilot (a handful of customers, under 100 GB of documents), in US dollars per month, before taxes. These are not quotes: check current pricing before deciding anything.**

| Item | Estimate |
|---|---|
| Managed PostgreSQL with a standby and point-in-time recovery | $100–400 |
| Two small API servers and one worker | $60–250 |
| Load balancer and TLS | $20–40 |
| Object storage with versioning and a replica | $10–50 |
| Backup storage in a separate account | $10–50 |
| Logs and monitoring | $0–150 |
| Email for invitations and resets | $0–20 |
| AI extraction | Per page, by provider and volume. **Has to be measured on real pilot scans** |
| **Total before AI** | **roughly $200–950** |
| Independent security review and penetration test, one-off | **roughly $5,000–25,000** |

To keep it in check: per-company AI spend limits; reusing extraction results on retry; lifecycle rules for old document versions; one region until a customer needs two; budgets with alerts at the provider.

## 11. Security review

Before any real customer document is uploaded, and again after major changes: an independent review and penetration test of the deployed system, by someone who didn't build it, covering tenant isolation, sign-in and sessions, document access, upload handling, the AI provider path and the infrastructure's configuration. Findings fixed, and `SECURITY_PRELAUNCH.md` updated to match. Passing this repository's tests is not a substitute.

## 12. Support

- Customers quote the reference shown with any error. It's the `request_id` in the logs, which hold no document contents.
- Support staff get the read-only database role, and no document access by default. Opening a customer's documents needs the customer's permission, and is logged.
- A status page, and a support address answered within one business day to start.
- Runbooks: `operations.md` for each recovery scenario, extended to the hosted setup.

## Release checklist for a hosted pilot

- [x] PostgreSQL CI green, including the concurrency tests
- [ ] Row-level security on, with the isolation tests passing without the application filters
- [ ] A copy of the pilot customer's data rehearsed, with exact money totals, and the report kept
- [ ] Object storage private and encrypted; a test proves another company gets 404
- [ ] Worker leases in place; a crash test on the hosted setup; no double-counted tickets or double-billed AI calls
- [ ] `TICKETFLOW_ENV=production`; HTTPS, secure cookies, exact hosts and origins; shared rate limits
- [ ] A restore drill on the hosted setup, done and timed
- [ ] Monitoring, alerts and the incident plan in place
- [ ] The document owner's written permission; the AI provider's retention terms reviewed
- [ ] Privacy policy, customer agreement, retention and deletion terms
- [ ] Independent security review, findings fixed
- [ ] `SECURITY_PRELAUNCH.md` updated to match reality

## Decisions needed from you

1. Whether a real customer needs hosting yet.
2. Budget, and which providers for hosting, storage, email and payments.
3. Who can legally agree to customer terms, and who handles an incident.
4. The recovery targets to offer customers.
