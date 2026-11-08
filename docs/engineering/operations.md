# Operating TicketFlow

For whoever runs an installation. Everything here is local. Hosting is covered in the [proposal](hosted-architecture-proposal.md).

## What has to be running

| Process | Command, from `backend/` | Without it |
|---|---|---|
| API | `uvicorn app.main:app --port 8000` | Nothing works |
| Worker | `python scripts/run_worker.py` | Uploads wait in the queue. Nothing is lost |
| Web | `npm run dev` or `npm start`, from `frontend/` | Nothing to look at |

**System health** in the sidebar (managers and owners) shows whether the worker is alive, what's queued, the database version, disk space and how old the latest backup is. Each problem says how to fix it.

## Backups

```bash
cd backend
python scripts/backup.py                                    # to backend/backups/, or set BACKUP_DIR
python scripts/backup.py --keep 14 --copy-to /Volumes/Off   # the everyday form
python scripts/verify_backup.py backups/ticketflow-20261105-120000
```

- Safe while everything is running. It reads the live database only and always writes to a new folder.
- Each backup has the database, the stored documents, and a manifest with checksums, record counts and each company's exact approved totals. Folders and files are private (0700 and 0600).
- `verify_backup.py` also catches an incomplete backup: every document the database refers to must be in the archive and match its recorded hash. A `--no-documents` backup always reports as incomplete.
- `--keep N` runs only after the new backup verifies. It removes only older folders it recognises as its own, by name and manifest, and never the newest. A damaged backup doesn't count toward N, so it can't push out the last good one, and it's left for you to look at.
- `--copy-to DIR` copies to another disk, verifies the copy there, then renames it into place, so a half-finished copy never looks like a backup. `--keep` applies there too. **A backup on the same disk doesn't protect against losing that disk**, and it warns if that's what you've asked for. The copy holds real documents, so keep it private. Encrypting that disk is up to you: nothing here encrypts backups.
- PostgreSQL isn't handled by `backup.py`. It says so and stops.

### Running it every night

Nothing runs on a schedule until you install it. These print a template with your paths filled in, and install nothing:

```bash
python scripts/backup.py --print-schedule launchd --keep 14 --copy-to /Volumes/Off   # macOS
python scripts/backup.py --print-schedule cron --keep 14 --copy-to /Volumes/Off      # Linux
```

System health warns when the latest backup is over 7 days old.

## Proving a backup can be recovered from

A backup that hasn't been restored doesn't count. Do this after the first backup, then monthly:

```bash
python scripts/restore_drill.py --latest
```

It restores into a brand-new temporary folder, starts the real application against that copy on a private port, compares counts and each company's exact totals with the backup, checks every document is in the copy and matches its hash, and opens a sample document of each kind through the API. Then it deletes the copy and exits 0 or 1. Your live data isn't touched. A restored copy older than this code is upgraded first, as a real recovery would be. It checks only what's in the backup: it can't tell you whether a backup is missing something that never reached the database.

## Restoring

```bash
python scripts/verify_backup.py backups/ticketflow-20261105-120000 --restore-to /some/new/empty/folder
```

That refuses a folder with anything in it, so it can't overwrite real data. To switch to a restored copy:

1. Stop the API and worker.
2. Back up the current state too, in case you want it back.
3. In `backend/.env`, point `DATABASE_URL` and `STORAGE_DIR` at the restored folder. Moving files isn't needed, and the old data stays where it was.
4. Run `python scripts/doctor.py` and `python scripts/check_documents.py --inside-only`. Both should come back clean.
5. Start everything. Open a recent ticket and its scan to confirm.

Moving is safe: stored document paths are relative to `STORAGE_DIR`, and older full paths are found under it too.

## Checking an installation

```bash
python scripts/doctor.py            # configuration, database, storage permissions, migration state
python scripts/check_documents.py   # every referenced file exists and matches its hash
```

Both read only, apart from `doctor.py` proving the storage folder is writable with one small file. Both exit 1 if something needs attention. `check_documents.py` can take a while, because it hashes every document.

## Upgrading

1. Stop the API and worker.
2. `python scripts/backup.py`, then `verify_backup.py` on the result.
3. `python -m alembic upgrade head`
4. Start everything. The API refuses to start if an upgrade was skipped, and says what to do.

Migrations only add things, and don't downgrade. To undo one, restore the backup from step 2.

## Recovery

Before any of these: **stop and back up what you have, even if it looks broken.** Copy the database file and the storage folder somewhere safe first. Recovery should never make things worse.

### Database corruption

Signs: "database disk image is malformed", or `doctor.py` reports the integrity check failed.

1. Stop the API and worker.
2. Copy `ticketflow.db` and any `ticketflow.db-journal` beside it to a safe place. Don't delete the journal: it may hold the only record of the last write.
3. Find the newest sound backup: `python scripts/verify_backup.py backups/<folder>`.
4. Restore it to a new folder: `python scripts/verify_backup.py backups/<folder> --restore-to /new/folder`.
5. In `backend/.env`, set `DATABASE_URL=sqlite:////new/folder/ticketflow.db` and `STORAGE_DIR=/new/folder/storage`.
6. `python scripts/doctor.py`, then `python scripts/check_documents.py --inside-only`.
7. Start everything. **Work done since that backup is gone**, so compare recent batches against the paper. Documents uploaded after the backup are still in the old storage folder, and aren't linked to any record in the restored database. Upload them again.

Do not try to repair the file in place. If no backup is sound, keep the copy from step 2, and don't overwrite it.

### Application crash

The API keeps no state outside the database, so a crash loses at most the one request that was running, and its transaction rolls back.

1. Note the time. Search the log for `Unhandled error`, and quote its `request_id`.
2. Start the API again. If it refuses, its message names what to fix. `python scripts/doctor.py` checks the same things.
3. If it crashes again on the same action, that's a bug. Keep the log line, which holds no document contents, and work around that action.

### Interrupted processing

A worker that stops mid-job (crash, power loss, closed terminal):

1. Start it: `python scripts/run_worker.py`. The file lock clears by itself when a worker dies.
2. It reclaims interrupted jobs. Finished files and reviewed tickets aren't redone, and a file interrupted partway starts again, because each file saves all at once.
3. A job that has stopped the worker 3 times (`MAX_JOB_ATTEMPTS`) is set aside. Its unfinished files show as Failed with an explanation, and other uploads carry on. **Retry failed files** once. If it fails again, re-scan the paper, or enter those tickets by hand. Look for `job_gave_up` in the worker's log.

AI calls are at-least-once: a job interrupted mid-file may repeat paid calls for that file.

### Missing documents

Signs: "The stored image for this ticket is missing", or System health's **Stored documents** check.

1. List them: `python scripts/check_documents.py`. It gives table names and row ids, and exits 1.
2. If `STORAGE_DIR` is wrong (it was moved, or a drive isn't mounted), fix that and check again. Nothing else is needed.
3. Otherwise, find the newest backup holding them, and restore it to a new, empty folder: `python scripts/verify_backup.py backups/<folder> --restore-to /tmp/restored`.
4. Copy the missing files into place, keeping their paths. Never overwrite a file that's already there:
   `rsync -a --ignore-existing /tmp/restored/storage/ "$STORAGE_DIR/"`
5. `python scripts/check_documents.py` again. Then delete `/tmp/restored`, which holds real documents.
6. If no backup has a document, the record stays and its image doesn't. Keep the paper. An approved ticket's figures aren't affected, but it no longer has its evidence: say so if anyone asks.

A document reported as damaged (hash differs) is handled the same way, except that the bad file has to be moved aside first, since step 4 won't replace it.

### Duplicates found during an upgrade

A unique index can't be created over rows that already break it, and migration `20261106_18` never changes a record to make one fit. It leaves the index out and logs which rows. System health and `doctor.py` then report **Duplicate safeguards** as a problem until a person resolves them. Until then, the database isn't enforcing that rule. The application's own checks still run.

1. Back up.
2. See what's affected. On SQLite, `sqlite3 ticketflow.db`:

   ```sql
   -- uq_tickets_approved_scan: the same scan approved twice. Double counted tons and cost.
   SELECT organization_id, crop_sha256, GROUP_CONCAT(id) FROM tickets WHERE status = 'verified' GROUP BY 1, 2 HAVING COUNT(*) > 1;
   -- uq_tickets_slot: two records for one position on one page.
   SELECT file_id, source_page, position, GROUP_CONCAT(id) FROM tickets GROUP BY 1, 2, 3 HAVING COUNT(*) > 1;
   -- uq_supplier_rate: two rates for one supplier and material.
   SELECT organization_id, supplier_id, material_id, GROUP_CONCAT(id) FROM supplier_rates GROUP BY 1, 2, 3 HAVING COUNT(*) > 1;
   -- uq_invoice_document_file: one file uploaded twice.
   SELECT organization_id, sha256, GROUP_CONCAT(id) FROM invoice_documents WHERE status <> 'voided' GROUP BY 1, 2 HAVING COUNT(*) > 1;
   ```

3. Resolve each one. Which to keep is a person's decision, made by looking at the records: nothing here decides for you.
   - **Approved scan:** open both tickets, and skip the wrong one, which records who did it. That removes its tons and cost from totals, so tell whoever relies on them.
   - **Rate:** delete the wrong rate on the Pricing page.
   - **Invoice document:** void the wrong upload. It's refused if a line from it created a comparison, so correct that comparison first.
   - **Slot:** the app has no way to do this, because a skipped ticket still occupies its slot. Decide which record is right, then remove the other by hand, but only if it has no history: `SELECT COUNT(*) FROM audit_events WHERE ticket_id = ?` must be 0. If it has history, stop: history is append-only on purpose, and that needs a closer look. Leave the safeguard out and keep the backup.
4. Create the safeguards. Each is refused if duplicates remain, which is safe. Run only those that were missing:

   ```sql
   CREATE UNIQUE INDEX uq_tickets_approved_scan ON tickets (organization_id, crop_sha256) WHERE status = 'verified';
   CREATE UNIQUE INDEX uq_tickets_slot ON tickets (file_id, source_page, position);
   CREATE UNIQUE INDEX uq_supplier_rate ON supplier_rates (organization_id, supplier_id, material_id);
   CREATE UNIQUE INDEX uq_invoice_document_file ON invoice_documents (organization_id, sha256) WHERE status <> 'voided';
   ```

5. `python scripts/doctor.py` should report the safeguards as enforced.

## Reading the logs

The API writes one JSON line per request to stderr:

```json
{"request_id": "3ce7954ebebe", "method": "POST", "route": "/api/rates/import", "status": 422, "ms": 1.3, "user_id": 4}
```

Logs hold route templates and never the query string, so filters containing names stay out. `user_id` is the signed-in person's id, or null. An unhandled error adds a line with the error's type and where in the code it happened, and leaves out its message, because that can quote a record. Treat log files as private anyway.

When someone sees "quote reference abc123", search the log for that id. Every response carries it in `X-Request-ID`.

The worker also writes one JSON line per event to stderr, with ids and no document contents:

```json
{"at": "2026-11-06T17:04:12.331+00:00", "level": "INFO", "logger": "app.services.jobqueue", "event": "job_finished", "job_id": 12, "batch_id": 7, "attempt": 1, "batch_status": "ready_for_review", "ms": 8421}
```

Events: `worker_started`, `worker_stopped`, `worker_lock_held`, `job_started`, `job_finished`, `job_failed`, `job_requeued`, `job_gave_up`, `jobs_recovered`, `file_failed`, `heartbeat_failed`. An error line gives the error's type and where in the code it happened, and leaves out its message, because an error's text can quote a ticket.

## Settings that matter for security

| Setting | Default | Meaning |
|---|---|---|
| `TICKETFLOW_ENV` | `development` | `production` refuses to start without PostgreSQL, secure cookies, exact https origins and real hostnames, and API docs off |
| `ENABLE_API_DOCS` | `false` | Serves `/docs`, `/redoc` and `/openapi.json` |
| `SESSION_IDLE_MINUTES` | `120` | Signed out after this long with no requests. The 12-hour limit applies as well |
| `RATE_LIMIT_UPLOADS_PER_MINUTE`, `..._EXPORTS_...`, `..._REPORTS_...` | 30, 30, 120 | Per person, in each API process. 0 turns one off, which a load test needs |
| `MAX_JOB_ATTEMPTS` | `3` | How many times one upload may stop the worker before it's set aside |

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| An upload stays "Uploaded" | Worker isn't running | Start it. The upload picks up where it was |
| Worker says another holds the lock | One is already running | Check for an old terminal. The lock clears itself if a worker dies |
| Worker died mid-job | Its job is reclaimed on next start | Start it. Finished files and reviewed tickets aren't redone |
| A file says processing stopped the worker | That upload took the worker down 3 times (`MAX_JOB_ATTEMPTS`), so it was set aside and other uploads carried on | **Retry failed files** once. If it fails again, re-scan it or enter those tickets by hand |
| A file shows Failed | That file couldn't be processed | **Retry failed files** on the batch, or enter those tickets by hand |
| "The stored file is missing" on a ticket or scan | The database refers to a document that isn't in `STORAGE_DIR` | See [Missing documents](#missing-documents) |
| "Someone else changed this ticket" | A teammate saved first | Check the values now shown, then accept |
| API refuses to start, naming a migration | Code is newer than the database | Follow Upgrading above |
| API or worker refuses to start in production | A setting is unsafe for hosting | The message names it. `python scripts/doctor.py` lists them all |
| "Too many exports in a short time" | One person passed a per-minute limit | Wait the seconds it says. Raise the limit if it's normal work |
| Signed out "after a while without activity" | Idle longer than `SESSION_IDLE_MINUTES` | Sign in again |
| A number looks wrong | Open the ticket's **Change history** | Every edit and approval is recorded with a name |
