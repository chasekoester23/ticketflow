import "@fontsource/barlow-semi-condensed/latin-600.css";
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "@fontsource/reenie-beanie/latin-400.css";
import "./globals.css";
import "./public.css";
import type { ReactNode } from "react";
import AuthShell from "./AuthShell";

export const metadata = {
  title: "Quarvera",
  description: "Construction material and trucking cost management, from the field to the invoice. Every delivery. Every truck. Every dollar.",
  applicationName: "Quarvera",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="en"><body><AuthShell>{children}</AuthShell></body></html>;
}
