# Backups and recovery for hosted PostgreSQL

Two tools, for two kinds of installation:

| Installation | Back up with | Prove it with |
|---|---|---|
| One machine, SQLite | `scripts/backup.py` (see `docs/engineering/operations.md`) | `scripts/restore_drill.py` |
| Hosted, PostgreSQL | `scripts/backup_pg.py` | `scripts/restore_pg_drill.py` |

This page is about the second.

## What a backup is

Every run of `python scripts/backup_pg.py`:

1. Opens **one read-only `REPEATABLE READ` transaction** on `DATABASE_URL` and exports its
   snapshot. The manifest's counts and each company's exact approved tons and cost, and
   `pg_dump -Fc --snapshot`, all read that same snapshot: the manifest describes exactly
   what was dumped, even while people keep working. Nothing is written to the live
   database (apart from one `backup_runs` row afterwards, once that table exists).
2. Checks the archive is readable (`pg_restore --list`), then **encrypts** it with
   AES-256-GCM using `BACKUP_ENCRYPTION_KEY`. The key's name (`BACKUP_ENCRYPTION_KEY_ID`)
   is in the authenticated header and the manifest.
3. Uploads it to the **backup bucket** (`BACKUP_S3_*`), then downloads it again, checks
   size and SHA-256, and decrypts it, which proves the authentication tag. Only then
   does it write the encrypted manifest and, last, `meta.json`, which marks it complete.
4. Copies **documents** (from `BACKUP_SOURCE_S3_*`, or `STORAGE_DIR`) into the same
   backup bucket, each one encrypted, incrementally by key (an index remembers each
   source object's ETag and size, so unchanged ones aren't read again), and proves each
   copy by downloading, decrypting and hashing it. Documents referred to by the
   snapshot but missing from storage fail the run.
5. Applies **retention**: the newest backup of each of the newest `BACKUP_KEEP_DAILY`
   days (default 14) and `BACKUP_KEEP_MONTHLY` months (default 12). Only after this run
   was proved; never the newest; unfinished uploads are removed after two days.
   Documents are never pruned: deleting one from storage doesn't delete its backup.

Any failure exits 1, records the failure, and posts to `ALERT_WEBHOOK_URL` if set.

Bucket layout: `<prefix>/postgres/<YYYYMMDDTHHMMSSZ>/{dump.enc, manifest.enc, meta.json}`,
`<prefix>/documents/<key>`, `<prefix>/documents-index.json`. `meta.json` holds sizes,
hashes, the key id and the time, nothing about any company; the counts and totals are
in `manifest.enc`.

## Setting it up

1. **A separate bucket**, ideally at another provider or at least another account from
   the documents bucket (Cloudflare R2's free tier, Backblaze B2, or any S3-compatible
   store). The script refuses to use the documents bucket. Turn on object versioning or
   object lock there if the provider offers it, so a stolen backup credential can't
   also delete history.
2. **Credentials**: one key pair that can write and delete only in the backup bucket
   (`BACKUP_S3_ACCESS_KEY_ID`/`SECRET`), and a **read-only** key pair for the documents
   bucket (`BACKUP_SOURCE_S3_*`). The API process needs neither.
3. **The encryption key**: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
   Store it in the platform's secrets for the backup job **and** somewhere else that
   survives losing the platform account (a password manager two people can open).
   Losing it makes every backup unreadable; nobody can recover it. To rotate, set a new
   key and a new `BACKUP_ENCRYPTION_KEY_ID`; keep the old key until every backup made
   with it has aged out of retention.
4. **A schedule**: a Railway cron service (same image, command
   `python scripts/backup_pg.py`, schedule `30 2 * * *`), with the variables above,
   `DATABASE_URL`, `pg_dump` 16 or newer in the image (`postgresql-client-16`), and
   `requirements-backup.txt` installed.
5. **A drill** before the first customer and then monthly:
   `python scripts/restore_pg_drill.py --latest --target-url <an empty database> --cleanup`.
   It refuses `DATABASE_URL` and any database that already holds tables.

## RPO and RTO, honestly

**RPO** (how much recent work can be lost) and **RTO** (how long until it's back):

| Situation | RPO | RTO | What it rests on |
|---|---|---|---|
| Daily `backup_pg.py` only | **up to 24 hours** of approvals, edits and uploads | **hours**: provision an empty database, run the drill without `--cleanup`, point `DATABASE_URL` at it, migrate if needed, restart | This repository's scripts |
| Plus Railway's own PostgreSQL backups / point-in-time recovery, where the plan provides it | as small as the provider's interval | the provider's restore time | Railway, not tested by us |
| Documents | since the last run; the documents bucket's own versioning shortens it | copying back from the backup bucket: no script for that yet | |

What CI proves, on every backend change and weekly (`.github/workflows/backup-restore.yml`):
a fictional two-company business on PostgreSQL 16 is backed up to MinIO, restored with
`pg_restore` into a **second** database, and matches **row for row** (a hash of every
table), with every count and each company's totals equal to the manifest, every
referenced document present and a sample decrypting to its recorded hash; the source is
proved byte-for-byte unchanged; a flipped byte, the wrong key, a non-empty target and
the live database as target are all refused; the second run copies no unchanged document;
retention leaves only what it should.

What it does **not** prove: a restore at production size or speed (time your own drill
and write the number here), restoring documents back into the documents bucket (manual
today: decrypt with the key, `decrypt_bytes` in `backup_pg.py`), the application
starting on the restored database in the hosted environment, or Railway's own backups.
Until a timed drill on production-sized data is recorded, treat the RTO as "same
working day", not a number.

## Restoring for real

1. Stop the worker and put the app in maintenance (scale the API to zero).
2. Create a new empty database. Never restore over the damaged one: keep it for
   investigation.
3. `python scripts/restore_pg_drill.py --backup <name> --target-url <new database>`
   (no `--cleanup`), and read every check. `--list` on `backup_pg.py` names backups.
4. Point `DATABASE_URL` at the new database, run `python -m alembic upgrade head` if the
   code is newer than the backup, start the API, check System Health, then the worker.
5. Everything since the backup must be re-entered from the paper tickets and invoices.
