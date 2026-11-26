# Market notes: e-ticketing and ticket reconciliation (2026-11-25)

A short record of what public sources say. It was gathered with web search only, with no vendor demos or customer interviews. Vendor claims are marked as such and are unverified.

## Electronic ticketing is arriving, unevenly

- FHWA describes e-ticketing as recording and transferring material quantities electronically "in real-time, in lieu of paper tickets", mainly for hot mix asphalt, ready-mix concrete and aggregates. Its stated reasons are safety (people collecting paper from trucks in work zones) and paperwork. [FHWA, What is e-Ticketing](https://www.fhwa.dot.gov/construction/econstruction/what_is_e_ticketing.pdf); [EDC-6 fact sheet](https://www.fhwa.dot.gov/innovation/everydaycounts/edc_6/docs/eticketing_factsheet_edc6.pdf)
- Adoption is led by state DOTs:
  - Iowa ran the first pilot (asphalt, 2015) and has since completed hundreds of projects.
  - Delaware required it on asphalt paving from spring 2022.
  - Caltrans and PennDOT run programs.
  - [Caltrans e-ticketing](https://dot.ca.gov/programs/construction/e-ticketing); [PennDOT e-Ticketing](https://www.pa.gov/agencies/penndot/about-penndot/advisory-committees-boards-and-commissions/state-transportation-innovation-council/innovations/eticketing-and-digital-as-builts.html); [FHWA HIF-24-037](https://www.fhwa.dot.gov/construction/econstruction/hif24037.pdf)
- **No single open data standard was found.** Agencies commonly receive tickets from pre-authorised producers through API-based web portals. Iowa is standardising ticket data attributes in its own specification. A "Standard Specification for Material Delivery Management Systems" (2023) is listed by GlobalSpec, but its issuer and content weren't confirmed. [FHWA HIF-24-068](https://www.fhwa.dot.gov/construction/econstruction/hif24068.pdf); [GlobalSpec listing](https://standards.globalspec.com/std/14645863/mdms-1)

**What this means for Quarvera:** for now, contractors get electronic tickets as producer exports, portal downloads and PDFs, not through one API. So spreadsheet import, PDF text reading and paper or photo capture all go through one review and approval path. A producer API connector should wait for a real customer's specification. The intake layer records where each record came from, so a connector can be added later without a second financial system.

## Contractor-side tools

- **Command Alkon** (TicketPro, Ticket Accounting, CONNEX). Vendor pages describe:
  - OCR of paper tickets and receiving tickets from scale systems;
  - matching invoices to tickets line by line;
  - flags for missing, duplicate and already-invoiced tickets.
  - A vendor case study claims one contractor reconciled six times as many invoices a day; that's one self-reported example.
  - [Ticket Accounting](https://commandalkon.com/products/ticket-accounting); [TicketPro launch](https://aggbusiness.com/ab1/products/command-alkon-launches-ticketpro); [App Store](https://apps.apple.com/us/app/id1560216839)
- **PackFields Construction.** Its listing describes reading delivery tickets and invoices, matching lines to POs and cost codes, flagging mismatches with evidence, and foremen texting photos of tickets. [Capterra listing](https://www.capterra.co.uk/software/1105172/PackFields-Construction)

**Quarvera already covers:** paper, scan and photo intake with human approval; invoice line matching with missing, duplicate and already-billed findings; hauling by the hour, per ton and per load; exact dollar effects.

**Gaps worth checking with pilots before building:**
- PO and cost-code matching (budget against actual is Mission 04);
- photo capture by text message;
- receiving producer e-tickets directly.
