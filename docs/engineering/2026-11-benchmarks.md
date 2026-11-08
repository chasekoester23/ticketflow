# Benchmarks, November 2026

**Fictional data, on a shared laptop and on GitHub's runners. These figures compare
runs and show where limits are. They are not a promise about production performance.**
Each figure carries the machine's load average, because other work was running beside it.

How they were made:

- `backend/scripts/load_test.py`: a real uvicorn server (one process) on a new database,
  N signed-in employees over real HTTP for 30 seconds each, reading, searching, editing,
  approving and exporting in fixed shares (55, 12, 12, 14, 7 percent; clerks edit instead
  of approving). Afterwards it checks from the database and from the dashboard, register,
  pilot report and CSV export that approved tons and cost are exact, and that no scan or
  approval is recorded twice.
- `backend/scripts/measure_performance.py`: one request per fresh process, at each size.
- `backend/scripts/load_test.py --explain`: PostgreSQL's EXPLAIN ANALYZE for every SELECT
  each read makes, on the 100,000-ticket database.

## Simultaneous employees, SQLite, this laptop

Apple M2, 8 cores, 8 GB, macOS 15.2, Python 3.13.3, SQLite 3.49.1. 20,000 fictional
tickets. Measured 2026-11-08 00:05 EST with two other agents working on the machine.

| Employees | Requests | Per second | p50 ms | p95 ms | p99 ms | Errors | Load average (1, 5, 15 min) before → after |
|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 567 | 18.8 | 26.9 | 104.4 | 151.6 | 0 | 7.11 6.24 8.05 → 7.43 6.42 8.05 |
| 5 | 696 | 23.1 | 128.4 | 498.4 | 657.1 | 0 | 6.99 6.34 8.02 → 10.44 7.20 8.26 |
| 20 | 592 | 19.1 | 838.4 | 1926.5 | 2436.8 | 0 | 11.85 7.54 8.38 → 11.88 7.95 8.50 |

After every run: totals exact in every view, nothing approved or counted twice, no
errors, no refused approvals. Throughput stops growing after about five people: one
server process and SQLite's single writer take turns, so more people mean longer waits,
not more work done. At 20 people the slowest reads (the register and project report)
pass two seconds at p99.

Per action at 20 employees (p50 / p95 / p99 ms): reads 1020 / 2079 / 2523, search
1175 / 1819 / 2336, edit 514 / 1049 / 1261, approve 568 / 1727 / 2063, CSV export of
one project 573 / 1237 / 1265.

One thing the checks found in the benchmark data, not the application: the generator
in `measure_performance.py` rounds each seeded ticket's stored cost half to even, while
the application rounds half up. 28 of the seeded approved tickets therefore store a cost
one cent off tons times rate, so a report summing stored costs and an export multiplying
tons by rate disagree by a few cents on seeded data. Tickets approved through the
application during the runs all agree.

## Simultaneous employees, PostgreSQL, GitHub runner

`Load and scale on PostgreSQL` workflow, commit 305344f, 2026-11-08 05:05 UTC.
GitHub's ubuntu-24.04 runner (Linux x86_64, 4 vCPUs), PostgreSQL 16 in a service
container on the same runner, Python 3.12, 20,000 tickets, 30 seconds per run.

| Employees | Requests | Per second | p50 ms | p95 ms | p99 ms | Errors | Load average before |
|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 393 | 13.0 | 51.1 | 125.2 | 197.9 | 0 | 0.91 0.26 0.09 |
| 5 | 667 | 22.1 | 192.7 | 379.2 | 450.3 | 0 | 1.21 0.42 0.15 |
| 20 | 650 | 21.0 | 830.5 | 1413.9 | 1672.3 | 0 | 2.39 0.85 0.31 |

Totals exact in every view after each run, nothing approved or counted twice. The same
pattern as SQLite: one server process saturates at about 22 requests a second, so a
fourth of the people waiting is the queue, not the database. More server processes
are the next thing to measure; nothing here has.

## measure_performance.py on PostgreSQL

Same runner and commit. Seconds for one request in a fresh process; peak memory
includes the process's own ~166 MB.

| Request | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---:|---:|---:|---:|
| Generating the data | 0.4 | 1.4 | 9.9 | 96.1 |
| Dashboard | 0.14 | 0.17 | 0.21 | 0.93 |
| Register, deep page | 0.14 | 0.19 | 0.17 | 0.43 |
| Search a ticket number | 0.14 | 0.18 | 0.27 | 1.43 |
| Search and sort by tons | 0.15 | 0.19 | 0.28 | 1.39 |
| Project report | 0.18 | 0.17 | 0.23 | 0.50 |
| Pilot report | 0.20 | 0.23 | 0.34 | 1.53 |
| Invoice list, 20 lines | 0.17 | 0.16 | 0.16 | 0.34 |
| CSV export, one project | 0.14 | 0.12 | 0.29 | 1.47 |
| CSV export, everything | 0.15 | 0.26 | 2.57 | 15.8 (470 MB) |
| Excel export, one project | 0.11 | 0.16 | 0.74 | 6.4 |
| Excel export, everything | 0.25 | 1.24 | 11.1 | refused, as designed (over 200,000 rows; points to CSV) |
| Edit one ticket, approve one ticket | 0.01 | 0.01 | 0.01 | 0.01 |

Edits and approvals stay at about 10 ms and 10 to 16 queries at every size. Every read
finished at 1,000,000 tickets within the 300 second limit per request. The whole job,
four sizes, took five minutes, well inside its limit.

## Query plans at 100,000 tickets

`load_test.py --explain`: every SELECT of each read under EXPLAIN ANALYZE.

Used as intended: `ix_tickets_org_date_recent` for the register's newest-first pages,
`ix_tickets_org_batch_status` (from c59ab6e) for one project's register and exports,
`ix_tickets_org_number` and `ix_tickets_org_load` for duplicate checks,
`ix_tickets_org_scope` and `ix_tickets_org_loose` for invoice matching,
`ix_tickets_org_date` for whole-company exports, `uq_invoice_ticket_link_active` for links.
Invoice list and overview read no table in full.

Full scans of the company's tickets (88,257 of the 100,000 belong to it) remain in the
dashboard, the register's totals, name variants, the project report and the pilot
report: each adds up the whole company or project, so PostgreSQL reads every row once.
That is cheap at this size (slowest statement 27 to 58 ms) and is what grows at a million.
The pilot report also reads all 100,000 scan slots, and exports by project still join
batches and uploaded files by scanning them (4,719 rows). Text search (`q=`) is an
ILIKE match, which no ordinary index serves: 1.4 s at a million tickets.
`ix_tickets_organization_id` was used by none of these reads, since every query also
filters on something the composite indexes cover.

## SQLite before and after

<!-- PLACEHOLDER: the coordinator fills this section in, measured on a quiet machine. -->
**To be added by the coordinator.** Not measured by the testing branch.
