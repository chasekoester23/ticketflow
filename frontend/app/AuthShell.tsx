"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { getAuthMe, logOut, type Account, type Permission } from "../lib/api";
import { AccountContext } from "./account-context";

type IconName = "overview" | "projects" | "tickets" | "records" | "rates" | "invoices" | "team" | "menu" | "close" | "arrow" | "chevron" | "logout" | "layers" | "report" | "health";

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
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

type NavLink = { href: string; label: string; icon: IconName; requires?: Permission };
const workLinks: NavLink[] = [
  { href: "/", label: "Overview", icon: "overview" },
  { href: "/upload", label: "Ticket intake", icon: "tickets" },
  { href: "/records", label: "Delivery records", icon: "records" },
  { href: "/invoices", label: "Invoice comparison", icon: "invoices" },
  { href: "/reports/project", label: "Project report", icon: "report" },
];
const setupLinks: NavLink[] = [
  { href: "/projects", label: "Projects", icon: "projects" },
  { href: "/pricing", label: "Suppliers & rates", icon: "rates" },
  { href: "/reports", label: "Pilot report", icon: "report" },
  { href: "/team", label: "Team", icon: "team", requires: "team.manage" },
  { href: "/system", label: "System health", icon: "health", requires: "system.read" },
];
const links = [...workLinks, ...setupLinks];
const pageTitles: Record<string, string> = {
  "/account": "Account settings", "/records/names": "Name cleanup",
  "/invoices/documents": "Invoice PDFs",
};

export default function AuthShell({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null);
  const [checking, setChecking] = useState(true);
  const [menuOpen, setMenuOpen] = useState(false);
  const pathname = usePathname();
  const isLogin = pathname === "/login";
  const current = links.find((l) => l.href === pathname);
  const allowed = (l: NavLink) => !l.requires || account?.permissions?.includes(l.requires);
  const navLink = ({ href, label, icon }: NavLink) => {
    const active = pathname === href;
    return <a key={href} href={href} aria-current={active ? "page" : undefined}
      className={`tf-nav-link ${active ? "tf-nav-link-active" : ""}`}
      onClick={() => setMenuOpen(false)}>
      <Icon name={icon} /><span>{label}</span>
      {active && <span className="tf-nav-active-rule" />}
    </a>;
  };
  const personName = account?.display_name || account?.username || "";
  const initial = (personName[0] || "U").toUpperCase();
  const mustChangePassword = Boolean(account?.must_change_password);

  useEffect(() => {
    if (isLogin) {
      setChecking(false);
      return;
    }
    let cancelled = false;
    getAuthMe().then((me) => {
      if (cancelled) return;
      // A temporary password issued by an owner must be replaced first.
      if (me.must_change_password && window.location.pathname !== "/account") {
        window.location.replace("/account");
        return;
      }
      setAccount(me);
      setChecking(false);
    }).catch(() => {
      if (!cancelled) window.location.replace("/login");
    });
    return () => { cancelled = true; };
  }, [isLogin]);

  useEffect(() => { setMenuOpen(false); }, [pathname]);

  if (isLogin) return <main className="tf-login-layout">{children}</main>;
  if (checking || !account) {
    return <main className="flex min-h-screen items-center justify-center bg-[#f4f5f6] text-sm text-slate-600" role="status">
      Checking your TicketFlow session…
    </main>;
  }

  return <div className="tf-app-shell">
    {menuOpen && <button aria-label="Close navigation" className="tf-mobile-scrim" onClick={() => setMenuOpen(false)} />}
    <aside className={`tf-sidebar ${menuOpen ? "tf-sidebar-open" : ""}`}>
      <a href="/" className="tf-sidebar-brand" aria-label="TicketFlow home">
        <span className="tf-brand-mark"><Icon name="layers" size={21} /></span>
        <span className="tf-brand-type">TICKET<span>FLOW</span><small>FIELD OPERATIONS</small></span>
      </a>
      <div className="tf-sidebar-divider" />
      <nav aria-label="Main navigation">
        {!mustChangePassword && <>
          <p className="tf-sidebar-section">WORK</p>
          <div className="tf-nav">{workLinks.filter(allowed).map(navLink)}</div>
          <p className="tf-sidebar-section">SETUP</p>
          <div className="tf-nav">{setupLinks.filter(allowed).map(navLink)}</div>
        </>}
      </nav>
    </aside>

    <div className="tf-shell-content">
      <header className="tf-topbar">
        <div className="flex min-w-0 items-center gap-3">
          <button type="button" aria-label="Open navigation" className="tf-menu-toggle"
            onClick={() => setMenuOpen(true)}><Icon name="menu" /></button>
          <div className="tf-topbar-context">
            <span className="tf-topbar-parent">Workspace</span>
            <Icon name="chevron" size={14} />
            <strong>{current?.label ?? pageTitles[pathname] ?? "TicketFlow"}</strong>
          </div>
        </div>
        <div className="tf-topbar-right">
          <span className="tf-organization-name" title={account.organization_name ?? "Workspace"}>
            {account.organization_name ?? "Workspace"}
          </span>
          <a href="/account" className="tf-account-link" title="Account settings">
            <span className="tf-account-avatar" aria-hidden="true">{initial}</span>
            <span className="tf-account-name">{personName}<small>{account.role_label}</small></span>
          </a>
          <button type="button" className="tf-sign-out" aria-label="Sign out" title="Sign out"
            onClick={async () => {
              try { await logOut(); } finally { window.location.replace("/login"); }
            }}><Icon name="logout" size={18} /><span>Sign out</span></button>
        </div>
      </header>
      <AccountContext.Provider value={account}>
        <main id="main-content" className="tf-main">{children}</main>
      </AccountContext.Provider>
    </div>
  </div>;
}
