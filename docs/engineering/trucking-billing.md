# Trucking billing methods

How Quarvera records what hauling costs, by how the contractor is billed. One rule underlies everything: **each delivery's hauling is paid one way, once.**

| How the hauling is billed | What Quarvera records | Where the cost comes from |
|---|---|---|
| **Hourly**: a hired truck billed by the hour | A truck shift from the time sheet | The approved shift (hours × hourly rate, minimums, standby, increments), locked at approval |
| **Per ton**: a hauler bills a rate per ton moved | The delivery ticket, with its hauler | The approved ticket: tons × the hauler's per-ton rate, locked at approval |
| **Per load**: a hauler bills a rate per trip | The delivery ticket, with its hauler | The approved ticket: one load × the hauler's per-load rate, locked at approval |
| **Freight included**: the supplier's delivered price includes trucking | The delivery ticket | Nothing extra: the material rate already covers it |
| **Freight billed separately by the supplier** | The delivery ticket | Not modelled yet (see open questions); recorded on the supplier invoice as a fee line |

## Rules that prevent double counting

These are enforced in code and covered by tests in `backend/tests/test_trucking_billing.py`.

1. **Freight included means no hauling charge.** If the material rate locked on a ticket is marked *freight included*, no per-ton or per-load charge is calculated for that ticket, even when a hauler and a hauling rate exist.
2. **A delivery hauled under an hourly shift gets no per-ton or per-load charge.** The shift's hours already pay for the truck. A ticket linked to a shift (`truck_shift_id`) is never charged per ton or per load.
3. **Invoices are never costs.** Supplier and hauling invoices are compared with approved records; they never add to project or company cost.
4. **Locked at approval.** The hauling rate and charge are snapshotted when the ticket is approved, like its material price. A later rate change never changes an approved ticket; the database refuses it.
5. **Missing is not zero.** A ticket with a hauler but no applicable per-ton or per-load rate is counted as *hauling rate missing*, not as $0 of hauling.

## How a rate is chosen

- Only rates of the ticket's billing method are considered: hourly rates price shifts, per-ton and per-load rates price tickets.
- A ticket's hauler is the hauler entered on the ticket.
- A project rate beats a company rate.
- Within either, the latest rate that has started on or before the delivery date applies.
- An expired rate doesn't fall back to a broader one, the same as hourly agreements.
- If both a per-ton and a per-load rate apply to the same hauler and project, the ticket can't be priced. It's shown as ambiguous, so no one guesses.

## Assumptions to confirm with pilot contractors

- **Tons for per-ton hauling** are the ticket's approved net tons (the same figure that prices the material). Some haulers bill on their own scale weights. If they differ, the hauling invoice comparison will show it.
- **A rate that's not marked** as freight included or separate is treated as material only. A per-ton hauling rate for that delivery's hauler is then charged.
- **Supplier-billed freight** (a freight line on the material invoice) isn't priced from rates yet. It's recorded as a fee on the supplier invoice. If pilots show it's common, it becomes a `freight_rate` on the supplier rate.
- **Fuel surcharges, minimum loads, wait time** on per-load hauling aren't modelled. Hourly agreements already cover minimums and standby.
