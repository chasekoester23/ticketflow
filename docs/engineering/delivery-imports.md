# Delivery spreadsheet imports

Status: built (migration `20261128_34_delivery_imports`).

Many suppliers can export a month of deliveries as CSV or Excel. An import turns each row
of that export into an ordinary **unapproved** ticket, so it goes through the same review,
approval, pricing and locking as a scanned ticket. There is no separate financial logic:
nothing is approved, priced or totalled by importing.

## Workflow

| Step | API | Permission |
| --- | --- | --- |
| Upload the file | `POST /api/delivery-imports` (multipart `file`) | `tickets.upload` (rate-limited as an upload) |
| Map columns, preview | `PUT /api/delivery-imports/{id}/mapping` | `tickets.upload` |
| Import | `POST /api/delivery-imports/{id}/commit` | `tickets.edit` |
| History | `GET /api/delivery-imports`, `GET /api/delivery-imports/{id}` | signed in |
| Original file | `GET /api/delivery-imports/{id}/file` | signed in |

Every query is filtered by the caller's company. Another company's import id answers
exactly like an id that doesn't exist (404 "Import not found"); this is covered by the
tenant sweep (`tests/test_t_tenant_sweep.py`) and the real-session sweep
(`tests/test_c_cross_company.py`). On PostgreSQL, `delivery_imports` has the same
company row policy as every company table.

The page is `/upload/import` (linked from Document Intake: "Import a spreadsheet").

## Reading files

- **CSV** (`.csv`, `.txt`, `.tsv`): UTF-8 (with or without BOM), else Windows-1252. The
  delimiter (`,` `;` tab `|`) is detected.
- **Excel** (`.xlsx`): the first sheet, opened with openpyxl `read_only=True,
  data_only=False`. Formulas are never evaluated and their cached results are never used.
  Old `.xls` files are refused with a "save as .xlsx or CSV" message.
- **Limits**: 10 MB file, 2,000 delivery rows, 60 columns, 500 characters a cell; an
  Excel file's uncompressed size must be under 100 MB (checked before opening).
- **Formulas**: a cell holding a formula (or, in a CSV, text starting with `=`) is a
  problem on its row: "Quantity is a formula (=2*10)…". Its text never becomes a ticket
  value, so it can't become a live formula in an export either.
- **Header row**: the row among the first 20 that most looks like column names (for
  example "Ticket #", "Date", "Product", "Net Tons", "UOM", "Truck", "Job"). A person can
  choose another. The suggested mapping is only a suggestion: the person confirms it.

## Interpreting a row

Each row is read with the review form's own parsers and `validation.compute_flags`:

- Ticket number and quantity columns are required; the unit must come from a column or
  be chosen for the whole file. Supplier and job may be a column, a value for the whole
  file, or both (the value fills blank cells).
- Units are those of `services/units.py` (tons, CY, loads, each, gallons), from a unit
  column, from text beside the number ("12.5 cy"), or for the whole file. An unknown unit
  is a problem and the quantity is left blank: it is never recorded as tons.
- **Pounds** convert exactly as on the review screen: `units.pounds_to_tons` (2,000 lb =
  1 t, half up to 0.001 t), the ticket's unit is `ton`, and `source_quantity` /
  `source_unit = 'lb'` keep the figure as written.
- **Problems** are the flags that would block approval plus anything that couldn't be
  read. Warnings that don't block approval ("Tonnage unusually large…", scale weights)
  are shown as warnings. Rows with problems are left out unless the person chooses
  "Bring rows with problems in for review"; then a value that couldn't be read is left
  blank, and the ticket carries the usual flags until someone enters it.
- Blank rows are ignored.

## Duplicates

A row is a duplicate when its supplier and ticket number (case, spacing and punctuation
ignored, as `Ticket.supplier_key` / `number_key`) are already on a ticket **in the same
company**, in any batch or project, approved or not; or appear on an earlier row of the
same file. Duplicates are **skipped by default**. Importing one anyway is a per-row choice;
it is recorded on the ticket's history (`duplicate_import`), and approval still asks the
person to confirm the duplicate exactly as for scans.

## How an imported row fits the ticket table

Imports create a batch with `intake_mode = 'import'`, `status = 'ready_for_review'` and
`expected_ticket_count` = rows imported, holding one `UploadedFile` that points at the
stored spreadsheet (the same storage key as `delivery_imports.storage_path`). No
processing job is queued. For each ticket:

| Column | Value |
| --- | --- |
| `file_id` | the spreadsheet's `UploadedFile` |
| `source_page` | the row number in the file (as a spreadsheet shows it) |
| `position` | 0 |
| `crop_path` | `crops/{batch}/import_r{row}.png`: a picture of the row as the file has it |
| `crop_box` | `[0, 0, width, height]` of that picture |
| `crop_sha256` | SHA-256 of that picture |
| `raw_extraction` | empty: nothing was read by the AI reader, so extraction measures are unaffected |

So `uq_tickets_slot (file_id, source_page, position)` holds (one ticket per row). The
picture shows the file name, the file's SHA-256 and the row number, so its hash differs for
every row of every file. That keeps `uq_tickets_approved_scan` ("identical scan already
approved") from firing between rows; it fires only for the *same row of the same file*
imported twice and both approved, which really is the same delivery counted twice. The
picture is a real stored document, so the document audit, backups and `GET
/api/tickets/{id}/image` work unchanged. The review screen shows "Imported from
<file>, row N" with that picture instead of the scan viewer, and a link to the original.

Each imported ticket's history starts with `imported_from` ("deliveries.csv, row 4"), and
the batch's scan history has `spreadsheet_imported`.

## Commit safety

The commit re-reads the file and re-checks duplicates. It is refused if the mapping
changed since the preview (`revision`), and it claims the import with a conditional update
in the same transaction as the tickets, so a second click or a second person can't import
the same file twice. If anything fails, nothing is kept and the row pictures are removed.

## Measured

With the test client (local storage, in-memory SQLite, a development laptop), a synthetic 2,000-row CSV (119 KB)
took 0.3 s to upload, map and preview and 7.9 s to import (row pictures and their
storage writes are most of it). Drawing the pictures with a per-character glyph cache
brought the import down from 24.6 s.

## Not done

- Only the first sheet of a workbook is read.
- Column mappings are not remembered per supplier.
- An import can't be undone as a whole; its tickets are skipped or flagged one by one
  like any others.
