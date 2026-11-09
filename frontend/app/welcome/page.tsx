import Link from "next/link";
import { Brand } from "../public-ui";

export const metadata = {
  title: "TicketFlow · Material delivery control for contractors",
  description: "TicketFlow turns scanned delivery tickets into verified tons and costs, then checks them against supplier invoices line by line.",
};

const STEPS = [
  { n: "01", title: "Capture", body: "Upload your scanned pages. Four-up scans are split into individual tickets, and the original page stays on file." },
  { n: "02", title: "Verify", body: "Your team checks ticket number, material, date and tons against the scan. Nothing is approved until a person approves it." },
  { n: "03", title: "Reconcile", body: "Compare supplier invoices with approved tickets, line by line. Overbilled tons, missing tickets and rate differences surface before you pay." },
];

const CONTROLS = [
  { title: "A person approves every ticket", body: "Values read from a scan stay suggestions until someone on your team confirms them." },
  { title: "Every approval carries a name", body: "Each employee signs in with their own account, and approvals and changes are recorded." },
  { title: "Roles that match your office", body: "Owners, managers, clerks and viewers each see and do only what their job needs." },
  { title: "Each company stays separate", body: "Your tickets, rates and invoices are kept apart from every other company's." },
];

function SampleDocuments() {
  return <figure className="tf-w-figure">
    <div className="tf-w-docs" aria-hidden="true">
      <div className="tf-w-ticket">
        <div className="tf-w-ticket-head"><span>Delivery ticket</span><span className="tf-w-hand">48213</span></div>
        <dl>
          <div><dt>Date</dt><dd className="tf-w-hand">11/03</dd></div>
          <div><dt>Material</dt><dd className="tf-w-hand">3/4&quot; crushed stone</dd></div>
          <div><dt>Gross</dt><dd className="tf-w-hand">48.62</dd></div>
          <div><dt>Tare</dt><dd className="tf-w-hand">26.21</dd></div>
          <div><dt>Net tons</dt><dd className="tf-w-hand tf-w-net">22.41</dd></div>
        </dl>
      </div>
      <div className="tf-w-invoice">
        <div className="tf-w-invoice-head"><strong>Invoice comparison</strong><span>Sample Aggregates</span></div>
        <div className="tf-w-row tf-w-row-head"><span>Ticket</span><span>Ticketed</span><span>Invoiced</span><span>Result</span></div>
        <div className="tf-w-row"><span>48213</span><span>22.41</span><span>22.41</span><span className="tf-w-ok">Matched</span></div>
        <div className="tf-w-row"><span>48214</span><span>19.87</span><span>21.87</span><span className="tf-w-flag">2.00 t over</span></div>
        <div className="tf-w-row"><span>48219</span><span>—</span><span>18.30</span><span className="tf-w-flag">No ticket</span></div>
      </div>
    </div>
    <figcaption>Illustration using sample data.</figcaption>
  </figure>;
}

export default function WelcomePage() {
  return <div className="tf-w">
    <div className="tf-w-hero">
      <header className="tf-w-bar">
        <Brand />
        <nav aria-label="Account">
          <Link href="/login" className="tf-w-bar-link">Sign in</Link>
          <Link href="/signup" className="tf-pub-button tf-pub-button-primary tf-w-bar-cta">Create account</Link>
        </nav>
      </header>
      <main className="tf-w-hero-grid">
        <div>
          <p className="tf-pub-kicker">Material delivery control for contractors</p>
          <h1>Know what was delivered before you pay for it.</h1>
          <p className="tf-w-sub">TicketFlow turns stacks of scanned delivery tickets into verified tons and costs, then checks them against supplier invoices line by line.</p>
          <div className="tf-w-actions">
            <Link href="/signup" className="tf-pub-button tf-pub-button-primary tf-w-action">Create account</Link>
            <Link href="/login" className="tf-pub-button tf-pub-button-ghost tf-w-action">Sign in</Link>
          </div>
        </div>
        <SampleDocuments />
      </main>
    </div>

    <section className="tf-w-section" aria-labelledby="w-steps">
      <h2 id="w-steps">From the truck to the invoice</h2>
      <ol className="tf-w-steps">
        {STEPS.map((step) => <li key={step.n}>
          <span className="tf-w-num" aria-hidden="true">{step.n}</span>
          <h3>{step.title}</h3>
          <p>{step.body}</p>
        </li>)}
      </ol>
    </section>

    <section className="tf-w-band" aria-labelledby="w-controls">
      <div className="tf-w-band-inner">
        <h2 id="w-controls">Built for the people who answer for the numbers</h2>
        <ul className="tf-w-controls">
          {CONTROLS.map((item) => <li key={item.title}><h3>{item.title}</h3><p>{item.body}</p></li>)}
        </ul>
      </div>
    </section>

    <section className="tf-w-close" aria-labelledby="w-pilot">
      <div>
        <h2 id="w-pilot">TicketFlow is in a private pilot</h2>
        <p>We set up each workspace with the company using it. Tell us about yours and we&apos;ll follow up.</p>
      </div>
      <Link href="/signup" className="tf-pub-button tf-pub-button-primary tf-w-action">Create account</Link>
    </section>

    <footer className="tf-w-footer">
      <Brand tone="dark" />
      <span>Material delivery control for contractors</span>
    </footer>
  </div>;
}
