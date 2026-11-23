# Workers: leases, fairness, limits and usage

The API records each upload as a job in `processing_jobs` and never reads tickets itself. Workers (`backend/scripts/run_worker.py`) claim jobs from the database and read them.

## Two ways to coordinate

`WORKER_COORDINATION` chooses. The default, `auto`, picks by database.

| Mode | Default on | How many workers | Recovery after a crash |
|---|---|---|---|
| `local` | SQLite | One per machine. It holds an OS file lock, so a second refuses to start | At start, the new worker takes back everything left running, because no other worker can be alive |
| `database` | PostgreSQL | Any number, on any number of machines | A job whose lease has lapsed goes back to pending, checked every `min(30 s, lease ÷ 3)` |

In both modes, every claim takes a lease, every worker records itself in `worker_heartbeats`, and every commit is fenced (see below). A local worker also touches `storage/worker.heartbeat`, as before.

## Leases and fencing

- **Claiming.** On PostgreSQL, claims take turns on an advisory lock and pass over rows another transaction holds (`FOR UPDATE SKIP LOCKED`). On SQLite, the claim is one guarded `UPDATE`. The claim sets `lease_owner`, `claimed_at`, `heartbeat_at` and `lease_expires_at` (now + `WORKER_LEASE_SECONDS`).
- **Renewal.** The worker's heartbeat thread renews the lease every `WORKER_HEARTBEAT_SECONDS`, also during a long AI call.
- **Recovery.** A running job whose lease lapsed, or which has no lease because an older worker claimed it, goes back to pending. After `MAX_JOB_ATTEMPTS` it is set aside for a person, as before: only unfinished files are marked failed, and finished tickets and approvals are untouched.
- **Fencing.** A worker's session carries its job id and claim token. Before every commit it checks, holding the job's row, that the token is still current. If another worker has taken the job over, the commit is refused with `LeaseLost`, nothing more is written, and the worker moves on. The job is closed with a guarded `UPDATE` too, so an old worker can't mark a job finished or failed after losing it.
- **Idempotency.** Results were already written once per scan slot, and a person's edit to a failed ticket wins over a read that finishes later. `test_cloud_workers.py` proves this with a lease lost mid-job, with three workers at once, and with real worker processes, one of them killed.

AI calls are at-least-once: a ticket being read when its worker dies is read again by the next holder, and both reads are recorded as usage.

## Fairness and limits

The next job goes to the company with the fewest jobs running, then the company served longest ago (within the last 6 hours), then the oldest job. One company's backlog therefore can't hold up another, even with a single worker.

| Setting | Default | Meaning |
|---|---|---|
| `MAX_CONCURRENT_JOBS_PER_ORGANIZATION` | 0 (no limit) | Jobs of one company running at once. Exact on both databases. 1 or 2 is sensible when hosting |
| `MONTHLY_TICKET_READS_PER_ORGANIZATION` | 0 (no limit) | Reader calls per company per calendar month, UTC. Failed calls count, since they cost too. Tickets over the limit aren't read: each says why and stays open to enter by hand or retry. Checked when a job starts, so jobs of one company running at once can together pass it by up to their size |
| `WORKER_LEASE_SECONDS` | 90 | How long a silent worker keeps its job |
| `WORKER_HEARTBEAT_SECONDS` | 10 | Keep it under a third of the lease |

## Usage and cost

Every reader call writes one `extraction_usage` row in the same commit as its ticket: company, batch, ticket, model, attempts, prompt, output and thinking tokens, seconds and outcome (`ok` or the failure's kind). Rows never hold a value read from a ticket.

`GET /api/system/usage` (permission `usage.read`, owners only) reports this month for the caller's company: tickets read, failed reads, tokens by model, and estimated cost. Cost uses `MODEL_PRICES`, US dollars per million tokens, for example `MODEL_PRICES='{"gemini-3.1-pro-preview": {"input": "…", "output": "…"}}'` from your provider's price page. Thinking tokens are priced as output. With no price set for a model, cost is `null` and the report says it's unknown. **No prices are built in.**

## System Health

`/api/system/status` reports the worker as running if any worker's row in `worker_heartbeats` was updated recently, or the local heartbeat file was touched recently, and how many workers are live. It never says which company's job a worker holds, or its host name.

## Measured

`scripts/worker_load_test.py` starts a real API server and real worker processes, has several companies upload at once, kills one worker partway with `--kill-one`, and checks exact totals, no doubled tickets, no cross-company access and the per-company limit. It runs on PostgreSQL in CI (`.github/workflows/worker-load-test.yml`) and on SQLite locally. The stand-in reader takes a fixed time, so the speeds aren't Gemini's.

## Not done

- Workers on SQLite in `database` mode work (the load test runs that way locally), but SQLite has one writer, so it is for testing, not hosting.
- A deploy sends SIGTERM: the worker takes no new job and finishes its current one if it has time. If it's killed first, the job waits for its lease to lapse.
