"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ProjectSummary, createProject, getProjectDirectory, setProjectStatus, updateProject, quantityText } from "../../lib/api";
import { useAccount } from "../account-context";
import { Dialog } from "../Dialog";
import { useProject } from "../project-context";

const money = (v: string) => `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
function ago(iso: string | null) {
  if (!iso) return "No activity yet";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 60) return minutes <= 1 ? "Just now" : `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return days < 30 ? `${days} day${days === 1 ? "" : "s"} ago` : new Date(iso).toLocaleDateString();
}

type Draft = { code: string; name: string; job_location: string; description: string };
const EMPTY: Draft = { code: "", name: "", job_location: "", description: "" };

/** Create a project, or edit one. The project number can't change: records and exports are filed under it. */
function ProjectForm({ editing, onClose, onSaved }: { editing: ProjectSummary | null; onClose: () => void; onSaved: (id: number) => void }) {
  const [form, setForm] = useState<Draft>(editing
    ? { code: editing.code, name: editing.name, job_location: editing.job_location ?? "", description: editing.description ?? "" } : EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const field = (key: keyof Draft) => ({ value: form[key], onChange: (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value }) });
  return <Dialog label={editing ? `Edit ${editing.code}` : "New project"} onClose={onClose} onSubmit={async (event) => {
    event.preventDefault();
    if (saving || !form.code.trim() || !form.name.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const saved = editing
        ? await updateProject(editing.id, { name: form.name.trim(), job_location: form.job_location.trim() || null, description: form.description.trim() || null })
        : await createProject({ code: form.code.trim(), name: form.name.trim(), job_location: form.job_location.trim() || undefined,
          description: form.description.trim() || undefined });
      onSaved(saved.id);
    } catch (e) { setError((e as Error).message); setSaving(false); }
  }}>
    <div className="space-y-3 p-5">
      <h2 className="text-[16px] font-semibold text-[#041d2f]">{editing ? `Edit ${editing.code}` : "New project"}</h2>
      {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}
      <label className="tf-field-label">Project number
        <input className="tf-field w-full" required placeholder="24-105" maxLength={50} readOnly={Boolean(editing)} {...field("code")} />
        {!editing && <span className="font-normal text-[#5f6c7b]">The job number your team already uses. It can&apos;t be changed later.</span>}
      </label>
      <label className="tf-field-label">Project name
        <input className="tf-field w-full" required placeholder="East Main Street reconstruction" maxLength={200} {...field("name")} />
      </label>
      <label className="tf-field-label">Location
        <input className="tf-field w-full" placeholder="City, state" maxLength={200} {...field("job_location")} />
      </label>
      <label className="tf-field-label">Notes
        <textarea className="tf-field w-full" rows={2} {...field("description")} />
      </label>
    </div>
    <div className="flex justify-end gap-2 border-t border-[#dce1e6] bg-[#f7f8f9] px-5 py-3">
      <button type="button" className="tf-secondary-button" onClick={onClose}>Cancel</button>
      <button type="submit" className="tf-primary-button" disabled={saving || !form.code.trim() || !form.name.trim()}>
        {saving ? "Saving…" : editing ? "Save changes" : "Create project"}</button>
    </div>
  </Dialog>;
}

export default function ProjectsPage() {
  const canManage = useAccount().can("projects.manage");
  const { setProjectId, reloadProjects } = useProject();
  const router = useRouter();
  const [rows, setRows] = useState<ProjectSummary[] | null>(null);
  const [status, setStatus] = useState<"active" | "archived" | "all">("active");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<ProjectSummary | "new" | null>(null);
  const load = useCallback(() => getProjectDirectory().then(setRows).catch((e) => setError(e.message)), []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (canManage && new URLSearchParams(window.location.search).get("new") === "1") setEditing("new");
  }, [canManage]);

  const shown = useMemo(() => {
    const words = query.trim().toLowerCase();
    return (rows ?? []).filter((r) => (status === "all" || r.status === status)
      && (!words || `${r.code} ${r.name} ${r.job_location ?? ""}`.toLowerCase().includes(words)));
  }, [rows, status, query]);
  const counts = { active: rows?.filter((r) => r.status === "active").length ?? 0, archived: rows?.filter((r) => r.status !== "active").length ?? 0 };

  function open(row: ProjectSummary) {
    setProjectId(String(row.id));
    router.push(`/projects/${row.id}`);
  }
  async function changeStatus(row: ProjectSummary) {
    const next = row.status === "active" ? "archived" : "active";
    if (next === "archived" && !window.confirm(`Archive ${row.code} · ${row.name}? New uploads to it stop. Its records, costs and documents are kept.`)) return;
    try { setError(null); await setProjectStatus(row.id, next); await Promise.all([load(), reloadProjects()]); }
    catch (e) { setError((e as Error).message); }
  }

  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Projects</h1>
        <p className="tf-page-description">Every job, with its open work, verified deliveries and approved costs.</p></div>
      {canManage && <button type="button" className="tf-primary-button" onClick={() => setEditing("new")}>+ New project</button>}
    </div>

    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}
    {notice && <div role="status" className="tf-alert tf-alert-good">{notice}</div>}

    <section className="tf-panel" aria-labelledby="directory-title">
      <div className="tf-panel-heading flex-wrap">
        <h2 id="directory-title" className="tf-panel-title">Project directory</h2>
        <div className="flex flex-wrap items-center gap-2">
          <input type="search" className="tf-field w-[240px]" placeholder="Search number, name, location" aria-label="Search projects"
            value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="tf-segmented" role="group" aria-label="Project status">
            {([["active", `Active (${counts.active})`], ["archived", `Archived (${counts.archived})`], ["all", "All"]] as const).map(([key, text]) =>
              <button key={key} type="button" aria-pressed={status === key} onClick={() => setStatus(key)}>{text}</button>)}
          </div>
        </div>
      </div>
      {rows === null ? <p className="tf-empty" role="status">Loading projects…</p>
        : shown.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Projects">
          <thead><tr>
            <th>Project</th><th className="tf-hide-narrow">Location</th><th>Status</th><th className="tf-hide-narrow">Last activity</th>
            <th className="text-right">To review</th><th className="text-right">Verified deliveries</th>
            <th className="text-right">Material cost</th><th className="text-right">Trucking cost</th><th className="text-right">Verified cost</th><th><span className="sr-only">Actions</span></th>
          </tr></thead>
          <tbody>{shown.map((r) => {
            const open_ = r.open_tickets + r.open_shifts;
            return <tr key={r.id}>
              <td className="min-w-[200px]">
                <button type="button" className="text-left" onClick={() => open(r)} title="Open this project's workspace">
                  <span className="block font-mono text-[12px] font-semibold text-[#a24b0a]">{r.code}</span>
                  <span className="block font-semibold text-[#041d2f] hover:underline">{r.name}</span>
                </button>
              </td>
              <td className="tf-hide-narrow">{r.job_location || <span className="text-[#6b7787]">—</span>}</td>
              <td><span className={`tf-status ${r.status === "active" ? "tf-status-open" : ""}`}>{r.status === "active" ? "Active" : "Archived"}</span></td>
              <td className="tf-hide-narrow whitespace-nowrap text-[#5f6c7b]">{ago(r.last_activity)}</td>
              <td className="text-right">{open_ ? <Link prefetch={false} href={`/records?project_id=${r.id}&status=needs_review`}
                title={`${r.open_tickets} tickets · ${r.open_shifts} truck shifts`}>{open_.toLocaleString()}</Link> : <span className="text-[#6b7787]">0</span>}</td>
              <td className="text-right whitespace-nowrap">{r.verified_tickets.toLocaleString()}<span className="block text-[11px] text-[#5f6c7b]">{quantityText(r.verified_tons, r.other_units)}</span></td>
              <td className="text-right whitespace-nowrap">{money(r.material_cost)}
                {r.unpriced_tickets > 0 && <span className="block text-[11px] font-semibold text-[#995a18]">+ {r.unpriced_tickets} without a rate</span>}</td>
              <td className="text-right whitespace-nowrap">{money(r.trucking_cost)}</td>
              <td className="text-right whitespace-nowrap font-semibold">{money(r.verified_cost)}{r.unpriced_tickets > 0 && <span className="block text-[11px] font-normal text-[#995a18]">incomplete</span>}</td>
              <td><div className="flex justify-end gap-3 whitespace-nowrap text-[12px]">
                <Link prefetch={false} href={`/records?project_id=${r.id}`}>Records</Link>
                <Link prefetch={false} href={`/projects/${r.id}`}>Report</Link>
                {canManage && <button type="button" className="font-semibold text-[#4b5a69] hover:underline" onClick={() => setEditing(r)}>Edit</button>}
                {canManage && <button type="button" className="font-semibold text-[#4b5a69] hover:underline" onClick={() => void changeStatus(r)}>
                  {r.status === "active" ? "Archive" : "Restore"}</button>}
              </div></td>
            </tr>;
          })}</tbody>
        </table></div>
        : <div className="tf-empty" data-testid="projects-empty">
          {rows.length && (query || status !== "active") ? "No project matches."
            : canManage ? <>No projects yet. Add your first job with the number your team already uses for it.
              Every upload, record and invoice belongs to a project, so this comes first.
              <div className="mt-3"><button type="button" className="tf-primary-button" onClick={() => setEditing("new")}>+ New project</button></div></>
            : "No projects yet. A manager or owner adds each job here. Ask one to add the job you're working on."}
        </div>}
    </section>
    <p className="text-[11.5px] text-[#5f6c7b]">Costs are approved figures only. Material cost leaves out approved tickets that have no rate yet; those projects are marked incomplete.</p>

    {editing && <ProjectForm editing={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={async (id) => {
      const created = editing === "new";
      setEditing(null);
      await Promise.all([load(), reloadProjects()]);
      if (created) { setProjectId(String(id)); setNotice("Project created. It is now the selected project."); }
      else setNotice("Project saved.");
    }} />}
  </div>;
}
