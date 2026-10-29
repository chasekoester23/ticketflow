"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { getAuthMe, logOut } from "../lib/api";

type Owner = { id: number; username: string };

export default function AuthShell({ children }: { children: ReactNode }) {
  const [owner, setOwner] = useState<Owner | null>(null);
  const [checking, setChecking] = useState(true);
  const pathname = usePathname();
  const isLogin = pathname === "/login";

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

  if (isLogin) return <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>;
  if (checking || !owner) {
    return <main className="mx-auto max-w-7xl px-6 py-16 text-sm text-slate-600" role="status">
      Checking your TicketFlow session…
    </main>;
  }

  return <>
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-6 py-3">
        <span className="text-lg font-semibold tracking-tight">TicketFlow</span>
        <span className="mr-5 text-sm text-slate-500">Material ticket processing</span>
        <nav aria-label="Main navigation" className="flex flex-wrap gap-4 text-sm">
          <a href="/" className="text-slate-700 hover:underline">Dashboard</a>
          <a href="/projects" className="text-slate-700 hover:underline">Projects</a>
          <a href="/upload" className="text-slate-700 hover:underline">Upload &amp; Review</a>
          <a href="/records" className="text-slate-700 hover:underline">Records</a>
          <a href="/pricing" className="text-slate-700 hover:underline">Suppliers &amp; Pricing</a>
        </nav>
        <div className="ml-auto flex items-center gap-3 text-xs text-slate-600">
          <span>{owner.username}</span>
          <button type="button" className="font-semibold underline hover:text-slate-900"
            onClick={async () => {
              try { await logOut(); } finally { window.location.replace("/login"); }
            }}>Sign out</button>
        </div>
      </div>
    </header>
    <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>
  </>;
}
