"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { getAuthMe, logOut } from "../lib/api";

type Owner = { id: number; username: string; organization_name?: string | null };
type IconName = "overview" | "projects" | "tickets" | "records" | "rates" | "invoices" | "menu" | "close" | "arrow" | "chevron" | "logout" | "layers";

function Icon({ name, size = 19 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, ReactNode> = {
    overview: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
    projects: <><rect x="3" y="5" width="18" height="16" rx="1.5" /><path d="M8 5V3h8v2M3 11h18M8 16h3" /></>,
    tickets: <><path d="M5 3h11l3 3v15H5zM15 3v4h4M8 11h8M8 15h8M8 19h5" /></>,
    records: <><path d="M4 4h16v16H4zM4 10h16M9 10v10M12 6h5M12 14h5M12 17h4" /></>,
    rates: <><rect x="3" y="6" width="18" height="15" rx="1.5" /><path d="M7 6V3h10v3M3 12h18M7 16h4" /></>,
    menu: <path d="M4 7h16M4 12h16M4 17h16" />,
    close: <path d="M5 5l14 14M19 5L5 19" />,
    arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
    chevron: <path d="m9 6 6 6-6 6" />,
    logout: <><path d="M10 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h5M14 8l4 4-4 4m-9-4h13" /></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5" /></>,
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

const links: { href: string; label: string; icon: IconName; description?: string }[] = [
  { href: "/", label: "Overview", icon: "overview" },
  { href: "/projects", label: "Projects", icon: "projects" },
  { href: "/upload", label: "Ticket intake", icon: "tickets" },
  { href: "/records", label: "Delivery records", icon: "records" },
  { href: "/pricing", label: "Suppliers & rates", icon: "rates" },
];

export default function AuthShell({ children }: { children: ReactNode }) {
  const [owner, setOwner] = useState<Owner | null>(null);
  const [checking, setChecking] = useState(true);
  const [menuOpen, setMenuOpen] = useState(false);
  const pathname = usePathname();
  const isLogin = pathname === "/login";
  const current = links.find((l) => l.href === pathname);
  const initial = (owner?.username?.[0] || "U").toUpperCase();

  useEffect(() => {
    if (isLogin) {
      setChecking(false);
      return;
    }
    let cancelled = false;
    getAuthMe().then((account) => {
      if (!cancelled) {
        setOwner(account);
        setChecking(false);
      }
    }).catch(() => {
      if (!cancelled) window.location.replace("/login");
    });
    return () => { cancelled = true; };
  }, [isLogin]);

  useEffect(() => { setMenuOpen(false); }, [pathname]);

  if (isLogin) return <main className="tf-login-layout">{children}</main>;
  if (checking || !owner) {
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
      <p className="tf-sidebar-section">WORKSPACE</p>
      <nav aria-label="Main navigation" className="tf-nav">
        {links.map(({ href, label, icon }) => {
          const active = pathname === href;
          return <a key={href} href={href} aria-current={active ? "page" : undefined}
            className={`tf-nav-link ${active ? "tf-nav-link-active" : ""}`}
            onClick={() => setMenuOpen(false)}>
            <Icon name={icon} /><span>{label}</span>
            {active && <span className="tf-nav-active-rule" />}
          </a>;
        })}
      </nav>
      <div className="tf-sidebar-foot">
        <span className="tf-sidebar-foot-label">TICKETFLOW WORKSPACE</span>
        <span className="tf-sidebar-foot-text">Delivery documentation<br />& material tracking</span>
      </div>
    </aside>

    <div className="tf-shell-content">
      <header className="tf-topbar">
        <div className="flex min-w-0 items-center gap-3">
          <button type="button" aria-label="Open navigation" className="tf-menu-toggle"
            onClick={() => setMenuOpen(true)}><Icon name="menu" /></button>
          <div className="tf-topbar-context">
            <span className="tf-topbar-parent">Workspace</span>
            <Icon name="chevron" size={14} />
            <strong>{current?.label ?? "TicketFlow"}</strong>
          </div>
        </div>
        <div className="tf-topbar-right">
          <span className="tf-organization-name" title={owner.organization_name ?? "Workspace"}>
            {owner.organization_name ?? "Workspace"}
          </span>
          <span className="tf-account-avatar" aria-hidden="true">{initial}</span>
          <span className="tf-account-name">{owner.username}</span>
          <button type="button" className="tf-sign-out" aria-label="Sign out" title="Sign out"
            onClick={async () => {
              try { await logOut(); } finally { window.location.replace("/login"); }
            }}><Icon name="logout" size={18} /><span>Sign out</span></button>
        </div>
      </header>
      <main id="main-content" className="tf-main">{children}</main>
    </div>
  </div>;
}
