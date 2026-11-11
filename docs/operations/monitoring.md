# Monitoring and alerting, without paid services

## Probes

| Path | Answers | Use it for |
|---|---|---|
| `/health/live` (and the older `/health`) | `200 {"ok": true}` while the process runs; never touches the database | Restarting a hung process |
| `/health/ready` | `200` when the database answers `SELECT 1`, else `503` with `Retry-After` | Load balancer readiness, uptime monitors |

All three say nothing else, are `no-store`, are exempt from the HTTPS redirect, and
accept `HEALTHCHECK_HOSTS` (for example `healthcheck.railway.app`) as well as
`TRUSTED_HOSTS`. Nothing else accepts those hosts.

## Railway

- Set the service's **healthcheck path to `/health/ready`**: a deploy only takes traffic
  once the database answers, and Railway restarts on failure per the restart policy
  ("on failure", a few retries).
- Logs are JSON lines (`ticketflow.request` for the API, the worker's own events), with
  request ids, route templates, statuses and user ids; never queries, document text,
  extracted values, passwords or keys (tested in `test_c_cross_company.py` and
  `test_c_internet_facing.py`, including a real uvicorn's access log).

## An external uptime monitor

Railway can't tell you it is down. Use a free external monitor (UptimeRobot, Better
Stack's free tier, or a GitHub Actions cron that curls the URL) on
`https://<app>/health/ready` every 5 minutes, alerting by email. That catches the
platform, DNS, TLS and the database being unreachable.

## Scheduled health check and webhook alerts

`python scripts/check_health.py --url https://<app>` checks, for the whole installation:
the API's readiness, the queue (pending uploads and the oldest one's age), jobs failed in
24 hours, the worker's heartbeat (once the `worker_heartbeats` table exists), and the age
of the newest complete encrypted PostgreSQL backup. It prints one JSON line and exits 1
if anything needs a person. Run it from a Railway cron service every 15 minutes.

Set `ALERT_WEBHOOK_URL` (a Slack/Mattermost/Discord-style incoming webhook, or any URL
that accepts a JSON POST) for it, for `backup_pg.py` and for `restore_pg_drill.py`.
Each failure posts `{"text", "event", "summary", "source", "at", "details"}` with counts
and reasons, never a secret, address, path or record value. Unset, nothing is sent.

Events: `backup_failed`, `backup_incomplete`, `backup_retention_failed`,
`restore_drill_failed`, `health_check_failed`.

## System Health, in the app

Owners and managers (`system.read`) see `metrics` on `/api/system/status`:

- `api`: requests, server errors and error rate **for this API process since it
  started** (each replica counts its own; a restart resets it);
- `database` and `storage`: reachable or not, and how long the check took (storage is
  asked about a key that never exists, so it works for any storage driver);
- `queue`: this company's pending uploads and the oldest one's age;
- `jobs_24h`: this company's jobs created in the last 24 hours, by status (finished,
  failed, pending, running);
- `reader_24h`: this company's tickets the AI reader failed on. Retries of temporary
  Gemini failures aren't recorded in a table yet (`retries` is `null`); they're in the
  worker's log;
- `backups`: age in hours of the newest local backup (`backup.py`) and of the newest
  PostgreSQL backup (from `backup_runs`, or the status file beside local backups).

Job and reader numbers are this company's only. The rest describe the installation and
hold nothing about any company.

## What's missing

- Error-rate alerting across replicas needs a log platform (Railway's log search, or
  a free tier elsewhere); the in-app number is per process.
- Gemini retries and costs: the usage workstream's tables will hold them.
- A written incident response plan.
