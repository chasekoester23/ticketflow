# PostgreSQL

TicketFlow runs on SQLite for local installs and is being prepared for PostgreSQL for hosting. Nothing is hosted yet.

## What's verified

The whole backend suite runs on PostgreSQL 16 in CI on every push (`.github/workflows/postgres-tests.yml`), with the database built by the real migrations. That includes the forced-race tests in `test_concurrency.py`, which use real connections and real transactions there.

**Not verified:** load from many simultaneous users, a production-sized PostgreSQL server, failover, and connection poolers such as PgBouncer. See [the benchmarks](2026-11-benchmarks.md) for what's been measured.

## Running the tests on PostgreSQL yourself

```bash
pip install -r backend/requirements-postgres.txt
PG_BIN=/opt/homebrew/opt/postgresql@16/bin backend/scripts/pg_test_env.sh start   # prints a TEST_DATABASE_URL
export TEST_DATABASE_URL=...                                                      # paste what it printed
cd backend && python -m pytest -q
PG_BIN=/opt/homebrew/opt/postgresql@16/bin scripts/pg_test_env.sh stop
```

`TEST_DATABASE_URL` must name a database with `test` in its name, because every table in it is erased repeatedly. The script runs a private server in a temporary folder, on its own port, and `stop` removes it.

This script has not been run locally yet, because PostgreSQL isn't installed on the development machine and installing it was left for you to decide. CI runs the same tests against a service container.

## How the two databases differ, and what the code does about it

| Difference | Handling |
|---|---|
| SQLite stores decimals as floats. PostgreSQL's are exact | Sums add integer thousandths or cents, so both give the same exact answer. `test_exact_totals` compares them with Decimal arithmetic |
| SQLite enforces foreign keys only if asked, per connection | `enforce_references` asks, on the app's engine. Migrations use their own engine, so table rebuilds can't cascade |
| SQLite doesn't enforce string lengths or reject NUL bytes. PostgreSQL does | Inputs are validated. Anything that still reaches the database gets a 422, never a 500 |
| Row locks: PostgreSQL has them, SQLite has one writer | `take_turn` and the version check on tickets work the same way on both |
| Blank values sort first or last depending on the database | Sorts say `NULLS LAST` |
| PostgreSQL doesn't advance an id sequence for an explicit id | Only tests insert fixed ids, and a test-only trigger handles them. The copy tool sets sequences itself |

## Schema changes

- A fresh database starts from `migrations/frozen_schema.py`, the schema at `20261105_17`, and applies every later migration. It no longer depends on the current models.
- `test_schema.py` builds a database through the migrations and compares it with the code, on both databases.
- On PostgreSQL the API refuses to start unless the database is at the expected version, and never creates tables itself. SQLite local installs can still create tables at first start.
