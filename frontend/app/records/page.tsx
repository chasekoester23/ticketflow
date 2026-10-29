"use client";

import { useEffect, useState } from "react";
import { Filters, Project, Ticket, getRecords, listProjects, recordsExportUrl } from "../../lib/api";

const STATUS_LABEL: Record<string, string> = {
  needs_review: "Needs review", verified: "Verified", flagged: "Flagged",
  skipped: "Skipped", failed: "Failed",
};
const statusClass = (status: string) => status === "verified" ? "tf-status-open" :
  status === "failed" ? "tf-status-error" :
    status === "needs_review" || status === "flagged" ? "tf-status-attention" : "";

export default function RecordsPage() {
  const [filters, setFilters] = useState<Filters>({});
  const [rows, setRows] = useState<Ticket[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [initialized, setInitialized] = useState(false);

  useEffect(() => {
    listProjects(true).then(setProjects).catch(() => {});
    const params = new URLSearchParams(window.location.search);
    const next: Filters = {};
    for (const key of ["project_id", "supplier", "material", "status", "date_from", "date_to"] as const) {
      const value = params.get(key);
      if (value) next[key] = value;
    }
    setFilters(next);
    setInitialized(true);
  }, []);

  useEffect(() => {
    if (!initialized) return;
    let active = true;
    setLoading(true);
    getRecords(filters).then((results) => { if (active) { setRows(results); setError(null); } })
      .catch((e) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [filters, initialized]);

  const set = (key: keyof Filters) => (e: { target: { value: string } }) => setFilters((old) => ({ ...old, [key]: e.target.value || undefined }));
  const hasFilter = Object.values(filters).some(Boolean);
  const total = rows.length;
  const verified = rows.filter((r) => r.status === "verified");
  const approvedTons = verified.reduce((sum, r) => sum + Number(r.tonnage ?? 0), 0);

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div><p className="tf-eyebrow">DELIVERY DOCUMENTATION</p><h1 className="tf-page-title">Delivery records</h1>
        <p className="tf-page-description">Search, audit, and export material delivery tickets by project and status.</p></div>
      <a href="/upload" className="tf-primary-button"><span aria-hidden="true">+</span> New ticket upload</a>
    </div>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Filter delivery records</h2><p className="tf-panel-description">Exports reflect your current filters</p></div>
        {hasFilter && <button type="button" onClick={() => setFilters({})} className="tf-link">Clear filters</button>}
      </div>
      <div className="grid gap-3 p-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <label className="tf-field-label">Project
          <select className="tf-field w-full" value={filters.project_id ?? ""} onChange={set("project_id")}>
            <option value="">All projects</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
          </select>
        </label>
        <label className="tf-field-label">Supplier
          <input className="tf-field w-full" placeholder="Supplier name" value={filters.supplier ?? ""} onChange={set("supplier")} />
        </label>
        <label className="tf-field-label">Material
          <input className="tf-field w-full" placeholder="Material type" value={filters.material ?? ""} onChange={set("material")} />
        </label>
        <label className="tf-field-label">Review status
          <select className="tf-field w-full" value={filters.status ?? ""} onChange={set("status")}>
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABEL).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </select>
        </label>
        <label className="tf-field-label">Delivery from
          <input type="date" className="tf-field w-full" value={filters.date_from ?? ""} onChange={set("date_from")} />
        </label>
        <label className="tf-field-label">Delivery to
          <input type="date" className="tf-field w-full" value={filters.date_to ?? ""} onChange={set("date_to")} />
        </label>
      </div>
    </section>

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div>
          <h2 className="tf-panel-title">Ticket register</h2>
          <p className="tf-panel-description">
            {loading ? "Loading records…" : `${total} records · ${verified.length} verified · ${approvedTons.toLocaleString(undefined, { maximumFractionDigits: 2 })} verified tons`}
          </p>
        </div>
        <a href={recordsExportUrl(filters)} className="tf-secondary-button" aria-label="Download Excel (filtered)">
          <svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4" /></svg>
          Export Excel
        </a>
      </div>
      <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr>
          {["Ticket #", "Date", "Material", "Tons", "Supplier", "Job location", "Unit price", "Cost", "Status", ""].map((h) =>
            <th key={h}>{h}</th>)}
        </tr></thead>
        <tbody>{rows.map((t) => <tr key={t.id}>
          <td className="font-bold tabular-nums">{t.ticket_number || "—"}</td>
          <td className="whitespace-nowrap">{t.delivery_date || "—"}</td>
          <td className="min-w-[125px]">{t.material_type || "—"}</td>
          <td className="whitespace-nowrap tabular-nums">{t.tonnage ?? "—"}</td>
          <td className="min-w-[130px]">{t.supplier || "—"}</td>
          <td>{t.job_location || "—"}</td>
          <td className="whitespace-nowrap tabular-nums">{t.unit_price != null ? `$${Number(t.unit_price).toFixed(2)}` : "—"}</td>
          <td className="whitespace-nowrap tabular-nums">{t.cost != null ? `$${Number(t.cost).toFixed(2)}` : "—"}</td>
          <td><span className={`tf-status ${statusClass(t.status)}`}>
            {STATUS_LABEL[t.status] ?? t.status}
          </span>{t.duplicates.length > 0 &&
            <span className="ml-1.5 text-[10px] font-bold text-red-700" title={t.duplicates.join("\n")}>Possible duplicate</span>}</td>
          <td className="whitespace-nowrap"><a href={`/upload?batch=${t.batch_id}&ticket=${t.id}`}>Review →</a></td>
        </tr>)}</tbody>
      </table></div>
      {!loading && !rows.length && <div className="tf-empty">
        <p>{hasFilter ? "No delivery records match the current filters." : "No delivery tickets recorded yet."}</p>
        {hasFilter ? <button className="tf-link mt-2" onClick={() => setFilters({})}>Clear filters</button> :
          <a className="tf-link mt-2 inline-block" href="/upload">Start ticket intake →</a>}
      </div>}
    </section>
    <p className="text-[11px] text-[#768391]">Verified tonnage and costs include approved tickets only. Unverified values may be visible in the register but are not included in financial totals.</p>
  </div>;
}
