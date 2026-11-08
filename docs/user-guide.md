# TicketFlow user guide

TicketFlow keeps material delivery tickets and checks supplier invoices against them. A person
checks each ticket against its scan and approves it. Only approved tickets count toward tons,
costs and invoice comparisons.

## 1. First setup

1. On the computer TicketFlow runs on, open TicketFlow in a browser. A new server with no
   accounts shows **Set up TicketFlow** instead of the sign-in form.
2. Enter your company name, your name, a username and a password of at least 12 characters.
   This creates the company and its first **owner**, and signs you in. Setup only works once.
   By default it only works from that computer.
3. Add your team from **Team**. Each person gets their own sign-in and a one-time password,
   so every approval shows who made it. The roles are:
   - **Owner**: everything, including the team.
   - **Manager**: approves tickets, sets rates, records invoices, manages projects.
   - **Clerk**: uploads and enters tickets and flags them, but can't approve them.
   - **Viewer**: reads everything and changes nothing.
4. Create a **Project** for each job, using the job number your team already uses.
5. Under **Suppliers & rates**, add each supplier and material, spelled the way your tickets
   spell them, and the price per ton. An approved ticket locks the rate in force when it's
   approved, so later rate changes don't alter it.

## 2. Upload scanned tickets

On **Ticket intake**, choose the project, the layout (one ticket per page or four), and how the
tickets are entered:

- **Manual entry**: blank fields beside each scan. No AI is involved.
- **AI extraction**: fills the fields from the scan if your server has a provider key. Every
  value still needs checking.

Enter how many paper tickets you scanned if you can. The **scan check** compares that count
with the tickets found, and shows every slot on every page. Faint slots ask a person whether
they hold a ticket. A separate processing worker prepares uploads. If an upload waits at
"Uploaded", the page says so: run `python scripts/run_worker.py` from the backend folder.

## 3. Check and approve each ticket

Each ticket opens with the scan on one side and its fields on the other. The cursor starts in
the first empty field.

- Check the ticket number, date, material, tons, supplier and job against the scan.
- Fields explain themselves as you type. Tons above 60 and dates in the future, or more than
  two years ago, get a warning, not a block. A tons warning asks whether the figure is pounds.
  **TicketFlow never converts units**: enter the tons printed on the ticket.
- **Accept & next** approves this one ticket and opens the next one to review. Nothing approves
  more than one ticket at a time.
- **Flag for later** sets a ticket aside with a note. **Skip non-ticket** is for pages that
  aren't tickets.
- If a teammate changed the ticket while you had it open, nothing is saved. The latest values
  appear, and you check them again.
- A possible duplicate (same supplier and ticket number already approved) asks you to decide,
  and your decision is recorded.

**Unsaved work is kept on this computer.** If the browser crashes or the tab closes, reopening
the ticket brings back what you typed, still unsaved, with a one-click **Discard it**. If the
ticket changed in the meantime, what you typed appears beside the current values and you choose,
field by field. Signing out clears all of it, so sign out on a shared computer.

**If a save fails**, what you typed stays, and the message says why: lost connection, server
busy, too many requests, or signed out. It gives the server's reference when there is one, and a
**Try again** button where retrying can help.

Opening a batch again picks up where you left off.

### Keyboard

| Keys | What they do |
|---|---|
| Cmd/Ctrl + Enter | Accept (or save) and go to the next ticket |
| Enter | Next field, then the Accept button |
| Alt + D | Material, supplier and job: same as the previous ticket |
| Alt + ↑ / ↓ | Previous or next ticket, even while typing |
| J / K, Alt + ← / → | Next or previous ticket |
| N | Next ticket that still needs review |
| F | Next flagged ticket |
| + / − · 0 | Zoom the scan in or out · fit to width |
| R | Rotate the view (the stored scan isn't changed) |
| / | Search the ticket queue |
| Esc | Leave the current field |
| ? | Show or hide this list |

Single keys work when the cursor isn't in a field: press Esc first. A swipe on the scan scrolls
the page on a phone. Once it's zoomed, a drag pans it instead.

## 4. Records and exports

**Delivery records** lists every ticket, with filters for project, supplier, material, status and
dates. Totals and exports follow the filters and count approved tickets only. **Choose columns**
picks the export columns. **Save this view** keeps filters and columns for the whole team.
Under **Name these columns** you can give columns your own spreadsheet's headers, and exports from
that view use them.

## 5. Check supplier invoices

1. On **Invoice comparison**, record each invoice material line: project, supplier, material,
   delivery dates, billed tons, rate and material subtotal. Or upload the invoice PDF under
   **Invoice PDFs** and confirm its draft lines beside the original.
2. Paste the invoice's ticket numbers (and tons, if listed) to check each delivery, not just the
   totals.
3. Open a line's **Details**. **Why this result** walks from approved tons to billed tons, and
   from approved cost to the billed subtotal, in exact figures, then says what to do next.
4. **Download discrepancy list (CSV)** gives one row per difference for a chosen project and
   supplier, to send to the supplier.
5. On **Project report**, **Unbilled deliveries** lists approved tickets that no invoice line
   lists or covers, the date ranges no invoice covers, and a CSV of every one. Each figure leads
   to its tickets, scans or invoice line.

## What TicketFlow doesn't do

- It doesn't approve tickets, invoices or payments on its own. Every approval is one person,
  one ticket. A comparison result is never an approval for payment.
- It doesn't convert pounds, yards or loads into tons.
- It doesn't read scans perfectly. AI-read values and invoice PDF drafts are suggestions until a
  person checks them.
- It leaves tax, freight and other charges out of the material comparison.
- It doesn't prove completeness. The scan check compares against the paper count you enter. It
  can't know about tickets that were never scanned.
- Unbilled deliveries may be on an invoice that hasn't been entered yet, or billed under another
  name. Check with the supplier before acting.
