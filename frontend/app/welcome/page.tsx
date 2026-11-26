import Link from "next/link";
import type { ReactNode } from "react";
import { MARKETING, SiteFooter, SiteNav } from "../site-ui";

export const metadata = {
  title: "Quarvera · Every delivery accounted for.",
  description: "Material and hauling cost control for contractors. Quarvera reads your delivery tickets, time sheets and invoices, has your team verify them, prices them against your agreements, and shows you what to question before you pay.",
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

const SOURCES = ["Paper tickets", "Scanned batches", "Phone photos", "Digital PDFs", "Supplier spreadsheets", "Truck time sheets", "Supplier and hauler invoices"];

const ICON: Record<string, ReactNode> = {
  intake: <path d="M12 15V4m0 0L7.5 8.5M12 4l4.5 4.5M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" />,
  review: <><rect x="3" y="4" width="8" height="16" rx="1.5" /><path d="M14 7h7M14 12h7M14 17h4" /></>,
  price: <><path d="M3 12V4h8l10 10-8 8L3 12Z" /><circle cx="7.5" cy="8" r="1.5" /></>,
  truck: <><path d="M3 6h11v10H3zM14 10h4l3 3v3h-7" /><circle cx="7" cy="17.5" r="2" /><circle cx="17" cy="17.5" r="2" /></>,
  invoice: <><path d="M6 3h9l4 4v14H6z" /><path d="M15 3v4h4M9 12h7M9 16h5" /></>,
  chart: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>,
};

const CAPABILITIES: { icon: string; title: string; text: string; href: string }[] = [
  { icon: "intake", title: "AI document intake", text: "Scans, photos, PDFs and spreadsheets, read automatically.", href: "#intake" },
  { icon: "review", title: "Fast verification", text: "Each ticket beside its scan. One keystroke when it's right.", href: "#intake" },
  { icon: "price", title: "Pricing and contracts", text: "Project agreements and dated rates, locked at approval.", href: "#costs" },
  { icon: "truck", title: "Trucking", text: "Hourly, per ton or per load. Each load paid for once.", href: "#trucking" },
  { icon: "invoice", title: "Invoice reconciliation", text: "Every supplier and hauler line checked against what arrived.", href: "#reconcile" },
  { icon: "chart", title: "Project costs", text: "Material and hauling cost by job, current every day.", href: "#costs" },
];

const FEATURES: { id: string; eyebrow: string; title: string; text: string; points: string[]; src: string; alt: string }[] = [
  {
    id: "intake", eyebrow: "AI-powered intake", title: "Check tickets instead of typing them.",
    text: "Upload a stack and Quarvera finds each ticket, reads it, and puts it beside the original for a quick check.",
    points: ["Several tickets to a page, photos and digital PDFs", "Supplier spreadsheets imported row by row", "Duplicates caught before they count"],
    src: "/site/product-review.webp", alt: "Quarvera's review screen: the original scanned ticket beside the values read from it, with an Accept & next button.",
  },
  {
    id: "reconcile", eyebrow: "Invoice reconciliation", title: "Know what to question before you pay.",
    text: "Every invoice line is compared with approved deliveries at your agreed prices, and each difference is shown in dollars.",
    points: ["Wrong tons, wrong rates, bad math", "Tickets never received or billed twice", "Deliveries nobody has billed yet"],
    src: "/site/product-exceptions.webp", alt: "Quarvera's invoice exceptions: open items with their dollar effect, such as a load billed again or a ticket with no delivery record.",
  },
  {
    id: "trucking", eyebrow: "Trucking", title: "Every hauling bill, checked against the work.",
    text: "Hourly shifts with minimums and standby, or hauling per ton and per load. Hauler invoices are reconciled line by line.",
    points: ["Shifts split across jobs", "No load paid for twice", "Extra hours or tons shown in dollars"],
    src: "/site/product-hauling.webp", alt: "Quarvera's hauling invoices: billed against supported amounts, with a per-ton line that differs by $12.75 from the approved deliveries.",
  },
  {
    id: "costs", eyebrow: "Project costs", title: "Real material and hauling cost, by job, any day.",
    text: "Approved records are priced as they come in, so cost by project, material and supplier is always current.",
    points: ["Approved figures only", "Missing rates called out, never $0", "Excel exports that match to the cent"],
    src: "/site/product-analytics.webp", alt: "Quarvera's Analytics: material and hauling cost over time and by project for a fictional contractor.",
  },
];

const STEPS = [
  { n: "01", title: "Capture", text: "Paper, PDF, photo or spreadsheet." },
  { n: "02", title: "Verify", text: "Your team confirms each record against its source." },
  { n: "03", title: "Price", text: "Priced from your agreements and locked on approval." },
  { n: "04", title: "Reconcile", text: "Invoices checked before you pay." },
];

const CONTROLS = [
  { title: "People approve, AI assists", text: "Nothing reaches a total until someone on your team approves it." },
  { title: "Locked prices and full history", text: "Approved figures never change quietly, and every edit has a name on it." },
  { title: "Missing is never zero", text: "Unpriced deliveries are flagged, so totals never look better than they are." },
  { title: "Your data stays yours", text: "Each company's records are isolated from every other company's." },
];

export default function WelcomePage() {
  return <div className="qs">
    <SiteNav />
    <main>
      <section className="qs-hero">
        <div className="qs-wrap">
          <p className="qs-eyebrow">Material and hauling cost control for contractors</p>
          <h1>{"Every delivery "}<br />accounted for.</h1>
          <p className="qs-hero-lead">Quarvera captures every delivery ticket, time sheet and invoice, has your team verify it, and shows you exactly what to question before you pay.</p>
          <div className="qs-hero-actions">
            <Link href="/demo" className="qs-btn qs-btn-primary">Request a demo</Link>
            <a href="#platform" className="qs-btn qs-btn-quiet">See the platform</a>
          </div>
          <ul className="qs-hero-points">
            <li><Check />AI-powered document processing</li>
            <li><Check />Verified by your team</li>
            <li><Check />Every invoice checked</li>
          </ul>
        </div>
        <div className="qs-wrap qs-hero-shot">
          <Shot src="/site/product-overview.webp" priority
            alt="Quarvera's Overview: verified material tons, material and hauling cost, spending by week and cost by project, for a fictional contractor." />
        </div>
      </section>

      <section className="qs-sources" aria-label="What Quarvera takes in">
        <div className="qs-wrap">
          <p>Works with what you already receive</p>
          <ul>{SOURCES.map((s) => <li key={s}>{s}</li>)}</ul>
        </div>
      </section>

      <section id="platform" className="qs-section" aria-labelledby="platform-title">
        <div className="qs-wrap">
          <header className="qs-section-head">
            <p className="qs-eyebrow">The platform</p>
            <h2 id="platform-title">Every delivery, every truck, every invoice. One place.</h2>
          </header>
          <div className="qs-cap-grid">
            {CAPABILITIES.map((c) => <a key={c.title} href={c.href} className="qs-cap">
              <span className="qs-cap-icon"><svg aria-hidden="true" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{ICON[c.icon]}</svg></span>
              <h3>{c.title}</h3>
              <p>{c.text}</p>
            </a>)}
          </div>
        </div>
      </section>

      {FEATURES.map((f, i) => <section key={f.id} id={f.id} className={`qs-feature${i % 2 ? " qs-feature-alt" : ""}`} aria-labelledby={`${f.id}-title`}>
        <div className={`qs-wrap qs-feature-row${i % 2 ? " qs-feature-flip" : ""}`}>
          <div className="qs-feature-copy">
            <p className="qs-eyebrow">{f.eyebrow}</p>
            <h2 id={`${f.id}-title`}>{f.title}</h2>
            <p>{f.text}</p>
            <ul className="qs-list">{f.points.map((p) => <li key={p}><Check /><span>{p}</span></li>)}</ul>
          </div>
          <div className="qs-feature-media">
            <Shot width={1800} height={1351} src={f.src} alt={f.alt} />
          </div>
        </div>
      </section>)}

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
        <div className="qs-wrap qs-controls-row">
          <div className="qs-controls-copy">
            <p className="qs-eyebrow qs-eyebrow-dark">Built for money</p>
            <h2 id="controls-title">Numbers your controller can stand behind.</h2>
          </div>
          <div className="qs-controls-grid">
            {CONTROLS.map((c) => <div key={c.title}><h3>{c.title}</h3><p>{c.text}</p></div>)}
          </div>
        </div>
      </section>

      <section id="mission" className="qs-section qs-mission" aria-labelledby="mission-title">
        <div className="qs-wrap qs-mission-row">
          <div>
            <p className="qs-eyebrow">Our mission</p>
            <h2 id="mission-title">Give every contractor a complete, verified record of what they buy and haul.</h2>
            <p className="qs-mission-text">Material and trucking make up a large share of every job&apos;s cost, and they&apos;re still tracked on paper. Quarvera exists so the field keeps working the way it does, the office stops retyping, and no invoice gets paid without being checked.</p>
          </div>
          <figure className="qs-phone">
            <img src="/site/product-phone.webp" width={780} height={1533} loading="lazy" decoding="async"
              alt="Quarvera on a phone: a scanned delivery ticket ready for review." />
          </figure>
        </div>
      </section>

      <section className="qs-cta" aria-labelledby="cta-title">
        <div className="qs-wrap qs-cta-row">
          <div>
            <h2 id="cta-title">See it with your own tickets.</h2>
            <p>A 30-minute walkthrough built around how your company buys material and pays for trucking.</p>
          </div>
          <div className="qs-cta-actions">
            <Link href="/demo" className="qs-btn qs-btn-primary qs-btn-lg">Request a demo</Link>
            {!MARKETING && <Link href="/login" className="qs-cta-signin">Already a customer? Sign in</Link>}
          </div>
        </div>
      </section>
    </main>
    <SiteFooter />
  </div>;
}
