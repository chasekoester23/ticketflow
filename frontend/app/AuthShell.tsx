"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { ApiError, getAuthMe, getReviewQueue, logOut, type Account, type Permission } from "../lib/api";
import { mayNavigate } from "../lib/navigation-guard";
import { beginSignOut, clearAllDrafts, clearOtherAccountsDrafts, hasDrafts } from "../lib/drafts";
import { AccountContext } from "./account-context";
import { ProjectProvider } from "./project-context";
import ProjectPicker from "./ProjectPicker";
import ProjectBar from "./ProjectBar";

type IconName = "overview" | "projects" | "tickets" | "records" | "rates" | "invoices" | "team" | "menu" | "close" | "arrow" | "chevron" | "logout" | "layers" | "report" | "health" | "truck" | "settings" | "upload" | "building";

function Icon({ name, size = 19 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, ReactNode> = {
    overview: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
    projects: <><rect x="3" y="5" width="18" height="16" rx="1.5" /><path d="M8 5V3h8v2M3 11h18M8 16h3" /></>,
    tickets: <><path d="M5 3h11l3 3v15H5zM15 3v4h4M8 11h8M8 15h8M8 19h5" /></>,
    records: <><path d="M4 4h16v16H4zM4 10h16M9 10v10M12 6h5M12 14h5M12 17h4" /></>,
    rates: <><rect x="3" y="6" width="18" height="15" rx="1.5" /><path d="M7 6V3h10v3M3 12h18M7 16h4" /></>,
    invoices: <><path d="M5 3h14v18l-3-2-4 2-4-2-3 2zM8 8h8M8 12h8M8 16h5" /></>,
    team: <><circle cx="9" cy="8" r="3.2" /><path d="M3.5 20c.6-3.4 2.8-5.3 5.5-5.3s4.9 1.9 5.5 5.3M15.5 5.2a3 3 0 0 1 0 5.6M17.5 14.9c1.6.6 2.7 2.3 3 5.1" /></>,
    menu: <path d="M4 7h16M4 12h16M4 17h16" />,
    close: <path d="M5 5l14 14M19 5L5 19" />,
    arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
    chevron: <path d="m9 6 6 6-6 6" />,
    logout: <><path d="M10 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h5M14 8l4 4-4 4m-9-4h13" /></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5" /></>,
    report: <><path d="M4 20V4M4 20h16M8 16v-5M12 16V7M16 16v-3" /></>,
    health: <path d="M3 12h4l2.5-6 4 12 2.5-6h5" />,
    truck: <><path d="M3 6h11v10H3zM14 9h4l3 3v4h-7" /><circle cx="7" cy="18" r="2" /><circle cx="17" cy="18" r="2" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" /></>,
    upload: <path d="M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4" />,
    building: <><path d="M4 21V5l8-3v19M12 9h8v12M4 21h17M7 8h2M7 12h2M7 16h2M15 13h2M15 17h2" /></>,
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

type Tab = { href: string; label: string; requires?: Permission };
type Section = { href: string; label: string; icon: IconName; match: string[]; tabs: Tab[]; badge?: "review"; requires?: Permission };
// Every daily workflow has its own place. Tabs only join views of the same thing.
// Old addresses keep working: each belongs to the section that now holds it.
const groups: { label: string; items: Section[] }[] = [
  { label: "Workspace", items: [
    { href: "/", label: "Overview", icon: "overview", match: ["/"], tabs: [] },
    { href: "/projects", label: "Projects", icon: "projects", match: ["/projects", "/reports/project"], tabs: [] },
  ] },
  { label: "Operations", items: [
    // What came in and whether it was read; then what a person still has to check.
    { href: "/upload", label: "Document Intake", icon: "upload", match: ["/upload"], tabs: [] },
    { href: "/review", label: "Review Queue", icon: "tickets", match: ["/review"], badge: "review", tabs: [] },
    { href: "/records", label: "Delivery Records", icon: "records", match: ["/records"], tabs: [] },
    { href: "/trucking", label: "Trucking", icon: "truck", match: ["/trucking"], tabs: [] },
  ] },
  { label: "Financials", items: [
    { href: "/invoices", label: "Invoices", icon: "invoices", match: ["/invoices"], tabs: [
      { href: "/invoices", label: "All invoices" }, { href: "/invoices/exceptions", label: "Exceptions" },
      { href: "/invoices/unbilled", label: "Unbilled activity" }] },
    { href: "/analytics", label: "Analytics", icon: "report", match: ["/analytics", "/reports"], tabs: [] },
  ] },
  { label: "Management", items: [
    { href: "/suppliers", label: "Suppliers & Haulers", icon: "building", match: ["/suppliers", "/records/names"], tabs: [
      { href: "/suppliers", label: "Directory" }, { href: "/records/names", label: "Name cleanup", requires: "tickets.edit" }] },
    { href: "/pricing", label: "Rates & Pricing", icon: "rates", match: ["/pricing"], tabs: [] },
  ] },
];
const settingsTabs: Tab[] = [
  { href: "/team", label: "Team", requires: "team.manage" }, { href: "/system", label: "System health", requires: "system.read" },
  { href: "/reports/pilot", label: "Pilot measurement", requires: "system.read" }, { href: "/account", label: "Account" }];
const settings: Section = { href: "/account", label: "Settings", icon: "settings", match: ["/team", "/system", "/account", "/reports/pilot"], tabs: settingsTabs };
const allSections = [...groups.flatMap((g) => g.items), settings];
// The longest matching address wins, so /records/names is Name cleanup, not Delivery Records.
function sectionOf(path: string): Section | null {
  let best: Section | null = null;
  let length = -1;
  for (const s of allSections) for (const m of s.match) {
    const hit = m === "/" ? path === "/" : path === m || path.startsWith(m + "/");
    if (hit && m.length > length) { best = s; length = m.length; }
  }
  return best;
}
const titleOf = (path: string) => allSections.flatMap((s) => s.tabs).find((t) => t.href === path)?.label ?? sectionOf(path)?.label;
// One place to start any document: where each kind goes, and the page opens ready for it.
const uploads: { href: string; label: string; hint: string; requires: Permission }[] = [
  { href: "/upload?new=1", label: "Delivery tickets", hint: "Scans or phone photos, read automatically", requires: "tickets.upload" },
  { href: "/trucking?new=1", label: "Trucking time sheet", hint: "Enter the shift, attach the sheet", requires: "trucking.edit" },
  { href: "/invoices/documents", label: "Supplier invoice", hint: "PDF, checked against deliveries", requires: "invoices.manage" },
  { href: "/trucking?tab=invoices&new=1", label: "Trucking invoice", hint: "Checked against approved shifts", requires: "invoices.manage" },
];
const PUBLIC_PATHS = ["/welcome", "/login", "/signup", "/demo"];
// Pages change without reloading, so the session is re-checked in the background, at most this often.
const SESSION_RECHECK_MS = 30_000;

function onInternalLink(event: MouseEvent<HTMLAnchorElement>, href: string) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  // A page reads its address only when it opens. To clear filters or leave a batch on the
  // same page, it has to open again. A real page load also brings the browser's own warning.
  if (window.location.pathname === href && window.location.search) {
    event.preventDefault();
    window.location.assign(href);
    return;
  }
  if (!mayNavigate()) event.preventDefault();
}

export default function AuthShell({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null);
  const [checking, setChecking] = useState(true);
  const [menuOpen, setMenuOpen] = useState(false);
  const sessionCheckedAt = useRef(0);
  const shownBefore = useRef(false);
  const pathname = usePathname();
  // The marketing website has no application: every page it serves is public. The browser
  // still shows "/" after middleware.ts rewrites it to /welcome, so the path alone can't say.
  const isPublic = process.env.NEXT_PUBLIC_SITE_MODE === "marketing" || PUBLIC_PATHS.includes(pathname);
  const section = sectionOf(pathname);
  const [pending, setPending] = useState<{ review: number } | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const can = (p?: Permission) => !p || Boolean(account?.permissions?.includes(p));
  const navLink = ({ href, label, icon, badge }: Section) => {
    const active = section?.label === label;
    const count = badge && pending ? pending[badge] : 0;
    return <Link key={href} href={href} aria-current={active ? "page" : undefined}
      className={`tf-nav-link ${active ? "tf-nav-link-active" : ""}`}
      onClick={(event) => { setMenuOpen(false); onInternalLink(event, href); }}>
      <Icon name={icon} /><span>{label}</span>
      {count > 0 && <span className="tf-nav-badge" aria-label={`${count} to review`}>{count > 999 ? "999+" : count}</span>}
      {active && <span className="tf-nav-active-rule" />}
    </Link>;
  };
  const personName = account?.display_name || account?.username || "";
  const initial = (personName[0] || "U").toUpperCase();
  const mustChangePassword = Boolean(account?.must_change_password);

  useEffect(() => {
    // The marketing website has no application behind it to ask (middleware.ts).
    if (process.env.NEXT_PUBLIC_SITE_MODE === "marketing") return;
    let cancelled = false;
    getAuthMe().then((me) => {
      if (cancelled) return;
      // Someone already signed in has no use for the public pages.
      if (isPublic) {
        window.location.replace(me.must_change_password ? "/account" : "/");
        return;
      }
      // A temporary password issued by an owner must be replaced first.
      if (me.must_change_password && window.location.pathname !== "/account") {
        window.location.replace("/account");
        return;
      }
      clearOtherAccountsDrafts(me.id);
      sessionCheckedAt.current = Date.now();
      setAccount(me);
      setChecking(false);
    }).catch(() => {
      // A first visit gets the welcome page. A deep link usually means an expired session.
      if (!cancelled && !isPublic) window.location.replace(window.location.pathname === "/" ? "/welcome" : "/login");
    });
    return () => { cancelled = true; };
  }, [isPublic]);

  useEffect(() => { setMenuOpen(false); setUploadOpen(false); }, [pathname]);

  // What's waiting for a person, on the sidebar. Refreshed as people move around, at most once a minute.
  const pendingAt = useRef(0);
  useEffect(() => {
    if (isPublic || checking || Date.now() - pendingAt.current < 60_000) return;
    pendingAt.current = Date.now();
    // The Review Queue's own total, so the badge and the queue never disagree.
    getReviewQueue().then((q) => setPending({ review: q.summary.total }))
      .catch(() => setPending(null));
  }, [pathname, isPublic, checking]);

  // The shell stays up between pages, so nothing waits on this. Each API call is still
  // checked by the server; this catches an ended session or a changed role.
  useEffect(() => {
    if (isPublic || checking || Date.now() - sessionCheckedAt.current < SESSION_RECHECK_MS) return;
    sessionCheckedAt.current = Date.now();
    getAuthMe().then((me) => {
      if (me.must_change_password && window.location.pathname !== "/account") window.location.replace("/account");
      else if (account && me.id !== account.id) window.location.replace("/");
      else setAccount((old) => JSON.stringify(old) === JSON.stringify(me) ? old : me);
    }).catch((failure) => {
      if (failure instanceof ApiError && failure.status === 401) window.location.replace("/login");
    });
  }, [pathname, isPublic, checking, account]);

  // A reload used to retitle the tab and put a screen reader at the top of the new page.
  useEffect(() => {
    if (isPublic || checking) return;
    const label = titleOf(pathname);
    document.title = label ? `${label} · Quarvera` : "Quarvera";
    if (!shownBefore.current) {
      shownBefore.current = true;
      return;
    }
    if (!document.activeElement?.closest("input, textarea, select")) document.getElementById("main-content")?.focus({ preventScroll: true });
  }, [pathname, isPublic, checking]);

  useEffect(() => {
    if (!menuOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenuOpen(false); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [menuOpen]);

  // A table wider than a phone scrolls sideways inside its box. A keyboard can
  // only scroll what it can reach, so a box that scrolls becomes a tab stop.
  useEffect(() => {
    if (checking) return;
    let frame = 0;
    const mark = () => {
      frame = 0;
      document.querySelectorAll<HTMLElement>(".tf-table-wrap").forEach((wrap) => {
        const scrolls = wrap.scrollWidth > wrap.clientWidth + 1;
        if (scrolls && !wrap.hasAttribute("tabindex")) {
          const name = wrap.querySelector("table")?.getAttribute("aria-label")
            ?? wrap.closest("section")?.querySelector("h2, h3")?.textContent ?? "Table";
          wrap.tabIndex = 0;
          wrap.setAttribute("role", "region");
          wrap.setAttribute("aria-label", `${name}, scrolls sideways`);
          wrap.dataset.tfScrolls = "1";
        } else if (!scrolls && wrap.dataset.tfScrolls) {
          wrap.removeAttribute("tabindex");
          wrap.removeAttribute("role");
          wrap.removeAttribute("aria-label");
          delete wrap.dataset.tfScrolls;
        }
      });
    };
    const soon = () => { if (!frame) frame = requestAnimationFrame(mark); };
    const main = document.getElementById("main-content");
    const observer = new MutationObserver(soon);
    if (main) observer.observe(main, { childList: true, subtree: true });
    window.addEventListener("resize", soon);
    soon();
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", soon);
      cancelAnimationFrame(frame);
    };
  }, [checking, pathname]);

  if (isPublic) return <>{children}</>;
  if (checking || !account) {
    return <main className="flex min-h-screen items-center justify-center bg-[#f4f5f6] text-sm text-slate-600" role="status">
      Checking your Quarvera session…
    </main>;
  }

  return <AccountContext.Provider value={account}><ProjectProvider accountId={account.id}><div className="tf-app-shell">
    <a href="#main-content" className="tf-skip-link">Skip to main content</a>
    {menuOpen && <button aria-label="Close navigation" className="tf-mobile-scrim" onClick={() => setMenuOpen(false)} />}
    <aside id="main-menu" className={`tf-sidebar ${menuOpen ? "tf-sidebar-open" : ""}`}>
      <button type="button" aria-label="Close menu" className="tf-sidebar-close" onClick={() => setMenuOpen(false)}><Icon name="close" size={18} /></button>
      <Link href="/" className="tf-sidebar-brand" aria-label="Quarvera home" onClick={(event) => { setMenuOpen(false); onInternalLink(event, "/"); }}>
        {/* The approved artwork, unaltered. */}
        <img src="/brand/quarvera-horizontal-640.png" alt="Quarvera" width={640} height={222} className="tf-brand-logo" />
      </Link>
      <p className="tf-sidebar-workspace" title={account.organization_name ?? "Workspace"}>{account.organization_name ?? "Workspace"}</p>
      <nav aria-label="Main navigation" className="flex flex-1 flex-col">
        {!mustChangePassword && <>
          {groups.map((group) => <div key={group.label} className="tf-nav" role="group" aria-labelledby={`nav-${group.label}`}>
            <p id={`nav-${group.label}`} className="tf-nav-group">{group.label}</p>
            {group.items.filter((item) => can(item.requires)).map(navLink)}
          </div>)}
          <div className="tf-nav tf-nav-settings">{navLink({ ...settings, href: settingsTabs.find((t) => can(t.requires))?.href ?? "/account" })}</div>
        </>}
      </nav>
      <Link href="/account" className="tf-sidebar-account" onClick={(event) => { setMenuOpen(false); onInternalLink(event, "/account"); }}>
        <span className="tf-account-avatar" aria-hidden="true">{initial}</span>
        <span>{personName}<small>{account.role_label} · Your account</small></span>
      </Link>
    </aside>

    <div className="tf-shell-content">
      <header className="tf-topbar">
        <div className="flex min-w-0 items-center gap-3">
          <button type="button" aria-label="Open navigation" aria-expanded={menuOpen} aria-controls="main-menu" className="tf-menu-toggle"
            onClick={() => setMenuOpen(true)}><Icon name="menu" /></button>
          {!mustChangePassword && <ProjectPicker canCreate={can("projects.manage")} />}
        </div>
        {/* In the top bar, so the section's pages cost no height: review keeps its whole screen. */}
        {section && section.tabs.filter((t) => can(t.requires)).length > 1 && <nav aria-label="In this section" className="tf-section-tabs tf-header-tabs">
          {section.tabs.filter((t) => can(t.requires)).map((t) => <Link key={t.href} href={t.href} prefetch={false}
            aria-current={pathname === t.href ? "page" : undefined} className={pathname === t.href ? "tf-section-tab-active" : ""}
            onClick={(event) => onInternalLink(event, t.href)}>{t.label}</Link>)}
        </nav>}
        <div className="tf-topbar-right">
          {!mustChangePassword && uploads.some((u) => can(u.requires)) && <div className="relative">
            <button type="button" className="tf-primary-button" aria-label="Upload documents" aria-haspopup="menu" aria-expanded={uploadOpen} onClick={() => setUploadOpen((v) => !v)}>
              <Icon name="upload" size={16} /> <span className="tf-upload-label">Upload documents</span></button>
            {uploadOpen && <div role="menu" aria-label="Upload documents" className="tf-upload-menu">
              {uploads.filter((u) => can(u.requires)).map((u) => <Link key={u.href} role="menuitem" href={u.href} prefetch={false}
                onClick={(event) => { setUploadOpen(false); onInternalLink(event, u.href.split("?")[0]); }}>
                <strong>{u.label}</strong><span>{u.hint}</span></Link>)}
            </div>}
          </div>}
          <Link href="/account" className="tf-account-link" title="Account settings" onClick={(event) => onInternalLink(event, "/account")}>
            <span className="tf-account-avatar" aria-hidden="true">{initial}</span>
            <span className="tf-account-name">{personName}<small>{account.role_label}</small></span>
          </Link>
          <button type="button" className="tf-sign-out" aria-label="Sign out" title="Sign out"
            onClick={async () => {
              // Typed values kept for crash recovery mustn't outlive a sign-out on a shared computer.
              if (hasDrafts() && !window.confirm("Signing out clears unsaved ticket work kept on this computer. Sign out anyway?")) return;
              beginSignOut();
              try { await logOut(); } finally { clearAllDrafts(); window.location.replace("/login"); }
            }}><Icon name="logout" size={18} /><span>Sign out</span></button>
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="tf-main focus:outline-none">{!pathname.startsWith("/projects/") && <ProjectBar />}{children}</main>
    </div>
  </div></ProjectProvider></AccountContext.Provider>;
}
