# Invoice ticket evidence: specification

Status: implemented for full-ticket linking. **Partial allocation is deliberately not implemented.**

## The problem

The per-ticket check recalculates on every read. It tells you what looks true now, but records nothing a person decided, and nothing durable stops one delivery being billed on two invoice lines. Evidence adds that record.

## The rule

A manager may confirm that a verified delivery is billed in full by one invoice line. A ticket can have **at most one active link**, enforced by a partial unique index on `invoice_ticket_links(ticket_id) WHERE released_at IS NULL`. That holds under concurrent requests because the database refuses the second insert, not because application code checked first.

The overlapping-scope guard stays in place. It's an independent protection, and this doesn't replace it.

## States, per ticket row

| State | Meaning | Can be confirmed? |
|---|---|---|
| `unmatched` | Not found, outside scope, delivery not on invoice, or delivery not verified | No |
| `candidate` | Exact match, verified, tons equal or not entered, no flags | **Yes**, by a manager |
| `confirmed` | A person linked it, and the delivery is unchanged since | Already confirmed |
| `conflicting` | Ambiguous number, billed tons differ, repeated on this invoice, on another saved invoice, or linked to another line | No |
| `invalidated` | Linked, but the delivery changed afterward (approval withdrawn, tons, number, supplier, material, or date) | Re-confirm once it matches again, or release |

No AI or score can create a link. Only `POST /api/invoice-comparisons/{id}/ticket-links` can, and it requires `invoices.manage`.

## Why partial allocation isn't built

Supporting a load split across two invoices needs an allocation ledger proving that assigned tons never exceed approved tons under concurrency, plus decisions on rounding and rate per portion. There's no customer evidence yet that it's needed. So a billed quantity that differs from verified tons is `conflicting` and can't be linked. People see the difference and resolve it outside the system.

## What links never do

- Change a ticket, an approval, or a locked price.
- Approve an invoice or authorize payment.
- Change the math in a comparison. `status` still reflects figures and warnings. A separate evidence summary reports how many tickets people have confirmed.
- Disappear silently. Release is explicit or caused by a correction, and both are recorded in the comparison history.

## Test matrix

Backend tests in `backend/tests/test_ticket_evidence.py` unless noted.

| Scenario | Expected | Test |
|---|---|---|
| 1 invoice line, many verified tickets | All link; summary counts them; tons add up by Decimal | `test_manager_confirms_clean_matches_and_summary_counts_them` |
| One ticket on 2 invoice lines (sequential) | Second refused | `test_ticket_already_linked_elsewhere_is_conflicting` |
| One ticket on 2 lines (concurrent) | Exactly one wins, at the database | `test_concurrent_claims_on_one_ticket_produce_one_link` |
| Partial billed quantity | `conflicting`; can't link | `test_rows_that_are_not_clean_cannot_be_linked` |
| Ticket numbers reused across suppliers/projects | Separate deliveries; only in-scope matches | `test_same_number_from_another_supplier_is_not_linked` |
| Reused number inside scope | Ambiguous; can't link | `test_rows_that_are_not_clean_cannot_be_linked` |
| Wrong date | `unmatched`; can't link | `test_rows_that_are_not_clean_cannot_be_linked` |
| Corrected ticket values after linking | `invalidated`; comparison incomplete | `test_changing_a_linked_delivery_invalidates_the_link` |
| Approval revoked after linking | `invalidated`; re-approval doesn't restore it without a person | `test_changing_a_linked_delivery_invalidates_the_link` |
| Old approval with no recorded time | Links normally; unchanged | `test_legacy_approval_without_a_time_can_link` |
| Mismatched rate | Reported by the comparison; link allowed only as a tons claim; figures unchanged | `test_rate_mismatch_is_still_reported_when_linked` |
| Invoice line corrected to remove a ticket | Link released and recorded | `test_correcting_the_line_releases_links_it_no_longer_lists` |
| Duplicate PDF upload | Refused before any draft | `test_invoice_documents.py::test_duplicate_uploads_and_repeated_invoice_numbers_are_refused` |
| Retried confirm request | No second link | `test_repeating_a_confirm_is_harmless` |
| Other company's IDs | 404 | `test_links_are_company_scoped` |
| Clerk or viewer | 403 | `test_only_managers_and_owners_link` |
| Verified price immutable | Ticket and cost unchanged by link and release | `test_links_never_change_tickets_or_prices` |
| Decimal arithmetic | No float rounding | `test_confirmed_tons_are_exact` |

## Remaining limits

- A link can only be made on a line with a ticket list. A totals-only line has no evidence and says so.
- SQLite serializes writers locally. The same index exists for PostgreSQL, but concurrency has only been tested on SQLite.
- Numbers come from what people enter or the PDF reader drafts; nothing here reads a scanned invoice.
