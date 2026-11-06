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
python scripts/backup.py                       # to backend/backups/, or set BACKUP_DIR
python scripts/verify_backup.py backups/ticketflow-20261105-120000
```

- Safe while everything is running. It reads the live database only and always writes to a new folder.
- Each backup has the database, the stored documents, and a manifest with checksums and record counts.
- **A backup on the same disk doesn't protect against losing that disk.** Copy the folder somewhere else. It holds real documents, so keep it private.
- Nothing runs this on a schedule yet. System health warns when the latest is over 7 days old.

## Restoring

Prove it works before you need it:

```bash
python scripts/verify_backup.py backups/ticketflow-20261105-120000 --restore-to /some/new/empty/folder
```

That refuses a folder with anything in it, so it can't overwrite real data. To switch to a restored copy:

1. Stop the API and worker.
2. Back up the current state too, in case you want it back.
3. In `backend/.env`, point `DATABASE_URL` and `STORAGE_DIR` at the restored folder. Moving files isn't needed, and the old data stays where it was.
4. Start everything. Open a recent ticket and its scan to confirm.

## Upgrading

1. Stop the API and worker.
2. `python scripts/backup.py`, then `verify_backup.py` on the result.
3. `python -m alembic upgrade head`
4. Start everything. The API refuses to start if an upgrade was skipped, and says what to do.

Migrations only add things, and don't downgrade. To undo one, restore the backup from step 2.

## Reading the logs

The API writes one JSON line per request to stderr:

```json
{"request_id": "3ce7954ebebe", "method": "POST", "route": "/api/rates/import", "status": 422, "ms": 1.3}
```

Logs hold route templates and never the query string, so filters containing names stay out. Unhandled errors also log a stack trace, which can include internal detail, so treat log files as private.

When someone sees "quote reference abc123", search the log for that id. Every response carries it in `X-Request-ID`.

The worker also writes one JSON line per event to stderr, with ids and no document contents:

```json
{"at": "2026-11-06T17:04:12.331+00:00", "level": "INFO", "logger": "app.services.jobqueue", "event": "job_finished", "job_id": 12, "batch_id": 7, "attempt": 1, "batch_status": "ready_for_review", "ms": 8421}
```

Events: `worker_started`, `worker_stopped`, `worker_lock_held`, `job_started`, `job_finished`, `job_failed`, `job_requeued`, `job_gave_up`, `jobs_recovered`, `file_failed`, `heartbeat_failed`. An error line gives the error's type and where in the code it happened, and leaves out its message, because an error's text can quote a ticket.

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| An upload stays "Uploaded" | Worker isn't running | Start it. The upload picks up where it was |
| Worker says another holds the lock | One is already running | Check for an old terminal. The lock clears itself if a worker dies |
| Worker died mid-job | Its job is reclaimed on next start | Start it. Finished files and reviewed tickets aren't redone |
| A file says processing stopped the worker | That upload took the worker down 3 times (`MAX_JOB_ATTEMPTS`), so it was set aside and other uploads carried on | **Retry failed files** once. If it fails again, re-scan it or enter those tickets by hand |
| A file shows Failed | That file couldn't be processed | **Retry failed files** on the batch, or enter those tickets by hand |
| "Someone else changed this ticket" | A teammate saved first | Check the values now shown, then accept |
| API refuses to start, naming a migration | Code is newer than the database | Follow Upgrading above |
| A number looks wrong | Open the ticket's **Change history** | Every edit and approval is recorded with a name |
