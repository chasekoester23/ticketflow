import Link from "next/link";
import { SiteMenu } from "./site-menu";

/** The approved logo, unaltered, on the light navbar it was drawn for. No plate behind it. */
export function SiteLogo({ className = "" }: { className?: string }) {
  return <Link href="/welcome" className={`qs-logo ${className}`} aria-label="Quarvera home">
    <img src="/brand/quarvera-horizontal-640.png" alt="Quarvera" width={640} height={222} />
  </Link>;
}

const LINKS = [
  { href: "/welcome#platform", label: "Platform" },
  { href: "/welcome#reconcile", label: "Invoices" },
  { href: "/welcome#trucking", label: "Trucking" },
  { href: "/welcome#how", label: "How it works" },
  { href: "/welcome#mission", label: "Mission" },
];

export function SiteNav() {
  return <header className="qs-nav">
    <div className="qs-wrap qs-nav-row">
      <SiteLogo />
      <nav aria-label="Site" className="qs-nav-links">
        {LINKS.map((l) => <a key={l.href} href={l.href}>{l.label}</a>)}
      </nav>
      <div className="qs-nav-actions">
        <Link href="/login" className="qs-nav-signin">Sign in</Link>
        <Link href="/demo" className="qs-btn qs-btn-primary qs-btn-sm">Request a demo</Link>
      </div>
      <SiteMenu links={LINKS} />
    </div>
  </header>;
}

export function SiteFooter() {
  return <footer className="qs-footer">
    <div className="qs-wrap">
      <div className="qs-footer-top">
        <div className="qs-footer-brand">
          <SiteLogo />
          <p>Material delivery and hauling cost control for contractors. Every delivery accounted for.</p>
        </div>
        <nav aria-label="Footer" className="qs-footer-nav">
          <div><h2>Platform</h2><ul>
            <li><a href="/welcome#intake">Document intake</a></li>
            <li><a href="/welcome#reconcile">Invoice reconciliation</a></li>
            <li><a href="/welcome#trucking">Trucking</a></li>
            <li><a href="/welcome#costs">Project costs</a></li>
          </ul></div>
          <div><h2>Company</h2><ul>
            <li><a href="/welcome#mission">Mission</a></li>
            <li><Link href="/demo">Request a demo</Link></li>
            <li><Link href="/login">Sign in</Link></li>
          </ul></div>
        </nav>
      </div>
      <div className="qs-footer-end">
        <span>© {new Date().getFullYear()} Quarvera</span>
        <span>Product screens show a fictional demo company. No customer data appears on this site.</span>
      </div>
    </div>
  </footer>;
}
