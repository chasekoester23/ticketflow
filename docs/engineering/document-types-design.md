# Design note: other document types (not implemented)

Status: design only. Nothing here is built. Build it after a pilot shows who needs it.

## Why it isn't built yet

Ticketflow's approval flow requires six fields, and verification refuses a ticket with any missing. Hauling time tickets have hours, truck and rate-per-hour instead of tons. Forcing them into `tonnage` would break the rule that tons mean tons, so they stay out until there's a real customer case.

## Migration-ready shape

1. Add `Ticket.document_type` as a nullable column meaning `material_delivery` when null, so existing tickets don't change.
2. Add a `TicketExtraField` table (`ticket_id`, `name`, `value`, `unit`), rather than widening `tickets`.
3. Make `compute_flags` choose its required fields by document type. `material_delivery` keeps today's six.
4. Totals, pricing and invoice comparison filter to `material_delivery` until an hours-based pricing rule is designed. A time ticket must never add to tons or use a per-ton rate.
5. Units stay explicit. A quantity in pounds or cubic yards is never converted to tons. The extraction prompt already returns null for those.

## Open questions for pilot customers

- Who enters hauling tickets today, and are they invoiced separately from material?
- Is a per-supplier material code needed? Aliases are company-wide today: one spelling means one thing across all suppliers.
