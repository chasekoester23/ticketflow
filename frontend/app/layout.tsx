import "./globals.css";
import "./public.css";
import type { ReactNode } from "react";
import AuthShell from "./AuthShell";

export const metadata = { title: "TicketFlow", description: "Verify delivered material and reconcile supplier invoices." };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="en"><body><AuthShell>{children}</AuthShell></body></html>;
}
