import Link from "next/link";
import { Brand } from "../public-ui";

export const metadata = {
  title: "TicketFlow · Delivery tickets and supplier invoices for contractors",
  description: "TicketFlow reads your material delivery tickets, puts each one beside its scan for your office to check, and compares approved tons with the supplier's invoice.",
};

const WIDTH = 1960;
const HEIGHT = 1130;

const INVOICE: { ticket: string; date: string; billed: string; approved: string | null; result: string; over?: string }[] = [
  { ticket: "48207", date: "11/03", billed: "21.18", approved: "21.18", result: "Matched" },
  { ticket: "48208", date: "11/03", billed: "20.64", approved: "20.64", result: "Matched" },
  { ticket: "48213", date: "11/03", billed: "22.41", approved: "22.41", result: "Matched" },
  { ticket: "48214", date: "11/03", billed: "21.87", approved: "19.87", result: "Tons differ", over: "+$37.00" },
  { ticket: "48219", date: "11/04", billed: "18.30", approved: null, result: "Not in TicketFlow", over: "+$338.55" },
  { ticket: "48221", date: "11/04", billed: "23.02", approved: "23.02", result: "Matched" },
  { ticket: "48221", date: "11/04", billed: "23.02", approved: null, result: "Repeated on this invoice", over: "+$425.87" },
  { ticket: "48226", date: "11/05", billed: "19.75", approved: "19.75", result: "Matched" },
];

function Cell({ label, children, className = "" }: { label: string; children?: React.ReactNode; className?: string }) {
  return <div className={`tf-t-cell ${className}`}><span className="tf-t-label">{label}</span>{children}</div>;
}

function Ticket() {
  return <div className="tf-t" data-sample-ticket aria-hidden="true">
    <div className="tf-t-head">
      <div><strong>Sample Aggregates</strong><span>Plant 4 Scale House · Delivery Ticket</span></div>
      <div className="tf-t-serial">No 48213</div>
    </div>
    <div>
      <Cell label="Date"><span className="tf-t-hand">11/3/26</span></Cell>
      <Cell label="Truck"><span className="tf-t-hand">214</span></Cell>
      <Cell label="Driver" className="tf-t-hide-sm"><span className="tf-t-hand">R. Dominguez</span></Cell>
    </div>
    <div><Cell label="Job / deliver to"><span className="tf-t-hand">Route 9 widening - Lot B</span></Cell></div>
    <div><Cell label="Material"><span className="tf-t-hand">#57 Stone</span></Cell></div>
    <div>
      <Cell label="Gross"><span className="tf-t-hand">48.62</span></Cell>
      <Cell label="Tare"><span className="tf-t-hand">26.21</span></Cell>
      <Cell label="Net tons"><span className="tf-t-hand tf-t-net">22.41</span></Cell>
    </div>
    <div><Cell label="Received by"><span className="tf-t-hand tf-t-sign">J Whitfield</span></Cell></div>
  </div>;
}

export default function WelcomePage() {
  return <div className="tf-s">
    <header className="tf-s-nav">
      <div className="tf-s-wrap">
        <Brand />
        <nav aria-label="Site">
          <a href="#tickets" className="tf-s-anchor">Tickets</a>
          <a href="#invoices" className="tf-s-anchor">Invoices</a>
          <Link href="/login">Sign in</Link>
          <Link href="/signup" className="tf-s-nav-cta">Request access</Link>
        </nav>
      </div>
    </header>

    <main>
      <section className="tf-s-hero">
        <div className="tf-s-wrap">
          <h1>Know what was delivered<br /> before you pay for it.</h1>
          <div className="tf-s-hero-lower">
            <div className="tf-s-hero-copy">
              <div>
                <p className="tf-s-hero-text">TicketFlow reads your delivery tickets, puts each one beside its scan for your office to check, then compares the approved tons with the supplier&apos;s invoice.</p>
                <div className="tf-s-hero-actions">
                  <Link href="/signup" className="tf-pub-button tf-pub-button-primary">Request access</Link>
                  <Link href="/login" className="tf-s-text-link">Sign in</Link>
                </div>
              </div>
              <p className="tf-s-pilot">Private pilot. We set up each company ourselves.</p>
            </div>
            <figure className="tf-s-shot">
              <picture>
                <source media="(max-width: 620px)" srcSet="/site/review-phone.webp" />
                <img src="/site/review.webp" width={WIDTH} height={HEIGHT} alt="TicketFlow's review screen: a scanned delivery ticket beside the values read from it, with an Accept button." />
              </picture>
              <figcaption className="tf-s-caption">TicketFlow&apos;s review screen, with fictional data.</figcaption>
            </figure>
          </div>
        </div>
      </section>

      <section id="tickets" className="tf-s-wrap tf-s-tickets" aria-labelledby="tickets-title">
        <header>
          <h2 id="tickets-title">Paper delivery tickets pile up fast. Your office shouldn&apos;t have to key every load into a spreadsheet just to check an invoice.</h2>
          <p className="tf-s-lead">Scan the stack. TicketFlow reads each ticket and shows the values beside the original. Nothing counts toward your tons until someone in your office approves it.</p>
        </header>
        <div className="tf-s-stage">
          <Ticket />
          <div>
            <dl className="tf-r">
              <div><dt>Supplier</dt><dd>Sample Aggregates</dd></div>
              <div><dt>Ticket number</dt><dd>48213</dd></div>
              <div><dt>Delivery date</dt><dd>Nov 3, 2026</dd></div>
              <div><dt>Job</dt><dd>Route 9 widening, Lot B</dd></div>
              <div><dt>Material</dt><dd>#57 Stone</dd></div>
              <div className="tf-r-tons"><dt>Net tons</dt><dd>22.41</dd></div>
            </dl>
            <p className="tf-s-approved"><strong>Approved</strong> after a person checked it against the ticket. Priced at $18.50 a ton and locked at $414.59. The scan stays on file.</p>
          </div>
          <p className="tf-s-caption">Fictional ticket, supplier and job.</p>
        </div>
      </section>

      <section id="invoices" className="tf-s-invoices" aria-labelledby="invoices-title">
        <div className="tf-s-wrap">
          <div className="tf-s-inv-grid">
            <div className="tf-s-sheet">
              <header><strong>Invoice 20771</strong><span>Sample Aggregates · #57 Stone at $18.50 a ton</span></header>
              <table className="tf-i">
                <caption className="sr-only">A fictional supplier invoice compared with approved tickets, one row per billed ticket.</caption>
                <thead><tr>
                  <th scope="col">Ticket</th><th scope="col" className="tf-i-date">Date</th><th scope="col">Billed</th><th scope="col">Approved</th><th scope="col" className="tf-i-money">Over</th><th scope="col" className="tf-i-result">Result</th>
                </tr></thead>
                <tbody>
                  {INVOICE.map((row, index) => <tr key={index} className={row.over ? "tf-i-off" : undefined}>
                    <td>{row.ticket}</td>
                    <td className="tf-i-date">{row.date}</td>
                    <td>{row.billed}</td>
                    <td className={row.approved ? undefined : "tf-i-nil"}>{row.approved ?? "none"}</td>
                    <td className="tf-i-money">{row.over ?? ""}</td>
                    <td className="tf-i-result">{row.result}</td>
                  </tr>)}
                </tbody>
                <tfoot><tr>
                  <td>Total</td><td className="tf-i-date" /><td>170.19</td><td>126.87</td><td className="tf-i-money">+$801.42</td><td className="tf-i-result" />
                </tr></tfoot>
              </table>
              <span className="tf-s-pen" aria-hidden="true">call supplier!</span>
            </div>
            <div className="tf-s-inv-copy">
              <h2 id="invoices-title">Then the invoice comes in.</h2>
              <p className="tf-s-sum">$801.42</p>
              <p className="tf-s-sum-label">billed on this fictional invoice that the tickets don&apos;t support.</p>
              <p className="tf-s-body">A tonnage that doesn&apos;t match, a ticket you never received, a load billed twice. Each one shows up by line and in dollars, before you pay. So do approved deliveries nobody has billed yet.</p>
            </div>
          </div>
          <p className="tf-s-caption">Fictional invoice and figures, using TicketFlow&apos;s own result labels.</p>
        </div>
      </section>

      <section className="tf-s-wrap tf-s-office" aria-labelledby="office-title">
        <h2 id="office-title">Your office checks tickets instead of typing them.</h2>
        <dl className="tf-s-roles">
          <div><dt>Accounts payable</dt><dd>The scan and the values side by side, and the keyboard moves you through the stack.</dd></div>
          <div><dt>Project managers</dt><dd>Delivered tons by job and material, any day of the month.</dd></div>
          <div><dt>Controllers, owners</dt><dd>Every approval has a name on it, and approved figures are locked.</dd></div>
          <div><dt>The field</dt><dd>Nothing changes. Foremen keep turning in the same tickets.</dd></div>
        </dl>
      </section>

      <section className="tf-s-pilotband" aria-labelledby="pilot-title">
        <div className="tf-s-wrap">
          <div>
            <h2 id="pilot-title">We&apos;re bringing on a small number of contractors.</h2>
            <p>You can&apos;t sign yourself up yet. Tell us what you buy and about how many tickets you handle a month, and we&apos;ll talk it through.</p>
          </div>
          <div className="tf-s-hero-actions">
            <Link href="/signup" className="tf-pub-button tf-pub-button-white">Request access</Link>
            <Link href="/login" className="tf-s-text-link">Sign in</Link>
          </div>
        </div>
      </section>
    </main>

    <footer className="tf-s-footer">
      <div className="tf-s-wrap">
        <div className="tf-s-footer-top">
          <div><Brand /><p>Delivery tickets and supplier invoices for contractors.</p></div>
          <nav aria-label="Footer">
            <div><h2>Product</h2><ul><li><a href="#tickets">Reading tickets</a></li><li><a href="#invoices">Checking invoices</a></li></ul></div>
            <div><h2>Account</h2><ul><li><Link href="/login">Sign in</Link></li><li><Link href="/signup">Request access</Link></li></ul></div>
          </nav>
        </div>
        <div className="tf-s-footer-end"><span>© {new Date().getFullYear()} TicketFlow</span><span>Companies, tickets and figures on this page are fictional examples.</span></div>
      </div>
    </footer>
  </div>;
}
