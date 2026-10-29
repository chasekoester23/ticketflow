"use client";

import { useCallback, useEffect, useState } from "react";
import { Project, createProject, listProjects, setProjectStatus } from "../../lib/api";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({ code: "", name: "", job_location: "", description: "" });
  const load = useCallback(() => listProjects(showArchived).then(setProjects).catch((e) => setError(e.message)), [showArchived]);

  useEffect(() => { load(); }, [load]);

  async function add() {
    if (!form.code.trim() || !form.name.trim() || saving) return;
    setError(null);
    setSaving(true);
    try {
      await createProject({
        code: form.code.trim(), name: form.name.trim(),
        job_location: form.job_location || undefined, description: form.description || undefined,
      });
      setForm({ code: "", name: "", job_location: "", description: "" });
      await load();
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  }

  async function changeStatus(p: Project) {
    const next = p.status === "active" ? "archived" : "active";
    if (next === "archived" && !window.confirm(`Archive ${p.name}? This prevents new uploads to the project. Existing records are retained.`)) return;
    try { setError(null); await setProjectStatus(p.id, next); await load(); }
    catch (e) { setError((e as Error).message); }
  }

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div><p className="tf-eyebrow">PROJECT MANAGEMENT</p><h1 className="tf-page-title">Projects</h1>
        <p className="tf-page-description">Organize delivery records by job. Each batch stays associated with its project.</p></div>
      <a href="/upload" className="tf-secondary-button">Ticket intake <span aria-hidden="true">→</span></a>
    </div>

    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Create a project</h2><p className="tf-panel-description">Enter the job number and name used by your team</p></div>
        <span className="text-[10px] font-semibold uppercase tracking-[.08em] text-[#87929f]">PROJECT SETUP</span>
      </div>
      <form className="grid items-end gap-4 p-5 md:grid-cols-2 xl:grid-cols-[150px_1.2fr_1fr_1fr_auto]"
        onSubmit={(event) => { event.preventDefault(); void add(); }}>
        <label className="tf-field-label">Project ID <span className="text-[10px] font-normal text-[#8793a0]">Required</span>
          <input className="tf-field w-full" required placeholder="24-105" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} />
        </label>
        <label className="tf-field-label">Project name
          <input className="tf-field w-full" required placeholder="East Main Street" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label className="tf-field-label">Job location
          <input className="tf-field w-full" placeholder="City, state" value={form.job_location} onChange={(e) => setForm({ ...form, job_location: e.target.value })} />
        </label>
        <label className="tf-field-label">Description
          <input className="tf-field w-full" placeholder="Optional notes" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </label>
        <button type="submit" disabled={saving || !form.code.trim() || !form.name.trim()} className="tf-primary-button h-[38px]">{saving ? "Creating…" : "+ Create project"}</button>
      </form>
    </section>

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Project directory</h2>
          <p className="tf-panel-description">{projects.length} {showArchived ? "listed" : "active"} project{projects.length !== 1 ? "s" : ""}</p></div>
        <label className="flex items-center gap-2 text-[12px] font-medium text-[#576677]">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} /> Show archived
        </label>
      </div>
      {projects.length ? <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr><th>Project ID</th><th>Project name</th><th>Location</th><th>Status</th><th className="text-right">Actions</th></tr></thead>
        <tbody>{projects.map((p) => <tr key={p.id}>
          <td className="whitespace-nowrap font-bold tabular-nums">{p.code}</td>
          <td className="font-semibold text-[#263646]">{p.name}</td>
          <td>{p.job_location || "—"}</td>
          <td><span className={`tf-status ${p.status === "active" ? "tf-status-open" : ""}`}>{p.status === "active" ? "Active" : "Archived"}</span></td>
          <td><div className="flex justify-end gap-4 whitespace-nowrap">
            <a href={`/records?project_id=${p.id}`}>View records</a>
            <button type="button" onClick={() => void changeStatus(p)} className="text-[12px] font-semibold text-[#637385] hover:underline">
              {p.status === "active" ? "Archive" : "Unarchive"}
            </button>
          </div></td>
        </tr>)}</tbody>
      </table></div> : <div className="tf-empty">
        {showArchived ? "No projects found." : "No active projects yet. Use the form above to add your first job."}
      </div>}
    </section>
  </div>;
}
