"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import type { Project } from "../lib/api";
import { useProject } from "./project-context";

const label = (p: Project) => `${p.code} · ${p.name}`;

/** The one project selector, in the top bar. Every page reads the choice from the project context. */
export default function ProjectPicker({ canCreate }: { canCreate: boolean }) {
  const { projectId, project, projects, recent, setProjectId } = useProject();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const away = (event: MouseEvent) => { if (!box.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", escape); };
  }, [open]);
  useEffect(() => { if (!open) { setQuery(""); setShowArchived(false); } }, [open]);

  const choose = (id: string) => { setProjectId(id); setOpen(false); trigger.current?.focus(); };
  const words = query.trim().toLowerCase();
  const matches = (p: Project) => !words || label(p).toLowerCase().includes(words) || (p.job_location ?? "").toLowerCase().includes(words);
  const active = projects.filter((p) => p.status === "active" && matches(p));
  const archived = projects.filter((p) => p.status !== "active" && matches(p));
  const recentProjects = words ? [] : recent.map((id) => projects.find((p) => p.id === id)).filter((p): p is Project => Boolean(p && p.status === "active")).slice(0, 3);

  const option = (p: Project | null, key: string) => {
    const id = p ? String(p.id) : "";
    const chosen = id === projectId;
    return <button key={key} type="button" className={`tf-picker-option ${chosen ? "tf-picker-option-chosen" : ""}`} aria-current={chosen ? "true" : undefined}
      onClick={() => choose(id)}>
      {p ? <><span className="tf-picker-code">{p.code}</span><span className="tf-picker-name">{p.name}{p.status !== "active" && <em> · archived</em>}</span></>
        : <span className="tf-picker-name">All projects</span>}
    </button>;
  };

  return <div className="tf-picker" ref={box}>
    <button ref={trigger} type="button" className="tf-picker-trigger" aria-haspopup="dialog" aria-expanded={open}
      data-testid="project-picker" onClick={() => setOpen((v) => !v)}>
      <span className="tf-picker-caption">Project</span>
      <span className="tf-picker-current">{project ? label(project) : "All projects"}</span>
      <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m6 9 6 6 6-6" /></svg>
    </button>
    {open && <div role="dialog" aria-label="Choose a project" className="tf-picker-panel">
      <input ref={search} className="tf-field w-full" type="search" placeholder="Search by number, name or location" aria-label="Search projects"
        value={query} onChange={(e) => setQuery(e.target.value)} />
      <div className="tf-picker-list">
        {!words && option(null, "all")}
        {recentProjects.length > 0 && <><p className="tf-picker-group">Recent</p>{recentProjects.map((p) => option(p, `r${p.id}`))}</>}
        <p className="tf-picker-group">Active projects</p>
        {active.length ? active.map((p) => option(p, `a${p.id}`)) : <p className="tf-picker-empty">{words ? "No active project matches." : "No active projects yet."}</p>}
        {archived.length > 0 && (showArchived || words
          ? <><p className="tf-picker-group">Archived</p>{archived.map((p) => option(p, `x${p.id}`))}</>
          : <button type="button" className="tf-picker-more" onClick={() => setShowArchived(true)}>Show {archived.length} archived</button>)}
      </div>
      <div className="tf-picker-footer">
        <Link prefetch={false} href="/projects" onClick={() => setOpen(false)}>All projects →</Link>
        {canCreate && <Link prefetch={false} href="/projects?new=1" onClick={() => setOpen(false)}>+ New project</Link>}
      </div>
    </div>}
  </div>;
}
