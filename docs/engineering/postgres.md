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
| Each database sorts text its own way | Report groups that a person reads are sorted in Python, so a copied database gives the same answer |
| SQLite drops the time zone from timestamps | They're marked as UTC on the way out, and PostgreSQL sessions are set to UTC whatever the server's own setting |

## Copying a SQLite database to PostgreSQL

`scripts/migrate_sqlite_to_postgres.py` makes the move, or refuses and says why. **It has only been run on fictional data, in CI. It has never been run against a real database, and nothing about a real database's contents is known until preflight has run on it.**

```bash
cd backend
# 1. Stop the API and worker, and take a backup. Then check, which changes nothing and needs no target:
python scripts/migrate_sqlite_to_postgres.py --source ticketflow.db --preflight-only --report preflight.json

# 2. Build the empty target at the current migration:
DATABASE_URL=postgresql://user@host/ticketflow python -m alembic upgrade head

# 3. Copy. Put the password in PGPASSWORD or ~/.pgpass, so it isn't in your shell history:
python scripts/migrate_sqlite_to_postgres.py --source ticketflow.db --target postgresql://user@host/ticketflow --report copy.json
```

Exit code 0 means clean or copied. 1 means refused or rolled back, and nothing was kept. The report is written with mode 0600 either way. It holds table and column names, row ids, counts, checksums and money totals, and never a ticket's values, a password hash or a session token. PostgreSQL's own error text can quote a row, so only the kind of error is reported.

**The source.** Opened read-only, with its checksum compared before and after. It's refused if it isn't at the current migration, or if a journal or write-ahead log shows something is writing to it.

**The target.** It must be PostgreSQL, at the current migration, and empty. The migrations create one placeholder company called "Original workspace", so a new database has that one row. The copy removes it inside its transaction. If it has been renamed or anything else exists, the target is refused as already in use.

**What preflight reports.** Each with row ids and no values, and with nothing guessed at or fixed:

| Kind | Meaning |
|---|---|
| `missing_reference` | A row points at something that doesn't exist. SQLite didn't enforce foreign keys until recently |
| `duplicate_rows` | Rows that break a unique rule. The source can lack the index, because migration `20261106_18` leaves one out rather than change existing rows |
| `company_mismatch` | Two linked rows disagree about which company they belong to |
| `value_too_long`, `nul_byte`, `not_utf8` | Text PostgreSQL would refuse. SQLite checks none of these |
| `too_many_decimals`, `number_too_large`, `not_a_number` | A figure its column would change or refuse |
| `integer_out_of_range`, `not_an_integer`, `not_a_boolean`, `bad_date`, `bad_timestamp`, `bad_json`, `null_in_required_column` | Values of the wrong kind |
| `missing_table`, `missing_column`, `unexpected_table`, `unexpected_column` | The schema isn't what this code expects |
| `source_not_at_current_migration`, `source_corrupt` | Upgrade it, or restore from a backup |

A person resolves these in the app, or from a backup, and runs preflight again. There is deliberately no option to skip them.

**The copy.** Every table in dependency order, in one transaction, with the target's tables locked. Ids are kept. Booleans become booleans, decimals are exact at their column's scale, JSON keeps the difference between JSON null and blank, and timestamps are UTC. Sequences restart past the highest id with `ALTER SEQUENCE ... RESTART`, which is rolled back along with everything else, where `setval` wouldn't be.

**What it proves before committing,** reading back from PostgreSQL. Any difference rolls everything back and exits 1:

- Row counts per table, and a SHA-256 over every row in canonical form.
- Each company's approved ticket count, tons and cost, exact. The source's are added up in Decimal from its rows, and the target's by PostgreSQL.
- No row in the target points at nothing, and linked rows agree about their company.
- Audit history counts per table, and audit events per company.
- Sequences are where the copy set them, and the source file still has the same checksum.

Stored documents aren't copied, because they aren't in the database. Point `STORAGE_DIR` at the same documents.

**Tests.** `test_b_migrate_sqlite_to_postgres.py`. Preflight and the copy's logic, through a SQLite stand-in target, run everywhere. The real copy runs in CI: it builds a fictional two-company business through the API on SQLite, copies it, starts the API on the target and compares the dashboard, pilot report, project reports, invoice lines, records, audit history and more for an exact match. Others tamper partway, including with a locked figure, and crash partway, then show the target is back to untouched with its sequences unmoved.

**Not tested:** a real database, a large one, or the time a large copy takes. Rehearse on a copy of the real data before relying on it.

## Schema changes

- A fresh database starts from `migrations/frozen_schema.py`, the schema at `20261105_17`, and applies every later migration. It no longer depends on the current models.
- `test_schema.py` builds a database through the migrations and compares it with the code, on both databases.
- On PostgreSQL the API refuses to start unless the database is at the expected version, and never creates tables itself. SQLite local installs can still create tables at first start.
