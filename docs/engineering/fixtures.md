# Test fixtures

All tickets, suppliers and invoices are made up. **Fixtures test how the software behaves. They say nothing about reading accuracy on real tickets**, which has not been measured.

Generators are in `backend/tests/sample_data.py`.

| Case | How to make it | What it checks | Covered by |
|---|---|---|---|
| 1-up | `layout="single"` with `make_pdf([make_ticket_image(...)])` | One record per page | `test_api_e2e` |
| 4-up | `make_page([A, B, C, D])` | Four records, four slots | `test_scan_manifest` |
| 3-up, truly empty slot | `make_page([A, B, None, D])` | Slot kept and shown as empty; doesn't block alone | `test_scan_manifest` |
| Faint ticket | `make_page(["faint", A, None, None])` | Old blank test would lose it; now blocks until a person decides | `test_scan_manifest` |
| All blank | `make_page(["faint", None, None, None])` | File fails visibly; slots still kept | `test_scan_manifest` |
| Rotated | Rotate a slot through the API | Non-destructive; duplicate guard holds; retry reads the rotated image | `test_scan_manifest` |
| Multipage | `make_pdf([page1, page2])` | Per-page slots and previews | `test_scan_manifest` |
| Corrupt PDF | `b"%PDF-1.4 ..."` | Reported as failed, never as zero tickets | `test_scan_manifest` |
| Mixed layouts and handwriting style | `scripts/make_practice_tickets.py` | Printable self-written kit | `test_practice_kit` |
| Invoice and ticket match | `_deliveries` and `_line` helpers | Per-ticket reconciliation | `test_invoice_ticket_matching` |
| Invoice PDF with a text layer | `make_invoice_pdf(...)` | Drafts, unit handling, credits, duplicates | `test_invoice_documents` |
| Whole demo | `scripts/make_demo_kit.py` | Faint ticket plus an invoice with matching totals and a wrong ticket | `frontend/tests/e2e/demo.spec.mjs` |

The faint fixture is drawn in light grey (RGB 205) on white. It shows the logic works. The `assess_slot` thresholds have **not** been calibrated on real scanner output; expect real blank areas with scanner noise to read as "faint" and need a person to confirm them.
