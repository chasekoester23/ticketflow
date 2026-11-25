"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { listProjects, type Project } from "../lib/api";

/**
 * The project a person is working on, shared by every page so it is chosen once.
 *
 * "" means all projects. The address wins (`?project_id=` in a link or bookmark), then
 * the person's last choice on this computer. It is only a preference: every request
 * that carries a project id is still authorized by the server, which answers for the
 * signed-in company only.
 */
export type ProjectContextValue = {
  projectId: string;
  project: Project | null;
  /** Every project of the company, archived ones too, once loaded. */
  projects: Project[];
  loaded: boolean;
  /** Most recently chosen first. */
  recent: number[];
  setProjectId: (id: string) => void;
  reloadProjects: () => Promise<void>;
};

const NOOP = async () => undefined;
export const ProjectContext = createContext<ProjectContextValue>({
  projectId: "", project: null, projects: [], loaded: false, recent: [], setProjectId: () => undefined, reloadProjects: NOOP,
});

export function useProject(): ProjectContextValue {
  return useContext(ProjectContext);
}

const keyFor = (accountId: number) => `quarvera.project.${accountId}`;
const recentKeyFor = (accountId: number) => `quarvera.recentProjects.${accountId}`;
function read(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function write(key: string, value: string) {
  try { window.localStorage.setItem(key, value); } catch { /* a convenience only */ }
}

function initialChoice(accountId: number): string {
  const fromAddress = new URLSearchParams(window.location.search).get("project_id");
  if (fromAddress && /^\d+$/.test(fromAddress)) return fromAddress;
  const stored = read(keyFor(accountId));
  return stored && /^\d+$/.test(stored) ? stored : "";
}

export function ProjectProvider({ accountId, children }: { accountId: number; children: ReactNode }) {
  const [projectId, setChoice] = useState(() => initialChoice(accountId));
  const [projects, setProjects] = useState<Project[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [recent, setRecent] = useState<number[]>(() => {
    try { return (JSON.parse(read(recentKeyFor(accountId)) ?? "[]") as unknown[]).filter((n): n is number => typeof n === "number").slice(0, 5); }
    catch { return []; }
  });
  const latest = useRef(projectId);

  const setProjectId = useCallback((id: string) => {
    if (id === latest.current) return;
    latest.current = id;
    setChoice(id);
    write(keyFor(accountId), id);
    if (id) {
      setRecent((old) => {
        const next = [Number(id), ...old.filter((n) => n !== Number(id))].slice(0, 5);
        write(recentKeyFor(accountId), JSON.stringify(next));
        return next;
      });
    }
  }, [accountId]);

  const reloadProjects = useCallback(async () => {
    try {
      const found = await listProjects(true);
      setProjects(found);
      // A project that's gone or isn't this company's falls back to all projects.
      // An archived one stays chosen: its records are still worth looking at.
      if (latest.current && !found.some((p) => String(p.id) === latest.current)) setProjectId("");
    } catch { /* the picker shows "All projects"; pages still load */ }
    setLoaded(true);
  }, [setProjectId]);

  useEffect(() => { void reloadProjects(); }, [reloadProjects]);

  const value = useMemo<ProjectContextValue>(() => ({
    projectId, project: projects.find((p) => String(p.id) === projectId) ?? null, projects, loaded, recent, setProjectId, reloadProjects,
  }), [projectId, projects, loaded, recent, setProjectId, reloadProjects]);
  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}
