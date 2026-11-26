import Link from "next/link";
import type { ReactNode } from "react";
import { SiteFooter, SiteNav } from "../site-ui";

export const metadata = {
  title: "Quarvera · Every delivery accounted for.",
  description: "Quarvera captures material delivery tickets, trucking time sheets and invoices from paper, PDFs, photos and spreadsheets, has your team verify them, prices them against your contracts, and reconciles every supplier and hauler invoice before you pay.",
};

const SHOT_W = 2160;
const SHOT_H = 1350;

function Shot({ src, alt, priority = false, width = SHOT_W, height = SHOT_H }: { src: string; alt: string; priority?: boolean; width?: number; height?: number }) {
  return <div className="qs-frame">
    <div className="qs-frame-bar" aria-hidden="true"><i /><i /><i /><span>app.quarvera.com</span></div>
    <img src={src} alt={alt} width={width} height={height} loading={priority ? "eager" : "lazy"} decoding="async" />
  </div>;
}

function Check() {
  return <svg aria-hidden="true" viewBox="0 0 20 20" width="18" height="18"><path d="m4.5 10.5 3.5 3.5 7.5-8" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

const SOURCES = ["Paper delivery tickets", "Scanned batches", "Phone photos", "Digital PDF tickets", "Supplier spreadsheets",
  "Truck time sheets", "Supplier invoices", "Hauler invoices"];

const ICON: Record<string, ReactNode> = {
  intake: <path d="M12 15V4m0 0L7.5 8.5M12 4l4.5 4.5M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" />,
  sheet: <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M4 9h16M4 15h16M10 3v18" /></>,
  review: <><rect x="3" y="4" width="8" height="16" rx="1.5" /><path d="M14 7h7M14 12h7M14 17h4" /></>,
  price: <><path d="M3 12V4h8l10 10-8 8L3 12Z" /><circle cx="7.5" cy="8" r="1.5" /></>,
  truck: <><path d="M3 6h11v10H3zM14 10h4l3 3v3h-7" /><circle cx="7" cy="17.5" r="2" /><circle cx="17" cy="17.5" r="2" /></>,
  invoice: <><path d="M6 3h9l4 4v14H6z" /><path d="M15 3v4h4M9 12h7M9 16h5" /></>,
  chart: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>,
};

const CAPABILITIES: { icon: string; title: string; text: string; href: string }[] = [
  { icon: "intake", title: "Document intake", text: "Drop in scanned stacks, phone photos and digital PDFs. Quarvera finds each ticket on the page, reads it, and keeps the original.", href: "#intake" },
  { icon: "sheet", title: "Spreadsheet imports", text: "Bring in a supplier's CSV or Excel export. Map the columns once, preview every row, and catch duplicates before anything is created.", href: "#intake" },
  { icon: "review", title: "Fast verification", text: "Each ticket sits beside its scan. The cursor goes to what needs a person, and a clean ticket is one keystroke.", href: "#intake" },
  { icon: "price", title: "Pricing and contracts", text: "Company rates, project agreements, dated price changes and units. The rate is locked the moment a delivery is approved.", href: "#costs" },
  { icon: "truck", title: "Trucking", text: "Hourly shifts from time sheets, minimums and standby, and hauling billed per ton or per load. Each load is paid for once.", href: "#trucking" },
  { icon: "invoice", title: "Invoice reconciliation", text: "Supplier and hauler invoices checked line by line against approved activity, with every difference explained in dollars.", href: "#reconcile" },
  { icon: "chart", title: "Project cost tracking", text: "Material and hauling cost by project, material, supplier and week, from approved records only. Export to Excel any time.", href: "#costs" },
];

const STEPS = [
  { n: "01", title: "Capture", text: "Upload tickets, time sheets and invoices however they arrive: paper, PDF, photo or spreadsheet." },
  { n: "02", title: "Verify", text: "Your team checks each record against its source. Nothing counts until a named person approves it." },
  { n: "03", title: "Price and lock", text: "Approved deliveries are priced from your contracts, and the price is locked with the approval." },
  { n: "04", title: "Reconcile", text: "Invoices are compared with what was approved. You see exactly what to question, before you pay." },
];

const CONTROLS = [
  { title: "A person approves every record", text: "Quarvera reads and suggests. Your team decides. Unapproved values never reach a total." },
  { title: "Locked prices", text: "A delivery keeps the rate it was approved at. Changing a rate later never rewrites history." },
  { title: "Full history", text: "Every edit, approval and correction is recorded with who made it and when." },
  { title: "Missing is never zero", text: "A delivery without a rate is shown as incomplete, not as $0, so totals never look better than they are." },
  { title: "Units kept straight", text: "Tons, cubic yards, loads and gallons are never added together. Pounds convert to tons with the original kept." },
  { title: "Your data stays yours", text: "Each company's records are isolated from every other company's, down to the database." },
];

export default function WelcomePage() {
  return <div className="qs">
    <SiteNav />
    <main>
      <section className="qs-hero">
        <div className="qs-wrap">
          <p className="qs-eyebrow">Material and trucking cost control for contractors</p>
          <h1>{"Every delivery "}<br />accounted for.</h1>
          <p className="qs-hero-lead">Quarvera turns delivery tickets, truck time sheets and supplier invoices into verified, priced project costs, then checks every invoice against what actually arrived on your jobs.</p>
          <div className="qs-hero-actions">
            <Link href="/demo" className="qs-btn qs-btn-primary">Request a demo</Link>
            <a href="#how" className="qs-btn qs-btn-quiet">See how it works</a>
          </div>
          <ul className="qs-hero-points">
            <li><Check />Paper, PDF, photo and spreadsheet intake</li>
            <li><Check />A person approves every record</li>
            <li><Check />Invoices checked line by line</li>
          </ul>
        </div>
        <div className="qs-wrap qs-hero-shot">
          <Shot src="/site/product-overview.webp" priority
            alt="Quarvera's Overview: verified material tons, material and hauling cost, spending by week and cost by project, for a fictional contractor." />
          <p className="qs-caption">Quarvera&apos;s Overview, showing a fictional demo company.</p>
        </div>
      </section>

      <section className="qs-sources" aria-label="What Quarvera takes in">
        <div className="qs-wrap">
          <p>Works with the paperwork you already get</p>
          <ul>{SOURCES.map((s) => <li key={s}>{s}</li>)}</ul>
        </div>
      </section>

      <section className="qs-problem" aria-labelledby="problem-title">
        <div className="qs-wrap">
          <div className="qs-problem-head">
            <p className="qs-eyebrow qs-eyebrow-dark">The problem</p>
            <h2 id="problem-title">The tickets are on the truck. The invoice is in accounting. Nobody has time to check one against the other.</h2>
          </div>
          <div className="qs-problem-grid">
            <div><h3>Tickets pile up</h3><p>Hundreds of scale tickets a week, handed off in the field, keyed into spreadsheets by hand, or never keyed in at all.</p></div>
            <div><h3>Invoices come weeks later</h3><p>By the time a supplier bills, matching each line to a delivery means digging through stacks of paper.</p></div>
            <div><h3>Trucking is billed three ways</h3><p>By the hour, by the ton, by the load. Standby, minimums and split shifts make every hauler invoice its own puzzle.</p></div>
            <div><h3>Small errors add up</h3><p>A load billed twice, a ticket from another job, a rate that crept up. Each one is easy to miss and hard to recover once paid.</p></div>
          </div>
        </div>
      </section>

      <section id="platform" className="qs-section" aria-labelledby="platform-title">
        <div className="qs-wrap">
          <header className="qs-section-head">
            <p className="qs-eyebrow">The platform</p>
            <h2 id="platform-title">One place for every delivery, every truck and every invoice.</h2>
            <p>Quarvera covers the work between the scale house and accounts payable: capturing the record, verifying it, pricing it against your agreements, and proving the invoice matches.</p>
          </header>
          <div className="qs-cap-grid">
            {CAPABILITIES.map((c) => <a key={c.title} href={c.href} className="qs-cap">
              <span className="qs-cap-icon"><svg aria-hidden="true" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{ICON[c.icon]}</svg></span>
              <h3>{c.title}</h3>
              <p>{c.text}</p>
            </a>)}
            <Link href="/demo" className="qs-cap qs-cap-cta">
              <h3>See it with your own tickets</h3>
              <p>Bring a stack of tickets and an invoice. We&apos;ll walk through them together.</p>
              <span className="qs-cap-arrow">Request a demo <span aria-hidden="true">→</span></span>
            </Link>
          </div>
        </div>
      </section>

      <section id="intake" className="qs-feature" aria-labelledby="intake-title">
        <div className="qs-wrap qs-feature-row">
          <div className="qs-feature-copy">
            <p className="qs-eyebrow">Document intake and review</p>
            <h2 id="intake-title">Check tickets instead of typing them.</h2>
            <p>Upload a scanned stack with several tickets to a page, a handful of phone photos, or a supplier&apos;s digital PDFs. Quarvera finds each ticket, reads it, and puts the values beside the original so your office can confirm them in seconds.</p>
            <ul className="qs-list">
              <li><Check /><span><strong>Finds the tickets on each page</strong>, straightens them, and never silently drops one it isn&apos;t sure about.</span></li>
              <li><Check /><span><strong>Reads digital PDFs from their text</strong>, and imports supplier spreadsheets row by row.</span></li>
              <li><Check /><span><strong>Keyboard-first review</strong>: the cursor lands on whatever needs a person, and flagged values must be checked.</span></li>
              <li><Check /><span><strong>Catches duplicates</strong>: the same scan or ticket number can&apos;t quietly count twice.</span></li>
            </ul>
          </div>
          <div className="qs-feature-media">
            <Shot width={1800} height={1351} src="/site/product-review.webp" alt="Quarvera's review screen: the original scanned ticket beside the values read from it, with a ticket queue and an Accept & next button." />
          </div>
        </div>
      </section>

      <section id="reconcile" className="qs-feature qs-feature-alt" aria-labelledby="reconcile-title">
        <div className="qs-wrap qs-feature-row qs-feature-flip">
          <div className="qs-feature-copy">
            <p className="qs-eyebrow">Invoice reconciliation</p>
            <h2 id="reconcile-title">Know what to question before you pay.</h2>
            <p>Every supplier invoice line is compared with the deliveries your team approved, at the prices you agreed. Every difference is explained by cause and in dollars, and an exception list says who owns each one.</p>
            <ul className="qs-list">
              <li><Check /><span><strong>Billed tons that don&apos;t match</strong> the approved tickets, ticket by ticket.</span></li>
              <li><Check /><span><strong>Tickets you never received</strong>, loads billed twice, and deliveries from another job.</span></li>
              <li><Check /><span><strong>Rates above your agreement</strong>, and invoice math that doesn&apos;t add up.</span></li>
              <li><Check /><span><strong>Approved deliveries nobody has billed yet</strong>, so nothing slips through the other way.</span></li>
            </ul>
          </div>
          <div className="qs-feature-media">
            <Shot width={1800} height={1351} src="/site/product-exceptions.webp" alt="Quarvera's invoice exceptions: open items by severity with their dollar effect, such as a load billed again or a ticket with no delivery record." />
          </div>
        </div>
      </section>

      <section id="trucking" className="qs-feature" aria-labelledby="trucking-title">
        <div className="qs-wrap qs-feature-row">
          <div className="qs-feature-copy">
            <p className="qs-eyebrow">Trucking</p>
            <h2 id="trucking-title">Hourly, per ton or per load. Each load paid for once.</h2>
            <p>Record truck shifts from time sheets with minimums, standby and billing increments, or price hauling per ton or per load from your hauler agreements. Hauler invoices are reconciled against approved shifts and deliveries, line by line.</p>
            <ul className="qs-list">
              <li><Check /><span><strong>Shifts split across projects</strong> are charged to each job in its share.</span></li>
              <li><Check /><span><strong>No double payment</strong>: a load already covered by an hourly shift or a delivered price is never charged again.</span></li>
              <li><Check /><span><strong>Extra hours, extra tons, extra loads</strong>, each shown with its exact dollar effect.</span></li>
            </ul>
          </div>
          <div className="qs-feature-media">
            <Shot width={1800} height={1351} src="/site/product-hauling.webp" alt="Quarvera's hauling invoices: billed against supported amounts, with a per-ton line that differs by $12.75 from the approved deliveries." />
          </div>
        </div>
      </section>

      <section id="costs" className="qs-feature qs-feature-alt" aria-labelledby="costs-title">
        <div className="qs-wrap qs-feature-row qs-feature-flip">
          <div className="qs-feature-copy">
            <p className="qs-eyebrow">Project cost tracking</p>
            <h2 id="costs-title">Real material and hauling cost, by job, any day of the month.</h2>
            <p>Because every record is verified and priced as it comes in, project managers see delivered quantities and cost by project, material and supplier without waiting for month-end, and controllers can export the detail behind any number.</p>
            <ul className="qs-list">
              <li><Check /><span><strong>Approved figures only</strong>: anything still in review or missing a rate is called out, never hidden.</span></li>
              <li><Check /><span><strong>Average price paid</strong> per supplier and material, per unit.</span></li>
              <li><Check /><span><strong>Excel exports</strong> that match the screen to the cent.</span></li>
            </ul>
          </div>
          <div className="qs-feature-media">
            <Shot width={1800} height={1351} src="/site/product-analytics.webp" alt="Quarvera's Analytics: material and hauling cost over time and by project for a fictional contractor." />
          </div>
        </div>
      </section>

      <section id="how" className="qs-section qs-how" aria-labelledby="how-title">
        <div className="qs-wrap">
          <header className="qs-section-head">
            <p className="qs-eyebrow">How it works</p>
            <h2 id="how-title">From the scale house to accounts payable.</h2>
          </header>
          <ol className="qs-steps">
            {STEPS.map((s) => <li key={s.n}><span className="qs-step-n">{s.n}</span><h3>{s.title}</h3><p>{s.text}</p></li>)}
          </ol>
        </div>
      </section>

      <section className="qs-controls" aria-labelledby="controls-title">
        <div className="qs-wrap">
          <header className="qs-section-head qs-section-head-dark">
            <p className="qs-eyebrow qs-eyebrow-dark">Built for money</p>
            <h2 id="controls-title">Numbers your controller can stand behind.</h2>
            <p>Quarvera is designed around one rule: a figure only counts once a person has checked it against the source, and once it counts it doesn&apos;t quietly change.</p>
          </header>
          <div className="qs-controls-grid">
            {CONTROLS.map((c) => <div key={c.title}><h3>{c.title}</h3><p>{c.text}</p></div>)}
          </div>
        </div>
      </section>

      <section className="qs-section qs-teams" aria-labelledby="teams-title">
        <div className="qs-wrap qs-teams-row">
          <div>
            <p className="qs-eyebrow">For the whole team</p>
            <h2 id="teams-title">The field keeps working the way it does. The office stops retyping.</h2>
            <dl className="qs-roles">
              <div><dt>Foremen and field staff</dt><dd>Snap a photo of a ticket on a phone, or keep turning in paper as you do today.</dd></div>
              <div><dt>Project engineers and accounts payable</dt><dd>Verify a stack of tickets from the keyboard, with each scan right beside its values.</dd></div>
              <div><dt>Project managers</dt><dd>Delivered quantities and cost by job and material, current every day.</dd></div>
              <div><dt>Controllers and owners</dt><dd>Every approval carries a name, prices are locked, and every invoice is checked.</dd></div>
            </dl>
          </div>
          <figure className="qs-phone">
            <img src="/site/product-phone.webp" width={780} height={1533} loading="lazy" decoding="async"
              alt="Quarvera on a phone: a scanned delivery ticket ready for review." />
            <figcaption className="qs-caption">Reviewing a ticket on a phone (fictional demo).</figcaption>
          </figure>
        </div>
      </section>

      <section id="founder" className="qs-founder" aria-labelledby="founder-title">
        <div className="qs-wrap qs-founder-row">
          <div className="qs-founder-mark" aria-hidden="true">“</div>
          <div>
            <p className="qs-eyebrow">Why we built Quarvera</p>
            <h2 id="founder-title">Built from three summers on construction sites.</h2>
            <div className="qs-founder-text">
              <p>I spent three summers working in construction with Walsh. A lot of what I saw came down to paperwork: delivery tickets collected from trucks, keyed into spreadsheets, and checked against supplier and trucking invoices long after the loads were placed.</p>
              <p>It was careful, necessary work, and almost all of it was manual. Every ticket was a small piece of a large bill, and the only way to know whether an invoice was right was to go back through the paper.</p>
              <p>Quarvera is the tool I wanted on those jobs: capture every delivery however it arrives, have the right person confirm it once, and let the software do the comparing. Every delivery accounted for.</p>
            </div>
            <p className="qs-founder-sign"><strong>Chase Koester</strong><span>Founder, Quarvera</span></p>
            <p className="qs-founder-note">Walsh is named only to describe the founder&apos;s work experience. Walsh is not affiliated with Quarvera and does not endorse it.</p>
          </div>
        </div>
      </section>

      <section className="qs-cta" aria-labelledby="cta-title">
        <div className="qs-wrap qs-cta-row">
          <div>
            <h2 id="cta-title">See Quarvera with your own tickets.</h2>
            <p>We&apos;re working with a small number of contractors to start. Tell us what you buy and how you&apos;re billed, and we&apos;ll show you the workflow on a real stack of tickets and a real invoice.</p>
          </div>
          <div className="qs-cta-actions">
            <Link href="/demo" className="qs-btn qs-btn-primary qs-btn-lg">Request a demo</Link>
            <Link href="/login" className="qs-cta-signin">Already a customer? Sign in</Link>
          </div>
        </div>
      </section>
    </main>
    <SiteFooter />
  </div>;
}
