"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useProject } from "./project-context";

/**
 * A project's workspace: its report, plus the company screens filtered to it. The tabs
 * reuse Delivery Records, Trucking, Invoices and Document Intake with the project
 * chosen, so there is one register of each thing, never a project-only copy.
 */
const TABS = [
  { label: "Project report", href: (id: number) => `/projects/${id}`, match: (path: string, id: number) => path === `/projects/${id}` },
  { label: "Deliveries", href: () => "/records", match: (path: string) => path === "/records" },
  { label: "Trucking", href: () => "/trucking", match: (path: string) => path === "/trucking" },
  { label: "Invoices", href: () => "/invoices", match: (path: string) => path.startsWith("/invoices") },
  { label: "Documents", href: () => "/upload", match: (path: string) => path === "/upload" },
];
const SHOWN_ON = ["/records", "/trucking", "/invoices", "/upload"];

export default function ProjectBar() {
  const { project } = useProject();
  const path = usePathname();
  if (!project) return null;
  const onProjectPage = path === `/projects/${project.id}`;
  if (!onProjectPage && !SHOWN_ON.some((p) => path === p || path.startsWith(p + "/"))) return null;
  return <div className="tf-project-bar" data-testid="project-bar">
    <span className="tf-project-bar-name"><small>{project.code}</small>{project.name}{project.status !== "active" && " (archived)"}</span>
    <nav aria-label="Project">
      {TABS.map((t) => <Link key={t.label} prefetch={false} href={t.href(project.id)} aria-current={t.match(path, project.id) ? "page" : undefined}>{t.label}</Link>)}
    </nav>
  </div>;
}
