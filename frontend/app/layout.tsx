import "./globals.css";
import type { ReactNode } from "react";

export const metadata = { title: "TicketFlow", description: "Material ticket automation" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-7xl items-center px-6 py-3">
            <span className="text-lg font-semibold tracking-tight">TicketFlow</span>
            <span className="ml-3 text-sm text-slate-500">Material ticket processing</span>
            <nav className="ml-8 flex gap-4 text-sm">
              <a href="/" className="text-slate-700 hover:underline">Tickets</a>
              <a href="/pricing" className="text-slate-700 hover:underline">Suppliers &amp; Pricing</a>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>
      </body>
    </html>
  );
}
