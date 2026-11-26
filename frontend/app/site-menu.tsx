"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

/** The phone menu: opens over the page, closes when a link is chosen or Escape is pressed. */
export function SiteMenu({ links }: { links: { href: string; label: string }[] }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  const close = () => setOpen(false);
  return <div className="qs-menu" data-open={open || undefined}>
    <button type="button" className="qs-menu-button" aria-expanded={open} aria-controls="qs-menu-panel"
      aria-label={open ? "Close menu" : "Open menu"} onClick={() => setOpen((v) => !v)}><span /><span /><span /></button>
    {open && <nav id="qs-menu-panel" aria-label="Site menu" className="qs-menu-panel">
      {links.map((l) => <a key={l.href} href={l.href} onClick={close}>{l.label}</a>)}
      <Link href="/login" onClick={close}>Sign in</Link>
      <Link href="/demo" className="qs-btn qs-btn-primary" onClick={close}>Request a demo</Link>
    </nav>}
  </div>;
}
