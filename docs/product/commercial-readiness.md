# Quarvera commercial readiness roadmap

This is the product development roadmap, and the one source for it. A mission reaches 100% only when every acceptance criterion in its defined scope has evidence, and no P0 or P1 item is open. 100% means commercially complete within that scope. It does not mean every imaginable feature, and it does not mean validated by real contractors: that is tracked separately as *external validation*.

## How completion is scored

Each acceptance criterion belongs to one category and has a status:
- **Done** (1): implemented, tested and verified, with evidence named.
- **Partial** (0.5): works for part of the defined scope.
- **Open** (0): not done.

A category's score is the mean status of its criteria. The mission score is the weighted sum:

| Category | Weight |
|---|---:|
| Core functionality | 25% |
| Real-world scenarios and edge cases | 15% |
| Frontend functionality and usability | 15% |
| Cross-system integration | 10% |
| Data integrity and security | 10% |
| Automated testing | 10% |
| Manual end-to-end verification | 10% |
| Documentation and maintainability | 5% |

Priorities: **P0** critical financial, security or operational blocker. **P1** required for commercial completion. **P2** valuable, not blocking. **P3** future, outside the scope.

## Missions

| # | Mission | External estimate | Assessed | Status |
|---|---|---:|---:|---|
| 01 | Financial engine and invoice reconciliation | 73% | see Mission 01 | In progress (this run) |
| 02 | Document intake and ticket processing | 75% | 70% | Not started as a mission |
| 03 | Supplier and hauler management | 55% | 50% | Not started |
| 04 | Project reporting and analytics | 77% | 75% | Not started |
| 05 | Commercial UI/UX | 70% | 65% | Not started |
| 06 | Security and production infrastructure | 50% | 50% | Not started |
| 07 | Customer onboarding and hosted demo | 45% | 40% | Not started |
| 08 | Commercial launch infrastructure | 10% | 10% | Not started |
| 09 | Real-world contractor validation | 10% | 5% | Not started |

Missions 02 to 09 were not re-audited criterion by criterion in this run. Their "assessed" figures are a first correction from known gaps, listed below. Each mission gets the same criterion table when it becomes the current mission.

- **02:** AI reading of tickets only (no time sheets or invoices by AI); no multi-ticket photo detection; the Review Queue and intake exist.
- **03:** suppliers and haulers are separate tables matched by name; no vendor status, documents or profile page.
- **04:** reports and analytics exist; there is no budget versus actual and no cost codes on material.
- **05:** navigation, project workspaces and design tokens exist; no full design-system or usability pass.
- **06:** production refusal checks, row security, S3 storage and backups exist; no hosted deployment and no external security review. Sessions and limits are per process.
- **07:** demo seeder exists; no hosted demo and no self-serve onboarding.
- **08:** no billing, plans or status page.
- **09:** no real contractor has used it.

---

## Mission 01: Financial engine and invoice reconciliation

### Scope (locked 2026-11-26)

From the source document to verified project cost:
- Material deliveries with their quantity and unit.
- Rates and agreements.
- Hourly, per-ton and per-load trucking, and freight-included material prices.
- Approved cost.
- Supplier and hauler invoices reconciled against approved activity.
- Exceptions and their resolution.
- Unbilled activity, corrections, project cost reporting and financial exports.

**Out of scope** (recorded as P2/P3):
- Payments, accounting ledgers and ERP sync.
- Taxes.
- Automatic conversion between volume and weight, or loads and weight, without a configured factor (P2: a per-material contractual factor).
- AI extraction of hauler invoices (P3).
- Multi-currency.

### Design decisions

- **Quantity and unit.**
  - A ticket's approved quantity is in one billing unit: `ton` (US short ton), `cy` (cubic yard), `load`, `each` or `gal`. The column is the existing `tonnage`, read as "quantity in the ticket's unit"; for every existing ticket the unit is `ton`, so no value changes.
  - **Pounds** are a source unit, not a billing unit. They convert to short tons (2,000 lb = 1 t, rounded half up to 0.001 t), and the original figure and unit are kept on the ticket.
  - **Gross, tare and net weights** are recorded with their unit. When the ticket is weighed in tons, Quarvera checks net against the entered quantity and flags a mismatch. It never silently changes the quantity.
- **Rate unit.**
  - A material rate has a unit (default `ton`) and only prices tickets in that unit.
  - A ticket in another unit is unpriced with the reason. No volume-to-weight guessing.
- **Totals never mix units.**
  - Tons are totalled from ton tickets only. Other units are totalled per unit and shown as such.
  - Money totals are always compatible, because each cost was priced in its own unit.
- **Hauling once per delivery** (`docs/engineering/trucking-billing.md`):
  - Hourly is paid by the shift.
  - Per ton or per load is paid by the approved ticket's locked hauling charge.
  - Freight included is paid in the material price.
- **Invoices are never cost.**
  - Supplier and hauler invoices are compared with approved records.
  - Billed amounts appear apart from approved cost, everywhere.

### Acceptance criteria

The starting status is the audit of commit `e19433d`. The current status is updated at the end of each run, with evidence.

| ID | Cat. | Pri. | Requirement | Start | Current | Evidence |
|---|---|---|---|---|---|---|
| F1 | Core | P0 | Approval locks a delivery's quantity, rate and cost; corrections withdraw the approval and are audited | Done | | `tests/test_price_snapshots.py`, `db_guards.py` trigger |
| F2 | Core | P0 | Material rates by supplier, material, project and effective dates; most specific started rate; expired never falls back | Done | | `tests/test_project_rates.py` |
| F3 | Core | P1 | Units of measure (ton, cy, load, each, gal) on deliveries and rates; a rate prices only its own unit; no silent cross-dimension conversion | Open | | |
| F4 | Core | P1 | Pounds converted to tons with the source preserved; gross, tare and net recorded and checked | Open | | |
| F5 | Core | P0 | Hourly trucking priced and locked at approval (minimums, standby, increments, allocations) | Done | | `tests/test_trucking_*` |
| F6 | Core | P0 | Per-ton and per-load hauling charge locked at approval; never on freight-included or hourly-shift deliveries | Done | | `tests/test_trucking_billing.py` |
| F7 | Core | P1 | Supplier invoice lines compared with approved deliveries, ticket by ticket or in total, in the line's unit | Partial (tons only) | | `tests/test_invoice_*` |
| F8 | Core | P0 | Hauler invoice hourly, standby and minimum lines reconciled with approved shifts | Done | | `tests/test_hauling_invoices_api.py` |
| F9 | Core | P1 | Hauler invoice per-ton and per-load lines reconciled with approved deliveries carrying that hauler's charge | Open | | |
| F10 | Core | P1 | Unbilled activity covers approved deliveries, hourly shifts and per-ton/per-load hauling | Partial | | |
| S1 | Scenarios | P1 | Duplicate billing (same delivery or shift on two lines or invoices) detected for every billing method | Partial | | |
| S2 | Scenarios | P1 | Partial billing: what an invoice doesn't bill stays unbilled, for every billing method | Partial | | |
| S3 | Scenarios | P1 | Billing for another project's activity is flagged, for every billing method | Partial | | |
| S4 | Scenarios | P1 | Wrong rate, quantity and load count detected and explained, for every billing method | Partial | | |
| S5 | Scenarios | P0 | Missing rates are disclosed everywhere and never shown as $0 | Done | | `tests/test_financial_consistency.py` |
| S6 | Scenarios | P0 | A rate change never alters an approved cost | Done | | `tests/test_price_snapshots.py`, `test_trucking_billing.py` |
| S7 | Scenarios | P1 | Incompatible units are never aggregated or compared | Open | | |
| U1 | Frontend | P1 | Ticket review captures unit, quantity, gross/tare, hauler and truck, with the check visible | Partial | | |
| U2 | Frontend | P1 | Rates & Pricing: unit per material rate, freight, trucking billing methods | Partial | | |
| U3 | Frontend | P1 | Hauler invoice entry supports per-ton and per-load lines (by date and truck, or by ticket numbers) | Open | | |
| U4 | Frontend | P1 | An uncertain hauler-invoice line can be resolved by a person choosing the shift, recorded and audited | Open | | |
| U5 | Frontend | P1 | Every exception says why, links to the invoice, records and source document, and has owner, status and notes | Done | | `app/invoices/exceptions` |
| U6 | Frontend | P1 | Records, reports, analytics and exports show quantities with their units | Open | | |
| I1 | Integration | P0 | The same approved activity gives the same figures on Overview, project report, directory, analytics, records and exports | Partial (tons only) | | `test_financial_consistency.py` |
| I2 | Integration | P0 | Invoices never add cost | Done | | `test_financial_consistency.py` |
| I3 | Integration | P1 | Per-ton and per-load hauling appear in invoices, exceptions, unbilled and reports | Partial | | |
| D1 | Integrity | P0 | Database locks approved quantity, unit, rates and charges | Partial | | `db_guards.py` |
| D2 | Integrity | P0 | Company isolation on every financial endpoint, including new ones | Done | | `test_t_tenant_sweep.py`, `test_c_cross_company.py` |
| D3 | Integrity | P1 | Migrations additive and tested on legacy data | Partial | | |
| D4 | Integrity | P1 | Concurrent edits refused by revision on tickets, shifts and invoices | Done | | `tests/test_concurrency.py` |
| T1 | Testing | P1 | Regression tests for each billing method and invoice rule | Partial | | |
| T2 | Testing | P1 | Financial consistency tests cover units and per-unit hauling invoices | Partial | | |
| T3 | Testing | P0 | SQLite, PostgreSQL, browser and real-stack suites green | Partial (real-stack failing on #41) | | |
| M1 | Manual | P1 | Real-stack walkthrough for each billing method, delivery to reconciled invoice | Partial | | |
| M2 | Manual | P1 | Demo scenarios with independently calculated expected results, verified against the app | Open | | |
| Doc1 | Docs | P1 | Billing, units and reconciliation rules documented | Partial | | |
| Doc2 | Docs | P1 | This roadmap maintained with evidence | Open | | |

**Starting score: 53%.** Per category: Core 60% × 25 = 15.0; Scenarios 57% × 15 = 8.6; Frontend 33% × 15 = 5.0; Integration 67% × 10 = 6.7; Integrity 75% × 10 = 7.5; Testing 67% × 10 = 6.7; Manual 25% × 10 = 2.5; Docs 25% × 5 = 1.3.

### External validation (not part of the engineering score)

- **V1:** pilot contractors confirm the billing assumptions in `trucking-billing.md` (tons used for per-ton hauling, freight-not-stated treatment).
- **V2:** a real supplier invoice and a real hauler invoice reconcile as expected.
- **V3:** units used by the first customers are covered by ton, cy, load, each and gal.

### Findings from the audit (2026-11-26)

| Pri. | Finding |
|---|---|
| P1 | No units of measure: every quantity is treated as tons (F3, F4, S7, U6) |
| P1 | Per-ton and per-load hauling is charged on tickets but cannot be reconciled with a hauler's invoice, and isn't in unbilled activity (F9, F10, I3) |
| P1 | An ambiguous hauler-invoice line (two shifts that day) can't be resolved by a person (U4) |
| P1 | The real-stack test on PR #41 failed on a renamed button (fixed in `e19433d`) |
| P2 | Volume-to-weight factor per material, under contract |
| P2 | Excel export speed with `lxml` (4 s at 50,000 tickets) |
| P3 | AI reading of hauler invoices |
