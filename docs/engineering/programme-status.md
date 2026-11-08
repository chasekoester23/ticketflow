# Programme status, 8 November 2026

Where the November work stands: what's on which branch, what was tested and how, what's still broken, and what needs a decision from you. Nothing has been merged into `main`, deployed, or run against a real document. All test data is fictional. The overall assessment is in the [CTO report](2026-11-cto-report.md).

## Branches, in merge order

| Branch | What it adds | CI |
|---|---|---|
| `feature/c0-audit-and-ci-reliability` | Keyboard race fixes; CI failures that name themselves; benchmark tool | Green |
| `feature/c1-postgres-foundation` | Backend runs on PostgreSQL; migrations independent of the models | PostgreSQL job red alone; its test fixes are in c2 |
| `feature/c2-financial-integrity` | Forced-race fixes, database-enforced rules, half-up rounding | Green |
| `feature/c3-scale` | Totals in SQL, company-scoped indexes, streaming exports | Green |
| `feature/c4-backend` | PostgreSQL copy tool, worker retry cap, restorable storage, backups, security, new APIs | Green |
| `feature/c5-frontend` | Crash recovery, failure handling, validation, large batches, accessibility, setup, phones | Green |
| `feature/c6-testing` | Pinned CI packages, planted-error invoices, failure simulation, tenant sweep, load test | Green |
| `feature/c7-integration` | c4 + c5 + c6 merged, plus fixes for what c6 found | See below |

c4, c5 and c6 were built in parallel on c3. c7 merges them, which needed no conflict resolution, and adds four commits of fixes. Pull request text and compare links are in the coordinator's notes.

## Results on c7

| Suite | Where | Result |
|---|---|---|
| Backend, SQLite | Local, CI's pinned packages, Python 3.13 | 1188 passed, 18 skipped (PostgreSQL only), 8 xfailed (open bugs below) |
| Mocked browser suite | Local, production build | 250 passed, 1 skipped (opt-in timing test) |
| Real stack: whole workflow, then 3,000 records and two people | Local, merged backend | 1 of 1 and 7 of 7 passed |
| Backend, PostgreSQL, load test, PostgreSQL benchmark, browser, real stack, dependency audit | GitHub Actions | All nine workflows green on d68c536. Results for the last two commits (index, docs) are on the pull request |

## Open bugs

Each has a strict-xfail test that starts failing as soon as the bug is fixed, so the marker has to be removed then. None of them lets an approved figure change or a record cross between companies.

| Severity | Test | What happens | Suggested fix |
|---|---|---|---|
| Medium | `test_t_invoice_pipeline::test_a_pdf_holding_two_invoices_doesnt_pass_its_rows_off_as_one` | Rows from two invoices in one PDF are drafted under the first invoice's number | Flag the document when a second invoice number appears |
| Low | `test_t_failures::test_refused_simultaneous_invoice_copies_leave_no_file_behind` | The losing copy of a simultaneous duplicate upload leaves its file in storage | Wrap the flush in the same cleanup as the commit |
| Low | `test_t_failures::test_a_failed_invoice_upload_leaves_no_stored_file` | Same, when the database fails at commit | Same |
| Low | `test_t_failures::test_rotating_a_damaged_crop_gets_a_clear_answer` | Rotating a crop that won't decode gives a 500 | Catch the decode error and answer 409 |
| Low | `test_t_odd_data::test_an_implausibly_old_delivery_date_needs_a_second_look_before_approval` | A ticket dated 1900 or year 1 approves with no warning | Add a lower-bound flag beside the future-date one |
| Low | `test_t_odd_data::test_an_ancient_date_is_kept_exactly_in_every_export` | 1899-12-31 is written to Excel as day zero, which reads as a time | Write dates before 1900 as text |
| Low | `test_t_invoice_pipeline::test_adjacent_unit_and_price_columns_stay_separate` | Neighbouring "Unit" and "Price" headers merge, so the unit is lost; fails safe | Keep header cells apart when both are known words |

Known but untested: editing an invoice draft line has no revision check, so two managers can overwrite each other's unconfirmed edits. Confirming a line does check the revision, so an overwritten draft can't be confirmed by someone who didn't see it.

Fixed during integration: a PDF cut off mid-download passing for a shorter, complete one (scans and invoices); "Sand" and "SAND" both holding rates, with approval locking whichever came last; database pool exhaustion giving 500; a zero-page invoice's message. The backend branch had already fixed three more the tests found: half-written crops, a server path in an error, and a partial file after a full disk.

## Decisions for you

1. **Future-dated tickets.** The server refuses to approve a ticket dated in the future. The brief asked for a warning instead. The refusal stays, since removing a check needs your say-so. The review screen warns as soon as the date is typed, and shows the server's reason on Accept.
2. **Pillow is capped below 13.** 12.3.0 has 39 published advisories that 13.0.0 fixes, and the whole suite passes on 13. I recommend raising the cap.
3. **google-genai is pinned at 2.34.0**, the version on your machine. 3.0 is a major release that nobody has tried against the real Gemini service, which this work isn't allowed to call.
4. **npm audit:** postcss (high) and next (moderate), both at build time only. The fix needs Next 16, a major upgrade.
5. **The GitHub repository is publicly readable,** including these engineering documents. If that isn't intended, it's a free setting.
6. **Scheduled backups** aren't installed. `python scripts/backup.py --print-schedule launchd` prints a template; whether to install it is up to you.

## Upgrading your own install

Migrations `20261106_18` to `20261106_22` are additive. `18` adds unique indexes and skips any that existing rows would break, with a warning. System health then shows "Duplicate safeguards", and [operations](operations.md#duplicates-found-during-an-upgrade) says what to do.

Rehearsed on a read-only copy of your database: `20261103_07` to `20261106_22`, 4 tickets kept, and the API starts on the result. The PostgreSQL preflight on that copy was clean: 24 rows in 26 tables, nothing PostgreSQL would refuse. The original's SHA-256 and modification time were unchanged. **Nothing has been run on the live database; that's your step:**

1. Stop the backend and worker, and `cp backend/ticketflow.db backend/ticketflow-before-upgrade.db`.
2. `cd backend && python -m alembic upgrade head`
3. `python scripts/doctor.py`, then `python scripts/check_documents.py`.
4. Start everything, then `python scripts/backup.py` and `python scripts/restore_drill.py --latest`.

To undo, stop everything and put the copy from step 1 back.

## Not verified anywhere

- Any real ticket, invoice, scanner, or supplier layout. The invoice reader got 186 of 190 fields right, but only on layouts we drew ourselves.
- More than one API process. Rate limits and sign-in throttling are per process.
- A PostgreSQL server sized for production, failover, connection pooling (PgBouncer), or a copy of a large real database.
- Real users, real networks, phones, screen readers, or 200% zoom.
- Security, beyond automated tests. An independent review is still needed, and nothing here is a certification.
