import "./globals.css";
import type { ReactNode } from "react";
import AuthShell from "./AuthShell";

export const metadata = { title: "TicketFlow", description: "Construction material ticket processing" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="en"><body><AuthShell>{children}</AuthShell></body></html>;
}
