"use client";

import { useEffect, useState } from "react";
import { Filters, Project, Ticket, getRecords, listProjects, recordsExportUrl } from "../../lib/api";

const STATUS_LABEL: Record<string, string> = { needs_review: "Needs review", verified: "Verified", skipped: "Skipped", failed: "Failed" };

export default function RecordsPage() {
  const [filters, setFilters] = useState<Filters>({});
  const [rows, setRows] = useState<Ticket[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listProjects(true).then(setProjects).catch(() => {});
    const initial = new URLSearchParams(window.location.search).get("project_id");
    if (initial) setFilters({ project_id: initial });
  }, []);
  useEffect(() => {
    getRecords(filters).then(setRows).catch((e) => setError(e.message));
  }, [filters]);

  const set = (k: keyof Filters) => (e: { target: { value: string } }) => setFilters({ ...filters, [k]: e.target.value || undefined });
  const input = "rounded border border-slate-300 bg-white px-3 py-2 text-sm";
  const tons = rows.reduce((a, r) => a + Number(r.tonnage ?? 0), 0);

  return (
    <div className="space-y-4">
      {error && <div className="rounded border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800">{error}</div>}
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-200 bg-white p-4">
        <select className={input} value={filters.project_id ?? ""} onChange={set("project_id")}>
          <option value="">All projects</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
        </select>
        <input className={input} placeholder="Supplier contains…" value={filters.supplier ?? ""} onChange={set("supplier")} />
        <input className={input} placeholder="Material contains…" value={filters.material ?? ""} onChange={set("material")} />
        <select className={input} value={filters.status ?? ""} onChange={set("status")}>
          <option value="">Any status</option>
          {Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <label className="text-xs text-slate-500">From<input type="date" className={`${input} ml-2`} value={filters.date_from ?? ""} onChange={set("date_from")} /></label>
        <label className="text-xs text-slate-500">To<input type="date" className={`${input} ml-2`} value={filters.date_to ?? ""} onChange={set("date_to")} /></label>
        <a href={recordsExportUrl(filters)} className="ml-auto rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white">Download Excel (filtered)</a>
      </div>
      <p className="text-sm text-slate-600">{rows.length} records · {tons.toLocaleString(undefined, { maximumFractionDigits: 2 })} tons</p>
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-slate-100 text-xs uppercase text-slate-500">
            <tr>{["Ticket", "Date", "Material", "Tons", "Supplier", "Job", "$/ton", "Cost", "Status", ""].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id} className="border-t border-slate-100">
                <td className="px-3 py-2">{t.ticket_number ?? "—"}</td>
                <td className="px-3 py-2">{t.delivery_date ?? "—"}</td>
                <td className="px-3 py-2">{t.material_type ?? "—"}</td>
                <td className="px-3 py-2">{t.tonnage ?? "—"}</td>
                <td className="px-3 py-2">{t.supplier ?? "—"}</td>
                <td className="px-3 py-2">{t.job_location ?? "—"}</td>
                <td className="px-3 py-2">{t.unit_price ?? "—"}</td>
                <td className="px-3 py-2">{t.cost ? `$${t.cost}` : "—"}</td>
                <td className="px-3 py-2">{STATUS_LABEL[t.status]}{t.duplicates.length > 0 && <span className="ml-2 rounded bg-red-100 px-1.5 text-xs text-red-800" title={t.duplicates.join("\n")}>dup?</span>}</td>
                <td className="px-3 py-2"><a className="underline" href={`/upload?batch=${t.batch_id}`}>Open</a></td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="p-4 text-sm text-slate-500">No records match these filters.</p>}
      </div>
    </div>
  );
}
