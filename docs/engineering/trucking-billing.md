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

- Per-ton hauling prices only a delivery measured in tons. A delivery in cubic yards, loads, each or gallons is "rate missing", never converted. Per-load hauling charges a ticket measured in loads for each of its loads, and any other ticket as one load (see [billing-units.md](billing-units.md)).

## Reconciling a hauler's invoice

Hours, standby and minimum lines are matched with approved shifts: hauler, date, truck. Tons and loads lines are matched with approved deliveries that carry this hauler's per-ton or per-load charge:

- **Which deliveries.** A line that lists ticket numbers uses exactly those tickets. A line without them uses that day's approved deliveries for the hauler, on the invoice's project, on the line's truck if it names one.
- **What it should cost.** The sum of those deliveries' locked hauling charges. A difference is split exactly into a quantity effect (tons or loads at the billed rate), a rate effect (on the approved quantity) and the invoice's own arithmetic. The parts add up to the line's difference to the cent.
- **What can't be supported, and why.** These are never supported:
  - a delivery already billed on another line or invoice (*billed again*);
  - a delivery for another project (*billed against another project*);
  - a ticket number with no record;
  - a delivery covered by an hourly shift or by a freight-included material price (*already covered*).
  When nothing on a line can be supported, the line's whole amount sits on the exception that explains it: one problem, one exception, with its dollars.
- **Outside the period.** A listed delivery dated outside the invoice's period is still supported (its hauling is owed once, whichever invoice bills it), and the line gets a *needs a person* question with no dollars.
- **Split shifts.** A shift whose hours are allocated to several projects is compared, on a project's invoice, with that project's allocated hours and cost: the same figure the cost report and project directory show. Each project's share is billed once; a second bill of the same share, or of the whole shift after a share (or the reverse), is *billed again*. An invoice with no project answers for the whole shift. A split shift with standby or a minimum charge is left to a person, because which project's invoice bills those isn't recorded.
- **Partial billing.** Approved hauling in an invoice's period and project that the invoice doesn't bill is listed as *not billed yet*, one item per delivery. It is not an error on the invoice. Approved hauling that no invoice covers is grouped per hauler and project.
- **Uncertain lines.** An hours line that fits two shifts (no truck named) shows "Choose the shift". A person picks one. The choice is saved as a correction of the invoice, with its revision and history, and the line is then checked against that shift.

## Assumptions to confirm with pilot contractors

- **Tons for per-ton hauling** are the ticket's approved net tons (the same figure that prices the material). Some haulers bill on their own scale weights. If they differ, the hauling invoice comparison will show it.
- **A rate that's not marked** as freight included or separate is treated as material only. A per-ton hauling rate for that delivery's hauler is then charged.
- **Supplier-billed freight** (a freight line on the material invoice) isn't priced from rates yet. It's recorded as a fee on the supplier invoice. If pilots show it's common, it becomes a `freight_rate` on the supplier rate.
- **Fuel surcharges, minimum loads, wait time** on per-load hauling aren't modelled. Hourly agreements already cover minimums and standby.
