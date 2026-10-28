"use client";

import { useCallback, useEffect, useState } from "react";
import { Project, createProject, listProjects, setProjectStatus } from "../../lib/api";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ code: "", name: "", job_location: "", description: "" });

  const load = useCallback(() => listProjects(showArchived).then(setProjects).catch((e) => setError(e.message)), [showArchived]);
  useEffect(() => {
    load();
  }, [load]);

  async function add() {
    setError(null);
    try {
      await createProject({ code: form.code.trim(), name: form.name.trim(), job_location: form.job_location || undefined, description: form.description || undefined });
      setForm({ code: "", name: "", job_location: "", description: "" });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const input = "rounded border border-slate-300 px-3 py-2 text-sm";

  return (
    <div className="space-y-6">
      {error && <div className="rounded border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800">{error}</div>}
      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="font-semibold">New project</h2>
        <div className="mt-3 flex flex-wrap gap-2">
          <input className={input} placeholder="Project ID (e.g. 24-105)" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} />
          <input className={input} placeholder="Project name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className={input} placeholder="Job location" value={form.job_location} onChange={(e) => setForm({ ...form, job_location: e.target.value })} />
          <input className={`${input} w-72`} placeholder="Description (optional)" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <button disabled={!form.code.trim() || !form.name.trim()} onClick={add} className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40">Create</button>
        </div>
      </section>
      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Projects</h2>
          <label className="text-sm text-slate-600">
            <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} className="mr-2" />
            Show archived
          </label>
        </div>
        <table className="mt-3 w-full text-left text-sm">
          <thead className="text-xs uppercase text-slate-500">
            <tr><th className="py-1">ID</th><th>Name</th><th>Location</th><th>Status</th><th /></tr>
          </thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.id} className="border-t border-slate-100">
                <td className="py-1">{p.code}</td><td>{p.name}</td><td>{p.job_location ?? "—"}</td><td>{p.status}</td>
                <td className="space-x-3 text-right">
                  <a className="underline" href={`/records?project_id=${p.id}`}>Records</a>
                  <button className="text-slate-700 underline" onClick={() => setProjectStatus(p.id, p.status === "active" ? "archived" : "active").then(load).catch((e) => setError(e.message))}>
                    {p.status === "active" ? "Archive" : "Unarchive"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {projects.length === 0 && <p className="mt-3 text-sm text-slate-500">No projects yet.</p>}
      </section>
    </div>
  );
}
