# Units of measure

Status: built (migration `20261127_33_units_of_measure`). This replaces the earlier design note. Hourly trucking is not a ticket unit: it is billed through truck shifts (see [trucking-billing.md](trucking-billing.md)).

## The rules

1. **One approved quantity per delivery, in one unit.** The `tickets.tonnage` column holds the quantity in `tickets.unit`. The column name is historical. The units are `ton` (US short ton), `cy` (cubic yard), `load`, `each` and `gal`. Every ticket that existed before the migration is `ton`, which is what its quantity always was.
2. **Pounds are a way of writing tons, not a billing unit.** A quantity entered in `lb` is saved as tons: 2,000 lb = 1 t, rounded half up to 0.001 t. The figure as written stays on the ticket in `source_quantity` / `source_unit` and in the audit trail. Retyping the quantity in another unit clears it.
3. **Nothing converts between dimensions.** Cubic yards, loads, each and gallons are never turned into tons. The factor depends on the material and the contract, and a guessed factor would put a made-up number into a payable total.
4. **A rate prices only its own unit.** `supplier_rates.unit` defaults to `ton`. A ticket is priced only by the rate that applies to it (project agreement first, then company rate, latest start date) when that rate is in the ticket's unit. Otherwise the ticket is unpriced with the reason. It never falls back to another agreement in the right unit, because that would quietly put list price on contracted work.
5. **Totals never mix units.** "Tons" everywhere (Overview, Analytics, the project directory and report, Delivery Records, exports, unbilled) counts `ton` tickets only. Other quantities are reported per unit beside the tons (`other_units`). Money adds up across units, because each cost is already quantity × the price for that unit.
6. **Scale weights are evidence, never a value.** `gross_weight`, `tare_weight` and `weight_unit` (`lb` or `ton`) are optional. When gross minus tare differs from the tons by more than 0.010 t, the ticket shows a warning. A person checks the scan and approves the right figure. Tare above gross blocks approval. The weights never replace the quantity.
7. **Locked on approval.** A database trigger (`tickets_unit_locked`, both dialects) refuses a change of unit on an approved ticket, as it already does for quantity, price and hauling charge. Changing the unit through the app withdraws the approval.

## Where it applies

| Area | Behaviour |
|---|---|
| Review form | A "Measured in" choice: tons, pounds (saved as tons), cubic yards, loads, each, gallons. Optional gross and tare weights. Loads must be whole numbers. The plausible-size warning applies to tons only. |
| Rates & Pricing | Each rate is "priced per" a unit. A unit change is recorded in pricing history (`unit_changed`). Re-saving a price without choosing a unit keeps the rate's unit. |
| Supplier invoices | Each line has a unit. It is compared only with deliveries in that unit. Deliveries for the same supplier and material in another unit are named in a warning, not added. A listed ticket in another unit is "outside scope". An approved delivery is "covered" by a line only in its own unit. PDF lines in CY, loads, each or gallons can now be confirmed. Pounds and hours on a material line are still refused. |
| Hauling | Per-ton hauling needs a ticket in tons: on any other unit it is unpriced, never converted. Per-load hauling charges the ticket's quantity of loads when it is measured in loads, otherwise one load. Hauler invoice reconciliation counts loads the same way. |
| Exports | The default columns don't move. "Tonnage" is blank for a delivery not in tons. Optional columns are `quantity`, `unit`, `as_written` (for example "44,300 lb") and `scale_weights`. The Summary sheet lists each other unit's approved quantity on its own line. |
| Reader | Unchanged on purpose. The AI reader returns tons only and never converts. A person enters any other unit by hand. |

## Known limits

- One rate per supplier, material, scope and start date. A supplier who quotes the same material both per ton and per cubic yard for the same scope needs two catalog materials (for example "Topsoil (CY)").
- `materials.unit` is an older label column that nothing reads. The material list no longer shows it.
- Metric tonnes and kilograms are not accepted. Adding them is a conversion rule (exact) plus a test, not a schema change.
