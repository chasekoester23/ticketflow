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

The original code (`main` at `3ed0018`) and the final code (`feature/c7-integration` at `d68c536`) were measured one after the other on 8 November, on the same quiet machine: a 15-inch MacBook Air with an M2, 8 cores and 8 GB of memory. The load average was 4 to 6, since nothing else was running. Both used Python 3.13 with CI's pinned packages and the same `measure_performance.py`. Each request runs in a fresh process, which gives a cold application but a warm operating-system file cache. One company holds 90% of the tickets and another 10%, so company scoping does real work. All data is fictional.

An earlier attempt is discarded. It ran while 12 leftover CPU-bound processes were active, and the baseline from 5 November had the same problem, so neither is comparable.

### 100,000 tickets

| Request | Before: s / peak MB | After: s / peak MB |
|---|---:|---:|
| Dashboard | 0.68 / 235 | 0.25 / 157 |
| Register, first page | 0.42 / 188 | 0.13 / 156 |
| Register, deep page | 0.42 / 200 | 0.13 / 156 |
| Register, one project | 0.12 / 157 | 0.12 / 153 |
| Search and sort by tons | 0.45 / 170 | 0.35 / 156 |
| Invoice list, 20 lines | 0.35 / 147 | 0.21 / 161 |
| Project report | 0.35 / 154 | 0.26 / 148 |
| Pilot report | 1.27 / 272 | 0.69 / 158 |
| CSV export, everything | 2.54 / 388 | 1.31 / 166 |
| Excel export, everything | 12.67 / 689 | 8.59 / 163 |
| Edit one ticket | 0.040 | 0.031 |
| Approve one ticket | 0.039 | 0.049 |

At 1,000 to 50,000 tickets, every request is under 0.5 s in both versions, except the full Excel export (5.8 s down to 4.5 s at 50,000). Peak memory now stays near the 150 MB of an idle process at every size.

### 1,000,000 tickets

| Request | Before: s / peak MB | After: s / peak MB |
|---|---:|---:|
| One batch's tickets | 4.6 / 158 | 3.2 / 182 |
| Dashboard | 12.7 / 919 | 7.7 / 153 |
| Register, first page | 7.8 / 555 | 4.2 / 154 |
| Register, deep page | 9.0 / 560 | 4.1 / 155 |
| **Register, one project** | 4.4 / 176 | **7.8 / 154** |
| **Register, one project, sort by tons** | 5.4 / 175 | **11.3 / 153** |
| Search a ticket number | 9.7 / 138 | 10.7 / 153 |
| Search and sort by tons | 10.8 / 282 | 10.6 / 154 |
| Invoice list, 20 lines | 6.8 / 156 | 7.7 / 156 |
| **Project report** | 8.4 / 177 | **16.3 / 156** |
| Pilot report | **Error** after 21.6 s, at 1,149 MB | 37.6 / 152 |
| CSV export, one project | 3.5 / 301 | 2.4 / 164 |
| CSV export, everything | 46.9 / 1,397 | 13.1 / 433 |
| Excel export, one project | 8.7 / 509 | 6.9 / 157 |
| Excel export, everything | 244.7 / 1,716 | Refused in 0.5 s: over 200,000 rows, use CSV |
| Edit one ticket | 0.98 | 2.5, then **0.004** with the image index |
| Approve one ticket | 0.98 | 8.8, then **0.006** with the image index |

**Image index.** This run found that approvals and edits had become slower at 1,000,000 tickets. Each one checks for copies of the same scanned image, and SQLite chose to answer that by walking every ticket the company has: 2.1 s per check. Migration `20261106_22` adds an index on company and image, and a plan test fails without it. On the same 1,000,000-ticket database, measured the same way, edit went from 2.0 s to 0.004 s and approve from 2.0 s to 0.006 s. The other rows were measured before this index; it doesn't change their plans.

**Slower than before** (bold above): views filtered to one project, and the project report. Project 1 holds about 50,000 tickets at this size. The new totals and status counts each read all of them to add up exact figures, where the old code read fewer columns. Each query takes 2 to 5 s. Making these fast on SQLite would need summary tables or wider covering indexes. On PostgreSQL, at the same size, the project report took 0.5 s (table above). That run didn't measure the register filtered to one project. Treat SQLite as suitable for one company's local use up to a few hundred thousand tickets, and PostgreSQL beyond that.

**Still slow at 1,000,000, in both versions:** text search (about 10 s, since ILIKE reads every ticket) and the pilot report (now 38 s, where it used to fail).

Reproduce one version with `python scripts/measure_performance.py --tickets 100000 1000000 --timeout 300` from its `backend/` folder.
